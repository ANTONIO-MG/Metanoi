"""Context processor exposing shop summary stats to every template
(badge counts in the sidebar, dashboard widgets, etc.).

Scoped by role: admin/staff see all orders & revenue; everyone else sees only
their own orders. The product/service catalogue counts are public to all.

Lazy: each value is computed only when a template reads it (the layouts read
just ``cart_count``), and ``cart_count`` — drawn on every page — is cached per
user for :data:`core.counters.COUNTER_TTL` seconds. Any write request by the
user, or a change to their cart's items, clears it (see :mod:`core.counters`).
"""

from django.db.models import Sum

from core.counters import LazyStats, cached_counter, memo
from core.roles import role_flags

from . import models
from core.request_cache import cached_per_request


def _cart_count(user):
    """Items in the user's current (newest, not checked out) cart."""
    if user is None or not user.is_authenticated:
        return 0
    cart = (models.Order.objects.filter(buyer=user, checked_out=False)
            .order_by('-created_at').values_list('pk', flat=True)[:1])
    total = (models.OrderItem.objects.filter(order_id__in=cart)
             .aggregate(n=Sum('quantity'))['n'])
    return total or 0


def _build_shop_stats(request):
    """Build the lazy :class:`~core.counters.LazyStats` for ``request``.

    Non-admins only see their own orders and spend. ``cart_count`` is the live
    count of items in the current user's cart, for the navbar badge.
    """
    flags = role_flags(request)
    user = getattr(request, 'user', None)
    scoped = {}

    def orders():
        """The orders queryset visible to the user (memoised with the completed payments)."""
        if 'orders' not in scoped:
            qs = models.Order.objects.all()
            completed = models.Payment.objects.filter(status=models.Payment.STATUS_COMPLETED)
            if not flags['is_admin_staff'] and user is not None and user.is_authenticated:
                qs = qs.filter(buyer=user)
                completed = completed.filter(order__buyer=user)
            scoped['orders'], scoped['completed'] = qs, completed
        return scoped['orders']

    def completed():
        """Completed payments visible to the user."""
        orders()
        return scoped['completed']

    product = models.Product.objects
    return LazyStats({
        'products': memo(lambda: product.filter(kind=models.Product.KIND_PRODUCT).count()),
        'services': memo(lambda: product.filter(kind=models.Product.KIND_SERVICE).count()),
        'active_products': memo(lambda: product.filter(status='active').count()),
        'orders': memo(lambda: orders().count()),
        'pending_orders': memo(lambda: orders().filter(status=models.Order.STATUS_PENDING).count()),
        'paid_orders': memo(lambda: orders().filter(status=models.Order.STATUS_PAID).count()),
        'total_revenue': memo(lambda: completed().aggregate(total=Sum('amount'))['total'] or 0),
        'cart_count': memo(lambda: cached_counter(user, 'shop', lambda: _cart_count(user))
                           if user is not None and user.is_authenticated else 0),
        'recent_products': memo(lambda: product.order_by('-created_at')[:5]),
    })


def shop_stats(request):
    """Cached for the life of the request — these are nav badges, and this
    processor runs once per template rendered in a response, not once per
    response."""
    return {'shop_stats': cached_per_request(request, 'shop_stats',
                                             lambda: _build_shop_stats(request))}
