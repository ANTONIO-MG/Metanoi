"""What a finished registration sends the student — e-mail, and WhatsApp.

**Exactly two e-mails**, non-overlapping: (1) thank-you, with no money or
personal detail; (2) the invoice (free week/EFT) or receipt (card), with the PDF
as the single authoritative copy of the figures. :func:`deliver_registration`
is the one entry point, so the count cannot drift again.

WhatsApp also gets the invoice PDF, unpaid branch only.

Nothing here may break registration: a failed channel is reported to the
student, and an unconfigured WhatsApp is silently skipped.
"""
import datetime
import logging

from django.utils import timezone

from core import whatsapp
from core.utils import absolute_url

logger = logging.getLogger(__name__)

TRIAL_DAYS = 7
"""How long the free week lasts, in days. Mirrors ``ModuleEnrolment.TRIAL_DAYS``."""


def whatsapp_enabled():
    """True when WhatsApp is configured on this installation."""
    return whatsapp.enabled()


def send_invoice_whatsapp(invoice, person):
    """Send the invoice PDF to the student's WhatsApp number.

    Returns ``(sent: bool, reason: str)``; ``reason`` explains a ``False``.

    A new registrant has not written to us, so this is usually outside
    WhatsApp's 24-hour window: the invoice then goes as the approved template
    with the PDF as its header. Inside the window a plain document is enough.
    """
    if not whatsapp.enabled():
        return False, 'WhatsApp is not configured on this installation'

    number = whatsapp.number_for(person)
    if not number:
        return False, 'no usable WhatsApp number on the contact details'

    try:
        from apps.finance.pdf import render_invoice_pdf
        pdf_bytes = render_invoice_pdf(invoice)
        if hasattr(pdf_bytes, 'read'):
            pdf_bytes = pdf_bytes.read()
    except Exception:                              # pragma: no cover
        logger.exception('finance: could not render invoice %s as PDF', invoice.number)
        return False, 'the invoice PDF could not be produced'

    from django.core.files.base import ContentFile

    from apps.communication.whatsapp_inbox import log_outbound, thread_for
    from core.utils import display_name
    from core.whatsapp_templates import INVOICE

    filename = f'{invoice.number}.pdf'
    amount = f'R{invoice.total:,.2f}'
    caption = (f'Your Metanoia Group invoice {invoice.number} — {amount}. '
               'Your free week has started; pay by EFT before it ends and your modules '
               'stay open.')
    thread = thread_for(number)
    template = ''
    if thread.window_open:
        sent, reason, wamid = whatsapp.deliver_media(number, pdf_bytes, filename, caption=caption)
    else:
        media_id = whatsapp.upload_document(pdf_bytes, filename)
        if not media_id:
            sent, reason, wamid = False, 'WhatsApp did not accept the file', ''
        else:
            template = INVOICE
            sent, reason, wamid = whatsapp.send_template(
                number, INVOICE, [display_name(person.user), invoice.number, amount],
                header_document=(media_id, filename))
    log_outbound(number, caption, kind='document', person=person, sent=sent, reason=reason,
                 wamid=wamid, thread=thread, template=template, media=ContentFile(pdf_bytes),
                 mime='application/pdf', filename=filename)
    return sent, reason


def send_invoice_email(invoice):
    """The invoice by e-mail. Returns ``(sent, reason)`` like its sibling."""
    try:
        from apps.finance import emails
        emails.send_invoice_email(invoice)
        return True, ''
    except Exception:                              # pragma: no cover - SMTP
        logger.exception('finance: invoice %s e-mail failed', invoice.number)
        return False, 'the e-mail could not be sent'


def _module_labels(enrolments):
    """Readable module names from a list of ModuleEnrolment rows."""
    labels = []
    for e in enrolments or []:
        offering = getattr(e, 'programme_module', None)
        if offering is None:
            continue
        labels.append(getattr(offering, 'display_name', None) or str(offering))
    return labels


def send_registration_emails(person, invoice, enrolments, *, paid, programme=None,
                             trial_ends=None):
    """The **two** registration e-mails: thank-you, then invoice or receipt with
    the PDF. A PDF that fails to render is just not attached; the e-mails still go.

    Returns ``{'thank_you': bool, 'document': bool}``. Never raises.
    """
    sent = {'thank_you': False, 'document': False}
    try:
        from apps.communication import emails

        institution = getattr(getattr(programme, 'institution', None), 'name', '')
        programme_label = (getattr(programme, 'display_name', None)
                           or (str(programme) if programme else ''))

        sent['thank_you'] = bool(emails.send_registration_thank_you(
            person,
            institution=institution,
            programme=programme_label,
            modules=_module_labels(enrolments),
            login_url=absolute_url('/myhub/'),
            paid=paid,
        ))
    except Exception:                              # pragma: no cover
        logger.exception('registration: thank-you e-mail failed for invoice %s',
                         getattr(invoice, 'number', '?'))

    try:
        from apps.communication import emails
        from apps.finance import pdf as finance_pdf

        if paid:
            content = finance_pdf.render_receipt_pdf(invoice)
            filename, label = f'receipt-{invoice.number}.pdf', 'receipt'
        else:
            content = finance_pdf.render_invoice_pdf(invoice)
            filename, label = f'invoice-{invoice.number}.pdf', 'invoice'

        attachments = [(filename, content, 'application/pdf')] if content else []
        if not content:
            logger.warning('registration: %s PDF unavailable for %s — sending the e-mail '
                           'without it', label, invoice.number)

        pay_url = ''
        try:
            pay_url = absolute_url(invoice.get_pay_url())
        except Exception:                          # pragma: no cover - defensive
            pay_url = ''

        sent['document'] = bool(emails.send_registration_document(
            person, invoice, paid=paid, trial_ends=trial_ends, trial_days=TRIAL_DAYS,
            pay_url=pay_url, attachments=attachments, attachment_label=label,
        ))
    except Exception:                              # pragma: no cover
        logger.exception('registration: %s e-mail failed for invoice %s',
                         'receipt' if paid else 'invoice',
                         getattr(invoice, 'number', '?'))
    return sent


def trial_end_date(now=None):
    """When a free week started now would run out."""
    return ((now or timezone.now()) + datetime.timedelta(days=TRIAL_DAYS)).date()


def deliver_registration(person, invoice, enrolments, *, paid, programme=None,
                         trial_ends=None):
    """Everything a finished registration sends: two e-mails, plus WhatsApp
    (unpaid only). The **only** entry point registration should call.

    Returns ``{'sent': [...], 'failed': [...], 'trial_ends': date}`` naming the
    *channels* to report back to the student. Never raises. An installation
    without WhatsApp set up is not reported as a failed channel — the student
    never expected it; any other WhatsApp failure is.
    """
    if trial_ends is None and not paid:
        trial_ends = trial_end_date()

    sent, failed = [], []

    emails_sent = send_registration_emails(
        person, invoice, enrolments, paid=paid, programme=programme,
        trial_ends=trial_ends)
    (sent if emails_sent['document'] else failed).append('e-mail')

    if not paid:
        ok, reason = send_invoice_whatsapp(invoice, person)
        if ok:
            sent.append('WhatsApp')
        elif 'not configured' not in reason:
            failed.append('WhatsApp')

    return {'sent': sent, 'failed': failed, 'trial_ends': trial_ends}
