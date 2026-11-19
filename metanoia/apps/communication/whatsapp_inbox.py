"""The WhatsApp inbox: conversations, the people on a number, and sending.

Everything that touches a :class:`~apps.communication.models.WhatsAppThread`
goes through here — the webhook recording what arrived, the bot answering,
notification copies going out, and staff replying from ``/staff/whatsapp/``.
The transport itself (HTTP to Meta) is :mod:`core.whatsapp`.

**A number is not a person.** Several accounts may share one phone (a parent
and their child, siblings). :func:`people_for_number` returns all of them;
nothing here assumes there is only one. Messages *about* an account always say
whose account it is, so whoever holds the phone can tell them apart.

**The 24-hour window.** A free-form message only reaches someone who wrote to
us in the last 24 hours. A staff reply written later is *queued*: the person is
sent the approved "the team has replied" template, and the moment they answer
(which re-opens the window) every queued reply is delivered in order
(:func:`flush_queued`). Staff write the same way either way; the inbox shows
which of the two happened.

``ALLOWED_UPLOADS``
    What staff may attach, by MIME type, and the most WhatsApp accepts for
    each kind (in MB).
"""
import logging
import mimetypes

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Q

from core import whatsapp
from core import whatsapp_templates as templates
from core.utils import display_name

logger = logging.getLogger(__name__)

ALLOWED_UPLOADS = {
    'image/jpeg': 5, 'image/png': 5,
    'application/pdf': 100, 'text/plain': 100,
    'application/msword': 100,
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 100,
    'application/vnd.ms-excel': 100,
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 100,
    'application/vnd.ms-powerpoint': 100,
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': 100,
    'video/mp4': 16, 'audio/mpeg': 16, 'audio/ogg': 16, 'audio/aac': 16, 'audio/mp4': 16,
}

_KIND_FOR_TYPE = {'image': 'image', 'audio': 'audio', 'voice': 'audio', 'video': 'video',
                  'document': 'document', 'sticker': 'image'}


def normalise(number):
    """Return ``number`` as bare international digits (MSISDN), or just its digits when it does not parse."""
    return whatsapp.msisdn(number) or ''.join(ch for ch in str(number or '') if ch.isdigit())


def people_for_number(number):
    """Every account on a number: verified first, then numbers typed before
    verification existed. Preview (super-student) accounts are left out.

    Typed numbers are stored in more than one shape (``+27 82…``, ``082…``), so
    the query narrows by the last four digits in SQL and compares properly in
    Python.
    """
    from apps.accounts.models import Person

    digits = normalise(number)
    if len(digits) < 9:
        return []
    base = Person.objects.select_related('user', 'contact').filter(
        user__is_active=True, preview_of__isnull=True)

    verified = list(base.filter(whatsapp_number=digits).order_by('first_name', 'last_name'))
    seen = {p.pk for p in verified}

    tail = digits[-4:]
    legacy = []
    for person in base.filter(Q(contact__primary_phone__contains=tail) | Q(phone__contains=tail)) \
                      .exclude(pk__in=seen).filter(whatsapp_number=''):
        contact = getattr(person, 'contact', None)
        typed = {whatsapp.msisdn(getattr(contact, 'primary_phone', '')), whatsapp.msisdn(person.phone)}
        if digits in typed:
            legacy.append(person)
    legacy.sort(key=lambda p: (p.first_name.lower(), p.last_name.lower()))
    return verified + legacy


def people_map(numbers):
    """``{number: [people]}`` for many numbers in two queries — for the inbox list."""
    from apps.accounts.models import Person

    numbers = {normalise(n) for n in numbers if len(normalise(n)) >= 9}
    found = {n: [] for n in numbers}
    if not numbers:
        return found
    base = Person.objects.select_related('user', 'contact').filter(
        user__is_active=True, preview_of__isnull=True)
    for person in base.filter(whatsapp_number__in=numbers):
        found[person.whatsapp_number].append(person)
    tails = Q()
    for number in numbers:
        tails |= Q(contact__primary_phone__contains=number[-4:]) | Q(phone__contains=number[-4:])
    for person in base.filter(whatsapp_number='').filter(tails):
        contact = getattr(person, 'contact', None)
        for typed in {whatsapp.msisdn(getattr(contact, 'primary_phone', '')), whatsapp.msisdn(person.phone)}:
            if typed in found and person not in found[typed]:
                found[typed].append(person)
    return found


def name_of(person):
    """Return the display name of ``person``'s account, or ``''``."""
    return display_name(person.user) if person is not None and person.user_id else ''


def thread_for(number, *, contact_name=''):
    """Return the conversation for ``number``, creating it, and refresh the WhatsApp profile name when given."""
    from .models import WhatsAppThread

    digits = normalise(number)
    thread, created = WhatsAppThread.objects.get_or_create(number=digits)
    if contact_name and thread.contact_name != contact_name[:120]:
        thread.contact_name = contact_name[:120]
        thread.save(update_fields=['contact_name'])
    return thread


def thread_title(thread, people=None):
    """How a conversation is named in the inbox list."""
    people = thread.people() if people is None else people
    if len(people) == 1:
        return name_of(people[0])
    if people:
        return ' & '.join(name_of(p) for p in people[:3]) + (' …' if len(people) > 3 else '')
    return thread.contact_name or whatsapp.display(thread.number)


def _touch(thread, message, *, inbound):
    """Store the thread's last-message time and preview; an inbound message also opens the window and adds to unread."""
    from .models import WhatsAppThread

    preview = (message.body or message.media_filename or message.get_kind_display())[:160]
    fields = {'last_message_at': message.created_at, 'last_preview': preview}
    if inbound:
        fields['last_inbound_at'] = message.created_at
    WhatsAppThread.objects.filter(pk=thread.pk).update(
        **fields, **({'unread': F('unread') + 1} if inbound else {}))
    for key, value in fields.items():
        setattr(thread, key, value)


def record_inbound(number, *, text='', wa_id='', payload=None, kind='text', media_id='',
                   mime='', filename='', contact_name=''):
    """Store one inbound message. Returns ``(message, thread)``; ``message`` is
    ``None`` when Meta re-delivered something already stored."""
    from .models import WhatsAppMessage

    digits = normalise(number)
    if wa_id and WhatsAppMessage.objects.filter(direction=WhatsAppMessage.IN, wa_id=wa_id).exists():
        return None, None

    thread = thread_for(digits, contact_name=contact_name)
    people = people_for_number(digits)
    person = (thread.active_person if thread.active_person in people
              else (people[0] if len(people) == 1 else None))
    message = WhatsAppMessage.objects.create(
        direction=WhatsAppMessage.IN, number=digits, person=person, thread=thread,
        body=text or '', wa_id=wa_id, payload=payload or {}, kind=kind,
        media_id=media_id, media_mime=mime, media_filename=filename[:200])
    _touch(thread, message, inbound=True)
    return message, thread


def log_outbound(number, body, *, kind='text', person=None, sent=False, reason='', wamid='',
                 sent_by=None, template='', thread=None, status=None, command='', media=None,
                 mime='', filename=''):
    """Store one outbound message on its conversation."""
    from .models import WhatsAppMessage

    digits = normalise(number)
    thread = thread or thread_for(digits)
    message = WhatsAppMessage(
        direction=WhatsAppMessage.OUT, number=digits, person=person, thread=thread,
        body=body or '', kind=kind, wa_id=wamid, sent_by=sent_by, template=template,
        command=command, media_mime=mime, media_filename=(filename or '')[:200],
        status=status or (WhatsAppMessage.STATUS_SENT if sent else WhatsAppMessage.STATUS_FAILED),
        error='' if sent or status == WhatsAppMessage.STATUS_QUEUED else (reason or '')[:255],
        payload={'delivered': sent, 'reason': reason})
    if media is not None:
        message.media.save(filename or 'file', media, save=False)
    message.save()
    _touch(thread, message, inbound=False)
    return message


def update_status(wamid, status, *, error=''):
    """Apply a delivery receipt. Statuses only move forward."""
    from .models import WhatsAppMessage

    if not wamid or status not in WhatsAppMessage.STATUS_RANK:
        return 0
    rank = WhatsAppMessage.STATUS_RANK
    changed = 0
    for message in WhatsAppMessage.objects.filter(direction=WhatsAppMessage.OUT, wa_id=wamid):
        if rank.get(status, 0) > rank.get(message.status, 0) or status == WhatsAppMessage.STATUS_FAILED:
            message.status = status
            fields = ['status']
            if error:
                message.error = error[:255]
                fields.append('error')
            message.save(update_fields=fields)
            changed += 1
    return changed


def open_for_staff(thread):
    """Mark a conversation as needing a person. Returns True if it was closed."""
    from .models import WhatsAppThread

    if thread.status == WhatsAppThread.OPEN:
        return False
    thread.status = WhatsAppThread.OPEN
    thread.save(update_fields=['status'])
    return True


def alert_staff(thread, message):
    """Ping every admin and staff member's bell about a message that needs a person."""
    from django.urls import reverse

    from .realtime import push_alert

    User = get_user_model()
    team = User.objects.filter(is_active=True).filter(
        Q(profile__user_type__in=('admin', 'staff')) | Q(is_staff=True) | Q(is_superuser=True)
    ).filter(Q(profile__isnull=True) | Q(profile__preview_of__isnull=True)).values_list('pk', flat=True)
    url = reverse('staffdesk:whatsapp-thread', args=[thread.pk])
    title = f'WhatsApp · {thread_title(thread)}'
    body = (message.body or message.get_kind_display())[:80]
    for user_id in set(team):
        try:
            push_alert(user_id, kind='message', title=title, body=body, url=url)
        except Exception:  # pragma: no cover - realtime is best effort
            pass


def _greeting_name(thread):
    """Return who to greet on the thread: the active person, the only person, the profile name or "there"."""
    people = thread.people()
    if thread.active_person in people:
        return name_of(thread.active_person)
    if len(people) == 1:
        return name_of(people[0])
    return thread.contact_name or 'there'


def _signed(text, user):
    """Sign a staff reply with the sender's first name, WhatsApp-italic."""
    first = (getattr(getattr(user, 'profile', None), 'first_name', '') or '').strip()
    return f'{text}\n\n_{first}, Metanoia Group_' if first else text


def check_upload(upload):
    """``None`` when a staff attachment can be sent, otherwise why not."""
    mime = (getattr(upload, 'content_type', '') or mimetypes.guess_type(upload.name)[0] or '').lower()
    limit = ALLOWED_UPLOADS.get(mime)
    if limit is None:
        return 'WhatsApp cannot send that kind of file. Use a photo (JPG/PNG), PDF, Office file, MP4 or audio.'
    if upload.size > limit * 1024 * 1024:
        return f'That file is too large for WhatsApp ({limit} MB at most for this kind).'
    return None


def send_staff_reply(thread, user, *, text='', upload=None):
    """Send what a staff member wrote. Returns the stored message(s).

    Inside the window it goes straight away. Outside it, it is stored as queued
    and the person gets the "the team has replied" template; :func:`flush_queued`
    delivers it once they answer.

    The window is decided by their latest message, which may have arrived since
    the thread was loaded, so the thread is refreshed first. One "the team has
    replied" per silence is enough; a second reply written before they answer
    simply joins the queue.
    """
    from .models import WhatsAppMessage

    thread.refresh_from_db(fields=['last_inbound_at', 'active_person'])
    text = (text or '').strip()
    body = _signed(text, user) if text else ''
    person = thread.active_person if thread.active_person in thread.people() else None
    mime = filename = ''
    data = None
    if upload is not None:
        mime = (getattr(upload, 'content_type', '') or mimetypes.guess_type(upload.name)[0] or '').lower()
        filename = upload.name.rsplit('/', 1)[-1][:200]
        data = upload.read()
    kind = whatsapp.media_kind(mime) if upload is not None else WhatsAppMessage.KIND_TEXT

    if not thread.window_open:
        message = log_outbound(
            thread.number, body, kind=kind, person=person, sent_by=user, thread=thread,
            status=WhatsAppMessage.STATUS_QUEUED, mime=mime, filename=filename,
            media=ContentFile(data) if data is not None else None)
        asked = WhatsAppMessage.objects.filter(
            thread=thread, direction=WhatsAppMessage.OUT, template=templates.NEW_MESSAGE,
        ).exclude(status=WhatsAppMessage.STATUS_FAILED)
        if thread.last_inbound_at:
            asked = asked.filter(created_at__gt=thread.last_inbound_at)
        already_asked = asked.exists()
        if not already_asked:
            name = _greeting_name(thread)
            sent, reason, wamid = whatsapp.send_template(thread.number, templates.NEW_MESSAGE, [name])
            log_outbound(thread.number, f'Hi {name}, the Metanoia Group team has replied to you '
                         'about your account. Tap Show message below to read it here.',
                         kind=WhatsAppMessage.KIND_TEMPLATE, template=templates.NEW_MESSAGE,
                         person=person, sent=sent, reason=reason, wamid=wamid, thread=thread)
        return message

    if data is not None:
        sent, reason, wamid = whatsapp.deliver_media(thread.number, data, filename,
                                                     caption=body, mime=mime)
    else:
        sent, reason, wamid = whatsapp.deliver_free_text(thread.number, body)
    return log_outbound(thread.number, body, kind=kind, person=person, sent=sent, reason=reason,
                        wamid=wamid, sent_by=user, thread=thread, mime=mime, filename=filename,
                        media=ContentFile(data) if data is not None else None)


def flush_queued(thread):
    """Deliver staff replies that were waiting for the window to open."""
    from .models import WhatsAppMessage

    queued = list(WhatsAppMessage.objects.filter(
        thread=thread, direction=WhatsAppMessage.OUT, status=WhatsAppMessage.STATUS_QUEUED
    ).order_by('created_at'))
    for message in queued:
        if message.media:
            try:
                with message.media.open('rb') as handle:
                    data = handle.read()
            except OSError:
                data = None
            if data is None:
                sent, reason, wamid = False, 'The attachment is missing', ''
            else:
                sent, reason, wamid = whatsapp.deliver_media(
                    thread.number, data, message.media_filename or 'file',
                    caption=message.body, mime=message.media_mime)
        else:
            sent, reason, wamid = whatsapp.deliver_free_text(thread.number, message.body)
        message.status = WhatsAppMessage.STATUS_SENT if sent else WhatsAppMessage.STATUS_FAILED
        message.error = '' if sent else reason[:255]
        message.wa_id = wamid
        message.save(update_fields=['status', 'error', 'wa_id'])
    return len(queued)


def send_notification_copy(note, category=None):
    """The WhatsApp copy of one :class:`Notification`. Returns True when sent.

    Inside the recipient's window it is plain text (free); outside, the
    template for its category, with their full name in the greeting so a shared
    phone shows whose notification it is.
    """
    from .models import WhatsAppMessage

    person = getattr(note.recipient, 'profile', None)
    number = whatsapp.number_for(note.recipient)
    if not number:
        return False
    thread = thread_for(number)
    name = display_name(note.recipient)
    headline = (note.title or note.verb or 'Metanoia Group').strip()
    detail = (note.summary or note.body or '').strip()
    if note.body_format == note.FORMAT_HTML:
        from django.utils.html import strip_tags
        detail = strip_tags(detail).strip()
    from .services import _absolute
    link = _absolute(note.url) if note.url else _absolute('/myhub/')

    if thread.window_open:
        shared = len(thread.people()) > 1
        text = (f'For *{name}*\n\n' if shared else '') + f'*{headline}*'
        if detail:
            text += f'\n\n{detail[:600]}'
        text += f'\n\n{link}'
        sent, reason, wamid = whatsapp.deliver_text(number, text)
        template = ''
    else:
        template = templates.for_notification(category)
        params = [name, headline, detail[:600] or headline, link]
        sent, reason, wamid = whatsapp.send_template(number, template, params)
        text = f'Hi {name} · *{headline}*\n{detail[:600]}\n{link}'
    log_outbound(number, text, kind=WhatsAppMessage.KIND_NOTIFICATION, person=person, sent=sent,
                 reason=reason, wamid=wamid, thread=thread, template=template)
    return sent


def fetch_media(message):
    """Fetch an inbound file from Meta into ``message.media``. Returns True if stored."""
    if message.media or not message.media_id:
        return bool(message.media)
    data, mime = whatsapp.download_media(message.media_id)
    if data is None:
        return False
    mime = mime or message.media_mime
    extension = mimetypes.guess_extension((mime or '').split(';')[0].strip()) or ''
    name = message.media_filename or f'{message.kind}-{message.pk}{extension}'
    with transaction.atomic():
        message.media.save(name, ContentFile(data), save=False)
        message.media_mime = mime
        message.save(update_fields=['media', 'media_mime'])
    return True


def fetch_pending_media(limit=50):
    """Scheduler job: store inbound media before Meta's copy expires."""
    from .models import WhatsAppMessage

    if not whatsapp.enabled():
        return 'WhatsApp is not configured'
    pending = (WhatsAppMessage.objects.filter(direction=WhatsAppMessage.IN, media='')
               .exclude(media_id='').order_by('created_at')[:limit])
    stored = sum(1 for message in pending if fetch_media(message))
    return f'stored {stored} file(s)'
