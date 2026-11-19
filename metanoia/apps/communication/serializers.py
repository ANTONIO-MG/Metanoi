"""DRF serializers for the communication REST API (``/api/communication/``)."""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from core import validators as v
from core.utils import DISCONTINUED_USER, avatar_url, display_name, initials

from . import models

User = get_user_model()


class UserMiniSerializer(serializers.ModelSerializer):
    """Compact user representation embedded in chat / meeting / notification payloads.

    Identify a user by ``id`` and label them with ``display_name`` — the e-mail-only
    platform has no usernames, so no ``username`` field is exposed. ``avatar`` is
    the profile picture (``null`` when unset) and ``initials`` the fallback the UI
    draws in its place, so a client never has to guess how to letter a face.

    No e-mail address is exposed: everyone in a conversation would otherwise
    receive every other member's address. People are identified by name and
    picture.
    """

    display_name = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'display_name', 'avatar', 'initials']

    def get_display_name(self, obj):
        """Return the person's display name."""
        return display_name(obj)

    def get_avatar(self, obj):
        """Return the profile picture as an absolute URL when a request is available."""
        url = avatar_url(obj)
        request = self.context.get('request')
        return request.build_absolute_uri(url) if (url and request) else url

    def get_initials(self, obj):
        """Return the initials the UI letters a face with."""
        return initials(obj)


class MessageAttachmentSerializer(serializers.ModelSerializer):
    """A file attached to a chat message, with an absolute download ``url``."""
    url = serializers.SerializerMethodField()

    class Meta:
        model = models.MessageAttachment
        fields = ['id', 'kind', 'original_name', 'size', 'url', 'created_at']

    def get_url(self, obj):
        """Return the file's absolute URL, or ``None`` when the file is missing."""
        request = self.context.get('request')
        if not obj.file:
            return None
        return request.build_absolute_uri(obj.file.url) if request else obj.file.url


class MessageMentionSerializer(serializers.ModelSerializer):
    """A person tagged in a chat message."""
    user = UserMiniSerializer(read_only=True)

    class Meta:
        model = models.MessageMention
        fields = ['id', 'user', 'seen', 'created_at']


class MessageSerializer(serializers.ModelSerializer):
    """A chat message, as the chat page's ``renderMsg()`` reads it.

    Read-only fields worth knowing:

    ``sender``
        A method field rather than a nested serializer: ``Message.sender`` is
        ``SET_NULL``, so a message whose author closed their account has no user
        row left. The thread still belongs to everyone else in it, so the bubble
        stays and is attributed to the discontinued account instead of going blank.
    ``reply_preview``
        The quoted message a reply points at — just enough to draw the quote strip
        above the bubble without a second round trip per message.
    ``body_html``
        The body as safe HTML: escaped, with links made clickable (and LMS / Teams
        session links pointed inside the LMS) by :mod:`apps.communication.links` —
        the same rules as every server-rendered page. The chat renders it verbatim.
    ``read_by_count`` / ``other_member_count``
        How many other members have read this message (their ``last_read_at`` is
        at or past it), out of how many. Drives the sent / read ticks on your own
        bubbles.

    Write-only fields:

    ``mention_ids``
        User ids to tag, in addition to any ``@handles`` in ``body``.
    ``files``
        Uploaded files for the message. Each one goes through the same type and
        size validators as ``MessageAttachment.file``: the view creates
        attachments with ``.save()``, which never runs model validators, so
        without these the API would store any file type at any size.
    """
    sender = serializers.SerializerMethodField()
    attachments = MessageAttachmentSerializer(many=True, read_only=True)
    mention_links = MessageMentionSerializer(many=True, read_only=True)
    reply_preview = serializers.SerializerMethodField()
    body_html = serializers.SerializerMethodField()
    read_by_count = serializers.SerializerMethodField()
    other_member_count = serializers.SerializerMethodField()
    mention_ids = serializers.PrimaryKeyRelatedField(
        many=True, write_only=True, required=False, queryset=User.objects.all(), source='_mention_ids',
    )
    files = serializers.ListField(
        child=serializers.FileField(validators=v.validate_attachment),
        write_only=True, required=False, source='_files', max_length=10,
    )

    class Meta:
        model = models.Message
        fields = [
            'id', 'group', 'sender', 'body', 'body_html', 'reply_to', 'reply_preview', 'attachments',
            'mention_links', 'mention_ids', 'files',
            'read_by_count', 'other_member_count',
            'is_edited', 'edited_at', 'is_deleted', 'created_at',
        ]
        read_only_fields = ['sender', 'is_edited', 'edited_at', 'is_deleted', 'created_at']

    def get_body_html(self, obj):
        """Return the linkified body; newlines are kept by the bubble itself (``white-space: pre-wrap``)."""
        from .links import linkify
        return str(linkify(obj.body or '', linebreaks=False))

    def get_sender(self, obj):
        """Return the sender, or a placeholder for a discontinued account."""
        if obj.sender_id is None:
            return {'id': None, 'email': '', 'first_name': '', 'last_name': '',
                    'display_name': DISCONTINUED_USER, 'avatar': None, 'initials': '–',
                    'discontinued': True}
        return UserMiniSerializer(obj.sender, context=self.context).data

    def _read_marks(self, group_id):
        """Return ``(user_id, last_read_at)`` for every member of the group.

        One query per group per request, cached on the serializer context, rather
        than one per message: a 200-message thread would otherwise issue 200.
        """
        cache = self.context.setdefault('_read_marks', {})
        if group_id not in cache:
            cache[group_id] = list(
                models.ChatMembership.objects.filter(group_id=group_id)
                .values_list('user_id', 'last_read_at'))
        return cache[group_id]

    def get_read_by_count(self, obj):
        """Return how many other members have read up to this message."""
        if not obj.group_id:
            return 0
        return sum(
            1 for user_id, last_read in self._read_marks(obj.group_id)
            if user_id != obj.sender_id and last_read and last_read >= obj.created_at
        )

    def get_other_member_count(self, obj):
        """Return how many members besides the sender are in the group."""
        if not obj.group_id:
            return 0
        return sum(1 for user_id, _ in self._read_marks(obj.group_id) if user_id != obj.sender_id)

    def get_reply_preview(self, obj):
        """Return the quoted parent's id, sender, first 140 characters and attachment flag, or ``None``."""
        parent = obj.reply_to
        if parent is None:
            return None
        return {
            'id': parent.id,
            'sender': display_name(parent.sender) or 'Someone',
            'body': (parent.body or '')[:140],
            'has_attachment': parent.attachments.exists(),
        }

    def validate(self, attrs):
        """Keep a message inside its own conversation.

        ``reply_to`` must quote a message from the same group (the reply preview
        echoes the quoted body, so a foreign id would read another chat), and
        ``mention_ids`` may only tag members of that group. On an edit the
        message cannot be moved to a different conversation.
        """
        instance = getattr(self, 'instance', None)
        group = attrs.get('group')
        if instance is not None:
            if group is not None and group.pk != instance.group_id:
                raise serializers.ValidationError({'group': 'A message cannot be moved to another conversation.'})
            group = instance.group
        reply_to = attrs.get('reply_to')
        if reply_to is not None and group is not None and reply_to.group_id != group.pk:
            raise serializers.ValidationError({'reply_to': 'You can only reply to a message in this conversation.'})
        mentioned = attrs.get('_mention_ids') or []
        if mentioned and group is not None:
            member_ids = set(group.memberships.values_list('user_id', flat=True))
            if any(u.pk not in member_ids for u in mentioned):
                raise serializers.ValidationError({'mention_ids': 'You can only mention members of this conversation.'})
        return attrs

    def create(self, validated_data):
        """Create the message without the write-only ``_files`` / ``_mention_ids``.

        They are not model fields, so they are stripped before the ModelSerializer
        hands ``validated_data`` to ``Message(...)``. The view still reads them off
        ``serializer.validated_data`` to create attachments and mention rows.
        """
        validated_data.pop('_files', None)
        validated_data.pop('_mention_ids', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        """Update the message without the write-only ``_files`` / ``_mention_ids`` (see :meth:`create`)."""
        validated_data.pop('_files', None)
        validated_data.pop('_mention_ids', None)
        return super().update(instance, validated_data)


class ChatMembershipSerializer(serializers.ModelSerializer):
    """A person's membership of a chat, with their unread count."""
    user = UserMiniSerializer(read_only=True)
    unread_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = models.ChatMembership
        fields = ['id', 'user', 'role', 'is_auto', 'muted', 'last_read_at', 'joined_at', 'unread_count']


class ChatGroupSerializer(serializers.ModelSerializer):
    """A conversation (direct, module or custom group) with its last message.

    ``kind`` and the module link are system-managed and read-only: a writable
    ``kind`` would let a member turn a custom group into a "module" or "direct"
    one.
    """
    display_name = serializers.CharField(read_only=True)
    is_auto_managed = serializers.BooleanField(read_only=True)
    member_count = serializers.IntegerField(source='memberships.count', read_only=True)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = models.ChatGroup
        fields = [
            'id', 'kind', 'name', 'display_name', 'description',
            'programme_module', 'is_auto_managed', 'is_active', 'member_count',
            'last_message', 'created_at', 'updated_at',
        ]
        read_only_fields = ['kind', 'programme_module', 'created_at', 'updated_at']

    def get_last_message(self, obj):
        """Return a short summary of the newest non-deleted message, or ``None``."""
        msg = obj.messages.filter(is_deleted=False).order_by('-created_at').first()
        if not msg:
            return None
        return {'id': msg.id, 'body': (msg.body or '')[:120],
                'sender': display_name(msg.sender),
                'created_at': msg.created_at}


class MeetingRoomSerializer(serializers.ModelSerializer):
    """A meeting or call, with its absolute ``join_url`` and live/upcoming flags."""
    host = UserMiniSerializer(read_only=True)
    join_url = serializers.SerializerMethodField()
    external_url = serializers.CharField(read_only=True)
    is_live_now = serializers.BooleanField(read_only=True)
    is_upcoming = serializers.BooleanField(read_only=True)

    class Meta:
        model = models.MeetingRoom
        fields = [
            'id', 'title', 'slug', 'description', 'host', 'group',
            'scheduled_start', 'scheduled_end', 'is_recurring', 'recurrence',
            'requires_login', 'is_active', 'join_url', 'external_url',
            'is_live_now', 'is_upcoming', 'created_at',
        ]
        read_only_fields = ['slug', 'host', 'created_at']

    def get_join_url(self, obj):
        """Return the meeting's join URL, absolute when a request is available."""
        request = self.context.get('request')
        url = obj.get_join_url()
        return request.build_absolute_uri(url) if request else url


class NotificationSerializer(serializers.ModelSerializer):
    """A notification in someone's inbox; entirely read-only."""
    actor = UserMiniSerializer(read_only=True)

    class Meta:
        model = models.Notification
        fields = ['id', 'actor', 'verb', 'title', 'body', 'level', 'url',
                  'is_read', 'emailed', 'created_at', 'message', 'meeting', 'announcement']
        read_only_fields = fields


class AnnouncementSerializer(serializers.ModelSerializer):
    """A broadcast announcement.

    Only real model fields are listed: ``courses`` was removed from
    :class:`~apps.communication.models.Announcement`, and naming it here makes the
    serializer fail the moment there is an announcement to render.
    """
    sender = UserMiniSerializer(read_only=True)

    class Meta:
        model = models.Announcement
        fields = ['id', 'sender', 'title', 'body', 'level', 'url', 'audience',
                  'user_type', 'modules', 'users', 'send_email',
                  'sent_at', 'recipient_count', 'created_at']
        read_only_fields = ['sender', 'sent_at', 'recipient_count', 'created_at']
