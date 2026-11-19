"""Finance service layer: turn a shop cart into an invoice, and settle payments.

Kept separate from the views so the checkout flow and the PayFast callbacks
(ITN + return) share one idempotent settle path.
"""

import logging

from django.utils import timezone

from . import models

logger = logging.getLogger('apps')


def create_invoice_from_order(order, *, created_by=None, status=None):
    """Create a finance Invoice from a shop Order (cart), copying line items
    and linking each item back to its product. Returns the Invoice.

    Lines are copied gross, as checkout froze them
    (:func:`apps.shop.checkout.freeze_quote`); what the cart took off them becomes
    the invoice's one discount, so the invoice still shows the saving and charges
    exactly the cart total.
    """
    invoice = models.Invoice.objects.create(
        customer=order.buyer,
        order=order,
        created_by=created_by or order.buyer,
        status=status or models.Invoice.STATUS_SENT,
    )
    from decimal import Decimal
    off, labels = Decimal('0'), []
    for order_item in order.items.select_related('product', 'variant').all():
        models.InvoiceItem.objects.create(
            invoice=invoice,
            product=order_item.product,
            description=order_item.description[:255],
            quantity=order_item.quantity,
            unit_price=order_item.unit_price,
        )
        if order_item.discount_amount:
            off += Decimal(str(order_item.discount_amount))
            label = (order_item.discount_label or '').strip()
            if label and label not in labels:
                labels.append(label)
    if off:
        invoice.discount_amount = off
        invoice.discount_label = (' + '.join(labels) or 'Discount')[:120]
        invoice.discount_auto = False
        invoice.save(update_fields=['discount_amount', 'discount_label', 'discount_auto', 'updated_at'])
    if getattr(order, 'shipping_total', 0):
        models.InvoiceItem.objects.create(
            invoice=invoice, description='Delivery', quantity=1,
            unit_price=order.shipping_total)
    invoice.recalc_total()
    return invoice


def settle_free(invoice):
    """Close an invoice that costs nothing — a fully-discounted or free order.

    There is no money to collect, so no payment row is written and nothing goes
    to PayFast; the invoice is simply marked paid and the usual paid-invoice
    side effects run (order settled, downloads unlocked, receipt sent).
    """
    if invoice.total > 0 or invoice.status == models.Invoice.STATUS_PAID:
        return False
    invoice.status = models.Invoice.STATUS_PAID
    invoice.save(update_fields=['status', 'updated_at'])
    _on_invoice_paid(invoice)
    return True


def settle_payment(invoice, amount, *, gateway=models.InvoicePayment.GATEWAY_MANUAL,
                   method='online', gateway_ref='', raw=None, reference='', recorded_by=None):
    """Record a COMPLETED payment against ``invoice`` (idempotent on
    ``gateway_ref`` for gateway callbacks). Refreshes status and, when the
    invoice becomes fully paid, e-mails a receipt and settles the linked order.
    Returns the InvoicePayment (existing one if already recorded).

    Everything the payment changes is journalled (:mod:`apps.finance.undo`);
    ``payment.undo_action`` is the UndoAction that reverses it.
    """
    if gateway_ref:
        existing = invoice.payments.filter(gateway_ref=gateway_ref).first()
        if existing:
            return existing

    from . import undo
    with undo.track_effects(invoice) as effects:
        payment = _settle(invoice, amount, gateway=gateway, method=method,
                          gateway_ref=gateway_ref, raw=raw, reference=reference,
                          recorded_by=recorded_by)
    payment.undo_action = undo.record_payment(payment, payment.proofs.first(),
                                              effects=effects, by=recorded_by)
    return payment


def _settle(invoice, amount, *, gateway, method, gateway_ref, raw, reference, recorded_by):
    """Record a completed payment on ``invoice`` and run the paid side effects.

    Every payment leaves a proof: for a gateway payment the reference is the
    evidence, filed in the same payment history as uploaded deposit slips.
    """
    was_paid = invoice.status == models.Invoice.STATUS_PAID
    payment = models.InvoicePayment.objects.create(
        invoice=invoice, amount=amount, method=method, gateway=gateway,
        status=models.InvoicePayment.STATUS_COMPLETED, gateway_ref=gateway_ref,
        reference=reference, raw=raw or {}, paid_at=timezone.now(),
        recorded_by=recorded_by if getattr(recorded_by, 'pk', None) else None,
    )
    invoice.refresh_status()
    invoice.refresh_from_db()

    try:
        from . import access
        access.record_gateway_proof(invoice, payment)
    except Exception:  # pragma: no cover - a proof must never break settlement
        logger.exception('finance: could not record proof of payment for %s', invoice.number)

    if invoice.status == models.Invoice.STATUS_PAID and not was_paid:
        _on_invoice_paid(invoice)
    return payment


def _is_registration_invoice(invoice):
    """True when this invoice is the one raised by a student's registration.

    Told apart by the module enrolments that point at it. Registration owns its
    own e-mails (see :mod:`apps.accounts.registration_billing`) and sends a
    receipt of its own with the PDF attached, so the generic one below would be
    a third message repeating the figures.
    """
    try:
        from apps.learning.models import ModuleEnrolment
        return ModuleEnrolment.objects.filter(invoice_uid=invoice.public_id).exists()
    except Exception:  # pragma: no cover - defensive; never block settlement
        logger.exception('finance: could not classify invoice %s', invoice.number)
        return False


def _on_invoice_paid(invoice):
    """Side effects when an invoice flips to fully paid: receipt e-mail + mark
    the originating cart/order paid.

    A registration invoice gets its receipt from the accounts app instead (the
    ``invoice_paid`` receiver in :mod:`apps.accounts.signals`), so no duplicate is
    sent. Finally ``invoice_paid`` is sent so other apps can react (for example
    accounts enrols the student for a registration invoice); receivers must never
    break settlement, so their failures are logged.
    """
    try:
        from . import emails
        if not _is_registration_invoice(invoice):
            emails.send_receipt_email(invoice)
    except Exception:  # pragma: no cover
        logger.exception('finance: receipt e-mail failed for %s', invoice.number)
    try:
        order = invoice.order
        if order is not None:
            from apps.shop.models import Order
            order.status = Order.STATUS_PAID
            order.checked_out = True
            order.save(update_fields=['status', 'checked_out', 'updated_at'])
    except Exception:  # pragma: no cover
        logger.exception('finance: could not settle order for %s', invoice.number)
    try:
        from .dispatch import invoice_paid
        invoice_paid.send(sender=models.Invoice, invoice=invoice)
    except Exception:  # pragma: no cover
        logger.exception('finance: invoice_paid receivers failed for %s', invoice.number)
