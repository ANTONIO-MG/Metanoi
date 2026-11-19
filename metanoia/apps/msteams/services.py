"""High-level Teams live-classroom operations, called from the LMS.

This is the seam the rest of the app uses — views never touch Graph directly.
Microsoft Teams is the only video engine: when a meeting cannot be created the
caller is told (:class:`MeetingNotCreated`, or a session saved without a link
that is created on first join) — there is no other room to fall back to.
"""

import logging

from django.conf import settings
from django.utils import timezone

from . import graph

logger = logging.getLogger('apps')


class MeetingNotCreated(Exception):
    """Microsoft Teams could not create the meeting. ``str(exc)`` says why, for people."""


def teams_active():
    """True when Teams is the selected provider AND Graph is configured."""
    return getattr(settings, 'MEETING_PROVIDER', 'teams') == 'teams' and graph.is_configured()


def host_can_use_teams(user):
    """Admins / staff / educators — the licensed people who run classes and are
    invited to them in Outlook. (Everyone *joins* through Teams; this decides
    who is treated as staff on the Microsoft side.)"""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff or user.is_superuser:
        return True
    prof = getattr(user, 'profile', None)
    return bool(prof and prof.user_type in ('admin', 'staff', 'educator'))


def organizer_upn_for(user):
    """The Microsoft UPN that should own meetings for ``user``.

    Order: an explicit per-profile override (``profile.ms_upn`` if present) →
    the account e-mail → the configured default organizer. Returns '' if none.
    """
    if user is not None:
        prof = getattr(user, 'profile', None)
        upn = getattr(prof, 'ms_upn', '') if prof else ''
        if upn:
            return upn
        if getattr(user, 'email', ''):
            return user.email
    return getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or ''


def meeting_owner_for(user):
    """The UPN that will own a Teams meeting the user schedules: their own
    Microsoft account if it exists in the tenant, else the default organiser.
    (Attendee addresses use :func:`organizer_upn_for`, which may be external.)"""
    default = getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or ''
    candidates = []
    if user is not None and host_can_use_teams(user):
        prof = getattr(user, 'profile', None)
        candidates += [getattr(prof, 'ms_upn', '') if prof else '', getattr(user, 'email', '')]
    candidates.append(default)
    candidates = [c for c in dict.fromkeys(c.strip() for c in candidates) if c]
    for upn in candidates:
        if graph.resolve_user_id(upn):
            return upn
    return ''


def create_class_meeting(*, host, title, start=None, end=None, module=None, group=None,
                         description='', record=True, organizer=None, require_link=False):
    """Create a live session on Microsoft Teams and return its :class:`MeetingRoom`.

    The meeting is owned by ``organizer`` when given (group calls pass the
    platform account), otherwise by the host's own tenant account or the
    default organiser (:func:`meeting_owner_for`).

    When Teams is off or Microsoft refuses, a scheduled session is still saved —
    as a Teams session with no link yet, which ``livesessions`` creates on the
    first join (and says so if it still cannot). With ``require_link`` (a call
    that starts *now*) nothing is saved and :class:`MeetingNotCreated` is raised
    instead, so the person can be told.

    The room does not require a login: students join anonymously via the link.
    """
    from apps.communication.models import MeetingRoom

    start = start or timezone.now()
    room = MeetingRoom(
        title=title or 'Live session',
        description=description or '',
        host=host,
        group=group,
        module=module,
        scheduled_start=start,
        scheduled_end=end,
        requires_login=False,
        provider=MeetingRoom.PROVIDER_TEAMS,
    )

    reason = ''
    if not teams_active():
        reason = 'Microsoft Teams is not set up on this platform yet.'
    else:
        owner = organizer or meeting_owner_for(host)
        if not owner:
            reason = 'No Microsoft account is available to own the meeting.'
            logger.warning('msteams: no organiser UPN for %s', host)
        else:
            created = graph.create_online_meeting(
                owner,
                subject=title or 'Live session',
                start_iso=_iso(start),
                end_iso=_iso(end or start + timezone.timedelta(hours=1)),
                allow_recording=record,
            )
            if created:
                room.organizer_upn = owner
                room.teams_meeting_id = created['id']
                room.teams_join_url = created['join_url']
            else:
                reason = 'Microsoft Teams did not accept the meeting.'
                logger.warning('msteams: Graph meeting creation failed for %r', title)

    if reason and require_link:
        raise MeetingNotCreated(reason)
    room.save()
    return room


def _iso(dt):
    """Graph wants an ISO-8601 timestamp with an offset."""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.isoformat()


def sync_meeting(meeting, *, use_claude=False, force=False, recap=False):
    """Pull a finished Teams meeting's artifacts and file them into the LMS.

    Fetches the transcript (→ recap), the recording URL and the attendance
    report; stores the recap as an :class:`AiReport` linked to the meeting, posts
    it to the class feed, and reconciles attendance. Idempotent — skips meetings
    already synced unless ``force``. Returns True if it did work.

    ``recap`` is off by default: :mod:`apps.livesessions` writes the session
    summary. Turning it on adds the extractive recap + feed post.

    Only this occurrence's transcripts are used, since a recurring class is one
    Teams meeting. The recording itself is found, filed and played by
    :mod:`apps.livesessions` (``pipeline.locate_teams_recording`` /
    ``session_video``); Graph's ``recordingContentUrl`` is deliberately NOT
    stored as a link: it needs the app's token, and a browser opening it sees
    only a JSON error.
    """
    from . import graph, recap as recap_mod

    if not meeting.is_teams or not meeting.teams_meeting_id or not meeting.organizer_upn:
        return False
    if meeting.artifacts_synced_at and not force:
        return False
    if not graph.is_configured():
        return False

    organizer, mid = meeting.organizer_upn, meeting.teams_meeting_id
    updated = []

    from apps.livesessions.summaries import for_session

    cues = []
    for tr in (for_session(graph.list_transcripts(organizer, mid), meeting) if recap else []):
        content = graph.get_transcript_content(organizer, mid, tr.get('id'))
        cues += recap_mod.parse_vtt(content)
    if cues:
        data = (recap_mod.claude_recap if use_claude else recap_mod.extractive_recap)(
            cues, title=meeting.title)
        _file_recap(meeting, data, recap_mod.to_markdown(data, title=meeting.title))

    _reconcile_attendance(meeting, organizer, mid, graph)

    meeting.artifacts_synced_at = timezone.now()
    updated.append('artifacts_synced_at')
    meeting.save(update_fields=updated)
    return True


def _file_recap(meeting, data, markdown):
    """Store the recap as an AiReport + post it to the module's class feed.

    The feed post is a Discussion on the module, so students see it.
    """
    try:
        from apps.ai_assistant.models import AiReport
        AiReport.objects.update_or_create(
            linked_ref=f'meeting:{meeting.pk}',
            defaults={
                'kind': AiReport.KIND_MEETING_RECAP,
                'title': f'Class recap — {meeting.title}',
                'module': meeting.module,
                'created_by': meeting.host,
                'content': markdown,
                'data': {k: data.get(k) for k in
                         ('key_points', 'action_items', 'decisions', 'questions', 'timeline', 'speakers')},
                'used_llm': data.get('engine') == 'claude',
                'status': AiReport.STATUS_FINAL,
            },
        )
    except Exception:  # pragma: no cover
        logger.exception('msteams: could not file recap')

    if meeting.module_id:
        try:
            from apps.communication.models import Discussion
            Discussion.objects.get_or_create(
                title=f'Class recap — {meeting.title}',
                module=meeting.module,
                defaults={'author': meeting.host, 'body': markdown[:4000],
                          'course': getattr(meeting.module, 'course', None)},
            )
        except Exception:  # pragma: no cover
            logger.exception('msteams: could not post recap to the feed')


def _parse_graph_dt(value):
    """Parse a Graph ISO-8601 timestamp into an aware datetime (None on junk).

    Graph returns ``…Z``, which :meth:`datetime.fromisoformat` only accepts
    from Python 3.11, so it is rewritten as ``+00:00``. Naive values are UTC
    (``django.utils.timezone.utc`` was removed in Django 5.0, hence the
    standard-library zone).
    """
    if not value:
        return None
    from datetime import datetime, timezone as dt_timezone
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _graph_attendance_rows(organizer, mid, graph):
    """Flatten every attendance report for a meeting into one entry per attendee.

    Teams files a **separate report per session** (restarts, recurrences), so
    seconds are summed and the earliest join kept.

    Returns ``[{'record', 'seconds', 'first_join'}]``; the raw Graph record is
    kept for identity resolution (:mod:`apps.livesessions.identity`). One
    attendee may be reported under several spellings across reports; the
    strongest identity key they offer is what merges them.
    """
    from apps.livesessions import identity as ident

    merged = {}
    for report in graph.list_attendance_reports(organizer, mid):
        for record in graph.get_attendance_records(organizer, mid, report.get('id')):
            seconds = int(record.get('totalAttendanceInSeconds') or 0)
            if seconds <= 0:
                continue
            first_join = None
            for interval in (record.get('attendanceIntervals') or []):
                joined = _parse_graph_dt(interval.get('joinDateTime'))
                if joined and (first_join is None or joined < first_join):
                    first_join = joined

            keys = ident.keys_for_record(record)
            key = keys[0] if keys else ident.label_for_record(record).casefold()

            bucket = merged.setdefault(key, {'record': record, 'seconds': 0, 'first_join': None})
            bucket['seconds'] += seconds
            if first_join and (bucket['first_join'] is None or first_join < bucket['first_join']):
                bucket['first_join'] = first_join
    return list(merged.values())


def _reconcile_attendance(meeting, organizer, mid, graph):
    """Reconcile the class register against the Graph attendance report.

    Graph is **authoritative for time**: reported seconds replace the heartbeat's,
    and status is re-derived against ``min_attendance_pct``.

    Graph is **not** authoritative for identity (anonymous joiners type their
    own name); :mod:`apps.livesessions.identity` resolves it against a bounded
    roster, refusing anything ambiguous. Unresolved attendees are left for an
    educator. Manual educator marks are never overwritten.
    """
    session = getattr(meeting, 'class_session', None)
    if session is None:
        return 0

    from apps.communication import services as comm
    from apps.communication.models import Attendance
    from apps.livesessions import identity as ident
    from apps.livesessions.models import SessionJoin

    rows = _graph_attendance_rows(organizer, mid, graph)
    if not rows:
        return 0

    index, ambiguous = ident.build_index(meeting)
    joins = {j.user_id: j for j in SessionJoin.objects.filter(meeting=meeting)}

    applied, unresolved = 0, []
    for row in rows:
        user, key = ident.resolve(row['record'], index)
        if user is None:
            unresolved.append({
                'label': ident.label_for_record(row['record']),
                'seconds': row['seconds'],
                'keys': ident.keys_for_record(row['record']),
            })
            continue

        comm.apply_measured_attendance(
            session, user, seconds=row['seconds'], first_seen=row['first_join'],
            source=Attendance.SOURCE_TEAMS, authoritative=True)
        applied += 1

        join = joins.get(user.pk)
        if join is not None:
            join.matched_identity = key
            join.matched_seconds = row['seconds']
            join.save(update_fields=['matched_identity', 'matched_seconds'])

    _store_unresolved(meeting, unresolved)

    if ambiguous:
        logger.info('msteams: %s — %s name(s) were ambiguous within the roster and left unmatched',
                    meeting, len(ambiguous))
    logger.info('msteams: reconciled attendance for %s — %s of %s reported attendees matched',
                meeting, applied, len(rows))
    return applied


def _store_unresolved(meeting, unresolved):
    """Park the attendees we could not name on the pipeline row, for the educator.

    An expected outcome of anonymous joining, not an error. Failures are only
    logged: bookkeeping must never break the sync.
    """
    try:
        from apps.livesessions.models import SessionArtifact
        artifact, _ = SessionArtifact.objects.get_or_create(meeting=meeting)
        artifact.unresolved_attendees = unresolved
        artifact.save(update_fields=['unresolved_attendees', 'updated_at'])
    except Exception:   # pragma: no cover
        logger.exception('msteams: could not store the unresolved attendees for %s', meeting)
