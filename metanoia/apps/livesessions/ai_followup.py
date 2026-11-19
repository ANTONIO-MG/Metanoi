"""The Claude layer over a finished session: write-up, next agenda, action items.

The extractive recap :mod:`apps.msteams.recap` produces is fast, free and shallow
— it picks sentences out of the transcript. This is the other half: once the
session has a transcript, the **admin** Claude layer reads it and produces the
things a coach would otherwise write by hand on a Sunday evening:

1. a full written summary of what the session actually covered;
2. an **agenda for the next session**, built from what was left unfinished;
3. **action items**, split between the students and the educator.

…and then sends the class **one** message with all of it (:func:`deliver`): the
summary, the next agenda, the students' action items, and the links to the
recording and the transcript — in-app and by e-mail, to the intake on the module
(or to the student, for a one-on-one) and to the educator.

Deliberate constraints:

* **Admin layer only.** :mod:`apps.ai_assistant.admin_ai` is the paid, token-billed
  Claude tier reserved for admins and staff. This job runs on that layer under the
  platform's own admin identity — never on a student's or an educator's account,
  and never in a request. It is a background job precisely so nobody's page waits
  on it and no student's session can spend admin tokens.
* **Two versions.** The full pack (attendance roster with e-mail addresses,
  the long write-up, flags about struggling candidates) is staff material: it
  is stored on the AiReport and in the session's OneDrive folder. The class
  gets the session summary in the fixed layout of :mod:`.brief` — overview, key
  highlights, agenda for the next session, action items for the students, the
  educator and everyone — plus the recording and transcript links.
* **One message per class.** The summary lesson and its material row
  are saved quietly (no automatic "new item" notices); :func:`deliver` is the
  single announcement. Classes older than ``pipeline.ANNOUNCE_WITHIN_DAYS`` are
  filed and published without telling anyone.
* **Never destructive.** If Claude is unconfigured, rate-limited or returns
  nonsense, the job tries again (up to ``MAX_ATTEMPTS``) and then falls back to
  sending the plain session summary. Nothing filed is overwritten with an error.

Module constants:

* ``MAX_ATTEMPTS`` — Claude answers that could not be used before the job gives
  up on a session and sends the plain summary instead.
* ``MIN_MATERIAL`` — below this much transcript-derived text there is nothing
  worth summarising.
* ``PROMPT`` — the instructions Claude receives, asking for strict JSON.
* ``SUMMARY_PENDING`` — what the class reads in place of a summary that turned
  out to be an AI failure message (see :func:`without_ai_failure`).
"""

import json
import logging

from django.db.models import Q
from django.utils import timezone

from core import clock

logger = logging.getLogger('apps')

MAX_ATTEMPTS = 3
MIN_MATERIAL = 200

PROMPT = """You are preparing the follow-up pack for a live coaching session on a CTA \
(Chartered Accountancy) exam-preparation platform. You are given the session's \
details and its material: the session summary built from Microsoft Teams Copilot's \
meeting notes and action items (when there is one), and the transcript.

The class receives, in this order: "Overview" (a brief description of what was \
discussed, said and stated overall), "Key highlights" (a few bullets), "Agenda for the \
next session", then "Action items" for the students and for the educator — each a thing \
they must do, should do, or said they will do.

Produce STRICT JSON with exactly these keys and no prose outside the JSON:

{
  "overview": "3-5 sentences: what was discussed, said and stated in the session overall.",
  "highlights": ["3-6 key highlights a candidate who missed the session must not miss."],
  "summary_markdown": "A full written summary of the session in Markdown, for staff. Cover \
what was taught, the worked examples, the standards or sections referenced, the questions \
candidates asked and how they were answered, and anything explicitly left for next time.",
  "next_agenda": ["Ordered agenda items for the NEXT session, each a full sentence, \
built from what was unfinished, misunderstood or promised."],
  "student_actions": [{"task": "...", "kind": "must | should | will", "who": "a named \
student, or empty for the whole class", "due_hint": "before the next session | this week | \
before the test", "why": "one clause on why it matters"}],
  "educator_actions": [{"task": "...", "kind": "must | should | will", "who": "the \
educator's name", "due_hint": "...", "why": "..."}],
  "topics_covered": ["short topic or standard references, e.g. IAS 12, deferred tax"],
  "concerns": ["anything suggesting a candidate is struggling, or empty"]
}

Rules:
- Ground everything in the material provided. Do not invent examples, marks or dates.
- The attendance roster gives you real names and e-mail addresses. Use the names when attributing a question or a contribution, but only when the transcript makes it clear who spoke. Never put an e-mail address in the summary, and never guess who said something.
- If the material is too thin to answer a key, use an empty list or a short honest note.
- Keep each action item to one concrete, checkable thing. "kind": "must" for what someone \
was told they must do, "should" for what was recommended, "will" for what they said they will do.
- Use the owner names Copilot gives, where it gives them.
"""


def is_enabled():
    """Whether the follow-up job may run: switched on in the settings and the admin Claude layer configured."""
    from apps.ai_assistant import admin_ai
    from .models import LiveSessionSettings
    return bool(LiveSessionSettings.load().ai_followup_enabled and admin_ai.is_enabled())


def pending(limit):
    """Sessions whose material is in and whose pack has not been written.

    "In" means either the pipeline has published the session, or the summary
    job has published a summary for it — a class whose recording is not
    published still gets its pack. Newest first, so this week's classes are
    never queued behind a backfill.
    """
    from .models import SessionArtifact
    return (SessionArtifact.objects
            .filter(ai_followup_done=False)
            .filter(Q(state=SessionArtifact.STATE_PUBLISHED) | Q(summary_published_at__isnull=False))
            .exclude(state__in=[SessionArtifact.STATE_WAITING, SessionArtifact.STATE_POLLING,
                                SessionArtifact.STATE_FILING, SessionArtifact.STATE_SUMMARISING,
                                SessionArtifact.STATE_UPLOADING])
            .select_related('meeting', 'meeting__module', 'meeting__host', 'meeting__cohort')
            .order_by('-meeting__scheduled_end')[:limit])


def run(limit=30):
    """Write and send up to ``limit`` session packs. The scheduler's entry point."""
    if not is_enabled():
        return 'AI follow-up is off or the admin Claude layer is not configured'

    counts = {}
    for artifact in pending(limit):
        try:
            outcome = process(artifact)
        except Exception:
            logger.exception('livesessions: AI follow-up failed for %s', artifact.meeting)
            outcome = 'error'
        counts[outcome] = counts.get(outcome, 0) + 1
    return ', '.join(f'{n} {k}' for k, n in sorted(counts.items())) or 'nothing to do'


def process(artifact):
    """Write, file and send one session's pack. Returns a one-word outcome.

    When the material is thinner than ``MIN_MATERIAL`` no admin tokens are
    spent: the class is sent what there is (the recording / transcript links,
    any summary) and the job stops. When the admin AI fails, the reason goes to
    staff only (the log, and ``last_error`` shown on the Processing card of the
    session page) — never into anything the class is sent.
    """
    from apps.ai_assistant import admin_ai

    meeting = artifact.meeting
    material = _source_material(artifact)
    if len(material.strip()) < MIN_MATERIAL:
        _finish(artifact, report=None)
        deliver(artifact, pack=None)
        return 'no-material'

    question = f'{PROMPT}\n\nSESSION\n{_session_header(meeting)}\n\nMATERIAL\n{material[:120000]}'
    answer, usage = admin_ai.ask([], question)
    ai_failed = admin_ai.failed(answer, usage)
    pack = None if ai_failed else _parse(answer)
    if not pack:
        artifact.ai_followup_attempts += 1
        fields = ['ai_followup_attempts', 'updated_at']
        if ai_failed:
            artifact.last_error = f'AI follow-up: {usage.get("error") or answer}'[:2000]
            fields.append('last_error')
            logger.error('livesessions: the admin AI failed for %s (attempt %s): %s',
                         meeting, artifact.ai_followup_attempts, artifact.last_error)
        else:
            logger.warning('livesessions: Claude returned no usable JSON for %s (attempt %s)',
                           meeting, artifact.ai_followup_attempts)
        artifact.save(update_fields=fields)
        if artifact.ai_followup_attempts < MAX_ATTEMPTS:
            return 'retry'
        _finish(artifact, report=None)
        deliver(artifact, pack=None)
        return 'fallback'

    pack['student_markdown'] = student_markdown(artifact, pack)
    report = _file_report(artifact, pack)
    _file_to_onedrive(artifact, pack)
    _publish_student_summary(artifact, pack)
    _refresh_thumbnail(artifact, pack)
    _finish(artifact, report=report)
    deliver(artifact, pack=pack)
    return 'written'


def _finish(artifact, report):
    """Mark the artifact's follow-up as done, keeping any existing report id when ``report`` is ``None``."""
    artifact.ai_followup_done = True
    artifact.ai_report_id = getattr(report, 'pk', None) or artifact.ai_report_id
    artifact.save(update_fields=['ai_followup_done', 'ai_report_id', 'updated_at'])


def _session_header(meeting):
    """The facts Claude needs to write in the right register."""
    rows = [
        ('Title', meeting.title),
        ('Kind', meeting.get_session_kind_display()),
        ('Institution', getattr(meeting.resolved_institution, 'display_name', '')),
        ('Programme', getattr(meeting.resolved_programme, 'label', '')),
        ('Module', meeting.module.display_name if meeting.module_id else ''),
        ('Week / topic', meeting.week.display_title if meeting.week_id else ''),
        ('Prepares for', meeting.calendar_event.title if meeting.calendar_event_id else ''),
        ('Held', f'{clock.local(meeting.scheduled_start):%A %d %B %Y, %H:%M} SAST'
                 if meeting.scheduled_start else ''),
        ('Duration', f'{meeting.duration_minutes} minutes'),
        ('Description', meeting.description),
    ]
    header = '\n'.join(f'{k}: {v}' for k, v in rows if v)
    roster = attendance_roster(meeting)
    return f'{header}\n\nWHO ATTENDED\n{roster}' if roster else header


def attendance_roster(meeting):
    """Who was in the session, by name and e-mail, with how long they stayed.

    Drawn from the platform's own register, not from what people typed into
    Teams: students join anonymously, so the Graph report on its own is a list of
    nicknames. This is what puts real, checkable identities into the summary —
    and it is what lets the model say "Thabo asked about deferred tax" instead of
    "an attendee asked".
    """
    session = getattr(meeting, 'class_session', None)
    if session is None:
        return ''

    lines = []
    for record in (session.attendance
                   .select_related('student', 'student__profile')
                   .order_by('-seconds_attended')):
        student = record.student
        name = (student.get_full_name() or '').strip() or student.get_username()
        person = getattr(student, 'profile', None)
        if person is not None and (person.first_name or person.last_name):
            name = f'{person.first_name} {person.last_name}'.strip()
        lines.append(f'- {name} <{student.email}> — {record.minutes_attended} min '
                     f'({record.attended_pct}%), {record.get_status_display().lower()}')
    return '\n'.join(lines)


def _source_material(artifact):
    """The recap plus this occurrence's raw transcript, as one blob.

    Both, not either: the recap is structured but lossy, and the transcript has
    the detail but no shape. Claude gets to use the recap as an outline and the
    transcript as the evidence. Only this occurrence's transcripts — a recurring
    class is one Teams meeting, and last week's class is not evidence.
    """
    from apps.msteams import graph
    from .pipeline import recap_markdown, transcript_text

    meeting = artifact.meeting
    parts = []
    recap = recap_markdown(meeting)
    if recap:
        from .models import SessionArtifact
        label = ('Session summary from Teams Copilot (meeting notes and action items)'
                 if artifact.summary_source == SessionArtifact.SUMMARY_TEAMS else 'Automatic recap')
        parts.append(f'## {label}\n' + recap)
    if meeting.organizer_upn and meeting.teams_meeting_id and graph.is_configured():
        vtt = transcript_text(meeting)
        if vtt:
            parts.append('## Transcript\n' + vtt)
    return '\n\n'.join(parts)

def _parse(answer):
    """Pull the JSON object out of Claude's reply, tolerating a code fence."""
    if not answer:
        return None
    text = answer.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        text = text.rsplit('```', 1)[0]
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        pack = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if isinstance(pack, dict) and not pack.get('summary_markdown') and pack.get('overview'):
        pack['summary_markdown'] = pack['overview']
    return pack if isinstance(pack, dict) and pack.get('summary_markdown') else None


def _markdown(meeting, pack):
    """The whole pack as one readable document."""
    lines = [f'# {meeting.title}', '', _session_header(meeting), '']
    if (pack.get('overview') or '').strip():
        lines += ['## Overview', '', pack['overview'].strip(), '']
    highlights = [str(h).strip() for h in pack.get('highlights') or [] if str(h).strip()]
    if highlights:
        lines += ['## Key highlights', ''] + [f'- {h}' for h in highlights] + ['']
    lines += ['## Full write-up (staff)', '', pack.get('summary_markdown', '').strip(), '']

    agenda = pack.get('next_agenda') or []
    if agenda:
        lines += ['## Agenda for the next session', '']
        lines += [f'{i}. {item}' for i, item in enumerate(agenda, 1)] + ['']

    for heading, key in (('Action items — candidates', 'student_actions'),
                         ('Action items — educator', 'educator_actions')):
        items = pack.get(key) or []
        if not items:
            continue
        lines += [f'## {heading}', '']
        for item in items:
            task = item.get('task') if isinstance(item, dict) else str(item)
            due = (item.get('due_hint') or '') if isinstance(item, dict) else ''
            why = (item.get('why') or '') if isinstance(item, dict) else ''
            suffix = ' — '.join(x for x in (due, why) if x)
            lines.append(f'- [ ] {task}' + (f' ({suffix})' if suffix else ''))
        lines.append('')

    roster = attendance_roster(meeting)
    if roster:
        lines += ['## Who attended', '', roster, '']

    topics = pack.get('topics_covered') or []
    if topics:
        lines += ['## Topics covered', '', ', '.join(str(t) for t in topics), '']
    concerns = pack.get('concerns') or []
    if concerns:
        lines += ['## Flags', ''] + [f'- {c}' for c in concerns] + ['']
    return '\n'.join(lines)


def _file_report(artifact, pack):
    """Store the pack as an AiReport on the module."""
    from apps.ai_assistant.models import AiReport

    meeting = artifact.meeting
    report, _ = AiReport.objects.update_or_create(
        linked_ref=f'session-followup:{meeting.pk}',
        defaults={
            'kind': AiReport.KIND_MEETING_RECAP,
            'title': f'Session pack — {meeting.title}',
            'module': meeting.module,
            'created_by': meeting.host,
            'prompt': 'Automated post-session follow-up (admin Claude layer).',
            'content': _markdown(meeting, pack),
            'data': pack,
            'used_llm': True,
            'status': AiReport.STATUS_FINAL,
        },
    )
    return report


def _file_to_onedrive(artifact, pack):
    """Drop the pack into the session's own folder, beside the video."""
    from apps.msteams import onedrive
    from .pipeline import folder_ref

    folder = folder_ref(artifact)
    if not folder:
        return False
    name = f'{onedrive.artifact_stem(artifact.meeting)}-session-pack.md'
    return bool(onedrive.put_text(folder, name, _markdown(artifact.meeting, pack), 'text/markdown'))


def _items(pack, key):
    """``[(task, suffix)]`` for one action list, tolerating plain strings."""
    out = []
    for item in pack.get(key) or []:
        task = _task_text(item)
        if not task:
            continue
        due = (item.get('due_hint') or '') if isinstance(item, dict) else ''
        why = (item.get('why') or '') if isinstance(item, dict) else ''
        out.append((task, ' — '.join(x for x in (due, why) if x)))
    return out


def brief_from_pack(pack):
    """The pack's class-facing parts in the :mod:`.brief` shape."""
    from . import brief
    data = brief.empty()
    data['overview'] = str(pack.get('overview') or '').strip() or (pack.get('summary_markdown') or '').strip()
    data['highlights'] = [str(h).strip() for h in pack.get('highlights') or [] if str(h).strip()]
    data['next_agenda'] = [str(a).strip() for a in pack.get('next_agenda') or [] if str(a).strip()]
    for key, group, default in (('student_actions', 'students', 'should'),
                                ('educator_actions', 'educator', 'will')):
        for item in pack.get(key) or []:
            task = _task_text(item)
            if not task:
                continue
            item = item if isinstance(item, dict) else {}
            kind = str(item.get('kind') or '').strip().lower()
            due = (item.get('due_hint') or '').strip()
            data['actions'][group].append({
                'task': task + (f' ({due})' if due and due.casefold() not in task.casefold() else ''),
                'kind': kind if kind in brief.KIND_LABELS else brief.classify(task, default=default),
                'who': str(item.get('who') or '').strip()})
    return data


def student_markdown(artifact, pack):
    """What the class sees: overview, highlights, next agenda, action items, the links.

    The fixed layout of :mod:`.brief`. Students and the educator both have
    action items in it (the client asked for both). No roster, no e-mail
    addresses, no flags about struggling candidates — those stay in the staff pack.
    """
    from . import brief
    meeting = artifact.meeting
    held = clock.local(meeting.scheduled_start)
    lines = [f'# {meeting.title}', '']
    where = ' · '.join(x for x in (
        meeting.module.display_name if meeting.module_id else '',
        meeting.cohort.code if meeting.cohort_id else '',
        f'{held:%A %d %B %Y, %H:%M} SAST' if held else '') if x)
    if where:
        lines += [where, '']
    lines.append(brief.to_markdown(brief_from_pack(pack)).strip())
    lines.append('')

    links = _links(artifact)
    if links:
        lines += ['## Recording and transcript', '']
        lines += [f'- {label}: {url}' for label, url in links] + ['']
    return '\n'.join(lines)


def _links(artifact):
    """``[(label, url)]`` a student can open without a Microsoft account.

    Every one is on our own domain and absolute, so it is clickable from the
    e-mail: the session page's player, and the transcript page. Never the
    OneDrive share links — a transcript's lands on a Microsoft sign-in page.
    """
    from core.utils import absolute_url
    links = []
    if artifact.watch_url:
        links.append(('Watch the recording', absolute_url(artifact.watch_url)))
    if artifact.transcript_url:
        links.append(('Read the transcript', absolute_url(artifact.transcript_url)))
    return links


SUMMARY_PENDING = ('The written summary of this session is not ready yet. '
                   'The recording and the transcript are on the session page.')


def without_ai_failure(text):
    """``text`` with any admin-AI failure message taken out.

    Summaries filed before :func:`admin_ai.failed` existed hold the raw error
    ("Error code: 400 … credit balance is too low …") as their body. Whatever
    the class is sent passes through here, so a stored failure can never reach
    a student again; the paragraph is replaced by :data:`SUMMARY_PENDING`.
    """
    from apps.ai_assistant import admin_ai
    if not admin_ai.contains_failure(text):
        return text
    paragraphs = text.replace('\r\n', '\n').split('\n\n')
    kept, replaced = [], False
    for paragraph in paragraphs:
        if admin_ai.contains_failure(paragraph):
            if not replaced:
                kept.append(SUMMARY_PENDING)
                replaced = True
            continue
        kept.append(paragraph)
    return '\n\n'.join(kept)


def _publish_student_summary(artifact, pack):
    """The student version becomes *the* session summary everywhere it shows:
    the ``-summary.md`` on OneDrive, the reading lesson on the module (for that
    intake), and the recap on the session page."""
    from . import summaries
    from .models import SessionArtifact

    meeting = artifact.meeting
    summary = summaries.Summary(pack['student_markdown'], SessionArtifact.SUMMARY_CLAUDE)
    try:
        summaries.publish(meeting, artifact, summary)
    except Exception:
        logger.exception('livesessions: could not publish the session summary for %s', meeting)


def _refresh_thumbnail(artifact, pack):
    """Redraw the session card now that we know what the class covered."""
    from . import thumbnails
    try:
        thumbnails.store(artifact.meeting, topics=pack.get('topics_covered') or [])
    except Exception:
        logger.exception('livesessions: could not refresh the thumbnail for %s', artifact.meeting)


def deliver(artifact, pack=None):
    """Send the class its one session message — once, and only while recent.

    With a pack: summary, next agenda, the students' action items and the
    recording / transcript links. Without one (no transcript, or Claude kept
    failing): whatever summary was published plus the links, so the class still
    hears that the session's material is up.

    A class older than the announcement window is filed and published quietly,
    and nothing is sent when there is no text yet. Returns the number of people
    notified.
    """
    from apps.communication import services as comm
    from .audience import class_students
    from .pipeline import is_recent, recap_markdown

    meeting = artifact.meeting
    if artifact.followup_sent_at:
        return 0
    artifact.followup_sent_at = timezone.now()
    artifact.save(update_fields=['followup_sent_at', 'updated_at'])
    if not is_recent(meeting):
        return 0

    if pack:
        text = pack.get('student_markdown') or student_markdown(artifact, pack)
        title = f'Session summary, next agenda & action items — {meeting.title}'
    else:
        text = without_ai_failure(recap_markdown(meeting))
        links = _links(artifact)
        if links:
            text = (text + '\n\n' if text else '') + '\n'.join(f'{label}: {url}' for label, url in links)
        if not text.strip():
            return 0
        title = f'Session material — {meeting.title}'

    url = (artifact.summary_lesson.get_absolute_url() if artifact.summary_lesson_id
           else meeting.get_absolute_url())
    body = plain_text(without_ai_failure(text))
    people = list(class_students(meeting))
    if meeting.host_id and meeting.host not in people:
        people.append(meeting.host)
    sent = 0
    for user in people:
        if comm.notify(user, category='meetings', level='success', verb='session summary',
                       title=title[:200], body=body, url=url, meeting=meeting, email=True):
            sent += 1
    logger.info('livesessions: session message for %s sent to %s people', meeting, sent)
    return sent


def plain_text(markdown):
    """Markdown → readable plain text for the notification and its e-mail."""
    out = []
    for line in (markdown or '').splitlines():
        stripped = line.lstrip()
        if stripped.startswith('#'):
            heading = stripped.lstrip('#').strip()
            out += ['', heading.upper() if line.startswith('## ') else heading]
            continue
        nested = len(line) - len(stripped) >= 2
        if stripped.startswith('- [ ] '):
            line = '☐ ' + stripped[6:]
        elif stripped.startswith(('- ', '* ')):
            line = ('    ◦ ' if nested else '• ') + stripped[2:]
        out.append(line.replace('**', '').replace('__', ''))
    return '\n'.join(out).strip()


def _task_text(action):
    """The task text of one action item, whether Claude gave it as a dict or a plain string."""
    if isinstance(action, dict):
        return (action.get('task') or '').strip()
    return str(action or '').strip()
