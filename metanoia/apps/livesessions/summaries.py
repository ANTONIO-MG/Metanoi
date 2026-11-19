"""The session summary: fetched when it exists, filed, published, announced.

Once a summary is available it is, once each:

1. saved to OneDrive as ``<date> <title>-summary.md`` under
   ``<root>/<institution>/<programme>/<module>/<intake>/<Test n>/<Week n>/Session summaries/``
   (week levels dropped for a class not on a week) — a staff copy;
2. published as a reading lesson in the intake's week on the module, so it
   reaches only the intake it was taught to;
3. filed as the session's recap (shown on the session page);
4. announced to the class.

**Source.** Teams' Copilot meeting insight first (needs
``OnlineMeetingAiInsight.Read.All`` and a Copilot licence on the organiser).
The job waits for it for up to :data:`COPILOT_WAIT_HOURS` after the class, then
builds the summary via :mod:`.brief` (Claude when configured, else mapped from
Copilot). After the wait, or on a 403, it falls back to the transcript.

Until a summary exists the job retries for up to ``WINDOW_DAYS`` and records why
in ``summary_note``. A better summary arriving later replaces a weaker one in
place without notifying anyone twice.

One-on-one and private sessions are never published: a summary of a private
coaching session is not class material.

Module settings:

* ``WINDOW_DAYS`` -- how long after a class to keep looking for its summary.
* ``COPILOT_WAIT_HOURS`` -- how long after a class ends to wait for Teams
  Copilot's meeting insight before writing the summary from the transcript
  instead.
* ``COPILOT_WAIT_NOTE`` -- the ``summary_note`` written while the job is
  waiting for Copilot (the pipeline reads it).
* ``RECHECK_MINUTES`` -- do not ask Graph about the same class more often
  than this.
* ``BACKFILL_DAYS`` / ``BACKFILL_RECHECK_MINUTES`` -- how far back the daily
  sweep looks for classes the platform never saw, and for summaries that
  appeared after the regular job stopped looking; and how often it rechecks
  each one.
* ``BEFORE`` / ``AFTER`` -- a recurring class shares one online meeting, so
  its transcripts and insights are told apart by when they were created,
  inside this window around the occurrence.
* ``SESSION_RESOURCE_TITLE`` -- the lesson resource that takes a reader from
  the summary to the session page.
* ``OLD_SUMMARY_RESOURCE_TITLE`` -- the legacy OneDrive-link resource; it is
  replaced on republish and by ``manage.py repair_session_links``.
"""

import datetime as dt
import hashlib
import logging

from django.utils import timezone

from core import clock

logger = logging.getLogger('apps')

WINDOW_DAYS = 7
COPILOT_WAIT_HOURS = 48
COPILOT_WAIT_NOTE = 'Waiting for the Teams Copilot summary'

RECHECK_MINUTES = 25
BACKFILL_DAYS = 120
BACKFILL_RECHECK_MINUTES = 20 * 60
BEFORE = dt.timedelta(minutes=30)
AFTER = dt.timedelta(hours=8)


SESSION_RESOURCE_TITLE = 'Recording and transcript (session page)'
OLD_SUMMARY_RESOURCE_TITLE = 'Open the summary on OneDrive'


class Summary:
    """A summary's Markdown text and where it came from (a ``SUMMARY_*`` source)."""

    def __init__(self, markdown, source):
        """Hold ``markdown`` and its ``source``."""
        self.markdown = markdown
        self.source = source

    @property
    def digest(self):
        """SHA-256 of the Markdown, used to tell a changed summary from the same one."""
        return hashlib.sha256(self.markdown.encode('utf-8')).hexdigest()


def candidates(now=None, *, window_days=WINDOW_DAYS, recheck_minutes=RECHECK_MINUTES):
    """Ended, non-private Teams classes whose summary is still worth chasing.

    Skipped: a class that already has the Teams Copilot summary (the best there
    is), one whose AI session pack has replaced the summary, one with a
    published Claude summary (only a Teams recap could beat it), and one
    checked within the last ``recheck_minutes``.
    """
    from apps.communication.models import MeetingRoom
    from .models import SessionArtifact

    now = now or timezone.now()
    qs = (MeetingRoom.objects
          .filter(provider=MeetingRoom.PROVIDER_TEAMS, is_active=True,
                  scheduled_end__lt=now - dt.timedelta(minutes=10),
                  scheduled_end__gte=now - dt.timedelta(days=window_days))
          .exclude(teams_meeting_id='')
          .exclude(audience=MeetingRoom.AUDIENCE_PRIVATE)
          .select_related('module__programme__institution', 'cohort__programme__institution',
                          'programme__institution', 'institution', 'week__phase')
          .order_by('scheduled_end'))
    recent = now - dt.timedelta(minutes=recheck_minutes)
    out = []
    for meeting in qs:
        artifact = getattr(meeting, 'artifact', None)
        if artifact is not None:
            if artifact.summary_source == SessionArtifact.SUMMARY_TEAMS:
                continue
            if artifact.ai_followup_done:
                continue
            if (artifact.summary_published_at
                    and artifact.summary_source == SessionArtifact.SUMMARY_CLAUDE):
                continue
            if artifact.summary_checked_at and artifact.summary_checked_at > recent:
                continue
        out.append(meeting)
    return out


def _in_window(item, meeting):
    """True when a Graph item was created inside this occurrence's ``BEFORE``/``AFTER`` window.

    An item without a usable timestamp is given the benefit of the doubt.
    """
    created = item.get('createdDateTime') or item.get('createdDateTimeUtc') or ''
    if not created or not meeting.scheduled_start:
        return True
    try:
        when = dt.datetime.fromisoformat(created.replace('Z', '+00:00'))
    except ValueError:
        return True
    end = meeting.scheduled_end or meeting.scheduled_start
    return meeting.scheduled_start - BEFORE <= when <= end + AFTER


def for_session(items, meeting):
    """Only the recordings / transcripts that belong to this occurrence.

    A recurring class is one Teams meeting, so Graph lists every week's
    recordings and transcripts together; taking the first would file last
    week's class under this one.
    """
    return [i for i in (items or []) if _in_window(i, meeting)]


def copilot_wait_over(meeting, now=None):
    """True once :data:`COPILOT_WAIT_HOURS` have passed since the class ended."""
    ended = meeting.scheduled_end or meeting.scheduled_start
    if ended is None:
        return True
    return (now or timezone.now()) >= ended + dt.timedelta(hours=COPILOT_WAIT_HOURS)


def use_llm():
    """Whether this job asks Claude to write the summary.

    Only when the admin AI is configured and the AI follow-up is off (that job
    writes the Claude version itself; asking twice would bill twice).
    """
    from . import ai_followup, brief
    try:
        return brief.llm_available() and not ai_followup.is_enabled()
    except Exception:       # pragma: no cover
        return False


def fetch(meeting, *, now=None):
    """``(Summary | None, note)`` — the best summary available now, or why there is none.

    Copilot writes its insight minutes to hours after the class, so until
    :data:`COPILOT_WAIT_HOURS` have passed (and unless Graph refused with a
    403) this waits for it rather than sending a transcript extract now and a
    replacement later.
    """
    from apps.msteams import graph
    from . import brief
    from .models import SessionArtifact

    organizer = meeting.organizer_upn
    notes = []

    status, insights = graph.ai_insights_with_status(organizer, meeting.teams_meeting_id)
    mine = [i for i in insights if _in_window(i, meeting)]
    if status == 200 and mine:
        details = [graph.get_ai_insight(organizer, meeting.teams_meeting_id, i['id']) or i for i in mine]
        if any(d.get('meetingNotes') or d.get('actionItems') for d in details):
            data = None
            if use_llm():
                data = brief.from_llm(meeting, insights=details, transcript=_transcript(meeting))
            data = data or brief.from_copilot(details, meeting)
            if brief.has_content(data):
                return Summary(brief.to_markdown(data, title=meeting.title),
                               SessionArtifact.SUMMARY_TEAMS), ''
    copilot_refused = status == 403
    if copilot_refused:
        notes.append('Teams AI recap: the app needs the OnlineMeetingAiInsight.Read.All permission '
                     '(and the organiser a Copilot licence)')
    elif not copilot_wait_over(meeting, now):
        until = clock.local((meeting.scheduled_end or meeting.scheduled_start)
                            + dt.timedelta(hours=COPILOT_WAIT_HOURS))
        return None, f'{COPILOT_WAIT_NOTE} (until {until:%a %d %b %H:%M})'
    else:
        notes.append(f'No Teams Copilot summary {COPILOT_WAIT_HOURS}h after the class')

    status, transcripts = graph.transcripts_with_status(organizer, meeting.teams_meeting_id)
    mine = [t for t in transcripts if _in_window(t, meeting)]
    if status == 403:
        notes.append('Transcripts: Graph transcript access is disabled for this tenant')
    elif status == 200 and not mine:
        notes.append('No transcript for this class yet (was transcription on?)')
    if mine:
        from apps.msteams import recap
        vtt = '\n\n'.join(filter(None, (graph.get_transcript_content(organizer, meeting.teams_meeting_id, t['id'])
                                       for t in mine)))
        cues = recap.parse_vtt(vtt)
        if cues:
            data = None
            if use_llm():
                text = '\n'.join(f'{s + ": " if s else ""}{t}' for s, t in cues)
                data = brief.from_llm(meeting, transcript=text)
            if data:
                return Summary(brief.to_markdown(data, title=meeting.title),
                               SessionArtifact.SUMMARY_CLAUDE), ''
            data = brief.from_extractive(recap.extractive_recap(cues, title=meeting.title), cues)
            return Summary(brief.to_markdown(data, title=meeting.title),
                           SessionArtifact.SUMMARY_EXTRACTIVE), ''
    return None, '; '.join(notes) or 'No summary available yet'


def _transcript(meeting):
    """This occurrence's transcript as ``Speaker: text`` lines, or '' (for Claude)."""
    from apps.msteams import recap
    from .pipeline import transcript_text
    try:
        cues = recap.parse_vtt(transcript_text(meeting))
    except Exception:
        logger.warning('session summary: could not read the transcript of %s', meeting, exc_info=True)
        return ''
    return '\n'.join(f'{s + ": " if s else ""}{t}' for s, t in cues)


def process(meeting, *, now=None):
    """Check one class; publish or upgrade its summary. Returns a short status word.

    A same-rank summary is not an upgrade: Claude words the same class
    differently every time, and treating that as new would rewrite (and
    re-bill) it every run. With the AI follow-up on, the class is told once,
    by the session pack (summary, next agenda and action items, see
    :func:`ai_followup.deliver`), so no separate announcement is sent.
    """
    from .models import SessionArtifact

    now = now or timezone.now()
    artifact, _ = SessionArtifact.objects.get_or_create(meeting=meeting)
    summary, note = fetch(meeting, now=now)
    artifact.summary_checked_at = now
    if summary is None:
        artifact.summary_note = note[:300]
        artifact.save(update_fields=['summary_checked_at', 'summary_note', 'updated_at'])
        return 'waiting'

    current = SessionArtifact.SUMMARY_RANK.get(artifact.summary_source, 0)
    new = SessionArtifact.SUMMARY_RANK[summary.source]
    if (new < current or (artifact.summary_published_at and new == current)
            or summary.digest == artifact.summary_hash):
        artifact.save(update_fields=['summary_checked_at', 'updated_at'])
        return 'unchanged'

    first_time = not artifact.summary_published_at
    publish(meeting, artifact, summary)
    from . import ai_followup
    if first_time and not ai_followup.is_enabled():
        announce(meeting, artifact, summary)
    return 'published' if first_time else 'upgraded'


def publish(meeting, artifact, summary):
    """File the summary to OneDrive, the course and the session page."""
    _file_to_onedrive(meeting, artifact, summary)
    _file_report(meeting, summary)
    lesson = _publish_lesson(meeting, artifact, summary)
    artifact.summary_lesson = lesson
    artifact.summary_source = summary.source
    artifact.summary_hash = summary.digest
    artifact.summary_note = ''
    artifact.summary_published_at = artifact.summary_published_at or timezone.now()
    artifact.save()
    logger.info('session summary %s for %s (%s)', 'published', meeting, summary.source)
    return artifact


def summary_folder_parts(meeting):
    """``[INST, PROG, MODULE, INTAKE, Test n, Week n, 'Session summaries']`` for this class.

    A class not on a week is filed under the intake's block the class fell in
    ("Exam 1"), or straight under the intake when the module has no block for
    that date.
    """
    parts = list(meeting.storage_path_parts)
    if meeting.week_id:
        return parts + ['Session summaries']
    base = parts[:-1]
    if meeting.module_id:
        phase, _week = _placement(meeting)
        if phase is not None:
            from apps.communication.models import _safe_folder_name
            label = phase.short_label if phase.kind in ('test', 'exam', 'supp') else phase.display_title
            base = base + [_safe_folder_name(label)]
    return base + ['Session summaries']


def _file_to_onedrive(meeting, artifact, summary):
    """Save the staff copy to OneDrive, under the same file stem the pipeline uses.

    Records the item id, web URL and share link on ``artifact`` (unsaved). When
    OneDrive is unavailable the summary is published in-app only.
    """
    from apps.msteams import onedrive
    folder = onedrive.ensure_path(summary_folder_parts(meeting))
    if folder is None:
        logger.warning('session summary: OneDrive unavailable for %s — published in-app only', meeting)
        return
    name = f'{onedrive.artifact_stem(meeting)}-summary.md'
    item = onedrive.put_text(folder, name, summary.markdown, 'text/markdown')
    if not item:
        return
    artifact.summary_item_id = item.get('id', '')
    artifact.summary_web_url = item.get('webUrl', '')
    artifact.summary_share_url = onedrive.anonymous_link(folder, item.get('id')) or artifact.summary_share_url
    if not artifact.folder_id:
        artifact.folder_path = folder['path']


def _file_report(meeting, summary):
    """The session page reads its recap from this AiReport."""
    from apps.ai_assistant.models import AiReport
    AiReport.objects.update_or_create(
        linked_ref=f'meeting:{meeting.pk}',
        defaults={'kind': AiReport.KIND_MEETING_RECAP, 'title': f'Session summary — {meeting.title}'[:200],
                  'content': summary.markdown, 'module': meeting.module,
                  'used_llm': summary.source != 'extractive', 'status': 'final',
                  'created_by': meeting.host})


def _publish_lesson(meeting, artifact, summary):
    """A reading lesson on the module, in the intake's week — or None for a
    class that is not on a module (it still has its session page).

    The lesson is saved quietly because the class is told by the session
    message, not once per row. Its resource links back to the recording and
    transcript through the session page on our own domain, since a OneDrive
    share link stops at a Microsoft sign-in page.
    """
    from apps.learning.models import Lesson, LessonBlock, LessonSection, ModuleMaterial
    from core.metanoia_materials.textconv import sections_from_markdown

    if not meeting.module_id:
        return None
    when = clock.local(meeting.scheduled_start or timezone.now())
    title = f'Session summary — {meeting.title} ({when:%d %b %Y})'[:200]
    lesson = artifact.summary_lesson or Lesson(slug=f'session-summary-{meeting.pk}', created_by=meeting.host)
    lesson.title = title
    lesson.subtitle = (f'{meeting.module} · {meeting.cohort.code if meeting.cohort_id else ""} · '
                       f'live session {when:%a %d %b %Y, %H:%M}')[:300]
    lesson.module = meeting.module
    lesson.visibility = Lesson.VIS_MODULE
    lesson.status = Lesson.STATUS_PUBLISHED
    lesson.publish_at = lesson.publish_at or timezone.now()
    lesson.author_name = 'Metanoia Group'
    sections = sections_from_markdown(summary.markdown, default_title='Summary')
    lesson.estimated_minutes = sum(s['minutes'] for s in sections) or 3
    lesson._quiet = True
    lesson.save()
    lesson.blocks.all().delete()
    lesson.sections.all().delete()
    for order, section in enumerate(sections):
        row = LessonSection.objects.create(lesson=lesson, title=section['heading'], order=order,
                                           section_type=LessonSection.TYPE_READING,
                                           duration_minutes=section['minutes'], open_by_default=order == 0)
        LessonBlock.objects.create(lesson=lesson, section=row, order=0, block_type=LessonBlock.TYPE_TEXT,
                                   data={'html': section['html']})
    lesson.resources.filter(title=OLD_SUMMARY_RESOURCE_TITLE).delete()
    lesson.resources.update_or_create(
        title=SESSION_RESOURCE_TITLE,
        defaults={'kind': 'link', 'url': meeting.get_absolute_url(), 'order': 0})

    phase, week = _placement(meeting)
    if phase is not None:
        from .pipeline import quiet_upsert
        quiet_upsert(
            ModuleMaterial, {'source_ref': f'session-summary/{meeting.pk}'},
            {'phase': phase, 'week': week, 'kind': ModuleMaterial.KIND_DOCUMENT,
             'title': title, 'lesson': lesson, 'meeting': meeting,
             'description': 'Summary of the live session — key points and action items.',
             'audience': ModuleMaterial.AUDIENCE_STUDENTS, 'is_published': True,
             'estimated_minutes': lesson.estimated_minutes, 'order': 16,
             'used_on': when.date(), 'created_by': meeting.host})
    return lesson


def _placement(meeting):
    """``(phase, week)`` for the class — the intake's own block, never another's."""
    from .pipeline import phase_for
    if meeting.week_id:
        return meeting.week.phase, meeting.week
    phase = phase_for(meeting)
    if phase is None or (meeting.cohort_id and phase.cohort_id not in (None, meeting.cohort_id)):
        return None, None
    day = clock.local(meeting.scheduled_start).date() if meeting.scheduled_start else None
    week = None
    if day:
        week = phase.weeks.filter(starts_on__lte=day, ends_on__gte=day).order_by('number').first()
    return phase, week


def students_for(meeting):
    """Who the summary is for: the intake's students on the module — or, for an
    intake-wide class, the whole intake. Staff are not notified."""
    from .audience import class_students
    return class_students(meeting)


def announce(meeting, artifact, summary=None):
    """Tell the class (and the educator) the summary is ready, with the summary
    itself in the message and its e-mail: overview, key highlights, next agenda
    and action items. :func:`process` calls this only the first time a summary is
    published, never for an upgrade.

    A summary found two or more days after the class (by the daily sweep, or
    one that only appeared later) is announced as "Now available", naming the
    class. The host is included because the educator has action items too.
    """
    from apps.communication import services as comm
    from .ai_followup import _links, plain_text, without_ai_failure
    url = (artifact.summary_lesson.get_absolute_url() if artifact.summary_lesson_id
           else meeting.get_absolute_url())
    held = meeting.scheduled_start
    late = held and (timezone.now() - held).days >= 2
    if late:
        title = f'Now available: summary of {meeting.title} ({clock.local(held):%d %b})'
        intro = f'The summary of the live session on {clock.local(held):%A %d %B} is now on your module.'
    else:
        title = f'Session summary ready — {meeting.title}'
        intro = 'The summary of the live session is ready: overview, key highlights, the next agenda and action items.'
    body = intro
    text = without_ai_failure(getattr(summary, 'markdown', '') or '')
    if text.strip():
        body += '\n\n' + plain_text(_without_title(text))
    links = _links(artifact)
    if links:
        body += '\n\n' + '\n'.join(f'{label}: {link}' for label, link in links)
    people = list(students_for(meeting))
    if meeting.host_id and meeting.host not in people:
        people.append(meeting.host)
    sent = 0
    for user in people:
        if comm.notify(user, category='meetings', level='success', verb='session summary ready',
                       title=title[:200], body=body, url=url, meeting=meeting, email=True):
            sent += 1
    return sent


def display_sections(markdown):
    """The summary as ``[{'heading', 'html'}]`` for a page — Overview, Key highlights,
    Agenda for the next session, Action items… — sanitised. [] when empty."""
    if not (markdown or '').strip():
        return []
    from core.sanitize import clean_html
    from core.metanoia_materials.textconv import sections_from_markdown
    return [dict(section, html=clean_html(section['html']))
            for section in sections_from_markdown(markdown, default_title='Summary')]


def _without_title(markdown):
    """The summary without its ``# Title — session summary`` line (the message has a title)."""
    lines = markdown.lstrip().splitlines()
    if lines and lines[0].startswith('# '):
        lines = lines[1:]
    return '\n'.join(lines).strip()


def run(limit=20, now=None):
    """The scheduler entry point: import ended Teams classes, then chase their summaries.

    One failing class is logged and counted as ``error``; it must not stop the rest.
    """
    from apps.msteams import calendar_import
    from apps.msteams import graph
    if not graph.is_configured():
        return 'Graph not configured'
    imported = calendar_import.import_sessions(days=WINDOW_DAYS, now=now)
    counts = {}
    for meeting in candidates(now)[:limit]:
        try:
            status = process(meeting, now=now)
        except Exception:
            logger.exception('session summary failed for %s', meeting)
            status = 'error'
        counts[status] = counts.get(status, 0) + 1
    detail = ', '.join(f'{n} {k}' for k, n in sorted(counts.items())) or 'nothing to check'
    return f'{imported} · summaries: {detail}'


def run_backfill(days=BACKFILL_DAYS, limit=150, now=None):
    """The daily sweep backwards: add any group class the platform missed in the
    last ``days`` days, and publish any summary that has appeared since — each
    class at most once a day. Students are told when a summary becomes available
    ("Now available: summary of … (13 Oct)")."""
    from apps.msteams import calendar_import
    from apps.msteams import graph
    if not graph.is_configured():
        return 'Graph not configured'
    imported = calendar_import.import_sessions(days=days, now=now)
    counts = {}
    for meeting in candidates(now, window_days=days, recheck_minutes=BACKFILL_RECHECK_MINUTES)[:limit]:
        try:
            status = process(meeting, now=now)
        except Exception:
            logger.exception('session summary backfill failed for %s', meeting)
            status = 'error'
        counts[status] = counts.get(status, 0) + 1
    detail = ', '.join(f'{n} {k}' for k, n in sorted(counts.items())) or 'nothing to check'
    return f'back {days} days — {imported} · summaries: {detail}'
