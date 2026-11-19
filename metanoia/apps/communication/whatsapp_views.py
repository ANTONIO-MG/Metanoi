"""The WhatsApp webhook — Meta's end of the conversation.

Two endpoints on one URL, which is how the Cloud API works:

* **GET** — the subscription handshake. Meta calls it once when you point a
  webhook at us and echoes back ``hub.challenge`` if the verify token matches.
* **POST** — every inbound message and delivery receipt, forever. Messages go
  to the bot (:func:`whatsapp_bot.handle_inbound`), receipts move a sent
  message's ticks forward (:func:`whatsapp_inbox.update_status`).

This is an unauthenticated, publicly-reachable endpoint, so it is written to be
suspicious of what it receives:

* **Every POST is signature-checked** against ``WHATSAPP_APP_SECRET`` using the
  ``X-Hub-Signature-256`` header, compared in constant time. Without a secret
  configured the endpoint refuses everything rather than trusting the internet —
  a webhook that processes unsigned payloads is a stranger's remote control over
  student data.
* **It always answers 200** once the signature is good. Meta re-delivers
  anything else, so an error we return becomes an infinite retry; failures are
  logged instead and the message is dropped.
* **It does the smallest possible amount of work** before answering, and
  everything it does is idempotent on Meta's message id.
"""
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import whatsapp_bot, whatsapp_inbox

logger = logging.getLogger(__name__)


def _valid_signature(request):
    """True when the body really came from Meta.

    ``X-Hub-Signature-256`` is HMAC-SHA256 of the raw body keyed with the app
    secret. No secret configured → nothing is valid, deliberately: the safe
    failure for an unconfigured webhook is to accept nothing at all.
    """
    secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
    if not secret:
        logger.warning('whatsapp webhook: WHATSAPP_APP_SECRET is not set — rejecting')
        return False

    header = request.headers.get('X-Hub-Signature-256', '')
    if not header.startswith('sha256='):
        return False
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split('=', 1)[1])


_MEDIA_TYPES = {'image': 'image', 'sticker': 'image', 'audio': 'audio', 'voice': 'audio',
                'video': 'video', 'document': 'document'}


def _messages(payload):
    """Yield one dict per inbound message, whatever its type.

    Meta's envelope is deeply nested and carries the sender's WhatsApp profile
    name next to the message, and delivery receipts in the same shape; those
    come out of :func:`_statuses` instead.

    Contacts, reactions and unsupported kinds are kept as ``other`` and shown to staff.
    """
    for entry in payload.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            value = change.get('value') or {}
            names = {c.get('wa_id', ''): ((c.get('profile') or {}).get('name') or '')
                     for c in value.get('contacts', []) or []}
            for message in value.get('messages', []) or []:
                kind = message.get('type', '')
                item = {'number': message.get('from', ''), 'wa_id': message.get('id', ''),
                        'raw': message, 'kind': 'text', 'text': '', 'media_id': '',
                        'mime': '', 'filename': '',
                        'contact_name': names.get(message.get('from', ''), '')}
                if kind == 'text':
                    item['text'] = (message.get('text') or {}).get('body', '')
                elif kind in _MEDIA_TYPES:
                    media = message.get(kind) or {}
                    item.update(kind=_MEDIA_TYPES[kind], media_id=media.get('id', ''),
                                mime=media.get('mime_type', ''), filename=media.get('filename', ''),
                                text=media.get('caption', ''))
                elif kind == 'button':
                    item['text'] = (message.get('button') or {}).get('text', '')
                elif kind == 'interactive':
                    reply = message.get('interactive') or {}
                    chosen = reply.get('button_reply') or reply.get('list_reply') or {}
                    item['text'] = chosen.get('title', '')
                elif kind == 'location':
                    place = message.get('location') or {}
                    named = ', '.join(str(place[k]) for k in ('name', 'address') if place.get(k))
                    item['text'] = '📍 ' + (named or f"{place.get('latitude')}, {place.get('longitude')}")
                else:
                    item.update(kind='other', text=f'[{kind or "unknown"} message]')
                yield item


def _statuses(payload):
    """Yield ``(wamid, status, error)`` for each delivery receipt."""
    for entry in payload.get('entry', []) or []:
        for change in entry.get('changes', []) or []:
            for status in (change.get('value') or {}).get('statuses', []) or []:
                errors = status.get('errors') or []
                error = ''
                if errors:
                    first = errors[0] or {}
                    error = ((first.get('error_data') or {}).get('details')
                             or first.get('title') or first.get('message') or '')
                yield status.get('id', ''), status.get('status', ''), error


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def webhook(request):
    """Meta's WhatsApp Cloud API webhook.

    ``GET`` is the subscription handshake. The verify token is a shared secret,
    so it is compared in constant time: a timing oracle on it is free to exploit.
    A GET with no handshake at all is someone opening the URL (or a scanner) and
    is refused quietly; a real subscribe attempt with the wrong token is logged
    as a warning, because that is Meta's setup failing.

    ``POST`` must carry a valid signature. After that the answer is always 200,
    even for a body that is not JSON, or Meta retries it forever; each message
    goes to the bot and each receipt to the inbox, failures logged one by one.
    """
    if request.method == 'GET':
        token = getattr(settings, 'WHATSAPP_VERIFY_TOKEN', '')
        mode = request.GET.get('hub.mode', '')
        given = request.GET.get('hub.verify_token', '')
        if mode == 'subscribe' and token and hmac.compare_digest(token, given):
            return HttpResponse(request.GET.get('hub.challenge', ''), content_type='text/plain')
        log = logger.warning if mode == 'subscribe' else logger.info
        log('whatsapp webhook: verification refused (mode=%r)', mode)
        return HttpResponseForbidden('verification failed')

    if not _valid_signature(request):
        return HttpResponseForbidden('bad signature')

    try:
        payload = json.loads(request.body.decode() or '{}')
    except ValueError:
        logger.warning('whatsapp webhook: body was not JSON')
        return HttpResponse('ok')

    for item in _messages(payload):
        if not item['number']:
            continue
        try:
            whatsapp_bot.handle_inbound(
                item['number'], item['text'], wa_id=item['wa_id'], payload=item['raw'],
                kind=item['kind'], media_id=item['media_id'], mime=item['mime'],
                filename=item['filename'], contact_name=item['contact_name'])
        except Exception:  # pragma: no cover - defensive
            logger.exception('whatsapp webhook: could not handle message from %s', item['number'])

    for wamid, status, error in _statuses(payload):
        try:
            whatsapp_inbox.update_status(wamid, status, error=error)
        except Exception:  # pragma: no cover - defensive
            logger.exception('whatsapp webhook: could not apply status %s to %s', status, wamid)

    return HttpResponse('ok')
