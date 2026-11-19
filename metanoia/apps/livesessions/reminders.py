"""Session reminders — 24 hours before, and 30 minutes before.

Run every few minutes by the ``session-reminders`` job.

* **The ledger, not the clock.** Whether a reminder was sent is a
  :class:`~apps.livesessions.models.SessionReminder` row, not inferred from
  timestamps — the job ticks far more often than the leads, and must stay
  correct when it catches up late.
* **Everybody's own clock.** Each reminder is rendered in the recipient's time
  zone, with the zone named.
"""

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger('apps')


def run(now=None):
    """Send every reminder that is due. Returns a short summary string.

    A lead's window opens at the lead time and never closes before the session
    starts, so a job that missed its slot still delivers a late-but-useful
    "starts in 10 minutes".
    """
    from apps.communication.models import MeetingRoom
    from .models import LiveSessionSettings

    now = now or timezone.now()
    conf = LiveSessionSettings.load()
    leads = conf.lead_minutes
    if not leads:
        return 'no reminder leads configured'

    horizon = now + timedelta(minutes=max(leads))
    upcoming = (MeetingRoom.objects
                .filter(is_active=True, scheduled_start__gt=now, scheduled_start__lte=horizon)
                .select_related('module', 'programme', 'institution', 'cohort', 'host'))

    sent = 0
    for meeting in upcoming.iterator():
        for lead in leads:
            if meeting.scheduled_start - now <= timedelta(minutes=lead):
                sent += send_for(meeting, lead, conf=conf, now=now)
    return f'{sent} session reminder(s) sent'


def send_for(meeting, lead_minutes, *, conf=None, now=None):
    """Remind everyone in ``meeting``'s audience who is not already on the ledger.

    When a person has muted meeting notifications the ledger row is still
    written; otherwise every tick would re-ask and re-suppress forever.
    """
    from apps.communication import services as comm
    from .audience import recipients_for
    from .models import LiveSessionSettings, SessionReminder

    conf = conf or LiveSessionSettings.load()
    now = now or timezone.now()

    already = set(SessionReminder.objects
                  .filter(meeting=meeting, lead_minutes=lead_minutes)
                  .values_list('recipient_id', flat=True))
    people = recipients_for(meeting).exclude(pk__in=already)

    url = meeting.get_absolute_url()
    headline = _headline(lead_minutes)
    sent = 0
    for user in people.iterator():
        when = local_when(meeting.scheduled_start, user)
        note = comm.notify(
            user, category='meetings', level='warning',
            verb='session reminder',
            title=f'{headline}: {meeting.title}',
            body=(f'“{meeting.title}” starts at {when}. '
                  f'{meeting.audience_label}. Join from the session page.'),
            url=url, meeting=meeting, email=bool(conf.reminder_email), email_kind='reminders',
        )
        if note is None:
            SessionReminder.objects.get_or_create(
                meeting=meeting, recipient=user, lead_minutes=lead_minutes)
            continue
        SessionReminder.objects.get_or_create(
            meeting=meeting, recipient=user, lead_minutes=lead_minutes,
            defaults={'emailed': note.emailed})
        sent += 1
    return sent


def announce_new_session(meeting, *, email=True, automated=False):
    """Tell the audience a session has been scheduled. Returns how many were told.

    Called at creation (``automated``: e-mailed only to those who opted into
    reminder e-mails) and on a manager's re-share (e-mailed as they chose).
    """
    from apps.communication import services as comm
    from apps.communication.emails import send_branded_email
    from apps.communication.models import NotificationPreference
    from .audience import recipients_for

    url = meeting.get_absolute_url()
    told = 0
    for user in recipients_for(meeting).iterator():
        when = local_when(meeting.scheduled_start, user)
        note = comm.notify(
            user, category='meetings', level='info', verb='session scheduled',
            title=f'New session: {meeting.title}',
            body=f'{meeting.get_session_kind_display()} on {when}. {meeting.audience_label}.',
            url=url, meeting=meeting, email=False,
        )
        if note is None:
            continue
        told += 1
        if email and (not automated or NotificationPreference.for_user(user).allows_automated_email(
                NotificationPreference.EMAIL_REMINDERS, 'meetings')):
            _email_invite(meeting, user, when, send_branded_email)
    return told


def announce_session_update(meeting, *, changed=(), by=None):
    """Tell the audience that a session they were told about has changed.

    A change is a person acting (``by``), so the e-mail copy follows each
    recipient's ordinary e-mail preference, not the automated-reminder opt-in.
    The person who made the change is not told — they already know. Returns
    how many people were told.
    """
    from apps.communication import services as comm
    from .audience import recipients_for

    changed = set(changed or ())
    moved = bool(changed & {'scheduled_start', 'scheduled_end'})
    url = meeting.get_absolute_url()
    told = 0
    for user in recipients_for(meeting).iterator():
        if by is not None and user.pk == by.pk:
            continue
        when = local_when(meeting.scheduled_start, user)
        if moved:
            title = f'Session moved: {meeting.title}'
            body = f'“{meeting.title}” is now on {when}. {meeting.audience_label}.'
        else:
            title = f'Session updated: {meeting.title}'
            body = f'The details of “{meeting.title}” ({when}) have changed. {meeting.audience_label}.'
        body += ' Join from the session page when it starts.'
        note = comm.notify(
            user, actor=by, category='meetings', level='info', verb='session updated',
            title=title, body=body, url=url, meeting=meeting, email=True,
            email_kind=None if by is not None else 'reminders',
        )
        if note is not None:
            told += 1
    return told


def _email_invite(meeting, user, when, send_branded_email):
    """The branded "you have a session" e-mail, in the recipient's own clock."""
    from django.conf import settings as django_settings

    site = (getattr(django_settings, 'SITE_URL', '') or '').rstrip('/')
    try:
        send_branded_email(
            f'Live session scheduled — {meeting.title}',
            user.email,
            'session-invite',
            {
                'recipient': user,
                'meeting': meeting,
                'when_local': when,
                'timezone_label': zone_label(user),
                'session_url': f'{site}{meeting.get_absolute_url()}' if site else meeting.get_absolute_url(),
            'join_url': f'{site}{meeting.get_join_url()}' if site else meeting.get_join_url(),
                'audience_label': meeting.audience_label,
                'duration_minutes': meeting.duration_minutes,
            },
        )
    except Exception:       # pragma: no cover - e-mail must never break the loop
        logger.exception('livesessions: could not e-mail the invite to %s', user)


def zone_for(user):
    """The IANA zone ``user`` reads times in — their setting, else the platform's."""
    from django.conf import settings as django_settings
    name = ''
    try:
        row = getattr(user, 'account_settings', None)
        name = (getattr(row, 'timezone', '') or '').strip()
    except Exception:
        name = ''
    if not name:
        return None, django_settings.TIME_ZONE
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        return None, django_settings.TIME_ZONE


def zone_label(user):
    """The zone name to print next to a time, e.g. ``Africa/Lagos``."""
    return zone_for(user)[1]


def local_when(moment, user, fmt='%A %d %B, %H:%M'):
    """``moment`` written in ``user``'s own time zone, with the zone named.

    The abbreviation is included because "14:00" alone is exactly the ambiguity
    this whole function exists to remove.
    """
    if moment is None:
        return 'a time still to be confirmed'
    tzinfo, name = zone_for(user)
    local = timezone.localtime(moment, tzinfo) if tzinfo else timezone.localtime(moment)
    abbrev = local.strftime('%Z') or name
    return f'{local.strftime(fmt)} ({abbrev})'


def _headline(lead_minutes):
    """The reminder title prefix for a lead time, e.g. ``Session in 1 day`` or ``Session in 30 minutes``."""
    if lead_minutes >= 1440:
        days = lead_minutes // 1440
        return f'Session in {days} day' + ('s' if days > 1 else '')
    if lead_minutes >= 60:
        hours = lead_minutes // 60
        return f'Session in {hours} hour' + ('s' if hours > 1 else '')
    return f'Session in {lead_minutes} minutes'
