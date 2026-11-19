"""System documents the composer can attach by reference.

Any page can open a ready-to-send message with one link::

    {% url 'mailbox:compose' %}?attach=invoice:42

The ``invoice`` provider renders the invoice PDF, addresses the message to the
customer and writes a subject and a short covering note with the pay link. The
sender sees all of it in the composer and can change anything before sending.

A provider takes ``(request, ref)`` and returns an :class:`Attachable`, or
raises :class:`PermissionDenied` / :class:`LookupError`. To add one, decorate a
function with ``@provider('kind')``.

``PROVIDERS`` maps a kind to its provider function and ``AFTER_SEND`` maps a
kind to the hook registered with :func:`sent_hook`.
"""

from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.utils.html import escape

from core.roles import role_flags
from core.utils import absolute_url

PROVIDERS = {}
AFTER_SEND = {}


@dataclass
class Attachable:
    """What a provider hands the composer.

    ``label`` is shown as the composer's context chip; ``files`` is a list of
    ``(filename, bytes, content_type)`` tuples; ``to``, ``subject`` and
    ``body_html`` prefill the message.
    """

    label: str
    files: list = field(default_factory=list)
    to: str = ''
    subject: str = ''
    body_html: str = ''


def provider(kind):
    """Register the decorated ``fn(request, ref)`` as the provider for ``kind``."""
    def register(fn):
        """Store ``fn`` in :data:`PROVIDERS` and return it unchanged."""
        PROVIDERS[kind] = fn
        return fn
    return register


def sent_hook(kind):
    """Register ``fn(ref)`` to run once a message carrying ``kind:ref`` has gone."""
    def register(fn):
        """Store ``fn`` in :data:`AFTER_SEND` and return it unchanged."""
        AFTER_SEND[kind] = fn
        return fn
    return register


def after_send(spec):
    """Run the sent hook for ``spec`` (``'kind:ref'``), if its kind has one."""
    kind, _, ref = (spec or '').partition(':')
    fn = AFTER_SEND.get(kind)
    if fn and ref:
        fn(ref)


def resolve(request, spec):
    """``'invoice:42'`` → :class:`Attachable`."""
    kind, _, ref = (spec or '').partition(':')
    fn = PROVIDERS.get(kind)
    if fn is None or not ref:
        raise LookupError(f'Unknown attachment "{spec}".')
    return fn(request, ref)


def _require_staff(request):
    """Raise :class:`PermissionDenied` unless the user is admin staff."""
    if not role_flags(request)['is_admin_staff']:
        raise PermissionDenied


def _money(value):
    """Format ``value`` as rands with space thousands separators (``R 1 234.50``)."""
    return f'R {value:,.2f}'.replace(',', ' ')


def _first_name(user):
    """The name to greet ``user`` by, falling back to ``'there'``."""
    profile = getattr(user, 'profile', None)
    return (getattr(profile, 'first_name', '') or user.first_name or '').strip() or 'there'


def _address(user):
    """``Name <email>`` for ``user``, or just the e-mail address."""
    name = (user.get_full_name() or '').strip()
    return f'{name} <{user.email}>' if name and user.email else (user.email or '')


@provider('invoice')
def invoice(request, ref):
    """Invoice ``ref`` as a PDF, addressed to the customer with the pay link and EFT reference."""
    from apps.finance import pdf
    from apps.finance.models import Invoice
    _require_staff(request)
    inv = Invoice.objects.select_related('customer').prefetch_related('items').filter(pk=ref).first()
    if inv is None:
        raise LookupError('That invoice no longer exists.')
    data = pdf.render_invoice_pdf(inv)
    if not data:
        raise LookupError('The invoice PDF could not be generated.')
    pay_url = absolute_url(inv.get_pay_url())
    due = f' by <strong>{inv.due_date:%d %B %Y}</strong>' if inv.due_date else ''
    body = (
        f'<p>Hi {escape(_first_name(inv.customer))},</p>'
        f'<p>Please find attached invoice <strong>{escape(inv.number)}</strong> for '
        f'<strong>{_money(inv.balance if inv.amount_paid else inv.total)}</strong>{due}.</p>'
        f'<p>You can pay securely online here: <a href="{escape(pay_url)}">{escape(pay_url)}</a><br>'
        f'For an EFT, please use the reference <strong>{escape(inv.payment_reference)}</strong>.</p>'
        '<p>Kind regards,</p>'
    )
    return Attachable(label=f'Invoice {inv.number}', files=[(f'Invoice-{inv.number}.pdf', data, 'application/pdf')],
                      to=_address(inv.customer), subject=f'Invoice {inv.number} from Metanoia Group',
                      body_html=body)


@provider('receipt')
def receipt(request, ref):
    """The receipt PDF for invoice ``ref``, with a short thank-you note."""
    from apps.finance import pdf
    from apps.finance.models import Invoice
    _require_staff(request)
    inv = Invoice.objects.select_related('customer').prefetch_related('items', 'payments').filter(pk=ref).first()
    if inv is None:
        raise LookupError('That invoice no longer exists.')
    data = pdf.render_receipt_pdf(inv)
    if not data:
        raise LookupError('The receipt PDF could not be generated.')
    body = (f'<p>Hi {escape(_first_name(inv.customer))},</p>'
            f'<p>Thank you for your payment. Your receipt for invoice <strong>{escape(inv.number)}</strong> '
            'is attached.</p><p>Kind regards,</p>')
    return Attachable(label=f'Receipt {inv.number}', files=[(f'Receipt-{inv.number}.pdf', data, 'application/pdf')],
                      to=_address(inv.customer), subject=f'Receipt for invoice {inv.number}', body_html=body)


@provider('estimate')
def estimate(request, ref):
    """Estimate ``ref`` as a PDF, with its validity date and the link to accept it online."""
    from apps.finance import pdf
    from apps.finance.models import Estimate
    _require_staff(request)
    est = Estimate.objects.select_related('customer').prefetch_related('items').filter(pk=ref).first()
    if est is None:
        raise LookupError('That estimate no longer exists.')
    data = pdf.render_estimate_pdf(est)
    if not data:
        raise LookupError('The estimate PDF could not be generated.')
    link = absolute_url(est.get_public_url())
    valid = f' It is valid until <strong>{est.valid_until:%d %B %Y}</strong>.' if est.valid_until else ''
    body = (f'<p>Hi {escape(_first_name(est.customer))},</p>'
            f'<p>Please find attached estimate <strong>{escape(est.number)}</strong> for '
            f'<strong>{_money(est.total)}</strong>.{valid}</p>'
            f'<p>You can view and accept it online: <a href="{escape(link)}">{escape(link)}</a></p>'
            '<p>Kind regards,</p>')
    return Attachable(label=f'Estimate {est.number}', files=[(f'Estimate-{est.number}.pdf', data, 'application/pdf')],
                      to=_address(est.customer), subject=f'Estimate {est.number} from Metanoia Group',
                      body_html=body)


@sent_hook('invoice')
def _invoice_sent(ref):
    """As the finance "Send invoice" button does: draft → sent, and stamp sent_at."""
    from apps.finance import documents
    from apps.finance.models import Invoice
    inv = Invoice.objects.filter(pk=ref).first()
    if inv is not None:
        documents.mark_sent(inv)


@sent_hook('estimate')
def _estimate_sent(ref):
    """Mark estimate ``ref`` sent: a draft becomes sent, and ``sent_at`` is stamped."""
    from django.utils import timezone

    from apps.finance.models import Estimate
    est = Estimate.objects.filter(pk=ref).first()
    if est is not None:
        if est.status == Estimate.STATUS_DRAFT:
            est.status = Estimate.STATUS_SENT
        est.sent_at = timezone.now()
        est.save(update_fields=['status', 'sent_at', 'updated_at'])
