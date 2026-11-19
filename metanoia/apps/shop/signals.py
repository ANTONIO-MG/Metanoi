"""Signal handlers for the shop app.

* Keep :attr:`Order.total` in sync with its line items.
* Flip an order to *paid* once completed payments cover its balance.
* Record create/update/delete of products and orders in the shared
  :class:`apps.accounts.models.ActivityLog` audit trail.

Connected in :meth:`apps.shop.apps.ShopConfig.ready`.
"""

import logging

from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from apps.finance.dispatch import invoice_paid as finance_invoice_paid

from .models import Order, OrderItem, Payment, Product

logger = logging.getLogger('apps')


def _log(action, description):
    """Best-effort audit log entry (never breaks the request if it fails)."""
    try:
        from apps.accounts.models import ActivityLog
        ActivityLog.objects.create(action=action, description=description)
    except Exception:  # pragma: no cover - logging must never break saves
        logger.exception('shop: failed to write ActivityLog')


@receiver(post_save, sender=OrderItem)
def recalc_on_item_save(sender, instance, **kwargs):
    """Keep the order total in step when a line is added or changed."""
    instance.order.recalc_total()


@receiver(pre_delete, sender=OrderItem)
def release_booking_on_item_delete(sender, instance, **kwargs):
    """Taking a 1-on-1 out of the cart frees its slot straight away, rather
    than leaving it blocked until the hold runs out."""
    from .models import Booking
    Booking.objects.filter(order_item=instance, status=Booking.STATUS_HELD).update(
        status=Booking.STATUS_EXPIRED)


@receiver(post_delete, sender=OrderItem)
def recalc_on_item_delete(sender, instance, **kwargs):
    """Keep the order total in step when a line is removed (skipped while the order itself is being cascade-deleted)."""
    try:
        instance.order.recalc_total()
    except Order.DoesNotExist:
        pass


@receiver(post_save, sender=Payment)
def settle_order_on_payment(sender, instance, **kwargs):
    """Mark a pending order paid once its payments cover the total."""
    order = instance.order
    if order.status == Order.STATUS_PENDING and order.total > 0 and order.balance <= 0:
        order.status = Order.STATUS_PAID
        order.save(update_fields=['status', 'updated_at'])
        _log('update', f'Order {order.order_no} marked paid')


@receiver(post_save, sender=Product)
def log_product_save(sender, instance, created, **kwargs):
    """Audit-log a product being created or updated."""
    _log('create' if created else 'update',
         f"Product {'created' if created else 'updated'}: {instance.name}")


@receiver(post_delete, sender=Product)
def log_product_delete(sender, instance, **kwargs):
    """Audit-log a product being deleted."""
    _log('delete', f'Product deleted: {instance.name}')


@receiver(post_save, sender=Order)
def log_order_save(sender, instance, created, **kwargs):
    """Audit-log a new order."""
    if created:
        _log('create', f'Order created: {instance.order_no}')


@receiver(post_delete, sender=Order)
def log_order_delete(sender, instance, **kwargs):
    """Audit-log an order being deleted."""
    _log('delete', f'Order deleted: {instance.order_no}')


@receiver(finance_invoice_paid)
def email_downloads_on_payment(sender, invoice, **kwargs):
    """E-mail the buyer their files when the invoice behind their order is paid.

    Card and EFT payments share this path. finance.services has already flipped
    the order to paid before firing this. Never raises — a mail problem must not
    undo a payment that really happened.
    """
    order = getattr(invoice, 'order', None)
    if order is None:
        return
    try:
        from .fulfilment import process_paid_order
        process_paid_order(order)
    except Exception:  # pragma: no cover
        logger.exception('shop: paid-order processing failed for %s', order.order_no)
    try:
        from .entitlements import deliver_order
        deliver_order(order)
    except Exception:  # pragma: no cover
        logger.exception('shop: download delivery failed for order %s', order.order_no)


@receiver(finance_invoice_paid)
def unlock_tuition_on_payment(sender, invoice, **kwargs):
    """A paid invoice with module / programme fee lines opens those modules for
    the customer — one month per unit invoiced (see shop.catalogue)."""
    try:
        from .catalogue import unlock_paid_lines
        unlock_paid_lines(invoice)
    except Exception:  # pragma: no cover - never undo a real payment
        logger.exception('shop: tuition unlock failed for invoice %s', invoice.number)


@receiver(finance_invoice_paid)
def one_on_one_on_payment(sender, invoice, **kwargs):
    """Paid one-on-one hours reach the balance; a paid session invoice may
    settle the account (apps.shop.one_on_one)."""
    try:
        from . import one_on_one
        one_on_one.credit_paid_purchase(invoice)
        one_on_one.settled_invoice(invoice)
    except Exception:  # pragma: no cover - never undo a real payment
        logger.exception('shop: one-on-one payment handling failed for invoice %s', invoice.number)


def _sync_learning_item(sync, obj, raw):
    """Run a catalogue ``sync`` for ``obj``, never letting it block the learning edit.

    Skipped for ``raw`` saves: a fixture load is a restore, not an edit.
    """
    if raw:
        return
    try:
        sync(obj)
    except Exception:  # pragma: no cover - a catalogue hiccup must not block a module edit
        logger.exception('shop: catalogue sync failed for %r', obj)


@receiver(post_save, sender='learning.ProgrammeModule')
def sync_module_item(sender, instance, raw=False, **kwargs):
    """Keep the module's and its programme's catalogue items in step with an edited module."""
    from .catalogue import sync_module, sync_programme
    _sync_learning_item(sync_module, instance, raw)
    _sync_learning_item(sync_programme, instance.programme, raw)


@receiver(post_save, sender='learning.Programme')
def sync_programme_item(sender, instance, raw=False, **kwargs):
    """Keep the programme's catalogue item in step with an edited programme."""
    from .catalogue import sync_programme
    _sync_learning_item(sync_programme, instance, raw)
