"""Sending: a :class:`~apps.mailbox.models.MailDraft` out over the mailbox's SMTP.

After a successful send the copy is filed in Sent (unless the server does that
itself — see ``MailServer.server_saves_sent``), the original of a reply is
flagged answered, and the draft is deleted.

``BODY_WRAP`` wraps every HTML body in the Metanoia font, Montserrat, falling
back to Outlook's Aptos where it is not installed.

``RECIPIENTS_PER_SEND`` exists because mail servers cap the recipients of one
SMTP transaction (often at 100). A long Bcc list (a class list pasted in) goes
out as several transactions of the same message, each to the next slice of
Bcc addresses; To and Cc ride on the first one only, so nobody gets two copies.
"""

import logging
from email.utils import getaddresses, make_msgid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.validators import validate_email

from . import imap, parse
from .models import SECURITY_SSL, SECURITY_STARTTLS, MailFolder, MailServer

logger = logging.getLogger('apps')

BODY_WRAP = ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
             '<div style="font-family:Montserrat,Aptos,Calibri,Helvetica,Arial,sans-serif;font-size:11pt;color:#242424">'
             '{body}</div></body></html>')


class SendError(Exception):
    """The message could not be sent. The text is shown to the person sending."""


def split_addresses(value):
    """``"A <a@x>; b@y"`` → ``['A <a@x>', 'b@y']``, raising on a bad address."""
    value = (value or '').replace(';', ',').replace('\n', ',')
    out, bad = [], []
    for name, address in getaddresses([value]):
        address = address.strip()
        if not address:
            continue
        try:
            validate_email(address)
        except ValidationError:
            bad.append(address)
            continue
        out.append(f'{name} <{address}>' if name else address)
    if bad:
        raise SendError('These addresses don’t look right: ' + ', '.join(bad))
    return out


def smtp_connection(account):
    """An unopened SMTP connection for ``account``, after vetting any per-mailbox host override.

    The backend can be swapped with the ``MAILBOX_EMAIL_BACKEND`` setting.
    Raises :class:`SendError` when no SMTP server is set or the host is not public.
    """
    host, port, security, username = account.settings_for('smtp')
    if not host:
        raise SendError('No SMTP server is set up yet. An administrator can add it under Mail settings.')
    from .hosts import UnsafeHost, check_override
    try:
        check_override(account, 'smtp')
    except UnsafeHost as exc:
        raise SendError(str(exc)) from exc
    return get_connection(
        backend=getattr(settings, 'MAILBOX_EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend'),
        host=host, port=port, username=username, password=account.get_password(),
        use_tls=security == SECURITY_STARTTLS, use_ssl=security == SECURITY_SSL,
        timeout=30, fail_silently=False)


def build_message(draft, connection=None):
    """The :class:`~django.core.mail.EmailMultiAlternatives` for ``draft``.

    The body is cleaned to the incoming-mail allow-list, threading headers are
    set for a reply, and the signature's pictures travel inside the message as
    inline parts, as the automated mail's do. Raises :class:`SendError` for no
    recipients, a bad address, or attachments over ``MailServer.max_message_mb``.
    """
    account = draft.account
    to, cc, bcc = split_addresses(draft.to), split_addresses(draft.cc), split_addresses(draft.bcc)
    if not (to or cc or bcc):
        raise SendError('Add at least one recipient.')

    body_html = parse.clean_compose_html(draft.body_html)
    domain = account.email_address.partition('@')[2] or None
    headers = {'Message-ID': make_msgid(domain=domain), 'X-Mailer': 'Metanoia Mail'}
    if draft.in_reply_to:
        headers['In-Reply-To'] = draft.in_reply_to
        headers['References'] = (draft.references + ' ' + draft.in_reply_to).strip()

    msg = EmailMultiAlternatives(
        subject=draft.subject or '', body=parse.html_to_text(body_html),
        from_email=account.from_header, to=to, cc=cc, bcc=bcc, connection=connection, headers=headers)
    from email.mime.image import MIMEImage

    from . import signatures
    body_html, pictures = signatures.embed_images(body_html)
    msg.attach_alternative(BODY_WRAP.format(body=body_html), 'text/html')
    for cid, data, subtype, filename in pictures:
        img = MIMEImage(data, _subtype=subtype)
        img.add_header('Content-ID', f'<{cid}>')
        img.add_header('Content-Disposition', 'inline', filename=filename)
        msg.attach(img)

    limit = MailServer.get().max_message_mb * 1024 * 1024
    total = 0
    for att in draft.attachments.all():
        with att.file.open('rb') as fh:
            data = fh.read()
        total += len(data)
        msg.attach(att.filename, data, att.content_type or 'application/octet-stream')
    if total > limit:
        raise SendError(f'Attachments come to {total / 1048576:.1f} MB; the limit is '
                        f'{limit // 1048576} MB. Share large files as a link instead.')
    return msg


RECIPIENTS_PER_SEND = 90


def _send_in_batches(msg, connection):
    """Send ``msg`` in transactions of at most :data:`RECIPIENTS_PER_SEND` recipients.

    ``msg.recipients()`` lists To and Cc before Bcc, so they all fall in the
    first batch. The headers (From, To, Cc, subject) are the same on every
    copy; only the envelope recipients change.
    """
    everyone = msg.recipients()
    if len(everyone) <= RECIPIENTS_PER_SEND:
        msg.send()
        return
    batches = [everyone[i:i + RECIPIENTS_PER_SEND] for i in range(0, len(everyone), RECIPIENTS_PER_SEND)]
    connection.open()
    try:
        for batch in batches:
            msg.recipients = (lambda b=batch: b)
            connection.send_messages([msg])
    finally:
        del msg.recipients
        connection.close()


def send_draft(draft):
    """Send ``draft``. Raises :class:`SendError` with a reason the sender can act on.

    Once the mail has gone, the after-send hook of any attached system
    document runs and the copy is filed; both are best effort and only
    logged on failure. The draft is then deleted.
    """
    account = draft.account
    try:
        connection = smtp_connection(account)
        msg = build_message(draft, connection)
        _send_in_batches(msg, connection)
    except SendError:
        raise
    except Exception as exc:
        logger.warning('mailbox: send from %s failed: %s', account, exc)
        raise SendError(_friendly_smtp_error(exc)) from exc

    raw = msg.message().as_bytes()
    if draft.context_ref:
        try:
            from . import attachables
            attachables.after_send(draft.context_ref)
        except Exception:
            logger.exception('mailbox: after-send hook for %s failed', draft.context_ref)
    try:
        _after_send(draft, raw)
    except Exception:
        logger.exception('mailbox: post-send bookkeeping failed for %s', account)
    draft.delete()
    return msg


def _after_send(draft, raw):
    """File the sent copy in Sent, flag a replied-to original answered, and re-sync Sent."""
    from . import sync
    account = draft.account
    sent = account.folder(MailFolder.ROLE_SENT)
    source = draft.source
    if not account.has_password:
        return
    with imap.connect(account) as client:
        if sent is not None and not MailServer.get().server_saves_sent:
            client.append(sent.path, raw)
        if source is not None and draft.mode in (draft.MODE_REPLY, draft.MODE_REPLY_ALL):
            client.select(source.folder.path, readonly=False)
            client.set_flag(source.uid, r'\Answered')
            type(source).objects.filter(pk=source.pk).update(is_answered=True)
    if sent is not None:
        sync.run_in_background(sync.sync_account, account, folder=sent, notify=False)


def _friendly_smtp_error(exc):
    """A message the sender can act on for an SMTP or network exception."""
    import smtplib
    import socket
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'The mail server rejected the mailbox password. Update it under Mail settings.'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'The mail server refused these recipients: ' + ', '.join(exc.recipients)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return 'The mail server would not send as this address.'
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return 'Could not reach the outgoing mail server. Try again in a moment.'
    return f'The message could not be sent ({exc}).'
