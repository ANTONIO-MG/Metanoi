"""WebSocket consumers for real-time communication.

Two consumers, both authenticated via the session (AuthMiddlewareStack in
config/asgi.py puts the logged-in user on ``self.scope['user']``):

* :class:`AlertConsumer` — a per-user channel. The server pushes small events
  ("a notification arrived", "a message arrived") that the client turns into a
  sound + a badge bump, with no polling.
* :class:`ChatConsumer` — a per-conversation channel. When someone posts to a
  chat the server pushes a "new message" poke so open messengers refresh at once.

Server-side senders live in :mod:`apps.communication.realtime`.
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


def _alerts_group(user_id):
    """Channel-layer group name for a user's alert socket."""
    return f'alerts_{user_id}'


def _chat_group(group_id):
    """Channel-layer group name for a conversation's socket."""
    return f'chat_{group_id}'


def _lesson_group(lesson_id):
    """Channel-layer group name for a lesson's presence socket."""
    return f'lesson_{lesson_id}'


class AlertConsumer(AsyncJsonWebsocketConsumer):
    """Per-user socket carrying notification / message events for the navbar."""

    async def connect(self):
        """Accept signed-in users and join their personal alerts group; close otherwise.
        """
        user = self.scope.get('user')
        if user is None or not user.is_authenticated:
            await self.close()
            return
        self.group_name = _alerts_group(user.id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        """Leave the alerts group."""
        if getattr(self, 'group_name', None):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def alert(self, event):
        """Forward a server event to the client.

        Sent with ``channel_layer.group_send(..., {'type': 'alert', 'payload':
        {...}})``.
        """
        await self.send_json(event.get('payload', {}))


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """Per-conversation socket. Membership is checked once, on connect."""

    async def connect(self):
        """Accept members of the conversation and join its group; close for anyone else.
        """
        user = self.scope.get('user')
        self.group_id = self.scope['url_route']['kwargs']['group_id']
        if user is None or not user.is_authenticated or not await self._is_member(user.id, self.group_id):
            await self.close()
            return
        self.group_name = _chat_group(self.group_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        """Leave the conversation group."""
        if getattr(self, 'group_name', None):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def chat_message(self, event):
        """Forward a "new message" poke to the client.

        Sent with ``channel_layer.group_send(..., {'type': 'chat_message', 'payload':
        {...}})``.
        """
        await self.send_json(event.get('payload', {}))

    @database_sync_to_async
    def _is_member(self, user_id, group_id):
        """True when the user belongs to the conversation."""
        from .models import ChatMembership
        return ChatMembership.objects.filter(user_id=user_id, group_id=group_id).exists()


class LessonPresenceConsumer(AsyncJsonWebsocketConsumer):
    """"Who is in this lesson right now."

    Presence is peer-announced over the channel layer, so it is correct with the
    Redis layer across workers and needs no shared server-side roster: on connect
    a client broadcasts a ``join`` with its identity; every already-present client
    answers with a ``sync`` of its own identity (so the newcomer learns who is
    here); on disconnect a ``leave`` is broadcast. The browser keeps the roster.
    Access is gated by the same ``_can_view_lesson`` rule the lesson page uses.
    """

    async def connect(self):
        """Join the lesson's presence group if the user may view it, and announce them.

        The user's identity carries the hand-raise state (``hand``) in join/sync
        payloads, and the client is first told its own identity so it can toggle its own
        hand.
        """
        user = self.scope.get('user')
        self.lesson_id = self.scope['url_route']['kwargs']['lesson_id']
        self.me = None
        if user is not None and user.is_authenticated:
            self.me = await self._who(user.id, self.lesson_id)
        if not self.me:
            await self.close()
            return
        self.me['hand'] = False
        self.group_name = _lesson_group(self.lesson_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({'action': 'self', 'user': self.me})
        await self.channel_layer.group_send(
            self.group_name, {'type': 'presence_join', 'user': self.me, 'origin': self.channel_name})

    async def disconnect(self, code):
        """Announce the departure and leave the presence group."""
        if getattr(self, 'group_name', None):
            await self.channel_layer.group_send(
                self.group_name, {'type': 'presence_leave', 'user': self.me})
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        """Handle a client action: ``hand`` raises/lowers this user's hand and fans the
        state out.
        """
        if not getattr(self, 'group_name', None):
            return
        if content.get('action') == 'hand':
            self.me['hand'] = bool(content.get('up'))
            await self.channel_layer.group_send(self.group_name, {
                'type': 'presence_hand', 'user_id': self.me['id'], 'up': self.me['hand']})

    async def presence_join(self, event):
        """Relay an arrival, and answer someone else's arrival with this user's identity.

        A ``sync`` never triggers another ``sync``, so this cannot loop.
        """
        await self.send_json({'action': 'join', 'user': event['user']})
        if event.get('origin') != self.channel_name:
            await self.channel_layer.group_send(
                self.group_name, {'type': 'presence_sync', 'user': self.me})

    async def presence_sync(self, event):
        """Relay another participant's identity."""
        await self.send_json({'action': 'sync', 'user': event['user']})

    async def presence_leave(self, event):
        """Relay a departure."""
        await self.send_json({'action': 'leave', 'user': event['user']})

    async def presence_hand(self, event):
        """Relay a hand-raise change."""
        await self.send_json({'action': 'hand', 'user_id': event['user_id'], 'up': event['up']})

    @database_sync_to_async
    def _who(self, user_id, lesson_id):
        """Return {id,name,avatar} if the user may view the lesson, else None."""
        from django.contrib.auth import get_user_model

        from core.utils import avatar_url, display_name
        from apps.learning.models import Lesson
        from apps.learning.views import _can_view_lesson
        User = get_user_model()
        try:
            user = User.objects.select_related('profile').get(pk=user_id)
            lesson = Lesson.objects.select_related('module').get(pk=lesson_id)
        except (User.DoesNotExist, Lesson.DoesNotExist):
            return None
        try:
            if not _can_view_lesson(user, lesson):
                return None
        except Exception:
            return None
        return {'id': user_id, 'name': display_name(user) or 'Someone', 'avatar': avatar_url(user) or ''}
