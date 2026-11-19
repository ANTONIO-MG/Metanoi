"""Formatting for the shop and finance pages: ``{% load commerce %}``."""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()

_FILE_ICONS = {
    'pdf': 'bi-file-earmark-pdf', 'zip': 'bi-file-earmark-zip',
    'doc': 'bi-file-earmark-word', 'docx': 'bi-file-earmark-word', 'odt': 'bi-file-earmark-word',
    'xls': 'bi-file-earmark-excel', 'xlsx': 'bi-file-earmark-excel', 'ods': 'bi-file-earmark-excel',
    'csv': 'bi-file-earmark-spreadsheet',
    'ppt': 'bi-file-earmark-slides', 'pptx': 'bi-file-earmark-slides', 'odp': 'bi-file-earmark-slides',
}

_FULFILMENT_ICONS = {
    'digital': 'bi-file-earmark-arrow-down', 'booking': 'bi-person-video3',
    'shipped': 'bi-box-seam', 'none': 'bi-receipt',
}


def _decimal(value):
    """Coerce a template value to :class:`~decimal.Decimal`; blanks and junk become 0."""
    try:
        return Decimal(str(value if value not in (None, '') else 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


@register.filter
def rand(value):
    """``1234.5`` → ``R1,234.50``; negatives as ``−R12.00``."""
    amount = _decimal(value).quantize(Decimal('0.01'))
    sign = '−' if amount < 0 else ''
    return f'{sign}R{abs(amount):,.2f}'


@register.filter
def rand_short(value):
    """``150`` → ``R150``; ``149.5`` → ``R149.50`` — the price badge on a store card."""
    amount = _decimal(value).quantize(Decimal('0.01'))
    sign = '−' if amount < 0 else ''
    if amount == amount.to_integral_value():
        return f'{sign}R{abs(amount):,.0f}'
    return f'{sign}R{abs(amount):,.2f}'


@register.filter
def pct(value):
    """``20.00`` → ``20%``; ``12.50`` → ``12.5%``."""
    return f'{_decimal(value).normalize():f}%'


@register.filter
def file_icon(name):
    """Bootstrap Icons class for a file name's extension."""
    ext = str(name or '').rsplit('.', 1)[-1].lower()
    return _FILE_ICONS.get(ext, 'bi-file-earmark')


@register.filter
def fulfilment_icon(value):
    """Bootstrap Icons class for a product's fulfilment type."""
    return _FULFILMENT_ICONS.get(str(value), 'bi-bag')


@register.filter
def when(value):
    """An ISO timestamp from a courier/event log → ``3 Oct, 14:05`` local time."""
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    moment = parse_datetime(str(value or ''))
    if moment is None:
        return str(value or '')[:10]
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return moment.strftime('%-d %b, %H:%M')


@register.simple_tag
def delivery_rates():
    """The live flat delivery fees, for the Shipping Policy text, so the policy
    always quotes what checkout charges: ``{% delivery_rates as rates %}`` →
    ``rates.door``, ``rates.locker``, ``rates.free_over`` (Decimal; 0 = none)."""
    from django.conf import settings

    from apps.shop import courier
    return {
        'door': courier._money(getattr(settings, 'SHOP_DELIVERY_DOOR_FEE', 0)),
        'locker': courier._money(getattr(settings, 'SHOP_DELIVERY_LOCKER_FEE', 0)),
        'free_over': courier.free_threshold(),
    }


@register.simple_tag(takes_context=True)
def one_on_one_sessions(context):
    """The viewer's 1-on-1 bookings, for the Online sessions page.

    ``{% one_on_one_sessions as oo %}`` → ``{'upcoming', 'past', 'view', 'is_staff',
    'is_educator', 'meeting_ids', 'credit'}``. Students see what they booked;
    an educator the sessions booked with them (and, by ``?bookings=mine``, their
    own); staff may ask for every booking with ``?bookings=all``. ``meeting_ids``
    lets the page leave those rooms out of its general list, where they would
    otherwise appear a second time.

    A hold that lapsed in an open cart no longer holds the time, so it is left out.
    """
    from django.utils import timezone

    from apps.shop import booking as booking_service
    from apps.shop.models import Booking, CreditEntry

    request = context.get('request')
    user = getattr(request, 'user', None)
    empty = {'upcoming': [], 'past': [], 'view': 'mine', 'is_staff': False,
             'is_educator': False, 'meeting_ids': set(), 'credit': 0}
    if user is None or not user.is_authenticated:
        return empty
    is_staff = booking_service._is_staff(user)
    person = getattr(user, 'profile', None)
    base = (Booking.objects.select_related('product', 'educator__user', 'student', 'meeting')
            .exclude(status=Booking.STATUS_EXPIRED))
    is_educator = person is not None and base.filter(educator=person).exists()
    view = request.GET.get('bookings', '')
    if is_staff and view == 'all':
        qs = base
    elif is_educator and view != 'mine':
        qs, view = base.filter(educator=person), 'teaching'
    else:
        qs, view = base.filter(student=user), 'mine'
    now = timezone.now()
    live = qs.filter(end__gte=now).exclude(status=Booking.STATUS_CANCELLED)
    live = [b for b in live.order_by('start')
            if b.status != Booking.STATUS_HELD or (b.hold_expires_at and b.hold_expires_at > now)]
    past = list(qs.exclude(pk__in=[b.pk for b in live]).order_by('-start')[:20])
    return {
        'upcoming': live, 'past': past, 'view': view, 'is_staff': is_staff,
        'is_educator': is_educator,
        'meeting_ids': {b.meeting_id for b in live if b.meeting_id},
        'credit': CreditEntry.balance_for(user),
    }
