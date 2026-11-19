"""Video calls in a group — Microsoft Teams, owned by the platform account.

A group (a custom group chat, a module group, or the chat of a study group) can
start a video call. Every call is its own Teams meeting, created by the
platform's authorised Microsoft account (``MS_GRAPH_DEFAULT_ORGANIZER``) through
:func:`apps.msteams.services.create_class_meeting`, with the lobby bypassed for
everyone so members can go in before whoever started it. Members join through
the LMS hand-off (``livesessions:session-join``), which records who went in.

There are no calls in one-on-one chats, and there are no audio-only calls: the
call button is a video call, in groups only. When Microsoft cannot create the
meeting the person is told so — there is no other video engine to fall back to.

While a call is running (its hour has not passed) a second "Start call" joins
it rather than minting another meeting, so five people pressing the button at
once end up in one room.

``CALL_LENGTH`` is how long a group call is held open for.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('apps')

CALL_LENGTH = timedelta(hours=1)


def can_call(group):
    """Video calls are for groups — never 1:1 chats or Message Metanoia."""
    from .models import ChatGroup
    return bool(group and group.is_active
                and group.kind in (ChatGroup.KIND_CUSTOM, ChatGroup.KIND_SUBJECT))


def live_call(group, now=None):
    """The call running in ``group`` right now, if any."""
    from .models import MeetingRoom

    if not can_call(group):
        return None
    now = now or timezone.now()
    return (MeetingRoom.objects
            .filter(group=group, is_active=True, provider=MeetingRoom.PROVIDER_TEAMS,
                    scheduled_start__lte=now, scheduled_end__gt=now)
            .exclude(teams_join_url='')
            .order_by('-scheduled_start').first())


def add_participant(meeting, user, role='attendee'):
    """Add ``user`` to ``meeting`` (RSVP yes) unless they are already on it."""
    from .models import MeetingParticipant
    MeetingParticipant.objects.get_or_create(meeting=meeting, user=user,
                                             defaults={'role': role, 'rsvp': 'yes'})


def join_path(group, meeting):
    """The LMS path that joins ``group``'s call ``meeting``."""
    return reverse('communication:chat-call-join', args=[group.pk, meeting.pk])


def start_call(group, user):
    """``(meeting, created)`` — the group's running call, or a new one.

    Raises :class:`apps.msteams.services.MeetingNotCreated` when Teams is not
    available or Microsoft refuses the meeting.

    A new call is announced in the group itself, with the way in: the link turns into a
    session card for every member (:mod:`apps.communication.links`).
    """
    from apps.msteams import services as teams
    from .models import MeetingParticipant, MeetingRoom, Message

    running = live_call(group)
    if running is not None:
        add_participant(running, user)
        return running, False

    now = timezone.now()
    meeting = teams.create_class_meeting(
        host=user,
        title=f'{group.display_name} — video call'[:200],
        description=f'Video call in {group.display_name}.',
        start=now, end=now + CALL_LENGTH, group=group,
        organizer=getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or None,
        record=False, require_link=True,
    )
    meeting.audience = MeetingRoom.AUDIENCE_PRIVATE
    meeting.requires_login = True
    meeting.save(update_fields=['audience', 'requires_login', 'updated_at'])

    member_ids = list(group.memberships.values_list('user_id', flat=True))
    MeetingParticipant.objects.bulk_create(
        [MeetingParticipant(meeting=meeting, user_id=uid,
                            role='host' if uid == user.pk else 'attendee',
                            rsvp='yes' if uid == user.pk else 'pending')
         for uid in member_ids if uid],
        ignore_conflicts=True)
    add_participant(meeting, user, role='host')

    try:
        from core.utils import display_name
        link = (getattr(settings, 'SITE_URL', '') or '').rstrip('/') + join_path(group, meeting)
        message = Message.objects.create(
            group=group, sender=user,
            body=f'{display_name(user)} started a video call. Join here: {link}')
        from . import realtime
        realtime.broadcast_new_message(message)
    except Exception:
        logger.exception('group call: could not post the call message in %s', group)
    return meeting, True
