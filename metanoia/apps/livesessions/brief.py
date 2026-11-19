"""The session summary's shape: overview, highlights, next agenda, action items.

Every summary the class receives — built from Teams Copilot's meeting insight,
written by Claude, or (last resort) lifted from the transcript — is one
:class:`dict` in the same shape and is rendered by :func:`to_markdown` under the
same fixed headings, in this order::

    ## Overview                        what was discussed, said and stated, briefly
    ## Key highlights                  a few bullets
    ## Agenda for the next session     only when the material says what comes next
    ## Action items — students         "Must do / Should do / Will do", with names
    ## Action items — educator
    ## Action items — everyone

A section with nothing in it is left out rather than filled with invention.

The shape::

    {'overview': str,
     'highlights': [str],
     'next_agenda': [str],
     'actions': {'students': [item], 'educator': [item], 'everyone': [item]}}
    item = {'task': str, 'kind': 'must' | 'should' | 'will', 'who': str}

Three ways in:

* :func:`from_copilot` — no LLM: Copilot's meeting notes become the overview and
  highlights, its action items are sorted by owner (the host/educator →
  educator; "class", "students" → students; a named person → students, with the
  name; nobody or "everyone" → everyone), and sentences that talk about the next
  session/week/class become the agenda.
* :func:`from_llm` — Claude, given Copilot's insight and/or the transcript, with
  :data:`PROMPT` (the headings above are fixed in it). Falls back to
  :func:`from_copilot` / :func:`from_extractive` when it is off or fails.
* :func:`from_extractive` — the free transcript extract, when there is no Copilot
  insight and no LLM.

``MAX_HIGHLIGHTS``, ``MAX_AGENDA`` and ``MAX_OVERVIEW_CHARS`` set how much of
each part the no-LLM path keeps. :data:`PROMPT` asks for JSON, not Markdown: the
section headings are fixed in it and in :func:`to_markdown` and the model only
fills them, so a model that re-words a heading cannot change the layout the
class receives.
"""

import json
import logging
import re

from core import clock

logger = logging.getLogger('apps')

OVERVIEW = 'Overview'
HIGHLIGHTS = 'Key highlights'
AGENDA = 'Agenda for the next session'
ACTION_HEADINGS = (
    ('students', 'Action items — students'),
    ('educator', 'Action items — educator'),
    ('everyone', 'Action items — everyone'),
)
KIND_LABELS = {'must': 'Must do', 'should': 'Should do', 'will': 'Will do'}

MAX_HIGHLIGHTS = 6
MAX_AGENDA = 6
MAX_OVERVIEW_CHARS = 900

_MUST_RE = re.compile(r"\b(must|have to|has to|need to|needs to|required to|is required|are required|"
                      r"make sure|ensure|is due|are due|compulsory|mandatory)\b", re.I)
_SHOULD_RE = re.compile(r'\b(should|ought to|recommended|advised to|encouraged to|try to|consider)\b', re.I)
_WILL_RE = re.compile(r"\b(will|'ll|going to|promised to|agreed to|plans? to|intends? to|offered to|"
                      r"volunteered to)\b", re.I)
_NEXT_RE = re.compile(r'\b(next|upcoming|following|coming)\s+(session|class|lesson|lecture|week|meeting|time|'
                      r'tutorial|workshop)\b', re.I)
_DEADLINE_RE = re.compile(r'\b(before|by|ahead of|prior to)\s+(the\s+)?(next|upcoming|following|coming)\s+'
                          r'(session|class|lesson|lecture|week|meeting|time|tutorial|workshop)\b', re.I)
_STUDENT_WORDS = {'class', 'students', 'student', 'learners', 'candidates', 'the class', 'all students',
                  'attendees', 'all attendees', 'participants'}
_EVERYONE_WORDS = {'everyone', 'all', 'team', 'group', 'the group', 'everybody', 'us', 'we'}
_EDUCATOR_WORDS = {'educator', 'lecturer', 'coach', 'teacher', 'presenter', 'host', 'tutor', 'facilitator',
                   'instructor', 'organiser', 'organizer'}


def empty():
    """A brief with every section empty, in the shape described in the module docstring."""
    return {'overview': '', 'highlights': [], 'next_agenda': [],
            'actions': {'students': [], 'educator': [], 'everyone': []}}


def has_content(brief):
    """True when ``brief`` has anything to show in at least one section."""
    if not brief:
        return False
    return bool(brief.get('overview') or brief.get('highlights') or brief.get('next_agenda')
                or any((brief.get('actions') or {}).get(k) for k, _h in ACTION_HEADINGS))


def to_markdown(brief, *, title=''):
    """The brief under the fixed headings. Empty sections are left out.

    A highlight may carry sub-bullets on its following lines (``"    - ..."``);
    they are kept as they are.
    """
    lines = [f'# {title} — session summary', ''] if title else []
    overview = (brief.get('overview') or '').strip()
    if overview:
        lines += [f'## {OVERVIEW}', '', overview, '']
    highlights = [h for h in (brief.get('highlights') or []) if str(h).strip()]
    if highlights:
        lines += [f'## {HIGHLIGHTS}', '']
        for item in highlights:
            lines.append(f'- {str(item).strip()}')
        lines.append('')
    agenda = [a for a in (brief.get('next_agenda') or []) if str(a).strip()]
    if agenda:
        lines += [f'## {AGENDA}', '']
        lines += [f'{i}. {str(a).strip()}' for i, a in enumerate(agenda, 1)] + ['']
    actions = brief.get('actions') or {}
    for key, heading in ACTION_HEADINGS:
        items = [i for i in (actions.get(key) or []) if action_line(i)]
        if items:
            lines += [f'## {heading}', '']
            lines += [action_line(i) for i in items] + ['']
    return '\n'.join(lines).strip() + '\n'


def action_line(item):
    """``- **Must do:** Redo question 2 (Thabo Mokoena)`` — or '' for an empty item.

    The owner is added unless the task already names them ("Thabo will …").
    """
    if isinstance(item, str):
        item = {'task': item}
    task = (item.get('task') or '').strip()
    if not task:
        return ''
    kind = item.get('kind') if item.get('kind') in KIND_LABELS else classify(task)
    who = (item.get('who') or '').strip()
    named = who and (who.casefold() in task.casefold()
                     or who.split()[0].casefold() in _norm(task).split())
    suffix = f' ({who})' if who and not named else ''
    return f'- **{KIND_LABELS[kind]}:** {task}{suffix}'


def classify(text, default='should'):
    """``must`` / ``should`` / ``will`` from the wording of an action item."""
    if _MUST_RE.search(text or ''):
        return 'must'
    if _SHOULD_RE.search(text or ''):
        return 'should'
    if _WILL_RE.search(text or ''):
        return 'will'
    return default


def from_copilot(insights, meeting):
    """Teams Copilot meeting insights → the brief, without an LLM."""
    notes = [n for i in insights for n in (i.get('meetingNotes') or []) if isinstance(n, dict)]
    items = [a for i in insights for a in (i.get('actionItems') or []) if isinstance(a, dict)]
    brief = empty()
    brief['overview'] = _overview(notes)
    brief['highlights'] = _highlights(notes)

    educators = educator_names(meeting)
    for item in items:
        task = _task_text(item)
        if not task:
            continue
        owner = (item.get('ownerDisplayName') or '').strip()
        group, who = owner_group(owner, educators)
        brief['actions'][group].append({
            'task': task, 'who': who,
            'kind': classify(task, default='will' if group == 'educator' else 'should')})

    agenda = []
    for note in notes:
        for text in [note.get('text') or ''] + [s.get('text') or '' for s in note.get('subpoints') or []
                                                if isinstance(s, dict)]:
            agenda += _next_session_sentences(text)
    for item in items:
        agenda += _next_session_sentences(_task_text(item))
    brief['next_agenda'] = _dedupe(agenda)[:MAX_AGENDA]
    return brief


def _task_text(item):
    """Copilot's description of the action (its ``text``), or its short title."""
    return ' '.join(((item.get('text') or '').strip() or (item.get('title') or '').strip()).split())


def _overview(notes):
    """The overview for the no-LLM path: the note titles covered, then the first sentence of the first few notes, clipped to ``MAX_OVERVIEW_CHARS``."""
    titles = [n.get('title', '').strip() for n in notes if (n.get('title') or '').strip()]
    parts = []
    if titles:
        parts.append(f'This session covered {_join_and(titles[:5])}'
                     + (' and more' if len(titles) > 5 else '') + '.')
    for note in notes[:4]:
        first = _first_sentence(note.get('text') or '')
        if first:
            parts.append(first)
    text = ' '.join(parts)
    if len(text) > MAX_OVERVIEW_CHARS:
        cut = text[:MAX_OVERVIEW_CHARS]
        text = cut[:cut.rfind('. ') + 1] if '. ' in cut else cut.rstrip() + '…'
    return text.strip()


def _highlights(notes):
    """The highlights for the no-LLM path: one per Copilot note, up to ``MAX_HIGHLIGHTS``.

    A note with subpoints becomes a bold title followed by indented sub-bullets
    (``"\n    - ..."``); a note without becomes the title and a clip of its text.
    """
    out = []
    for note in notes[:MAX_HIGHLIGHTS]:
        title = (note.get('title') or '').strip()
        subs = []
        for sub in (note.get('subpoints') or [])[:4]:
            if not isinstance(sub, dict):
                continue
            head, body = (sub.get('title') or '').strip(), (sub.get('text') or '').strip()
            line = f'{head}: {body}' if head and body else head or body
            if line:
                subs.append(_clip(line, 240))
        if not subs:
            rest = (note.get('text') or '').strip()
            line = f'**{title}** — {_clip(rest, 280)}' if title and rest else (f'**{title}**' if title
                                                                             else _clip(rest, 280))
        else:
            line = f'**{title}**' if title else subs.pop(0)
            line += ''.join(f'\n    - {s}' for s in subs)
        if line.strip():
            out.append(line)
    return out


def educator_names(meeting):
    """Every way the session's educator(s) might be named by Teams."""
    users = []
    if getattr(meeting, 'host_id', None):
        users.append(meeting.host)
    try:
        users += [p.user for p in meeting.participants.filter(role__in=('host', 'cohost'))
                  .select_related('user', 'user__profile')]
    except Exception:       # pragma: no cover - an unsaved meeting
        pass
    names = set()
    for user in users:
        if user is None:
            continue
        person = getattr(user, 'profile', None)
        candidates = [user.get_full_name(), user.get_username(), (user.email or '').split('@')[0]]
        if person is not None:
            candidates.append(f'{person.first_name} {person.last_name}')
            candidates.append(person.first_name or '')
        candidates.append(user.first_name)
        names |= {_norm(c) for c in candidates if _norm(c)}
    upn = (getattr(meeting, 'organizer_upn', '') or '').split('@')[0]
    if _norm(upn):
        names.add(_norm(upn))
    return names


def owner_group(owner, educators):
    """``(group, who)`` for a Copilot action item's owner."""
    key = _norm(owner)
    if not key:
        return 'everyone', ''
    if key in {_norm(w) for w in _EVERYONE_WORDS}:
        return 'everyone', ''
    if key in {_norm(w) for w in _STUDENT_WORDS}:
        return 'students', ''
    if key in {_norm(w) for w in _EDUCATOR_WORDS}:
        return 'educator', ''
    tokens = key.split()
    if key in educators or (tokens and ' '.join(tokens[:2]) in educators):
        return 'educator', owner
    if len(tokens) == 1 and tokens[0] in educators:
        return 'educator', owner
    return 'students', owner


def _norm(name):
    """``name`` case-folded, with punctuation (except apostrophes) dropped and spaces collapsed."""
    return ' '.join(re.sub(r"[^\w' ]+", ' ', (name or '').casefold()).split())


def _next_session_sentences(text):
    """The sentences of ``text`` that talk about the next session.

    A sentence such as "...before the next session" is a deadline, not an agenda
    item, so anything matching ``_DEADLINE_RE`` is left out.
    """
    out = []
    for sentence in _sentences(text):
        if _NEXT_RE.search(sentence) and not _DEADLINE_RE.search(sentence):
            out.append(sentence)
    return out


def from_extractive(recap, cues=()):
    """The free transcript extract (:func:`apps.msteams.recap.extractive_recap`) → the brief."""
    from apps.msteams.recap import _sentences as transcript_sentences

    brief = empty()
    brief['overview'] = (recap.get('summary') or '').strip()
    brief['highlights'] = [_clip(k, 280) for k in (recap.get('key_points') or [])[:5]]
    agenda = [s for s in transcript_sentences(list(cues))
              if _NEXT_RE.search(s) and not _DEADLINE_RE.search(s)]
    brief['next_agenda'] = _dedupe(agenda)[:MAX_AGENDA]
    for sentence in recap.get('action_items') or []:
        brief['actions']['everyone'].append({'task': _clip(sentence, 280), 'who': '',
                                             'kind': classify(sentence)})
    return brief


PROMPT = f"""You are writing the summary of a live class on a CTA (Chartered Accountancy) \
exam-preparation platform. It is sent to the students and to their educator, and is \
published on the module under these headings, in this order:

  1. "{OVERVIEW}" — a brief description (3-5 sentences) of what was discussed, said and \
stated in the session overall.
  2. "{HIGHLIGHTS}" — a few (3-6) key highlights: the points a student who missed the \
session must not miss.
  3. "{AGENDA}" — what the next session will cover: what was left unfinished, promised or \
announced for next time. Leave it empty if the material does not say.
  4. "Action items" — for the next session, grouped: students, educator, everyone. Each \
item is one concrete thing someone must do, should do, or said they will do.

You are given the session details, the Microsoft Teams Copilot meeting insight (meeting \
notes and action items with owners) when there is one, and the transcript when there is \
one. Treat Copilot's notes and action items as the outline and the transcript as the \
evidence.

Reply with STRICT JSON only, no prose outside it:

{{
  "overview": "…",
  "highlights": ["…"],
  "next_agenda": ["…"],
  "actions": {{
    "students": [{{"task": "…", "kind": "must|should|will", "who": "a named student, or empty for the whole class"}}],
    "educator": [{{"task": "…", "kind": "must|should|will", "who": "the educator's name"}}],
    "everyone": [{{"task": "…", "kind": "must|should|will", "who": ""}}]
  }}
}}

Rules:
- Ground everything in the material. Do not invent examples, marks, dates or tasks.
- "kind": "must" for what someone was told they must/need to do; "should" for what was \
recommended; "will" for what someone said they will do.
- Use Copilot's owner names where it gives them. The educator is named in the session \
details; their own commitments go under "educator".
- Never include an e-mail address, and never guess who said something.
- Write in plain, student-friendly English. Empty list when there is nothing for a key.
"""


def llm_available():
    """True when the admin Claude layer is configured (key + ADMIN_AI_ENABLED)."""
    try:
        from apps.ai_assistant import admin_ai
        return bool(admin_ai.is_enabled())
    except Exception:       # pragma: no cover - the AI app is optional
        return False


def from_llm(meeting, *, insights=(), transcript=''):
    """Ask Claude for the brief. The brief, or None when it is off or fails."""
    if not llm_available() or not (insights or (transcript or '').strip()):
        return None
    from apps.ai_assistant import admin_ai

    parts = [PROMPT, 'SESSION', session_header(meeting)]
    if insights:
        parts += ['TEAMS COPILOT MEETING INSIGHT', copilot_text(insights)]
    if transcript:
        parts += ['TRANSCRIPT', transcript[:100000]]
    answer, usage = admin_ai.ask([], '\n\n'.join(parts))
    if admin_ai.failed(answer, usage):
        logger.error('livesessions: Claude summary failed for %s: %s', meeting,
                     (usage or {}).get('error') or (answer or '')[:300])
        return None
    brief = parse(answer)
    if brief is None:
        logger.warning('livesessions: Claude returned no usable summary JSON for %s', meeting)
    return brief


def parse(answer):
    """Claude's JSON reply → a clean brief, or None."""
    text = (answer or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0]
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    brief = empty()
    brief['overview'] = str(data.get('overview') or '').strip()
    brief['highlights'] = [str(h).strip() for h in data.get('highlights') or [] if str(h).strip()]
    brief['next_agenda'] = [str(a).strip() for a in data.get('next_agenda') or [] if str(a).strip()]
    actions = data.get('actions') if isinstance(data.get('actions'), dict) else {}
    for key, _heading in ACTION_HEADINGS:
        for item in actions.get(key) or []:
            if isinstance(item, str):
                item = {'task': item}
            if not isinstance(item, dict) or not str(item.get('task') or '').strip():
                continue
            kind = str(item.get('kind') or '').strip().lower()
            brief['actions'][key].append({
                'task': str(item['task']).strip(),
                'kind': kind if kind in KIND_LABELS else classify(str(item['task'])),
                'who': str(item.get('who') or '').strip()})
    return brief if has_content(brief) and brief['overview'] else None


def session_header(meeting):
    """The session facts the model needs: title, educator, module, week, when it was held, description."""
    rows = [
        ('Title', meeting.title),
        ('Educator', _host_name(meeting)),
        ('Module', meeting.module.display_name if meeting.module_id else ''),
        ('Week / topic', meeting.week.display_title if meeting.week_id else ''),
        ('Held', f'{clock.local(meeting.scheduled_start):%A %d %B %Y, %H:%M} SAST'
                 if meeting.scheduled_start else ''),
        ('Description', meeting.description),
    ]
    return '\n'.join(f'{k}: {v}' for k, v in rows if v)


def _host_name(meeting):
    """The host's display name, or ``''`` when the session has no host."""
    if not getattr(meeting, 'host_id', None):
        return ''
    from .identity import display_name_for
    return display_name_for(meeting.host)


def copilot_text(insights):
    """Copilot's insight as plain text for the prompt."""
    lines = []
    for insight in insights:
        for note in insight.get('meetingNotes') or []:
            lines.append(f"* {note.get('title') or 'Note'}: {(note.get('text') or '').strip()}")
            for sub in note.get('subpoints') or []:
                lines.append(f"  - {sub.get('title') or ''}: {(sub.get('text') or '').strip()}")
        for item in insight.get('actionItems') or []:
            owner = item.get('ownerDisplayName') or 'no owner given'
            lines.append(f'ACTION ({owner}): {_task_text(item)}')
        for event in ((insight.get('viewpoint') or {}).get('mentionEvents') or []):
            said = (event.get('transcriptUtterance') or '').strip()
            if said:
                lines.append(f'MENTION: {said}')
    return '\n'.join(lines)


def _sentences(text):
    """``text`` split into sentences, dropping fragments of three characters or fewer."""
    parts = re.split(r'(?<=[.!?])\s+', (text or '').strip())
    return [p.strip() for p in parts if len(p.strip()) > 3]


def _first_sentence(text):
    """The first sentence of ``text``, clipped to 300 characters."""
    sentences = _sentences(text)
    return _clip(sentences[0], 300) if sentences else ''


def _clip(text, limit):
    """``text`` with whitespace collapsed, cut to ``limit`` characters with an ellipsis when longer."""
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _join_and(items):
    """``['a', 'b', 'c']`` → ``'a, b and c'``, skipping empty items."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return ''.join(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def _dedupe(items):
    """``items`` without repeats, comparing the first 120 characters of their normalised form."""
    seen, out = set(), []
    for item in items:
        key = _norm(item)[:120]
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out
