"""WhatsApp — every message the LMS sends to a phone goes through here
(notification copies, registration invoice PDF, verification codes, staff inbox
replies).

The number is ``Person.whatsapp_number`` (verified), falling back to
``PersonContact.primary_phone`` then ``Person.phone``. Several accounts may share one
number — a parent and a child, say — so a number is never treated as a person;
every message is about one account and says whose it is.

Configuration (all via the environment — see ``.env.example``)::

    WHATSAPP_ENABLED=True
    WHATSAPP_PHONE_NUMBER_ID=...     # Meta → WhatsApp → API setup
    WHATSAPP_ACCESS_TOKEN=...        # permanent system-user token
    WHATSAPP_API_VERSION=v23.0       # optional
    WHATSAPP_BUSINESS_ACCOUNT_ID=... # needed only to submit templates
    DEFAULT_DIAL_CODE=27             # assumed when a stored number has no code

**Dormant until configured.** With ``WHATSAPP_ENABLED`` off — the default, and
the case on every dev machine — nothing is sent, nothing raises, and every
function reports why. That is deliberate: an unconfigured integration must never
break a notification, a registration or a request, and a developer's test data
has no business messaging real phone numbers.

**A note on the 24-hour window.** Meta only allows free-form messages to someone
who has messaged you in the last 24 hours; outside that window a business must
send an approved *template*. :func:`deliver_free_text` is the free-form call and
:func:`send_template` sends one of the templates in :mod:`core.whatsapp_templates`.
The callers that know about the window — notification copies and the staff
inbox — choose between them.

Every send goes through :func:`deliver`, which returns ``(sent, reason, wamid)``;
``wamid`` is what the webhook's delivery receipts refer to. :func:`send_text`
and :func:`send_document` return just ``(sent, reason)``.
"""
import json
import logging
import urllib.error
import urllib.request
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH = 'https://graph.facebook.com'


def _setting(name, default=None):
    """The Django setting ``name``, or ``default``."""
    return getattr(settings, name, default)


def _version():
    """The Graph API version to call (``WHATSAPP_API_VERSION``, default ``v23.0``)."""
    return _setting('WHATSAPP_API_VERSION', '') or 'v23.0'


def enabled():
    """True when there is enough configuration to actually send something."""
    return bool(_setting('WHATSAPP_ENABLED')
                and _setting('WHATSAPP_PHONE_NUMBER_ID')
                and _setting('WHATSAPP_ACCESS_TOKEN'))


def msisdn(phone):
    """``+27 82 000 0199`` → ``27820000199``; ``''`` when it cannot be dialled.

    A local number (``082…``) carries no country code, so the configured default
    is prepended rather than the number being sent somewhere unpredictable.
    """
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if not digits:
        return ''
    if digits.startswith('00'):
        digits = digits[2:]
    if digits.startswith('0'):
        digits = str(_setting('DEFAULT_DIAL_CODE', '27')) + digits[1:]
    return digits if len(digits) >= 10 else ''


def number_for(user_or_person):
    """The WhatsApp number on file for a ``User`` or ``Person``, or ``''``.

    The verified number wins; typed ones are the fallback for accounts that
    registered before verification existed.
    """
    person = getattr(user_or_person, 'profile', None) or user_or_person
    verified = getattr(person, 'whatsapp_number', '')
    if verified:
        return verified
    contact = getattr(person, 'contact', None)
    return (msisdn(getattr(contact, 'primary_phone', ''))
            or msisdn(str(getattr(person, 'phone', '') or '')))


def display(number):
    """``27821234567`` → ``+27 82 123 4567`` — for people to read, not to dial."""
    digits = msisdn(number) or ''.join(ch for ch in str(number or '') if ch.isdigit())
    if digits.startswith('27') and len(digits) == 11:
        return f'+27 {digits[2:4]} {digits[4:7]} {digits[7:]}'
    return f'+{digits}' if digits else ''


def _post_json(path, payload):
    """POST ``payload`` as JSON to Graph ``path`` with the access token; return the decoded reply."""
    request = urllib.request.Request(
        f'{GRAPH}/{_version()}/{path}', data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization': f"Bearer {_setting('WHATSAPP_ACCESS_TOKEN')}",
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode() or '{}')


def _reason_from(detail):
    """A reason staff can read, from Meta's error body."""
    try:
        error = json.loads(detail).get('error') or {}
    except ValueError:
        return 'WhatsApp rejected the message'
    code = error.get('code')
    if code == 131047:
        return 'More than 24 hours since they last wrote, so a template is needed'
    if code == 131026:
        return 'This number is not on WhatsApp, or cannot receive messages'
    if code in (132000, 132001, 132005, 132007, 132012, 132015, 132016):
        return 'The template was refused: check it is approved in WhatsApp Manager'
    message = (error.get('error_data') or {}).get('details') or error.get('message') or ''
    return f'WhatsApp rejected the message: {message}'[:250] if message else 'WhatsApp rejected the message'


def deliver(payload, *, what='message'):
    """POST a message payload. Returns ``(sent, reason, wamid)``.

    Nothing in this module raises. A phone that cannot be reached is a thing to
    log and carry on from, not a reason to fail whatever the user was doing.
    """
    if not enabled():
        return False, 'WhatsApp is not configured on this installation', ''
    try:
        reply = _post_json(f"{_setting('WHATSAPP_PHONE_NUMBER_ID')}/messages", payload)
        wamid = ((reply.get('messages') or [{}])[0] or {}).get('id', '')
        return True, '', wamid
    except urllib.error.HTTPError as exc:          # pragma: no cover - network
        detail = exc.read().decode(errors='ignore')[:600]
        logger.warning('whatsapp: %s to %s rejected: %s', what, payload.get('to'), detail[:300])
        return False, _reason_from(detail), ''
    except Exception:                              # pragma: no cover - network
        logger.exception('whatsapp: %s to %s could not be sent', what, payload.get('to'))
        return False, 'WhatsApp could not be reached', ''


def _to(number):
    """``number`` as Meta wants it: digits are kept, anything else is normalised with :func:`msisdn`."""
    return msisdn(number) if not str(number).isdigit() else str(number)


def text_payload(number, body):
    """A free-form text message for ``number``; the body is capped at 4000 characters."""
    return {'messaging_product': 'whatsapp', 'to': number, 'type': 'text',
            'text': {'preview_url': True, 'body': body[:4000]}}


def template_param(value, limit=900):
    """A value made safe for a template parameter.

    Meta refuses parameters containing new lines, tabs or more than four spaces
    in a row, so a multi-line notification body is folded onto one line.
    """
    text = ' '.join(str(value or '').split())
    return (text[:limit - 1] + '…') if len(text) > limit else (text or '-')


def template_payload(number, name, params=(), *, lang=None, button_params=None,
                     header_document=None):
    """The payload for an approved template.

    ``params`` fill the body's ``{{1}}``, ``{{2}}``…; ``button_params`` maps a
    button index to the value for its URL suffix or copy-code;
    ``header_document`` is ``(media_id, filename)`` for a template whose header
    is a document.
    """
    components = []
    if header_document:
        media_id, filename = header_document
        components.append({'type': 'header', 'parameters': [
            {'type': 'document', 'document': {'id': media_id, 'filename': filename}}]})
    if params:
        components.append({'type': 'body', 'parameters': [
            {'type': 'text', 'text': template_param(p)} for p in params]})
    for index, value in (button_params or {}).items():
        components.append({'type': 'button', 'sub_type': 'url', 'index': str(index),
                           'parameters': [{'type': 'text', 'text': str(value)}]})
    template = {'name': name,
                'language': {'code': lang or _setting('WHATSAPP_TEMPLATE_LANG', '') or 'en'}}
    if components:
        template['components'] = components
    return {'messaging_product': 'whatsapp', 'to': number, 'type': 'template',
            'template': template}


def send_template(number, name, params=(), *, lang=None, button_params=None,
                  header_document=None):
    """Send an approved template. Returns ``(sent, reason, wamid)``."""
    number = _to(number)
    if not number:
        return False, 'no usable WhatsApp number', ''
    return deliver(template_payload(number, name, params, lang=lang, button_params=button_params,
                                    header_document=header_document), what=f'template {name}')


def send_code(number, code):
    """Send a one-time code with the authentication template.

    Authentication templates carry the code twice, in the body and in the
    "Copy code" button, which is why it is passed in both places.
    """
    from core.whatsapp_templates import OTP
    return send_template(number, _setting('WHATSAPP_OTP_TEMPLATE', '') or OTP,
                         [code], button_params={0: code})


def send_text(number, body):
    """Send a plain message. Returns ``(sent, reason)``.

    Where :data:`WHATSAPP_TEMPLATE_NAME` is configured the message goes as that
    approved template (with the body as its single parameter) instead — the old
    catch-all, kept for installations that set it up before the per-purpose
    templates in :mod:`core.whatsapp_templates` existed.
    """
    sent, reason, _wamid = deliver_text(number, body)
    return sent, reason


def deliver_text(number, body):
    """:func:`send_text`, returning ``(sent, reason, wamid)``."""
    number = _to(number)
    if not number:
        return False, 'no usable WhatsApp number', ''
    template = _setting('WHATSAPP_TEMPLATE_NAME', '')
    if template:
        return deliver(template_payload(number, template, [body]), what='message')
    return deliver(text_payload(number, body), what='message')


def deliver_free_text(number, body):
    """Free-form text, never swapped for a template — for replies inside the window."""
    number = _to(number)
    if not number:
        return False, 'no usable WhatsApp number', ''
    return deliver(text_payload(number, body), what='message')


def upload_document(data, filename, mime='application/pdf'):
    """Upload a file and return its media id, or ``None``.

    The Cloud API takes files by media id rather than by URL, so anything
    being sent has to be uploaded first. This is the only multipart request in
    the codebase, so it is assembled by hand rather than adding a dependency.
    """
    if not enabled():
        return None
    phone_id = _setting('WHATSAPP_PHONE_NUMBER_ID')
    boundary = f'----metanoia{uuid.uuid4().hex}'
    parts = []

    def field(name, value):
        """Append one plain multipart form field."""
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n'.encode())

    safe_name = filename.replace('"', '').replace('\r', '').replace('\n', '')
    field('messaging_product', 'whatsapp')
    field('type', mime)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'.encode() + data + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())

    request = urllib.request.Request(
        f'{GRAPH}/{_version()}/{phone_id}/media', data=b''.join(parts), method='POST',
        headers={'Authorization': f"Bearer {_setting('WHATSAPP_ACCESS_TOKEN')}",
                 'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode() or '{}').get('id')
    except Exception:                              # pragma: no cover - network
        logger.exception('whatsapp: upload of %s failed', filename)
        return None


def media_kind(mime):
    """The Cloud API message type for a file: image, video, audio or document."""
    mime = (mime or '').lower()
    if mime in ('image/jpeg', 'image/png'):
        return 'image'
    if mime in ('video/mp4', 'video/3gpp'):
        return 'video'
    if mime.startswith('audio/'):
        return 'audio'
    return 'document'


def send_document(number, data, filename, *, caption='', mime='application/pdf'):
    """Upload a file and send it. Returns ``(sent, reason)``."""
    sent, reason, _wamid = deliver_media(number, data, filename, caption=caption, mime=mime)
    return sent, reason


def deliver_media(number, data, filename, *, caption='', mime='application/pdf'):
    """Upload a file and send it as the right kind. Returns ``(sent, reason, wamid)``."""
    number = _to(number)
    if not number:
        return False, 'no usable WhatsApp number', ''
    if not enabled():
        return False, 'WhatsApp is not configured on this installation', ''

    media_id = upload_document(data, filename, mime)
    if not media_id:
        return False, 'WhatsApp did not accept the file', ''
    kind = media_kind(mime)
    body = {'id': media_id}
    if kind == 'document':
        body['filename'] = filename
    if caption and kind != 'audio':
        body['caption'] = caption[:1000]
    return deliver({'messaging_product': 'whatsapp', 'to': number, 'type': kind, kind: body},
                   what=kind)


def _media_limit():
    """The largest inbound media file accepted (``WHATSAPP_MEDIA_MAX_BYTES``, default 25 MB)."""
    return int(_setting('WHATSAPP_MEDIA_MAX_BYTES', 0) or 25 * 1024 * 1024)


def download_media(media_id):
    """Fetch an inbound file from Meta. Returns ``(data, mime)`` or ``(None, '')``.

    Two requests: the media id resolves to a short-lived URL, and that URL is
    fetched with the same bearer token. Meta keeps inbound media for about 30
    days, so it is fetched once and stored on our side.
    """
    if not (enabled() and media_id):
        return None, ''
    auth = {'Authorization': f"Bearer {_setting('WHATSAPP_ACCESS_TOKEN')}"}
    try:
        info_request = urllib.request.Request(f'{GRAPH}/{_version()}/{media_id}', headers=auth)
        with urllib.request.urlopen(info_request, timeout=20) as response:
            info = json.loads(response.read().decode() or '{}')
        url = info.get('url') or ''
        if not url.startswith('https://'):
            return None, ''
        with urllib.request.urlopen(urllib.request.Request(url, headers=auth),
                                    timeout=60) as response:
            data = response.read(_media_limit() + 1)
        if len(data) > _media_limit():
            logger.warning('whatsapp: media %s is larger than the limit, not stored', media_id)
            return None, ''
        return data, info.get('mime_type', '')
    except Exception:                              # pragma: no cover - network
        logger.exception('whatsapp: could not download media %s', media_id)
        return None, ''


def mark_read(wamid):
    """Tell WhatsApp a message was read (blue ticks on their side). Best effort."""
    if not (enabled() and wamid):
        return False
    try:
        _post_json(f"{_setting('WHATSAPP_PHONE_NUMBER_ID')}/messages",
                   {'messaging_product': 'whatsapp', 'status': 'read', 'message_id': wamid})
        return True
    except Exception:                              # pragma: no cover - network
        logger.info('whatsapp: could not mark %s read', wamid)
        return False
