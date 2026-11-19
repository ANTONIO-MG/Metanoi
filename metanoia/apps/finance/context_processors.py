"""Context processor exposing finance summary stats to every template.

Scoped by role: admin/staff see all invoices; everyone else sees only the
invoices billed to them (``customer``).

Lazy: nothing is queried unless a template actually reads one of the values
(no layout does today), so this costs no queries on an ordinary page.
"""

from django.db.models import Sum

from core.counters import LazyStats, memo
from core.roles import role_flags

from . import models
from core.request_cache import cached_per_request


def _build_finance_stats(request):
    flags = role_flags(request)
    user = getattr(request, 'user', None)
    invoices = models.Invoice.objects.all()
    payments = models.InvoicePayment.objects.counted()
    if not flags['is_admin_staff'] and user is not None and user.is_authenticated:
        invoices = invoices.filter(customer=user)
        payments = payments.filter(invoice__customer=user)
    total_invoiced = memo(lambda: invoices.aggregate(total=Sum('total'))['total'] or 0)
    total_received = memo(lambda: payments.aggregate(total=Sum('amount'))['total'] or 0)
    return LazyStats({
        'invoices_count': memo(invoices.count),
        'draft_count': memo(lambda: invoices.filter(status=models.Invoice.STATUS_DRAFT).count()),
        'paid_count': memo(lambda: invoices.filter(status=models.Invoice.STATUS_PAID).count()),
        'overdue_count': memo(lambda: invoices.filter(status=models.Invoice.STATUS_OVERDUE).count()),
        'total_invoiced': total_invoiced,
        'total_received': total_received,
        'total_due': memo(lambda: (total_invoiced() or 0) - (total_received() or 0)),
    })


def finance_stats(request):
    """Cached for the life of the request — these are nav badges, and this
    processor runs once per template rendered in a response, not once per
    response."""
    return {'finance_stats': cached_per_request(request, 'finance_stats',
                                                lambda: _build_finance_stats(request))}
