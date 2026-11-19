"""Desktop notifications through the browser's push service (Web Push).

A browser that switched on "Desktop alerts" from the bell holds a
:class:`~apps.communication.models.WebPushSubscription`. :func:`send_to_user`
encrypts a small JSON payload for each of the user's browsers and hands it to
that browser's push service (Google, Mozilla, Apple, Microsoft). The service
worker (``/sw.js``, templates/communication/sw.js) shows it as a system
notification, even when no Metanoia tab is open, as long as the browser runs.

Best effort throughout: a push that fails is logged and forgotten, never
raised, and a subscription the push service reports gone (404/410) is deleted.

``RUN_IN_THREAD``
    When true (the default) delivery runs on a daemon thread so the request is
    not held up. Tests turn it off so pushes happen inline.
"""

import json
import logging
import threading

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

logger = logging.getLogger('apps.communication')

RUN_IN_THREAD = True


def enabled():
    """Return whether Web Push is configured (both VAPID keys are set)."""
    return bool(getattr(settings, 'VAPID_PUBLIC_KEY', '') and getattr(settings, 'VAPID_PRIVATE_KEY', ''))


def public_key():
    """Return the VAPID public key the browser subscribes with, or ``''`` when push is off."""
    return getattr(settings, 'VAPID_PUBLIC_KEY', '') if enabled() else ''


def _send_one(sub, data):
    """Push ``data`` to one subscription; return ``True`` on success.

    A 404/410 from the push service means the browser unsubscribed, so the
    subscription is deleted. Any other failure is logged and swallowed.
    """
    from pywebpush import WebPushException, webpush
    try:
        webpush(
            subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
            data=data,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': settings.VAPID_SUBJECT},
            ttl=3600,
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status in (404, 410):
            sub.delete()
        else:
            logger.warning('webpush to %s failed: %s', sub.pk, exc)
        return False
    except Exception:
        logger.warning('webpush to %s failed', sub.pk, exc_info=True)
        return False
    type(sub).objects.filter(pk=sub.pk).update(last_used_at=timezone.now())
    return True


def _deliver(user_id, data):
    """Push ``data`` to every subscription of ``user_id``; return how many succeeded."""
    from .models import WebPushSubscription
    sent = 0
    for sub in WebPushSubscription.objects.filter(user_id=user_id):
        sent += _send_one(sub, data)
    return sent


def send_to_user(user, *, title, body='', url='', tag='', kind='notification'):
    """Push one notification to every browser ``user`` switched desktop alerts on in."""
    if not enabled() or user is None:
        return
    data = json.dumps({'title': title[:120], 'body': body[:240], 'url': url or '/', 'tag': tag[:60],
                       'kind': kind})
    if not RUN_IN_THREAD:
        return _deliver(user.pk, data)

    def target():
        """Thread body: deliver with fresh database connections and close them afterwards."""
        close_old_connections()
        try:
            _deliver(user.pk, data)
        except Exception:
            logger.exception('webpush delivery failed')
        finally:
            connections.close_all()

    threading.Thread(target=target, daemon=True, name='metanoia-webpush').start()
