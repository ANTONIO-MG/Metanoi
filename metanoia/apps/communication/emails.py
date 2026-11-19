"""E-mail delivery helpers.

All outbound mail (including allauth's) renders from ``communication/email/``
templates extending ``base_email.html``. Brand strings from :mod:`core.branding`
are injected explicitly because context processors don't run for
``render_to_string``.

Registration sends exactly two e-mails, which must not overlap:

- :func:`send_registration_thank_you` — what you signed up for; no money, no personal
  detail, no attachment.
- :func:`send_registration_document` — the invoice or receipt as a PDF; no figures in
  the body, the attachment is the record.

Callers should use :func:`apps.accounts.registration_billing.send_registration_emails`
so the pair always goes out together.

``ANNOUNCEMENT_ATTACH_LIMIT`` is the most a broadcast e-mail carries as real
attachments; above this (or for a video) the e-mail lists the files and links to the
notification instead.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

from core.branding import strings as _strings

from . import senders

logger = logging.getLogger(__name__)


def _base_context(extra=None):
    """The shared template context: brand strings, the string catalogue and ``site_url``.

    ``subject`` is always present (blank by default) so a template that does ``{{
    x|default:subject }}`` never raises when the caller did not pass one — a missing
    *filter argument* is not swallowed by ``string_if_invalid``.
    """
    catalog = _strings()
    ctx = {
        'brand': catalog.get('brand', {}),
        'strings': catalog,
        'site_url': getattr(settings, 'SITE_URL', ''),
        'subject': '',
    }
    if extra:
        ctx.update(extra)
    return ctx


def _email_inline_map(brand):
    """Branding images to embed inline (CID): the signature's tick and logo and the closing banner.

    Inline so they render even when SITE_URL isn't publicly reachable or the
    client blocks remote images.
    """
    return {
        'sig_avatar': 'email/sig-avatar.png',
        'sig_logo': 'email/sig-logo.png',
        'banner': 'email/banner.jpg',
    }


def _attach_inline_image(msg, static_path, cid):
    """Attach a static image as an inline (Content-ID) part; no-op if missing."""
    import mimetypes
    import os
    from email.mime.image import MIMEImage

    from django.contrib.staticfiles import finders
    abspath = finders.find(static_path)
    if not abspath or not os.path.exists(abspath):
        return False
    try:
        with open(abspath, 'rb') as fh:
            data = fh.read()
        subtype = (mimetypes.guess_type(abspath)[0] or 'image/png').split('/')[-1]
        img = MIMEImage(data, _subtype=subtype)
        img.add_header('Content-ID', f'<{cid}>')
        img.add_header('Content-Disposition', 'inline', filename=os.path.basename(abspath))
        msg.attach(img)
        return True
    except Exception:  # pragma: no cover
        return False


def ringed_avatar(user, size=192):
    """``user``'s profile picture in the Brand Studio ring, as PNG bytes; ``None`` if they have none.

    The ring is drawn four times larger and shrunk once, for smooth edges. Its arc runs
    a third of the way round from the top left, as in the Studio artwork (Pillow
    measures angles clockwise from three o'clock).
    """
    import io

    from PIL import Image, ImageDraw, ImageOps

    person = getattr(user, 'profile', None)
    picture = getattr(person, 'profile_picture', None)
    if not picture:
        return None
    try:
        with picture.open('rb') as fh:
            photo = Image.open(fh).convert('RGB')
            photo.load()
    except Exception:  # pragma: no cover - missing or unreadable file
        return None

    k = 4
    big = size * k
    canvas = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    inner = int(big * 0.80)
    face = ImageOps.fit(photo, (inner, inner), Image.LANCZOS, centering=(0.5, 0.4))
    mask = Image.new('L', (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, inner - 1, inner - 1], fill=255)
    offset = (big - inner) // 2
    canvas.paste(face, (offset, offset), mask)
    draw = ImageDraw.Draw(canvas)
    stroke = max(k * 2, big // 60)
    ring = int(big * 0.05)
    draw.ellipse([ring, ring, big - ring, big - ring], outline=(0x0B, 0x5F, 0xC6, 255), width=stroke)
    draw.arc([stroke // 2, stroke // 2, big - stroke // 2, big - stroke // 2], start=-128, end=-6,
             fill=(0x7D, 0xBA, 0x28, 255), width=stroke + k)
    out = io.BytesIO()
    canvas.resize((size, size), Image.LANCZOS).save(out, format='PNG', optimize=True)
    return out.getvalue()


def _attach_inline_bytes(msg, data, cid, filename):
    """Attach PNG ``data`` to ``msg`` as an inline image part with Content-ID ``cid``.
    """
    from email.mime.image import MIMEImage
    img = MIMEImage(data, _subtype='png')
    img.add_header('Content-ID', f'<{cid}>')
    img.add_header('Content-Disposition', 'inline', filename=filename)
    msg.attach(img)


def _footer_for(recipients, automated):
    """``(footer, headers)`` — the Unsubscribe / preferences links and the
    ``List-Unsubscribe`` headers, for an automated e-mail to one known person.

    Personal mail (signed by its author) and mail to an address with no account
    behind it get neither: there is nothing of theirs for the link to change.
    """
    if not automated or len(recipients) != 1:
        return {}, {}
    from django.contrib.auth import get_user_model

    from . import email_prefs
    user = get_user_model().objects.filter(email__iexact=recipients[0], is_active=True).first()
    if user is None:
        return {}, {}
    return email_prefs.links_for(user), email_prefs.headers_for(user)


def send_branded_email(subject, recipients, template, context=None, *, from_email=None,
                       reply_to=None, attachments=None, mailbox=None, author=None, automated=None):
    """Render ``communication/email/<template>.{html,txt}`` and send it.

    ``recipients`` is a string or an iterable of e-mail addresses. The ``.txt``
    body is optional — if the template is missing, the HTML is stripped to text.
    ``attachments`` is an optional iterable of ``(filename, content, mimetype)``
    tuples (e.g. a PDF invoice). ``mailbox`` picks the sending mailbox
    (accounts / info / learninghub); by default it follows the template — see
    :mod:`apps.communication.senders`. Returns ``True`` on success, ``False`` if
    nothing was sent (no recipients / delivery error). Never raises.

    ``automated`` (default: true unless a person ``author`` signs it) puts the
    Unsubscribe button and the e-mail-preferences link at the foot, and the
    one-click ``List-Unsubscribe`` headers on the message.

    Super-student preview accounts have an undeliverable ``.invalid`` address; they are
    dropped here so no broadcast or reminder ever tries to reach one. Branding images
    are embedded inline so they render in every client; the template references them as
    ``{{ inline.logo }}`` etc. (falling back to ``email_asset`` URLs). A person signs
    with their own picture, the mailboxes with the ringed tick. Attachments are
    ``(filename, content, mimetype)`` tuples; ones with empty content are skipped.
    """
    if isinstance(recipients, str):
        recipients = [recipients]
    from core.student_view import is_preview_email
    recipients = [r for r in recipients if r and not is_preview_email(r)]
    if not recipients:
        return False

    ctx = _base_context(context)
    ctx.setdefault('subject', subject)
    inline_map = _email_inline_map(ctx.get('brand') or {})
    ctx['inline'] = {name: f'cid:{name}' for name in inline_map}
    mailbox = mailbox or senders.mailbox_for_template(template)
    from_email = from_email or senders.from_address(mailbox)
    ctx.setdefault('signature', senders.signature(mailbox, author=author))
    own_avatar = None
    if (ctx.get('signature') or {}).get('author') is not None:
        own_avatar = ringed_avatar(ctx['signature']['author'])
    if automated is None:
        automated = (ctx.get('signature') or {}).get('author') is None
    footer, list_headers = _footer_for(recipients, automated)
    ctx.setdefault('footer', footer)

    try:
        html_body = render_to_string(f'communication/email/{template}.html', ctx)
    except Exception:  # pragma: no cover
        logger.exception('Missing/invalid e-mail template %s.html', template)
        return False

    try:
        text_body = render_to_string(f'communication/email/{template}.txt', ctx)
    except Exception:
        text_body = strip_tags(html_body)

    try:
        msg = EmailMultiAlternatives(subject, text_body, from_email, recipients, reply_to=reply_to,
                                     connection=senders.connection(mailbox), headers=list_headers or None)
        msg.attach_alternative(html_body, 'text/html')
        for name, path in inline_map.items():
            if name == 'sig_avatar' and own_avatar:
                _attach_inline_bytes(msg, own_avatar, name, 'signature.png')
                continue
            _attach_inline_image(msg, path, name)
        for att in (attachments or []):
            if att and att[1]:
                msg.attach(*att)
        msg.send()
        return True
    except Exception:  # pragma: no cover
        logger.exception('Failed to send e-mail %r to %s', subject, recipients)
        return False


def send_notification_email(note, category=None):
    """E-mail a single :class:`~apps.communication.models.Notification`.

    ``category`` decides the sending mailbox: learning categories go out from
    learninghub@, the rest from info@.
    """
    email = getattr(note.recipient, 'email', '')
    if not email:
        return False
    return send_branded_email(
        note.title or note.verb or 'Notification',
        email,
        'notification',
        {'notification': note, 'recipient': note.recipient},
        mailbox=senders.mailbox_for_category(category),
    )


def _email_of(user):
    """The user's e-mail address, or ``''``."""
    return getattr(user, 'email', '') if user else ''


def send_general_email(recipients, heading, body, *, action_url=None, action_label=None,
                       subject=None, recipient=None):
    """A general communication / general user e-mail."""
    return send_branded_email(subject or heading, recipients, 'general', {
        'heading': heading, 'body': body, 'action_url': action_url,
        'action_label': action_label, 'recipient': recipient,
    })


def send_event_reminder(user, *, event_title, event_when, event_location='', action_url=''):
    """E-mail ``user`` a reminder about an upcoming event."""
    return send_branded_email(f'Upcoming: {event_title}', _email_of(user), 'event-upcoming', {
        'recipient': user, 'event_title': event_title, 'event_when': event_when,
        'event_location': event_location, 'action_url': action_url,
    })


def send_session_scheduled(user, *, session_title, session_when='', join_url=''):
    """E-mail ``user`` that a live session has been scheduled, with its join link."""
    return send_branded_email(f'Session scheduled: {session_title}', _email_of(user),
                              'session-scheduled', {
        'recipient': user, 'session_title': session_title,
        'session_when': session_when, 'join_url': join_url,
    })


def send_invoice_email(invoice, pay_url):
    """E-mail an invoice to its customer with the pay link."""
    return send_branded_email(f'Invoice {invoice.number} — payment requested',
                              _email_of(invoice.customer), 'invoice',
                              {'invoice': invoice, 'pay_url': pay_url})


def send_invoice_reminder(invoice, pay_url, *, stage='manual'):
    """A polite nudge about an unpaid invoice, with the pay link."""
    if stage == 'before_3':
        subject = f'Reminder: invoice {invoice.number} is due in 3 days'
    elif stage == 'due':
        subject = f'Invoice {invoice.number} is due today'
    elif stage == 'after_7':
        subject = f'Invoice {invoice.number} is overdue'
    else:
        subject = f'Reminder: invoice {invoice.number}'
    return send_branded_email(subject, _email_of(invoice.customer), 'invoice-reminder',
                              {'invoice': invoice, 'pay_url': pay_url, 'stage': stage})


def send_estimate_email(estimate, view_url):
    """E-mail an estimate to its customer with the link to view it."""
    return send_branded_email(f'Estimate {estimate.number} from us',
                              _email_of(estimate.customer), 'estimate',
                              {'estimate': estimate, 'view_url': view_url})


def send_purchase_email(invoice, pay_url=''):
    """E-mail the order confirmation for a purchase invoice."""
    return send_branded_email(f'Order confirmed — {invoice.number}',
                              _email_of(invoice.customer), 'purchase',
                              {'invoice': invoice, 'pay_url': pay_url})


def send_payment_made(invoice):
    """The receipt, with the receipt PDF attached as the record of payment.

    The PDF is best effort: if it will not render, the e-mail still goes — a
    buyer who paid must hear that they paid.
    """
    attachments = []
    try:
        from apps.finance.pdf import render_receipt_pdf
        pdf = render_receipt_pdf(invoice)
        if pdf:
            attachments.append((f'Receipt-{invoice.number}.pdf', pdf, 'application/pdf'))
    except Exception:  # pragma: no cover
        logger.exception('receipt PDF failed for %s', invoice.number)
    return send_branded_email(f'Receipt — Invoice {invoice.number} paid',
                              _email_of(invoice.customer), 'payment-made', {'invoice': invoice},
                              attachments=attachments)


def send_welcome_to_class(user, *, course=None, modules=None, action_url=''):
    """E-mail ``user`` a welcome to their course and modules."""
    return send_branded_email('Welcome to your course', _email_of(user), 'welcome-class', {
        'recipient': user, 'course': course,
        'modules': modules or [], 'action_url': action_url,
    })


def send_registration_thank_you(person, *, institution='', programme='', modules=(),
                                login_url='', paid=False):
    """E-mail 1 of 2 — thank you, the summary, and how to start.

    Deliberately carries no personal detail (no address, no phone, no ID) and no
    money at all. See ``registration-thank-you.html``.
    """
    user = getattr(person, 'user', None)
    if not _email_of(user):
        return False
    return send_branded_email(
        'Thank you for registering — your Metanoia Group summary',
        _email_of(user), 'registration-thank-you', {
            'person': person,
            'first_name': person.first_name or user.get_short_name() or user.get_username(),
            'institution': institution, 'programme': programme,
            'modules': list(modules), 'login_url': login_url, 'paid': paid,
        })


def send_registration_document(person, invoice, *, paid=False, trial_ends=None,
                               trial_days=7, pay_url='', attachments=None,
                               attachment_label='document'):
    """E-mail 2 of 2 — the invoice (free week, paying by EFT) or the receipt.

    The body says only what the reader has to *do*: on the unpaid branch, what
    the trial period is and by when to settle; on the paid branch, that it is
    settled. Every figure lives in the attached PDF, so there is exactly one
    authoritative copy of the numbers.
    """
    user = getattr(person, 'user', None)
    if not _email_of(user):
        return False
    subject = ('Your receipt — Metanoia Group' if paid else
               'Your invoice — Metanoia Group')
    return send_branded_email(
        subject, _email_of(user), 'registration-document', {
            'person': person,
            'first_name': person.first_name or user.get_short_name() or user.get_username(),
            'invoice': invoice, 'paid': paid,
            'trial_ends': trial_ends, 'trial_days': trial_days,
            'pay_url': pay_url, 'attachment_label': attachment_label,
        }, attachments=attachments)


def send_trial_expiry_reminder(person, invoice, *, days_left=1, trial_ends=None,
                               pay_url='', modules=(), attachments=None):
    """The nudge a day before a free week runs out on an unpaid invoice."""
    user = getattr(person, 'user', None)
    if not _email_of(user):
        return False
    return send_branded_email(
        f'Your free week ends tomorrow — invoice {invoice.number} is still open',
        _email_of(user), 'trial-ending', {
            'person': person,
            'first_name': person.first_name or user.get_short_name() or user.get_username(),
            'invoice': invoice, 'days_left': days_left, 'trial_ends': trial_ends,
            'pay_url': pay_url, 'modules': list(modules),
        }, attachments=attachments)


def send_password_changed(user, *, login_url=''):
    """Tell ``user`` their password was changed, with the sign-in link."""
    return send_branded_email('Your password was changed', _email_of(user),
                              'password-changed', {'account': user, 'login_url': login_url})


def email_announcement(announcement, recipients):
    """E-mail an :class:`~apps.communication.models.Announcement` to each user in ``recipients``, one message each."""
    sent_any = False
    for user in recipients:
        email = getattr(user, 'email', '')
        if not email:
            continue
        if send_branded_email(
            announcement.title,
            email,
            'announcement',
            {'announcement': announcement, 'recipient': user},
        ):
            sent_any = True
    return sent_any


ANNOUNCEMENT_ATTACH_LIMIT = 10 * 1024 * 1024


def announcement_email_files(announcement):
    """``(filename, content, mimetype)`` tuples for a broadcast's e-mail copy.

    Read once per broadcast and reused for every recipient. Videos are never
    attached, and nothing is attached if the documents together exceed
    :data:`ANNOUNCEMENT_ATTACH_LIMIT` — mail servers refuse large messages.
    """
    import mimetypes

    docs = [a for a in announcement.attachments.all() if a.kind != a.KIND_VIDEO]
    if not docs or sum(a.size or 0 for a in docs) > ANNOUNCEMENT_ATTACH_LIMIT:
        return []
    files = []
    for att in docs:
        try:
            with att.file.open('rb') as fh:
                content = fh.read()
        except Exception:  # pragma: no cover - a missing file just isn't attached
            logger.warning('Announcement attachment %s could not be read', att.pk)
            continue
        name = att.original_name or att.file.name.rsplit('/', 1)[-1]
        files.append((name, content, mimetypes.guess_type(name)[0] or 'application/octet-stream'))
    return files


def email_announcement_copy(announcement, note, files=None):
    """E-mail one recipient their copy of a broadcast.

    The button opens *their* notification, where the pictures, the video and
    every attachment are shown — the e-mail cannot play a video.
    """
    from django.urls import reverse

    user = note.recipient
    if not _email_of(user):
        return False
    base = getattr(settings, 'SITE_URL', '').rstrip('/')
    open_url = base + reverse('communication:notification-detail', args=[note.pk])
    link_url = announcement.url or ''
    if link_url.startswith('/'):
        link_url = base + link_url
    return send_branded_email(announcement.title, _email_of(user), 'announcement', context={
        'announcement': announcement, 'recipient': user, 'open_url': open_url, 'link_url': link_url,
        'attachment_list': list(announcement.attachments.all()),
        'attached': bool(files),
    }, attachments=files, author=announcement.sender)
