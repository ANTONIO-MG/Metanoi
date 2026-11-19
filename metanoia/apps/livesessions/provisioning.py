"""Get every scheduled Teams session a real Teams meeting before the day.

Programme seeders put a whole timetable on the platform as Teams sessions with
no Microsoft link (a seed never calls Microsoft). This walks those sessions and,
for each one, either

* **links** it to a meeting the organiser already made by hand in Outlook/Teams
  for that slot — same start, the programme and module codes in the subject —
  so nobody ends up with two meetings and the invitations already sent keep
  working; or
* **creates** a meeting of its own (:func:`services.ensure_teams_meeting`), with
  the lobby bypassed, auto-recording on and the staff invited on Outlook.

Nothing in Teams is changed or deleted. Linked meetings are flagged
``teams_meeting_external`` so the platform never patches them later; moving such
a session from the platform gives it its own meeting instead.

:data:`EARLY_START` lets a hand-made event start a little before the
platform's slot and still match. :data:`CREATE_PAUSE_SECONDS` is a pause
between meetings created in one run, to be gentle on Graph.
"""

import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field

from django.utils import timezone

from apps.msteams.calendar_import import _parse_when

logger = logging.getLogger('apps')

EARLY_START = dt.timedelta(minutes=15)
CREATE_PAUSE_SECONDS = 0.5


@dataclass
class Plan:
    """What :func:`prepare` did, or would do, with each session.

    ``linked`` sessions were attached to an existing hand-made Teams meeting,
    ``created`` ones were given a meeting of their own and ``failed`` ones could
    not be given either.
    """
    linked: list = field(default_factory=list)
    created: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def __str__(self):
        """One-line tally for command output."""
        return (f'{len(self.linked)} linked to an existing Teams meeting, '
                f'{len(self.created)} given a new meeting, {len(self.failed)} failed')


def sessions_needing_meetings(*, since=None, until=None, programme=None, cohort=None):
    """Active, scheduled Teams sessions that have no join link yet.

    Optionally narrowed to a start window (``since`` inclusive, ``until``
    exclusive) and to a programme or cohort code (case-insensitive). Ordered by
    start time.
    """
    from apps.communication.models import MeetingRoom
    qs = (MeetingRoom.objects
          .filter(provider=MeetingRoom.PROVIDER_TEAMS, is_active=True, teams_join_url='',
                  scheduled_start__isnull=False)
          .select_related('module', 'programme', 'cohort', 'host')
          .order_by('scheduled_start'))
    if since:
        qs = qs.filter(scheduled_start__gte=since)
    if until:
        qs = qs.filter(scheduled_start__lt=until)
    if programme:
        qs = qs.filter(programme__code__iexact=programme)
    if cohort:
        qs = qs.filter(cohort__code__iexact=cohort)
    return qs


def _words(text):
    """The upper-cased alphanumeric words in ``text``, for matching codes in a subject."""
    return set(re.findall(r'[A-Z0-9]+', (text or '').upper()))


def matching_event(meeting, events):
    """The hand-made calendar event that already covers ``meeting``, or None.

    It must be a live Teams event, start at most :data:`EARLY_START` before the
    session and still be running when the session starts, and name the
    session's programme and module in its subject. A session with no module
    (a whole-cohort debrief) is never linked: one module's event is not proof
    it is the cohort's.
    """
    if not (meeting.module_id and meeting.scheduled_start):
        return None
    need = {meeting.module.code.upper()}
    if meeting.programme_id and meeting.programme.code:
        need.add(meeting.programme.code.upper())
    for event in events:
        if event.get('isCancelled'):
            continue
        join_url = (event.get('onlineMeeting') or {}).get('joinUrl')
        if not (event.get('isOnlineMeeting') and join_url):
            continue
        starts, ends = _parse_when(event.get('start')), _parse_when(event.get('end'))
        if not (starts and ends):
            continue
        if not (starts - EARLY_START <= meeting.scheduled_start < ends):
            continue
        if need <= _words(event.get('subject')):
            return event
    return None


def prepare(meetings, *, organizer, apply=False, link_existing=True, out=None):
    """Link or create a Teams meeting for each session. Returns a :class:`Plan`.

    With ``apply`` off nothing is written and no meeting is created; the
    organiser's calendar is still read, so the plan shows what would be linked.

    A class that has already happened is never given a new meeting: it ran in
    whatever meeting it ran in, and a new one now would have no recording. It
    is linked or left alone.
    """
    from apps.msteams import graph
    from . import services

    say = out or (lambda line: None)
    meetings = list(meetings)
    plan = Plan()
    if not meetings:
        return plan

    events = []
    if link_existing:
        first = min(m.scheduled_start for m in meetings) - dt.timedelta(days=1)
        last = max(m.scheduled_start for m in meetings) + dt.timedelta(days=1)
        fmt = '%Y-%m-%dT%H:%M:%SZ'
        events = graph.calendar_view(
            organizer, first.astimezone(dt.timezone.utc).strftime(fmt),
            last.astimezone(dt.timezone.utc).strftime(fmt))

    online_ids = {}
    for meeting in meetings:
        when = timezone.localtime(meeting.scheduled_start).strftime('%a %d %b %H:%M')
        label = f'#{meeting.pk} {when} {meeting.title}'
        event = matching_event(meeting, events) if link_existing else None
        if event is not None:
            join_url = event['onlineMeeting']['joinUrl']
            say(f'link    {label}  →  "{(event.get("subject") or "").strip()}"')
            if apply:
                if join_url not in online_ids:
                    found = graph.find_online_meeting(organizer, join_url)
                    online_ids[join_url] = (found or {}).get('id', '')
                meeting.teams_join_url = join_url[:1000]
                meeting.teams_meeting_id = online_ids[join_url][:512]
                meeting.organizer_upn = organizer
                meeting.teams_meeting_external = True
                meeting.save(update_fields=['teams_join_url', 'teams_meeting_id', 'organizer_upn',
                                            'teams_meeting_external', 'updated_at'])
                services.open_pipeline(meeting)
            plan.linked.append(meeting)
            continue

        if meeting.scheduled_start <= timezone.now():
            say(f'skip    {label}  (already happened; no hand-made meeting found for it)')
            continue
        say(f'create  {label}')
        if not apply:
            plan.created.append(meeting)
            continue
        try:
            made = services.ensure_teams_meeting(meeting)
        except Exception:
            logger.exception('provisioning: could not create a meeting for %s', meeting)
            made = False
        if made:
            plan.created.append(meeting)
            services.publish_upcoming_to_schedule(meeting)
            time.sleep(CREATE_PAUSE_SECONDS)
        else:
            say(f'FAILED  {label}')
            plan.failed.append(meeting)
    return plan


def suffix_for(meeting):
    """What a session is about, without the parts the title prefix now carries.

    ``FREP live session · Unit 2: Group statements`` → ``Live session · Unit 2:
    Group statements``. A title already in the new form keeps its own suffix.
    """
    from . import services
    text = services.split_session_title(meeting.title).strip()
    code = getattr(meeting.module, 'code', '') if meeting.module_id else ''
    if code and text.upper().startswith(code.upper() + ' '):
        text = text[len(code) + 1:].strip()
    return text[:1].upper() + text[1:] if text else (meeting.get_session_kind_display() or 'Session')


@dataclass
class Rebuild:
    """What :func:`rebuild` changed, session by session.

    ``retitled``, ``new_meeting`` (given their own Teams meeting),
    ``moved_to_general`` (placed on the General module) and ``failed``.
    """
    retitled: list = field(default_factory=list)
    new_meeting: list = field(default_factory=list)
    moved_to_general: list = field(default_factory=list)
    failed: list = field(default_factory=list)

    def __str__(self):
        """One-line tally for command output."""
        return (f'{len(self.retitled)} retitled, {len(self.new_meeting)} given their own Teams '
                f'meeting, {len(self.moved_to_general)} placed on the General module, '
                f'{len(self.failed)} failed')


def rebuild(meetings, *, apply=False, general_module_for=None, out=None):
    """Give each upcoming session the standard title and a Teams meeting of its own.

    * title → ``<codes> · <date time> | <what it is about>`` (:func:`suffix_for`);
    * a session on a hand-made, shared meeting (or on none) gets its own
      meeting, created with the new title; one that already has its own has
      the new title patched onto it in Teams and Outlook;
    * a session with no module (a whole-intake debrief) is filed on the
      programme's General module when ``general_module_for`` is given — its
      audience is left as it is, so the same people are invited.

    Sessions are changed in place: their pages, registers, schedule entries and
    links on the platform stay the same. Nobody is notified here — the
    reminders before each class carry the link.
    """
    from . import services

    say = out or (lambda line: None)
    result = Rebuild()
    for meeting in meetings:
        changes = []
        general = None
        if not meeting.module_id and general_module_for and meeting.programme_id:
            general = general_module_for(meeting.programme)
            if general is not None:
                changes.append(f'module → {general.code}')
        module = general or meeting.module
        title = services.compose_session_title(
            institution=meeting.resolved_institution,
            programme=meeting.programme, module=module, cohort=meeting.cohort,
            start=meeting.scheduled_start, suffix=suffix_for(meeting))
        own = bool(meeting.teams_join_url) and not meeting.teams_meeting_external
        if title != meeting.title:
            changes.append('retitle')
        if not own:
            changes.append('own Teams meeting')
        when = timezone.localtime(meeting.scheduled_start).strftime('%a %d %b %H:%M')
        say(f'#{meeting.pk} {when}  {"; ".join(changes) or "already done"}\n'
            f'      {meeting.title}\n   →  {title}')
        if not changes or not apply:
            if changes:
                _tally(result, meeting, changes)
            continue
        try:
            if general is not None:
                meeting.module = general
            meeting.title = title
            meeting.save(update_fields=['title', 'module', 'updated_at'])
            if meeting.teams_meeting_external:
                ok = services.detach_from_external_meeting(meeting)
            elif not meeting.teams_join_url:
                ok = services.ensure_teams_meeting(meeting)
            else:
                services.update_session(meeting, reschedule=False)
                ok = True
            if not ok:
                raise RuntimeError('Microsoft Teams did not create the meeting')
            services.sync_class_session_times(meeting)
            services.ensure_thumbnail(meeting, force=True)
            services.publish_upcoming_to_schedule(meeting)
            _tally(result, meeting, changes)
            time.sleep(CREATE_PAUSE_SECONDS)
        except Exception as exc:
            logger.exception('provisioning: could not rebuild %s', meeting)
            say(f'   FAILED: {exc}')
            result.failed.append(meeting)
    return result


def _tally(result, meeting, changes):
    """Record ``meeting`` in each :class:`Rebuild` bucket its ``changes`` belong to."""
    if 'retitle' in changes:
        result.retitled.append(meeting)
    if 'own Teams meeting' in changes:
        result.new_meeting.append(meeting)
    if any(c.startswith('module') for c in changes):
        result.moved_to_general.append(meeting)
