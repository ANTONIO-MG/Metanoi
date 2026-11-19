"""Things staff do *to* finance documents: duplicate, void, convert, remind.

Kept out of the views so the scheduler (reminders, recurring expenses) and the
pages share one implementation.

``REMINDER_STAGES`` lists the automatic reminder stages as ``(key, days
relative to the due date)``; :func:`run_reminders` sends only the latest stage
that applies and has not been sent.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import models

logger = logging.getLogger('apps')

REMINDER_STAGES = [('before_3', -3), ('due', 0), ('after_7', 7)]


def duplicate_invoice(invoice, *, by):
    """A fresh draft with the same customer and lines, dated today.

    Lines are copied gross; every discount (an old invoice's line discounts
    included) moves onto the copy as one invoice discount.
    """
    with transaction.atomic():
        copy = models.Invoice.objects.create(
            customer=invoice.customer, created_by=by, status=models.Invoice.STATUS_DRAFT,
            summary=invoice.summary, po_number=invoice.po_number, notes=invoice.notes, footer=invoice.footer,
            discount_amount=invoice.discount_total or 0,
            discount_label=invoice.discount_label or ('Discount' if invoice.discount_total else ''),
            discount_auto=invoice.discount_auto)
        _copy_lines_gross(invoice.items.all(), copy)
        from . import discounts
        discounts.apply_auto(copy)
    return copy


def _copy_lines_gross(items, invoice):
    """Copy ``items`` onto ``invoice`` at their gross unit price, without line discounts."""
    for item in items:
        models.InvoiceItem.objects.create(
            invoice=invoice, product=item.product, description=item.description,
            detail=item.detail, quantity=item.quantity, unit_price=item.unit_price)


def void_invoice(invoice, *, by, reason=''):
    """Void an invoice that should never have been raised: it is **deleted**.

    The student's status is not touched — module enrolments keep whatever
    access they had (a trial stays a trial, a locked module stays locked), so
    voiding never locks anyone out. Paid ones cannot be voided. The delete keeps
    a full copy, so it can be undone. Returns the :class:`UndoAction`.
    """
    if invoice.amount_paid:
        raise ValueError('This invoice has payments against it — refund or credit it instead.')
    from django.urls import reverse

    from apps.accounts import undo

    summary = f'Invoice {invoice.number} voided (deleted)'
    if reason:
        summary = f'{summary}: {reason}'
    return undo.delete_with_undo(
        [invoice], summary=summary[:255], actor=by, target_user=invoice.customer,
        next_url=reverse('finance:invoices'))


def mark_sent(invoice):
    """Stamp the invoice as sent, moving a draft to *sent*."""
    fields = ['sent_at', 'updated_at']
    invoice.sent_at = timezone.now()
    if invoice.status == models.Invoice.STATUS_DRAFT:
        invoice.status = models.Invoice.STATUS_SENT
        fields.append('status')
    invoice.save(update_fields=fields)


def mark_viewed(document):
    """First open of the public link — shown as "Viewed" on the timeline."""
    if document.viewed_at:
        return
    document.viewed_at = timezone.now()
    fields = ['viewed_at', 'updated_at']
    if isinstance(document, models.Estimate) and document.status == models.Estimate.STATUS_SENT:
        document.status = models.Estimate.STATUS_VIEWED
        fields.append('status')
    document.save(update_fields=fields)


def send_reminder(invoice, *, stage='manual'):
    """E-mail a payment reminder for ``invoice`` and stamp ``last_reminder_at``.

    ``stage`` is the reminder stage key (``'manual'`` for one sent by staff).
    """
    from apps.communication import emails
    from core.utils import absolute_url

    sent = emails.send_invoice_reminder(invoice, absolute_url(invoice.get_pay_url()), stage=stage)
    if sent:
        invoice.last_reminder_at = timezone.now()
        stages = list(invoice.reminders_sent or [])
        if stage not in stages:
            stages.append(stage)
        invoice.reminders_sent = stages
        invoice.save(update_fields=['last_reminder_at', 'reminders_sent', 'updated_at'])
    return sent


def _is_registration(invoice):
    """True for a registration invoice — the trial reminders chase those."""
    try:
        from apps.learning.models import ModuleEnrolment
        return ModuleEnrolment.objects.filter(invoice_uid=invoice.public_id).exists()
    except Exception:  # pragma: no cover
        return False


def run_reminders(today=None):
    """Scheduler entry point: send due reminder stages; flag overdue invoices.

    A stage goes out once, on or after its day (so a missed run catches up the
    next day) but never after a later stage has become due — nobody wants the
    "due in 3 days" e-mail a week late. Registration invoices are skipped: the
    trial reminders in apps.accounts already chase those.
    """
    today = today or timezone.localdate()
    sent = 0
    open_invoices = (models.Invoice.objects
                     .filter(due_date__isnull=False, total__gt=0)
                     .exclude(status__in=[models.Invoice.STATUS_DRAFT, models.Invoice.STATUS_PAID,
                                          models.Invoice.STATUS_CANCELLED])
                     .select_related('customer'))
    for invoice in open_invoices:
        invoice.refresh_status()
        if invoice.balance <= 0 or _is_registration(invoice):
            continue
        done = set(invoice.reminders_sent or [])
        due_stages = [(key, offset) for key, offset in REMINDER_STAGES
                      if today >= invoice.due_date + timedelta(days=offset)]
        if not due_stages:
            continue
        key, _offset = due_stages[-1]
        if key in done:
            continue
        try:
            if send_reminder(invoice, stage=key):
                sent += 1
        except Exception:  # pragma: no cover — one bad address must not stop the run
            logger.exception('finance: reminder failed for %s', invoice.number)
    return f'{sent} reminder(s) sent'


def convert_estimate(estimate, *, by):
    """Turn an estimate into a draft invoice with the same lines.

    The estimate's line discounts become the invoice's one discount.
    """
    if estimate.converted_invoice_id:
        return estimate.converted_invoice
    with transaction.atomic():
        invoice = models.Invoice.objects.create(
            customer=estimate.customer, created_by=by, status=models.Invoice.STATUS_DRAFT,
            summary=estimate.summary, po_number=estimate.po_number or estimate.number,
            notes=estimate.notes, footer=estimate.footer,
            tax_rate=estimate.tax_rate, tax_inclusive=estimate.tax_inclusive,
            discount_amount=estimate.discount_total or 0,
            discount_label='Discount' if estimate.discount_total else '',
            discount_auto=False)
        _copy_lines_gross(estimate.items.all(), invoice)
        invoice.recalc_total()
        estimate.converted_invoice = invoice
        estimate.status = models.Estimate.STATUS_CONVERTED
        estimate.save(update_fields=['converted_invoice', 'status', 'updated_at'])
    return invoice


def respond_to_estimate(estimate, accepted):
    """Record the customer's acceptance or refusal of an estimate and notify staff.

    Has no effect on an estimate already converted, declined or accepted.
    """
    if estimate.status in (models.Estimate.STATUS_CONVERTED, models.Estimate.STATUS_DECLINED,
                           models.Estimate.STATUS_ACCEPTED):
        return estimate
    estimate.status = models.Estimate.STATUS_ACCEPTED if accepted else models.Estimate.STATUS_DECLINED
    estimate.responded_at = timezone.now()
    estimate.save(update_fields=['status', 'responded_at', 'updated_at'])
    try:
        from apps.communication.services import notify
        from apps.shop.fulfilment import staff_recipients
        verb = 'accepted' if accepted else 'declined'
        for person in staff_recipients():
            notify(person, title=f'Estimate {estimate.number} {verb}',
                   body=f'{estimate.bill_to_name} {verb} {estimate.number} (R{estimate.total:,.2f}).',
                   verb=f'estimate {verb}', level='success' if accepted else 'warning',
                   url=estimate.get_absolute_url())
    except Exception:  # pragma: no cover
        logger.exception('finance: estimate response notification failed')
    return estimate


def expire_estimates(today=None):
    """Mark sent or viewed estimates past their ``valid_until`` date as expired; return the count.
    """
    today = today or timezone.localdate()
    return models.Estimate.objects.filter(
        valid_until__lt=today,
        status__in=[models.Estimate.STATUS_SENT, models.Estimate.STATUS_VIEWED]).update(
        status=models.Estimate.STATUS_EXPIRED)


def run_recurring_expenses(today=None):
    """Scheduler entry point: book every recurring expense that has fallen due.

    Catches up missed runs (a profile three months behind books three
    expenses, each on its own date) and stops at the end date.
    """
    today = today or timezone.localdate()
    created = 0
    for profile in models.RecurringExpense.objects.filter(is_active=True, next_date__lte=today):
        with transaction.atomic():
            profile = models.RecurringExpense.objects.select_for_update().get(pk=profile.pk)
            guard = 0
            while profile.next_date and profile.next_date <= today and guard < 60:
                if profile.end_date and profile.next_date > profile.end_date:
                    profile.is_active = False
                    break
                models.Expense.objects.create(
                    date=profile.next_date, category=profile.category, vendor=profile.vendor,
                    description=profile.description, amount=profile.amount,
                    vat_treatment=profile.vat_treatment, paid_through=profile.paid_through,
                    institution=profile.institution, notes=profile.notes, recurring=profile,
                    created_by=profile.created_by, reference=f'{profile.name} · {profile.next_date:%b %Y}')
                created += 1
                profile.next_date = profile.advance(profile.next_date)
                guard += 1
            if profile.end_date and profile.next_date and profile.next_date > profile.end_date:
                profile.is_active = False
            profile.save(update_fields=['next_date', 'is_active', 'updated_at'])
    return f'{created} recurring expense(s) booked'


def run_daily():
    """Scheduler entry point for the finance day: reminders, expiry, recurring."""
    parts = [run_reminders(), f'{expire_estimates()} estimate(s) expired', run_recurring_expenses()]
    return '; '.join(parts)


def statement(user, start, end):
    """A statement of account: opening balance, movements, closing balance.

    Invoices are debits on their issue date, payments credits on the day they
    landed. Cancelled invoices are left out entirely.
    """
    invoices = models.Invoice.objects.filter(customer=user).exclude(status=models.Invoice.STATUS_CANCELLED)
    payments = models.InvoicePayment.objects.filter(invoice__customer=user,
                                                    status=models.InvoicePayment.STATUS_COMPLETED) \
        .exclude(invoice__status=models.Invoice.STATUS_CANCELLED)

    def total(qs, field):
        """Sum ``field`` over ``qs`` (zero when empty)."""
        from django.db.models import Sum
        return qs.aggregate(t=Sum(field))['t'] or Decimal('0')

    opening = total(invoices.filter(issue_date__lt=start), 'total') - \
        total(payments.filter(paid_at__date__lt=start), 'amount')
    rows = [{'date': inv.issue_date, 'kind': 'invoice', 'ref': inv.number,
             'text': inv.summary or ', '.join(i.description for i in inv.items.all()[:2]) or 'Invoice',
             'debit': inv.total, 'credit': Decimal('0'), 'obj': inv}
            for inv in invoices.filter(issue_date__gte=start, issue_date__lte=end).prefetch_related('items')]
    rows += [{'date': timezone.localdate(p.paid_at), 'kind': 'payment', 'ref': p.invoice.number,
              'text': f'Payment — {p.get_method_display()}', 'debit': Decimal('0'), 'credit': p.amount, 'obj': p}
             for p in payments.filter(paid_at__date__gte=start, paid_at__date__lte=end).select_related('invoice')]
    rows.sort(key=lambda r: (r['date'], 0 if r['kind'] == 'invoice' else 1))
    balance = opening
    for row in rows:
        balance += row['debit'] - row['credit']
        row['balance'] = balance
    return {'user': user, 'start': start, 'end': end, 'opening': opening, 'rows': rows, 'closing': balance,
            'invoiced': sum((r['debit'] for r in rows), Decimal('0')),
            'paid': sum((r['credit'] for r in rows), Decimal('0'))}
