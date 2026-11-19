"""Communication counters for every template: unread notifications and chat
messages, the announcement count, and ``alerts`` (the navbar bell's merged list).

A DB error degrades to ``{}``. The bell's list merges notifications and
messages, but its badge (``alerts_total``) counts notifications only — see
:func:`_build_cached`. Cached per request and for
:data:`core.counters.COUNTER_TTL` seconds per user; ``comm_stats`` is lazy.
"""

from core.counters import LazyStats, cached_counter, memo
from core.request_cache import cached_per_request

from . import models


def _build(user):
    """The counters, in a handful of queries flat (uncached; see comm_stats)."""
    data = _build_cached(user)
    data['announcements'] = _announcement_count()
    return data


def _announcement_count():
    """Total number of announcements (0 when the model is unavailable)."""
    if hasattr(models, 'Announcement'):
        return models.Announcement.objects.count()
    return 0


def _build_cached(user):
    """The per-user counters the layouts draw on every page — cached together.

    Unread messages are one aggregate over every conversation, not one COUNT per
    membership. The bell's *list* merges notifications and messages (see
    :func:`_inbox`), but its *badge* (``alerts_total``) counts notifications only — so
    opening one always visibly clears the bell. Counting messages too made the bell
    impossible to clear, and messages already have their own badge beside it.
    """
    unread_notifications = models.Notification.objects.filter(
        recipient=user, is_read=False).count()

    unread_messages = models.ChatMembership.unread_total_for(user)


    return {
        'unread_notifications': unread_notifications,
        'unread_messages': unread_messages,
        'alerts': _inbox(user),
        'alerts_total': unread_notifications,
    }


def _display_name(user, fallback='Someone'):
    """A person's name for the bell — profile name first, never their address."""
    if user is None:
        return fallback
    profile = getattr(user, 'profile', None)
    parts = [getattr(profile, 'first_name', '') or '', getattr(profile, 'last_name', '') or '']
    name = ' '.join(p for p in parts if p).strip()
    if not name:
        name = (user.get_full_name() or '').strip()
    return name if (name and '@' not in name) else fallback


def _bell_message_qs(user):
    """Unread messages that belong in the navbar bell.

    Every message in a 1:1 ``direct`` chat, and in group chats only those that
    ``@``-mention the user, so busy groups don't bury the notifications.

    "Unread" is judged against the conversation's ``last_read_at`` watermark, the
    reader's own messages are excluded, and ``distinct()`` collapses the row
    fan-out the mention join would otherwise cause.
    """
    from django.db.models import F, Q

    unseen = Q(group__memberships__last_read_at__isnull=True) | Q(
        created_at__gt=F('group__memberships__last_read_at'))
    aimed_at_me = Q(group__kind=models.ChatGroup.KIND_DIRECT) | Q(mention_links__user=user)
    return (models.Message.objects
            .filter(group__memberships__user=user, is_deleted=False)
            .exclude(sender=user)
            .filter(unseen)
            .filter(aimed_at_me)
            .distinct())


def _inbox(user, limit=8):
    """The navbar bell's list: notifications **and** messages aimed at you, merged.

    Newest first; each row keeps its own icon. Messages are limited as in
    :func:`_bell_message_qs`.

    Only unread notifications are listed: opening one (``notification-detail`` marks it
    read) drops it off the bell, so the bell shows only what still needs attention. Each
    links to the notification in full rather than to a deep link that may resolve to the
    page you are already on. Message senders are named by :func:`_display_name`, never
    by ``get_username()``: on this deployment that *is* the e-mail address, so the bell
    would list people by their address.
    """
    from django.urls import reverse

    rows = []

    for note in (models.Notification.objects
                 .filter(recipient=user, is_read=False)
                 .order_by('-created_at')[:limit]):
        is_mail = (note.url or '').startswith('/mail/')
        rows.append({
            'kind': 'mail' if is_mail else 'notification',
            'icon': 'bi-envelope-fill' if is_mail else 'bi-bell-fill',
            'title': note.title or note.verb or 'Notification',
            'body': (note.summary or note.body or '')[:120],
            'url': reverse('communication:notification-detail', args=[note.pk])
                   + ('?go=1' if (note.url or '').startswith('/mail/') else ''),
            'when': note.created_at,
            'unread': True,
        })

    for message in (_bell_message_qs(user)
                    .select_related('group', 'sender', 'sender__profile')
                    .order_by('-created_at')[:limit]):
        who = _display_name(message.sender)
        rows.append({
            'kind': 'message',
            'icon': 'bi-chat-left-text-fill',
            'title': who,
            'body': (message.body or 'Sent an attachment')[:120],
            'url': reverse('communication:chat-room', args=[message.group_id]),
            'when': message.created_at,
            'unread': True,
        })

    rows.sort(key=lambda r: r['when'], reverse=True)
    return rows[:limit]


def comm_stats(request):
    """Context processor: ``comm_stats`` — the lazy communication counters for templates.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'comm_stats': {}}

    def build():
        """Build the :class:`LazyStats` whose loaders read the cached per-user counters.
        """
        counters = memo(lambda: cached_counter(user, 'comm', lambda: _build_cached(user)))
        loaders = {key: (lambda key=key: counters()[key])
                   for key in ('unread_notifications', 'unread_messages', 'alerts', 'alerts_total')}
        loaders['announcements'] = memo(_announcement_count)
        return LazyStats(loaders)

    return {'comm_stats': cached_per_request(request, 'comm_stats', build)}
