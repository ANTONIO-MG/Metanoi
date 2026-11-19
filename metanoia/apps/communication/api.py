"""REST API for the communication app (mounted at ``/api/communication/``).

``/api/`` is exempt from the site's login / management / parent middlewares
(DRF authenticates itself), so every rule the HTML pages apply is repeated here:

* **Chats** — you see a conversation only if you are a member of it, exactly
  like ``views.chat_room``; Django staff/superusers may open any (the web view
  allows that too). A parent sees direct threads only. Renaming, archiving or
  deleting a conversation is for its ``owner``/``admin`` members (custom groups
  only — module groups and 1:1 threads are system-managed) or Django staff.
* **Messages** — only in conversations you are a member of; edit/delete your own
  (staff: any). A reply may only quote a message from the same conversation and
  ``@mention`` only its members, so neither can be used to read or ping across
  conversations. Attached files go through ``core.validators.validate_attachment``
  (see the serializer).
* **Meetings** — the sessions ``livesessions.audience.visible_sessions`` lets you
  see on the calendar, plus calls in conversations you belong to (the chat page
  rings for those). Editing, cancelling and inviting are for the host or whoever
  ``audience.can_manage`` allows (admin/staff, an educator for their modules).
"""

from django.db.models import Q
from django.utils import timezone
from rest_framework import decorators, permissions, response, status, viewsets
from rest_framework.exceptions import PermissionDenied

from core.errors import note
from core.scoping import PARENT_CHAT_KINDS

from . import models, serializers, services


def _is_member(user, group):
    """True when ``user`` is a member of chat ``group``."""
    return group.memberships.filter(user=user).exists()


def _sees_every_chat(user):
    """Django staff/superusers — the same test ``views.chat_room`` applies."""
    return bool(user.is_staff or user.is_superuser)


def _is_parent(user):
    """True when ``user`` is a parent account (see :func:`core.scoping.is_parent`)."""
    from core.scoping import is_parent
    return is_parent(user)


def _can_manage_group(user, group):
    """Rename / archive / delete a conversation.

    Staff may manage any. Otherwise only a custom group's ``owner``/``admin``
    member — module groups track enrolment and 1:1 threads belong to both
    people, so neither is anyone's to rename or delete.
    """
    if _sees_every_chat(user):
        return True
    if group.kind != models.ChatGroup.KIND_CUSTOM:
        return False
    return group.memberships.filter(user=user, role__in=('owner', 'admin')).exists()


class ChatGroupViewSet(viewsets.ModelViewSet):
    """Conversations the user belongs to: list, create custom groups,
    rename/archive/delete.

    ``filterset_fields`` uses the current field names only: ``course`` was removed from
    :class:`ChatGroup` and ``module`` is now ``programme_module`` — listing the old
    names made django-filter raise on every request (500).
    """
    serializer_class = serializers.ChatGroupSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['kind', 'is_active', 'programme_module']
    search_fields = ['name', 'description']
    ordering_fields = ['updated_at', 'created_at']

    def get_queryset(self):
        """The user's own conversations (all of them for staff); parents see direct threads
        only, the same rule as the chat page, which keeps them out of group rooms.
        """
        qs = models.ChatGroup.objects.all().prefetch_related('memberships')
        user = self.request.user
        if _sees_every_chat(user):
            return qs
        qs = qs.filter(memberships__user=user)
        if _is_parent(user):
            qs = qs.filter(kind__in=PARENT_CHAT_KINDS)
        return qs.distinct()

    def perform_create(self, serializer):
        """Create a custom (manual) group — course/module groups are auto-managed.

        Parents use direct messages and community members talk to the team through
        Message Metanoia, so neither may create groups.
        """
        if _is_parent(self.request.user) and not _sees_every_chat(self.request.user):
            raise PermissionDenied('Parent accounts use direct messages rather than group chats.')
        from core.roles import role_of_user
        if role_of_user(self.request.user) == 'community':
            raise PermissionDenied('Community members talk to the Metanoia team through Message Metanoia.')
        group = serializer.save(kind=models.ChatGroup.KIND_CUSTOM, created_by=self.request.user)
        group.add_member(self.request.user, role='owner')

    def perform_update(self, serializer):
        """Rename/edit a group; owner or admin members only (logged as ``CHAT-2001``).
        """
        if not _can_manage_group(self.request.user, serializer.instance):
            note('CHAT-2001', self.request, group=serializer.instance.pk, action='edit-group')
            raise PermissionDenied('Only the owner or an admin of this group can change it.')
        serializer.save()

    def perform_destroy(self, instance):
        """Delete a group; owner or admin members only (logged as ``CHAT-2001``)."""
        if not _can_manage_group(self.request.user, instance):
            note('CHAT-2001', self.request, group=instance.pk, action='delete-group')
            raise PermissionDenied('Only the owner or an admin of this group can delete it.')
        instance.delete()

    @decorators.action(detail=True, methods=['get'])
    def members(self, request, pk=None):
        """List the group's memberships."""
        group = self.get_object()
        data = serializers.ChatMembershipSerializer(
            group.memberships.select_related('user'), many=True, context={'request': request}
        ).data
        return response.Response(data)

    @decorators.action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark the conversation read for the requesting member (403 for non-members).
        """
        group = self.get_object()
        membership = group.memberships.filter(user=request.user).first()
        if not membership:
            return response.Response(status=status.HTTP_403_FORBIDDEN)
        membership.last_read_at = timezone.now()
        membership.save(update_fields=['last_read_at'])
        return response.Response({'status': 'ok', 'last_read_at': membership.last_read_at})


class MessageViewSet(viewsets.ModelViewSet):
    """Messages in the user's conversations: list, send, edit and soft-delete."""
    serializer_class = serializers.MessageSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['group', 'sender']
    ordering_fields = ['created_at']

    def get_queryset(self):
        """Live (not deleted) messages, limited to the user's conversations unless staff.
        """
        qs = (models.Message.objects.filter(is_deleted=False)
              .select_related('sender', 'sender__profile', 'group', 'reply_to', 'reply_to__sender')
              .prefetch_related('attachments', 'mention_links__user'))
        user = self.request.user
        if not _sees_every_chat(user):
            qs = qs.filter(group__memberships__user=user)
            if _is_parent(user):
                qs = qs.filter(group__kind__in=PARENT_CHAT_KINDS)
        return qs.distinct()


    def _save_extras(self, message, serializer):
        """Attach uploaded files and explicit mentions, then fan out notifications."""
        files = serializer.validated_data.get('_files') or []
        for f in files:
            att = models.MessageAttachment(message=message, file=f, original_name=getattr(f, 'name', ''))
            att.save()
        for user in serializer.validated_data.get('_mention_ids') or []:
            models.MessageMention.objects.get_or_create(message=message, user=user)
        services.create_mention_notifications(message)

    def perform_create(self, serializer):
        """Send a message after the membership, parent, connection and moderation checks.

        Student ↔ student direct chats require an accepted connection first, and muted
        or chat-suspended users (moderation penalties) are blocked from sending. After
        saving, the real-time fan-out pokes the open conversation and pings every other
        member's bell (sound/badge); it is best-effort, polling covers the rest.
        """
        group = serializer.validated_data['group']
        if not (_sees_every_chat(self.request.user) or _is_member(self.request.user, group)):
            note('CHAT-2001', self.request, group=group.pk)
            raise PermissionDenied('You are not a member of this chat group. (CHAT-2001)')
        if (_is_parent(self.request.user) and group.kind not in PARENT_CHAT_KINDS
                and not _sees_every_chat(self.request.user)):
            note('CHAT-2001', self.request, group=group.pk, reason='parent-group')
            raise PermissionDenied('Parent accounts use direct messages rather than group chats.')
        if group.kind == models.ChatGroup.KIND_DIRECT:
            other = services.other_member(group, self.request.user)
            if other is not None and not services.can_direct_message(self.request.user, other):
                note('CHAT-2002', self.request, group=group.pk, other=other.pk)
                raise PermissionDenied(
                    'You can only message this person once they accept your '
                    'connection request. (CHAT-2002)')
        penalty = services.active_silencing_penalty(self.request.user)
        if penalty is not None:
            until = f' until {penalty.ends_at:%Y-%m-%d %H:%M}' if penalty.ends_at else ''
            note('CHAT-2003', self.request, penalty=penalty.pk, kind=penalty.kind)
            raise PermissionDenied(
                f'You are {penalty.get_kind_display().lower()}{until} and cannot '
                f'send messages. (CHAT-2003)')
        message = serializer.save(sender=self.request.user)
        self._save_extras(message, serializer)
        group.save(update_fields=['updated_at'])
        try:
            from .realtime import broadcast_new_message
            broadcast_new_message(message)
        except Exception:
            pass

    def perform_update(self, serializer):
        """Edit a message; only its sender (or staff) may (logged as ``CHAT-2004``)."""
        message = self.get_object()
        if message.sender_id != self.request.user.id and not _sees_every_chat(self.request.user):
            note('CHAT-2004', self.request, message=message.pk, action='edit')
            raise PermissionDenied('You can only edit your own messages. (CHAT-2004)')
        message = serializer.save()
        message.mark_edited()
        message.save(update_fields=['is_edited', 'edited_at'])
        self._save_extras(message, serializer)

    def perform_destroy(self, instance):
        """Soft-delete a message (blank body, ``is_deleted``); sender or staff only."""
        if instance.sender_id != self.request.user.id and not _sees_every_chat(self.request.user):
            note('CHAT-2004', self.request, message=instance.pk, action='delete')
            raise PermissionDenied('You can only delete your own messages. (CHAT-2004)')
        instance.is_deleted = True
        instance.body = ''
        instance.save(update_fields=['is_deleted', 'body'])


class MeetingRoomViewSet(viewsets.ModelViewSet):
    """Meetings/live sessions the user may see; hosts and managers may change them."""
    serializer_class = serializers.MeetingRoomSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['is_active', 'is_recurring', 'group', 'host']
    search_fields = ['title', 'description']
    ordering_fields = ['scheduled_start', 'created_at']

    def get_queryset(self):
        """Everything for whole-calendar viewers; otherwise the calendar's own visibility
        rule, plus calls started in a conversation the user is in — the chat page polls
        this list to ring.
        """
        from apps.livesessions import audience

        user = self.request.user
        qs = models.MeetingRoom.objects.select_related('host', 'group')
        if audience.sees_whole_calendar(user):
            return qs.all()
        visible = audience.visible_sessions(user).values('pk')
        chat_calls = qs.filter(is_active=True, group__memberships__user=user).values('pk')
        if _is_parent(user):
            chat_calls = chat_calls.filter(group__kind__in=PARENT_CHAT_KINDS)
        return qs.filter(Q(pk__in=visible) | Q(pk__in=chat_calls))

    def _check_can_change(self, meeting, action):
        """Raise :class:`PermissionDenied` unless the user hosts or manages ``meeting``.
        """
        from apps.livesessions import audience
        user = self.request.user
        if meeting.host_id == user.pk or audience.can_manage(user, meeting):
            return
        raise PermissionDenied('Only the host or a manager of this session can do that.')

    def _check_group(self, serializer):
        """Refuse linking a meeting to a chat group the (non-staff) user is not a member
        of.
        """
        group = serializer.validated_data.get('group')
        user = self.request.user
        if group is None or _sees_every_chat(user):
            return
        if not _is_member(user, group):
            raise PermissionDenied('You can only attach a meeting to a conversation you are in.')

    def perform_create(self, serializer):
        """Create a meeting hosted by the requesting user."""
        self._check_group(serializer)
        serializer.save(host=self.request.user)

    def perform_update(self, serializer):
        """Update a meeting (host or manager only)."""
        self._check_can_change(serializer.instance, 'edit')
        self._check_group(serializer)
        serializer.save()

    def perform_destroy(self, instance):
        """Delete a meeting (host or manager only)."""
        self._check_can_change(instance, 'delete')
        instance.delete()

    @decorators.action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Invite users by id: ``{"user_ids": [...], "role": "attendee"}``. Notifies them.

        Only people the inviter could otherwise reach (their directory) are invited —
        the endpoint is not a way to e-mail anyone.
        """
        meeting = self.get_object()
        self._check_can_change(meeting, 'invite')
        role = request.data.get('role', 'attendee')
        if role not in dict(models.MeetingParticipant.ROLE_CHOICES) or role == 'host':
            role = 'attendee'
        from apps.livesessions import audience
        from django.contrib.auth import get_user_model
        ids = request.data.get('user_ids', [])
        if not isinstance(ids, (list, tuple)):
            ids = [ids]
        users = get_user_model().objects.filter(pk__in=ids, is_active=True)
        if not audience.sees_whole_calendar(request.user):
            from core.scoping import directory_users
            users = users.filter(pk__in=directory_users(request.user).values('pk'))
        for user in users:
            models.MeetingParticipant.objects.get_or_create(meeting=meeting, user=user, defaults={'role': role})
            services.notify(
                user, actor=request.user, verb='invited you to a meeting',
                title=f'Meeting: {meeting.title}',
                body=meeting.description, level='info',
                url=meeting.get_join_url(), meeting=meeting,
                email=meeting.scheduled_start is not None,
            )
        return response.Response({'invited': users.count()})


class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """The requesting user's own notifications (read-only, plus read actions)."""
    serializer_class = serializers.NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]
    filterset_fields = ['is_read', 'level']
    ordering_fields = ['created_at']

    def get_queryset(self):
        """Notifications addressed to the requesting user."""
        return models.Notification.objects.filter(recipient=self.request.user).select_related('actor')

    @decorators.action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Mark one notification read."""
        note = self.get_object()
        note.mark_read()
        return response.Response({'status': 'ok'})

    @decorators.action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        """Mark all of the user's unread notifications read; returns the count updated.
        """
        updated = self.get_queryset().filter(is_read=False).update(is_read=True)
        return response.Response({'updated': updated})

    @decorators.action(detail=False, methods=['get'])
    def unread_count(self, request):
        """Return the user's unread notification count."""
        return response.Response({'unread': self.get_queryset().filter(is_read=False).count()})


class AnnouncementViewSet(viewsets.ModelViewSet):
    """Announcements CRUD and sending; staff/admin only (``IsAdminUser``)."""
    serializer_class = serializers.AnnouncementSerializer
    permission_classes = [permissions.IsAdminUser]
    filterset_fields = ['audience', 'level', 'send_email']
    search_fields = ['title', 'body']
    ordering_fields = ['created_at']

    def get_queryset(self):
        """All announcements with their sender."""
        return models.Announcement.objects.select_related('sender').all()

    def perform_create(self, serializer):
        """Create an announcement sent by the requesting user."""
        serializer.save(sender=self.request.user)

    @decorators.action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Deliver this announcement (notifications + optional e-mail). Idempotent-ish."""
        announcement = self.get_object()
        count = services.send_announcement(announcement)
        return response.Response({'sent_to': count, 'sent_at': announcement.sent_at})
