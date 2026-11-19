"""ASGI entry point — serves both HTTP (Django) and WebSocket (Channels).

``get_asgi_application()`` is called first so Django's app registry is ready
*before* the consumer modules (which import models) are imported. The protocol
router then sends normal requests to Django and ``ws://…/ws/…`` connections
through the session/auth middleware into the WebSocket URL router.

Django must be initialised (``django_asgi_app``) before anything that touches
models is imported — Channels' ``AuthMiddlewareStack`` and our consumers both
do — hence the late imports marked ``noqa: E402``.

WebSocket connections are same-origin only (``AllowedHostsOriginValidator``),
then pass session + auth middleware so ``self.scope['user']`` is the logged-in
user inside every consumer.

See apps/communication/routing.py for the socket URLs and docs/WEBSOCKETS.md.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from apps.communication.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
