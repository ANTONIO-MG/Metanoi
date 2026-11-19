"""Signal handlers for the finance app.

* Recompute an invoice's totals when its line items change.
* Recompute an invoice's status when a payment is recorded.
* Record invoice create/update/delete in the shared
  :class:`apps.accounts.models.ActivityLog` audit trail.

Connected in :meth:`apps.finance.apps.FinanceConfig.ready`.
"""

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Invoice, InvoiceItem, InvoicePayment

logger = logging.getLogger('apps')


def _log(action, description):
    """Write an :class:`ActivityLog` entry, logging (never raising) on failure."""
    try:
        from apps.accounts.models import ActivityLog
        ActivityLog.objects.create(action=action, description=description)
    except Exception:  # pragma: no cover
        logger.exception('finance: failed to write ActivityLog')


@receiver(post_save, sender=InvoiceItem)
def recalc_on_item_save(sender, instance, **kwargs):
    """Recalculate the invoice total and status when a line is saved."""
    instance.invoice.recalc_total()
    instance.invoice.refresh_status()


@receiver(post_delete, sender=InvoiceItem)
def recalc_on_item_delete(sender, instance, **kwargs):
    """Recalculate the invoice when a line is deleted, unless the invoice itself is being cascade-deleted.
    """
    try:
        instance.invoice.recalc_total()
        instance.invoice.refresh_status()
    except Invoice.DoesNotExist:
        pass


@receiver(post_save, sender=InvoicePayment)
def refresh_status_on_payment(sender, instance, **kwargs):
    """Refresh the invoice status when a payment is saved."""
    instance.invoice.refresh_status()


@receiver(post_save, sender=Invoice)
def log_invoice_save(sender, instance, created, **kwargs):
    """Audit the creation of an invoice."""
    if created:
        _log('create', f'Invoice created: {instance.number}')


@receiver(post_delete, sender=Invoice)
def log_invoice_delete(sender, instance, **kwargs):
    """Audit the deletion of an invoice."""
    _log('delete', f'Invoice deleted: {instance.number}')
