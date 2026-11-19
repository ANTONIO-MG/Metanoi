"""Bring classes scheduled in Teams onto the platform.

Classes scheduled straight in Outlook/Teams are otherwise unknown to the
platform, so nothing after the class could run. This reads the organiser's
calendar and creates a :class:`~apps.communication.models.MeetingRoom` for each
*ended* group class, placed from its subject line, which follows one convention::

    MP PGDA P26F | TAX Debrief Session      Milpark · PGDA · P26F · TAX
    MP PGDA B26S | CGAU                     Milpark · BCTA · B26S · CGAU   (B-intakes are BCTA)
    MP P26S Revision                        Milpark · PGDA · P26S          (the whole intake)

**One-on-one coaching** ("Tayisha Pillay | Individual Coaching", "Gugu - MACF
…") is imported as a *private* session — never placed on a module or intake —
and only when someone on the invite is a student with an account here. That
student becomes the session's only invitee, so its recording, transcript and
summary reach them and nobody else. Coaches' and staff accounts (and the
personal addresses listed in the live-session settings) are never taken for the
student. A one-on-one whose student has no account is left out.

**Anything that has not ended** is left out: an imported future session would
start sending the platform's own reminders on top of Teams' invitations.

Importing never creates, changes or cancels anything in Teams. Imported
sessions have ``publish_recording`` on, so the session pipeline files the
recording and transcript to OneDrive and publishes them to the class.
Re-running is safe: each occurrence is keyed on its calendar event id.
"""

import datetime as dt
import logging
import re
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from . import graph

logger = logging.getLogger('apps')

INSTITUTION_CODES = {'MP': 'MILPARK', 'MILPARK': 'MILPARK'}
MODULE_WORDS = {'CGAU': 'CGAU', 'AUD': 'CGAU', 'FREP': 'FREP', 'MACF': 'MACF', 'MAF': 'MACF',
                'TAX': 'TAX', 'TAXA': 'TAX'}
CATALOGUE = {'CGAU': 'GA', 'MACF': 'MA', 'FREP': 'FR', 'TAX': 'TX'}

_SUBJECT_RX = re.compile(
    r'^\s*(?P<inst>MP|MILPARK)\s+(?:(?P<prog>PGDA|BCTA)\s+)?(?P<cohort>[BP]\d\d[SF])\b\s*[|\-–]?\s*(?P<rest>.*)$', re.I)
_TRAILING_COHORT_RX = re.compile(r'^\s*(?P<prog>PGDA|BCTA)\b(?P<rest>.*?)[\s\-–|]+(?P<cohort>[BP]\d\d[SF])\s*$', re.I)


@dataclass
class Placement:
    """Where a Teams class belongs, as read from its subject line.

    ``module`` is ``''`` for a session for the whole intake; ``topic`` is what
    follows the module, e.g. "Debrief Session" or "Mock Test 2".
    """

    institution: str
    programme: str
    cohort: str
    module: str
    topic: str


def parse_subject(subject):
    """A Teams subject → :class:`Placement`, or ``None`` when it is not a group class.

    Besides the usual ``MP PGDA P26F | TAX …`` form, a subject may end with the
    intake ("PGDA Consult Exam Prep - P26F"). The programme is always decided
    by the intake letter: B-intakes are BCTA, P-intakes PGDA.
    """
    subject = (subject or '').strip()
    m = _SUBJECT_RX.match(subject)
    if m:
        inst, cohort, rest = m.group('inst').upper(), m.group('cohort').upper(), m.group('rest').strip(' |-–')
    else:
        m = _TRAILING_COHORT_RX.match(subject)
        if not m:
            return None
        inst, cohort, rest = 'MP', m.group('cohort').upper(), m.group('rest').strip(' |-–')
    programme = 'BCTA' if cohort.startswith('B') else 'PGDA'
    first, _, remainder = rest.partition(' ')
    module = MODULE_WORDS.get(first.upper().strip('|:-'), '')
    topic = remainder.strip() if module else rest
    return Placement(INSTITUTION_CODES[inst], programme, cohort, module, topic)


def session_kind(topic):
    """The :class:`~apps.communication.models.MeetingRoom` kind a class topic suggests."""
    from apps.communication.models import MeetingRoom as M
    t = (topic or '').lower()
    if 'mock' in t or 'debrief' in t or 'post mortem' in t:
        return M.KIND_TEST_REVIEW
    if 'revision' in t:
        return M.KIND_REVISION
    if 'seminar' in t or 'workshop' in t or 'planning' in t or 'consult' in t:
        return M.KIND_WORKSHOP
    return M.KIND_CLASS


@dataclass
class ImportReport:
    """Counts of what one :func:`import_sessions` run did and why entries were left out."""

    seen: int = 0
    created: int = 0
    updated: int = 0
    one_on_one: int = 0
    skipped_private: int = 0
    skipped_future: int = 0
    skipped_unplaced: int = 0
    skipped_cancelled: int = 0
    skipped_linked: int = 0

    def __str__(self):
        """A one-line summary for the command output and job log."""
        return (f'{self.seen} calendar entries: {self.created} imported, {self.updated} updated '
                f'({self.one_on_one} one-on-one), '
                f'{self.skipped_private} one-on-one/private left out, {self.skipped_future} not ended yet, '
                f'{self.skipped_unplaced} not on the platform, {self.skipped_cancelled} cancelled, '
                f'{self.skipped_linked} already scheduled on the platform')


def _parse_when(value):
    """Graph's ``{'dateTime': '2026-10-13T16:00:00.0000000', 'timeZone': 'UTC'}`` → aware datetime."""
    raw = (value or {}).get('dateTime', '')[:19]
    if not raw:
        return None
    when = dt.datetime.fromisoformat(raw)
    zone = (value or {}).get('timeZone') or 'UTC'
    try:
        from zoneinfo import ZoneInfo
        return when.replace(tzinfo=ZoneInfo('UTC' if zone.upper() == 'UTC' else zone))
    except Exception:
        return when.replace(tzinfo=dt.timezone.utc)


def _host_for(organizer_upn):
    """The platform user for the organiser's Microsoft UPN (or e-mail), or ``None``."""
    from django.contrib.auth import get_user_model
    from apps.accounts.models import Person
    person = Person.objects.filter(ms_upn__iexact=organizer_upn).select_related('user').first()
    if person is not None:
        return person.user
    return get_user_model().objects.filter(email__iexact=organizer_upn).first()


def import_sessions(*, days=14, organizer_upn=None, now=None, dry_run=False):
    """Import ended group classes from the last ``days`` days. Returns an :class:`ImportReport`.

    An entry already covered by a session the platform scheduled (or linked to
    this Teams meeting) is skipped: importing it again would duplicate it, and
    a class subject the parser does not know, with students on the invite,
    would come in as somebody's private one-on-one.
    """
    from apps.communication.models import MeetingRoom
    from apps.learning.models import Cohort, ProgrammeModule

    report = ImportReport()
    if not graph.is_configured():
        return report
    organizer = (organizer_upn or getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip()
    if not organizer:
        return report
    now = now or timezone.now()
    start = (now - dt.timedelta(days=days)).astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    end = now.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    host = _host_for(organizer)
    meeting_ids = {}

    coaches = _coach_emails(organizer, host)
    select = 'id,subject,start,end,isOnlineMeeting,onlineMeeting,isCancelled,type,seriesMasterId,attendees'
    for event in graph.calendar_view(organizer, start, end, select=select):
        report.seen += 1
        if event.get('isCancelled'):
            report.skipped_cancelled += 1
            continue
        if not (event.get('isOnlineMeeting') and (event.get('onlineMeeting') or {}).get('joinUrl')):
            report.skipped_unplaced += 1
            continue
        placement = parse_subject(event.get('subject'))
        starts, ends = _parse_when(event.get('start')), _parse_when(event.get('end'))
        if _already_on_platform(event, starts, ends):
            report.skipped_linked += 1
            continue
        if placement is None:
            students = _one_on_one_students(event, coaches)
            if not students:
                report.skipped_private += 1
                continue
            if not ends or ends > now:
                report.skipped_future += 1
                continue
            if dry_run:
                report.one_on_one += 1
                continue
            _import_one_on_one(event, students, organizer, host, starts, ends, meeting_ids, report)
            continue
        if not ends or ends > now:
            report.skipped_future += 1
            continue

        cohort = Cohort.objects.filter(programme__institution__code=placement.institution,
                                       programme__code=placement.programme,
                                       code=placement.cohort).select_related('programme').first()
        if cohort is None:
            report.skipped_unplaced += 1
            continue
        offering = None
        if placement.module:
            offering = ProgrammeModule.objects.filter(
                programme=cohort.programme, module__code=CATALOGUE[placement.module]).first()

        join_url = event['onlineMeeting']['joinUrl']
        if join_url not in meeting_ids:
            meeting = graph.find_online_meeting(organizer, join_url)
            meeting_ids[join_url] = (meeting or {}).get('id', '')

        room = MeetingRoom.objects.filter(external_event_id=event['id']).first()
        created = room is None
        if dry_run:
            report.created += created
            report.updated += not created
            continue
        if created:
            room = MeetingRoom(external_event_id=event['id'], host=host,
                               publish_recording=True, record_automatically=True)
        room.title = (event.get('subject') or '').strip()[:200]
        room.provider = MeetingRoom.PROVIDER_TEAMS
        room.teams_join_url = join_url[:1000]
        room.teams_meeting_id = meeting_ids[join_url][:512]
        room.organizer_upn = organizer
        room.scheduled_start, room.scheduled_end = starts, ends
        room.institution = cohort.programme.institution
        room.programme = cohort.programme
        room.cohort = cohort
        room.module = offering
        room.audience = MeetingRoom.AUDIENCE_MODULE if offering else MeetingRoom.AUDIENCE_COHORT
        room.session_kind = session_kind(placement.topic)
        room.week = _week_for(offering, cohort, starts) if offering else None
        room.save()
        if created:
            report.created += 1
        else:
            report.updated += 1
    logger.info('teams calendar import: %s', report)
    return report


def _already_on_platform(event, starts, ends):
    """True when a platform session already uses this event's Teams meeting
    inside the event's own time slot (or is keyed on the event itself)."""
    from apps.communication.models import MeetingRoom
    if not (starts and ends):
        return False
    join_url = ((event.get('onlineMeeting') or {}).get('joinUrl') or '')[:1000]
    rooms = MeetingRoom.objects.filter(scheduled_start__lt=ends, scheduled_start__gte=starts - dt.timedelta(minutes=15))
    linked = rooms.filter(teams_join_url=join_url) if join_url else rooms.none()
    by_event = MeetingRoom.objects.filter(teams_calendar_event_id=event.get('id') or '-')
    return (linked.exclude(external_event_id=event.get('id')).exists()
            or by_event.exists())


def _coach_emails(organizer, host):
    """Addresses that are never the student on a one-on-one."""
    from apps.livesessions.models import LiveSessionSettings
    emails = set(LiveSessionSettings.load().coach_email_set)
    emails.add((organizer or '').lower())
    if host is not None and host.email:
        emails.add(host.email.lower())
    return emails


def _one_on_one_students(event, coaches):
    """The invitees of a non-class event who are students with an account here."""
    from django.contrib.auth import get_user_model
    addresses = {((a.get('emailAddress') or {}).get('address') or '').strip().lower()
                 for a in event.get('attendees') or []}
    addresses -= coaches
    addresses.discard('')
    if not addresses or not (event.get('onlineMeeting') or {}).get('joinUrl'):
        return []
    found = []
    for user in (get_user_model().objects
                 .filter(is_active=True, is_superuser=False, is_staff=False,
                         profile__user_type='student')
                 .select_related('profile')):
        if (user.email or '').lower() in addresses:
            found.append(user)
    return found


def _import_one_on_one(event, students, organizer, host, starts, ends, meeting_ids, report):
    """Create or refresh a private session for a one-on-one, invitees = the student(s)."""
    from apps.communication.models import MeetingParticipant, MeetingRoom

    join_url = event['onlineMeeting']['joinUrl']
    if join_url not in meeting_ids:
        found = graph.find_online_meeting(organizer, join_url)
        meeting_ids[join_url] = (found or {}).get('id', '')

    room = MeetingRoom.objects.filter(external_event_id=event['id']).first()
    created = room is None
    if created:
        room = MeetingRoom(external_event_id=event['id'], host=host,
                           publish_recording=True, record_automatically=True)
    room.title = (event.get('subject') or '').strip()[:200]
    room.provider = MeetingRoom.PROVIDER_TEAMS
    room.teams_join_url = join_url[:1000]
    room.teams_meeting_id = meeting_ids[join_url][:512]
    room.organizer_upn = organizer
    room.scheduled_start, room.scheduled_end = starts, ends
    room.audience = MeetingRoom.AUDIENCE_PRIVATE
    room.institution = room.programme = room.cohort = room.module = room.week = None
    room.session_kind = MeetingRoom.KIND_CONSULT
    room.save()
    for user in students:
        MeetingParticipant.objects.get_or_create(meeting=room, user=user, defaults={'role': 'attendee'})
    report.one_on_one += 1
    if created:
        report.created += 1
    else:
        report.updated += 1
    return room


def _week_for(offering, cohort, when):
    """The intake's week on this module that the class fell in, if the schedule has one."""
    from django.db.models import Q
    from apps.learning.models import ModuleWeek
    day = timezone.localtime(when).date()
    return (ModuleWeek.objects
            .filter(phase__programme_module=offering, starts_on__lte=day, ends_on__gte=day)
            .filter(Q(phase__cohort=cohort) | Q(phase__cohort__isnull=True))
            .order_by('phase__cohort', 'number').first())
