"""Creating, updating and cancelling a live session — everything in one place.

Scheduling a session is eight steps, ordered to survive any of them failing:

1. create the Teams meeting (or keep the session without one until the first join),
2. record who it is for,
3. open the attendance register,
4. draw the thumbnail,
5. put it on the organiser's Outlook calendar and invite the staff,
6. file it on the module's schedule as an upcoming session,
7. tell the audience,
8. open the pipeline row that will collect the recording afterwards.

Only (1) can change what the session *is*; everything after it is best-effort
and logged.

Module constants:

* :data:`TITLE_SEPARATOR` — sits between the generated title prefix and the
  part a person writes.
* :data:`NOTIFY_ON_CHANGE` — the edits the audience is told about. Moving a
  class, renaming it or changing what it is about changes what people planned
  around; fixing a thumbnail does not.
"""

import logging

from django.utils import timezone

logger = logging.getLogger('apps')


def create_session(*, host, title, start, end, audience, description='', session_kind='class',
                   institution=None, programme=None, cohort=None, module=None, week=None,
                   calendar_event=None, invitees=(), record=None, publish_recording=None,
                   group=None, notify=True):
    """Schedule a live session and return the :class:`MeetingRoom`.

    ``invitees`` is an iterable of ``User`` objects invited by name — required
    for a private/one-on-one session, optional (and additive) for the rest.
    """
    from apps.communication.models import MeetingParticipant
    from apps.msteams import services as teams
    from .models import LiveSessionSettings

    conf = LiveSessionSettings.load()
    record = conf.auto_record if record is None else bool(record)
    publish = conf.publish_recordings if publish_recording is None else bool(publish_recording)

    meeting = teams.create_class_meeting(
        host=host, title=title, start=start, end=end, module=module, group=group,
        description=description, record=record,
    )

    meeting.audience = audience
    meeting.session_kind = session_kind
    meeting.institution = institution
    meeting.programme = programme
    meeting.cohort = cohort
    meeting.week = week
    meeting.calendar_event = calendar_event
    meeting.record_automatically = record
    meeting.publish_recording = publish
    meeting.save(update_fields=[
        'audience', 'session_kind', 'institution', 'programme', 'cohort', 'week',
        'calendar_event', 'record_automatically', 'publish_recording', 'updated_at'])

    MeetingParticipant.objects.get_or_create(
        meeting=meeting, user=host, defaults={'role': 'host', 'rsvp': 'yes'})
    for user in invitees or ():
        if user and user.pk != getattr(host, 'pk', None):
            MeetingParticipant.objects.get_or_create(meeting=meeting, user=user)

    ensure_class_session(meeting)
    ensure_thumbnail(meeting, conf=conf)
    ensure_calendar_event(meeting, conf=conf)
    publish_upcoming_to_schedule(meeting)
    open_pipeline(meeting)

    if notify:
        try:
            from .reminders import announce_new_session
            announce_new_session(meeting, automated=True)
        except Exception:
            logger.exception('livesessions: could not announce %s', meeting)
    return meeting


TITLE_SEPARATOR = ' | '


def session_title_prefix(*, institution=None, programme=None, module=None, cohort=None, start=None):
    """``SAICA IAC FREP JAN2027 · Sat 14 Nov 2026 08:00`` — the codes, then when.

    Every session title starts with this, so two sessions sharing a topic never
    share a name in Teams, OneDrive or the calendar.
    """
    codes = [getattr(part, 'code', '') for part in (institution, programme, module, cohort) if part]
    head = ' '.join(code for code in codes if code)
    when = timezone.localtime(start).strftime('%a %d %b %Y %H:%M') if start else ''
    return ' · '.join(part for part in (head, when) if part)


def compose_session_title(*, suffix, **scope):
    """The full title: the prefix, then :data:`TITLE_SEPARATOR`, then ``suffix``."""
    prefix = session_title_prefix(**scope)
    suffix = (suffix or '').strip()
    if not suffix:
        return prefix[:200]
    if not prefix:
        return suffix[:200]
    room = 200 - len(prefix) - len(TITLE_SEPARATOR)
    return f'{prefix}{TITLE_SEPARATOR}{suffix[:max(room, 10)]}'[:200]


def split_session_title(title):
    """The part of ``title`` a person wrote (everything after the first separator)."""
    title = title or ''
    return title.split(TITLE_SEPARATOR, 1)[1] if TITLE_SEPARATOR in title else title


NOTIFY_ON_CHANGE = ('title', 'scheduled_start', 'scheduled_end', 'description')


def update_session(meeting, *, fields=None, reschedule=False, changed=(), by=None,
                   notify=False):
    """Persist edits, push them to Microsoft, and tell the audience.

    ``reschedule`` re-sends the times to Graph — an explicit flag, so changing a
    title cannot risk bouncing a meeting already in people's calendars.

    The Outlook/Teams event is patched on every edit so Exchange sends an
    updated invitation (or created now, if it never was). With ``notify``, the
    audience is told about :data:`NOTIFY_ON_CHANGE` edits present in ``changed``.

    A meeting made by hand in Teams (``teams_meeting_external``) — usually one
    link the whole timetable shares — is never patched from here: that would
    move every class on it. A new time gets this session a meeting of its own
    instead (:func:`detach_from_external_meeting`). A session put on the
    platform with no meeting yet (a seeded timetable) is given one now.
    """
    from apps.msteams import graph

    if fields:
        meeting.save(update_fields=list(fields) + ['updated_at'])
    else:
        meeting.save()

    sync_class_session_times(meeting)

    if meeting.is_teams and meeting.teams_meeting_external:
        if reschedule:
            detach_from_external_meeting(meeting)
    elif meeting.is_teams and not meeting.teams_meeting_id:
        ensure_teams_meeting(meeting)
    elif meeting.is_teams and meeting.teams_meeting_id and meeting.organizer_upn:
        payload = {'subject': meeting.title}
        if reschedule and meeting.scheduled_start:
            payload['startDateTime'] = _iso(meeting.scheduled_start)
            payload['endDateTime'] = _iso(_end_of(meeting))
        graph.update_online_meeting(meeting.organizer_upn, meeting.teams_meeting_id, **payload)

        if meeting.teams_calendar_event_id:
            patch = {
                'subject': meeting.title,
                'body': {'contentType': 'HTML', 'content': _event_body(meeting)},
                'attendees': graph.event_attendees(staff_attendees(meeting)),
            }
            if meeting.teams_join_url:
                patch['location'] = graph.teams_location(meeting.teams_join_url)
            if meeting.scheduled_start:
                patch['start'] = graph.graph_datetime(meeting.scheduled_start)
                patch['end'] = graph.graph_datetime(_end_of(meeting))
            graph.update_calendar_event(meeting.organizer_upn, meeting.teams_calendar_event_id,
                                        **patch)
        else:
            ensure_calendar_event(meeting)

    ensure_thumbnail(meeting, force=True)
    publish_upcoming_to_schedule(meeting)

    worth_telling = [name for name in (changed or ()) if name in NOTIFY_ON_CHANGE]
    if notify and worth_telling and meeting.is_active:
        try:
            from .reminders import announce_session_update
            announce_session_update(meeting, changed=worth_telling, by=by)
        except Exception:
            logger.exception('livesessions: could not announce the update to %s', meeting)
    return meeting


def cancel_session(meeting, *, by=None, reason=''):
    """Cancel a session: tear down the Microsoft side and tell the audience.

    The Outlook/Teams event is *cancelled* (not just deleted) so Exchange sends
    every invited member of staff a cancellation carrying ``reason``; then the
    Teams meeting itself is deleted, the "live session" card comes off the
    module schedule, and everyone in the audience is told in-app and by e-mail.
    Reminders stop on their own: the reminder job only reads active sessions.

    A hand-made Teams meeting is left as it is: it may carry other classes.
    """
    from apps.communication import services as comm
    from apps.msteams import graph
    from .audience import recipients_for

    if meeting.is_teams and meeting.organizer_upn and not meeting.teams_meeting_external:
        if meeting.teams_calendar_event_id:
            graph.cancel_calendar_event(meeting.organizer_upn, meeting.teams_calendar_event_id,
                                        comment=reason or f'“{meeting.title}” has been cancelled.')
        if meeting.teams_meeting_id:
            graph.delete_online_meeting(meeting.organizer_upn, meeting.teams_meeting_id)

    meeting.is_active = False
    meeting.save(update_fields=['is_active', 'updated_at'])
    withdraw_from_schedule(meeting)

    body = f'“{meeting.title}” has been cancelled.'
    if reason:
        body += f' {reason}'
    for user in recipients_for(meeting).iterator():
        comm.notify(user, actor=by, category='meetings', level='warning',
                    verb='session cancelled', title=f'Cancelled: {meeting.title}',
                    body=body, url=meeting.get_absolute_url(), meeting=meeting, email=True)
    return meeting


def withdraw_from_schedule(meeting):
    """Take a cancelled session's "live session" card off the module schedule.

    Only the upcoming card goes; a row the pipeline has already turned into a
    recording is a record of a class that happened and is left alone.
    """
    if not meeting.module_id:
        return 0
    from apps.learning.models import ModuleMaterial
    deleted, _ = ModuleMaterial.objects.filter(
        meeting=meeting, kind=ModuleMaterial.KIND_LIVE).delete()
    return deleted


def ensure_class_session(meeting):
    """Open the attendance register for a module session.

    Only module sessions: an empty register for a platform-wide briefing would
    put a meaningless 0% attendance row on everybody's record.
    """
    from apps.communication.models import ClassSession

    if not meeting.module_id or getattr(meeting, 'class_session', None) is not None:
        return None
    start = meeting.scheduled_start or timezone.now()
    session, _ = ClassSession.objects.get_or_create(
        meeting=meeting,
        defaults={
            'module': meeting.module,
            'title': meeting.title,
            'session_date': timezone.localtime(start).date(),
            'starts_at': meeting.scheduled_start,
            'ends_at': meeting.scheduled_end,
            'created_by': meeting.host,
        },
    )
    return session


def ensure_thumbnail(meeting, *, conf=None, force=False):
    """Draw and store the session card. Returns True if one was written."""
    from django.core.files.base import ContentFile
    from . import thumbnails

    if meeting.thumbnail and not force:
        return False
    blob = thumbnails.render(meeting, conf=conf)
    if not blob:
        return False
    try:
        meeting.thumbnail.save(thumbnails.filename_for(meeting), ContentFile(blob), save=True)
        return True
    except Exception:
        logger.exception('livesessions: could not save the thumbnail for %s', meeting)
        return False


def ensure_calendar_event(meeting, *, conf=None):
    """Put the session on the organiser's Outlook calendar and invite the staff.

    Only licensed people go on the Microsoft invite — see
    :func:`apps.msteams.graph.create_calendar_event` for why students must not.
    """
    from apps.msteams import graph
    from .models import LiveSessionSettings

    conf = conf or LiveSessionSettings.load()
    if not conf.create_calendar_events or not meeting.is_teams or meeting.teams_calendar_event_id:
        return None
    if not meeting.organizer_upn or not meeting.scheduled_start:
        return None

    event = graph.create_calendar_event(
        meeting.organizer_upn,
        subject=meeting.title,
        start_iso=_iso(meeting.scheduled_start),
        end_iso=_iso(_end_of(meeting)),
        body_html=_event_body(meeting),
        attendees=staff_attendees(meeting),
        join_url=meeting.teams_join_url,
    )
    if event and event.get('id'):
        meeting.teams_calendar_event_id = event['id']
        meeting.save(update_fields=['teams_calendar_event_id', 'updated_at'])
        return event
    return None


def staff_attendees(meeting):
    """``[(upn, name), …]`` for the Microsoft invite: the licensed people involved.

    Every named invitee who hosts on Teams plus the module's educators. The host
    is invited too unless they are the organiser (a host outside the tenant books
    under the default organiser and still needs it). Never students.
    """
    from apps.msteams import services as teams

    people = {}
    host = meeting.host
    if host is not None and host.is_active and teams.host_can_use_teams(host):
        people[host.pk] = host
    for participant in meeting.participants.select_related('user'):
        user = participant.user
        if user is not None and user.is_active and teams.host_can_use_teams(user):
            people[user.pk] = user
    if meeting.module_id:
        from django.contrib.auth import get_user_model
        educators = (get_user_model().objects
                     .filter(is_active=True, profile__taught_modules=meeting.module)
                     .distinct())
        for user in educators:
            people.setdefault(user.pk, user)

    organizer = (meeting.organizer_upn or '').strip().lower()
    out = []
    for user in people.values():
        upn = teams.organizer_upn_for(user)
        if upn and upn.strip().lower() != organizer:
            out.append((upn, user.get_full_name() or user.get_username()))
    return out


def publish_upcoming_to_schedule(meeting):
    """Show the session on the module's schedule before it happens.

    The pipeline later switches this same ModuleMaterial row to a recording.
    A module with no phases yet still has students who must find their class,
    so it is given a fallback block rather than the session being dropped.
    """
    if not meeting.module_id:
        return None
    from apps.learning.models import ModuleMaterial
    from .pipeline import phase_for

    phase = phase_for(meeting)
    if phase is None:
        phase = _default_phase_for(meeting)
        if phase is None:
            return None
    material, _ = ModuleMaterial.objects.update_or_create(
        phase=phase, kind=ModuleMaterial.KIND_LIVE, meeting=meeting,
        defaults={
            'week': meeting.week if meeting.week_id and meeting.week.phase_id == phase.pk else None,
            'title': meeting.title,
            'description': meeting.description or '',
            'url': '',
        },
    )
    return material


def _default_phase_for(meeting):
    """A "Live sessions" block on a module that has no phases yet.

    Every ModuleMaterial needs a phase. Idempotent: created at most once per
    module, and never for a module that already has a phase.
    """
    from apps.learning.models import ModulePhase

    try:
        phase, _created = ModulePhase.objects.get_or_create(
            programme_module=meeting.module,
            kind=ModulePhase.KIND_OTHER,
            sequence=0,
            defaults={
                'title': 'Live sessions',
                'summary': 'Classes and coaching sessions scheduled for this module.',
                'is_active': True,
            },
        )
        return phase
    except Exception:
        logger.exception('livesessions: could not open a default phase on %s', meeting.module)
        return None


def ensure_teams_meeting(meeting, *, conf=None):
    """Give a Teams session that has no Microsoft meeting yet its own one.

    Seeders write timetables with no link so a seed never touches Teams. The
    meeting is owned by the host's tenant account, else the default organiser,
    and put on the organiser's Outlook calendar.

    Idempotent, and the row is locked while the meeting is made so two
    simultaneous "Join" clicks cannot mint two meetings: if the locked row
    already has a link, someone else got there first and their fields are
    copied back. Returns True when a meeting was created.
    """
    from django.db import transaction
    from apps.communication.models import MeetingRoom
    from apps.msteams import graph, services as teams
    from .models import LiveSessionSettings

    if meeting.provider != MeetingRoom.PROVIDER_TEAMS or meeting.teams_join_url:
        return False
    if not (teams.teams_active() and meeting.scheduled_start and meeting.is_active):
        return False
    conf = conf or LiveSessionSettings.load()

    with transaction.atomic():
        locked = MeetingRoom.objects.select_for_update().get(pk=meeting.pk)
        if locked.teams_join_url:
            _copy_teams_fields(locked, meeting)
            return False
        organizer = locked.organizer_upn or teams.meeting_owner_for(locked.host)
        if not organizer:
            logger.warning('livesessions: no Teams organiser for %s', locked)
            return False
        created = graph.create_online_meeting(
            organizer, subject=locked.title, start_iso=_iso(locked.scheduled_start),
            end_iso=_iso(_end_of(locked)),
            allow_recording=locked.record_automatically)
        if not created:
            logger.warning('livesessions: Graph could not create a meeting for %s', locked)
            return False
        locked.organizer_upn = organizer
        locked.teams_meeting_id = created['id'][:512]
        locked.teams_join_url = (created.get('join_url') or '')[:1000]
        locked.teams_meeting_external = False
        locked.save(update_fields=['organizer_upn', 'teams_meeting_id', 'teams_join_url',
                                   'teams_meeting_external', 'updated_at'])
    _copy_teams_fields(locked, meeting)
    try:
        ensure_calendar_event(meeting, conf=conf)
    except Exception:
        logger.exception('livesessions: no Outlook event for %s', meeting)
    open_pipeline(meeting)
    return True


def detach_from_external_meeting(meeting):
    """Move a session off a Teams meeting the platform did not create.

    Hand-made meetings often share one link across a whole timetable, so
    patching one would move every class on it; the session gets its own meeting
    instead and the hand-made one is never changed. If Microsoft refuses, the
    session keeps the old link.
    """
    fields = ['teams_join_url', 'teams_meeting_id', 'teams_calendar_event_id',
              'organizer_upn', 'teams_meeting_external']
    before = {name: getattr(meeting, name) for name in fields}
    if _detach_and_create(meeting):
        return True
    for name, value in before.items():
        setattr(meeting, name, value)
    meeting.save(update_fields=fields + ['updated_at'])
    logger.warning('livesessions: %s keeps its hand-made Teams meeting — a new one could not be '
                   'created', meeting)
    return False


def _detach_and_create(meeting):
    """Clear the Teams link fields on ``meeting`` and mint it a fresh meeting. True on success."""
    meeting.teams_join_url = ''
    meeting.teams_meeting_id = ''
    meeting.teams_calendar_event_id = ''
    meeting.organizer_upn = ''
    meeting.teams_meeting_external = False
    meeting.save(update_fields=['teams_join_url', 'teams_meeting_id', 'teams_calendar_event_id',
                                'organizer_upn', 'teams_meeting_external', 'updated_at'])
    return ensure_teams_meeting(meeting)


def _copy_teams_fields(source, target):
    """Copy the Teams link fields from ``source`` onto ``target`` (e.g. from the locked row back to the caller’s instance)."""
    for name in ('organizer_upn', 'teams_meeting_id', 'teams_join_url',
                 'teams_calendar_event_id', 'teams_meeting_external'):
        setattr(target, name, getattr(source, name))


def sync_class_session_times(meeting):
    """Keep the attendance register's date and times on the session's own."""
    session = getattr(meeting, 'class_session', None)
    if session is None or not meeting.scheduled_start:
        return
    session.title = meeting.title
    session.starts_at = meeting.scheduled_start
    session.ends_at = meeting.scheduled_end
    session.session_date = timezone.localtime(meeting.scheduled_start).date()
    session.save(update_fields=['title', 'starts_at', 'ends_at', 'session_date'])


def open_pipeline(meeting):
    """Create the after-class pipeline row up front, in ``waiting``."""
    from .models import SessionArtifact
    if not meeting.is_teams:
        return None
    artifact, _ = SessionArtifact.objects.get_or_create(
        meeting=meeting, defaults={'state': SessionArtifact.STATE_WAITING})
    return artifact


def busy_blocks(day, *, hosts=None):
    """Every session on ``day``, as ``(start, end, meeting)``, in order.

    Deliberately ignores audience: a slot is occupied whether or not the viewer
    may see what occupies it.
    """
    from datetime import datetime, time
    from apps.communication.models import MeetingRoom

    start = timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())
    end = start + timezone.timedelta(days=1)
    qs = (MeetingRoom.objects
          .filter(is_active=True, scheduled_start__lt=end, scheduled_end__gt=start)
          .select_related('host', 'module'))
    if hosts:
        qs = qs.filter(host__in=hosts)
    return [(m.scheduled_start, m.scheduled_end, m) for m in qs.order_by('scheduled_start')]


def free_slots(day, *, hosts=None, open_from=8, open_to=20, minimum_minutes=30,
               for_user=None):
    """The gaps on ``day`` that are long enough to hold a session.

    Returns ``(start, end)`` pairs between ``open_from`` and ``open_to`` local
    time. Overlapping sessions are merged first, so two classes running at once
    do not produce a phantom gap between them.

    ``for_user`` also subtracts that person's *own* connected calendar.
    """
    from datetime import datetime, time

    tz = timezone.get_current_timezone()
    window_start = timezone.make_aware(datetime.combine(day, time(hour=open_from)), tz)
    window_end = timezone.make_aware(datetime.combine(day, time(hour=open_to)), tz)

    spans = [(start, end or start + timezone.timedelta(hours=1))
             for start, end, _meeting in busy_blocks(day, hosts=hosts) if start]
    if for_user is not None:
        from . import sources
        spans += sources.busy_spans(for_user, window_start, window_end)

    blocks = [(max(start, window_start), min(end, window_end)) for start, end in spans]
    blocks = [b for b in blocks if b[1] > b[0]]
    blocks.sort()

    merged = []
    for start, end in blocks:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    gaps, cursor = [], window_start
    for start, end in merged:
        if (start - cursor).total_seconds() >= minimum_minutes * 60:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if (window_end - cursor).total_seconds() >= minimum_minutes * 60:
        gaps.append((cursor, window_end))
    return gaps


def availability(start_day, *, days=7, hosts=None, open_from=8, open_to=20,
                 minimum_minutes=30, for_user=None, skip_weekends=False,
                 now=None):
    """``days`` days of bookable gaps, one row per day, ready to render.

    Each row is ``{'day', 'slots', 'sessions', 'is_today', 'is_weekend'}`` where
    ``slots`` are ``(start, end, minutes)`` triples. A fully-booked day is kept
    with empty ``slots``; slots already in the past are trimmed (today's gaps
    start no earlier than ``now``).
    """
    from datetime import timedelta as _td

    now = now or timezone.now()
    today = timezone.localdate(now)
    rows = []
    for offset in range(max(1, int(days))):
        day = start_day + _td(days=offset)
        if skip_weekends and day.weekday() >= 5:
            continue

        slots = []
        for start, end in free_slots(day, hosts=hosts, open_from=open_from,
                                     open_to=open_to, minimum_minutes=minimum_minutes,
                                     for_user=for_user):
            start = max(start, now) if day == today else start
            minutes = int((end - start).total_seconds() // 60)
            if minutes >= minimum_minutes:
                slots.append((start, end, minutes))

        rows.append({
            'day': day,
            'slots': slots,
            'sessions': [meeting for _s, _e, meeting in busy_blocks(day, hosts=hosts)],
            'is_today': day == today,
            'is_weekend': day.weekday() >= 5,
        })
    return rows


def _iso(moment):
    """ISO 8601 for Graph, treating a naive datetime as the current time zone."""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment.isoformat()


def _end_of(meeting):
    """The session's end, or an hour after its start when no end was given."""
    return meeting.scheduled_end or (meeting.scheduled_start + timezone.timedelta(hours=1))


def _event_body(meeting):
    """HTML body of the Outlook event: description, audience, Teams join link and the platform link."""
    from django.conf import settings as django_settings
    site = (getattr(django_settings, 'SITE_URL', '') or '').rstrip('/')
    link = f'{site}{meeting.get_join_url()}' if site else meeting.get_join_url()
    from django.utils.html import escape
    rows = [f'<p>{escape(meeting.description)}</p>' if meeting.description else '',
            f'<p><strong>Audience:</strong> {escape(meeting.audience_label)}</p>',
            (f'<p><a href="{escape(meeting.teams_join_url)}">Join the Microsoft Teams meeting</a></p>'
             if meeting.is_teams and meeting.teams_join_url else ''),
            f'<p><a href="{escape(link)}">Open this session on Metanoia Group</a></p>']
    return ''.join(r for r in rows if r)
