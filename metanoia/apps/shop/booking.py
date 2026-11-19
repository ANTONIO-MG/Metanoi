"""1-on-1 bookings: when an educator is free, and what happens to a booked slot.

A slot is offered only inside the educator's weekly windows, outside time off,
Metanoia live sessions they host, busy spans on their connected calendar feeds
(:mod:`apps.livesessions.sources`) and other paid/held bookings. Busy spans are
widened by the service's buffer; slots start on ``Product.slot_step_minutes``
and respect the minimum notice.

Weekly hours are the educator's wall-clock hours, read in their own zone (else
:data:`DEFAULT_EDUCATOR_TZ`); :func:`week_grid` shows them in the viewer's zone.

A chosen slot is **held** for :data:`HOLD_MINUTES` during checkout and
:data:`CHECKOUT_HOLD_HOURS` once the order is placed (EFT takes time). Holds are
taken under a row lock on the educator's rate, so two simultaneous requests for
one slot serialise and the second sees the first.
"""
import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Booking, CreditEntry, EducatorAvailability, EducatorRate, EducatorTimeOff, Order, OrderItem

logger = logging.getLogger('apps')

HOLD_MINUTES = 15
CHECKOUT_HOLD_HOURS = 48
SLOT_STEP_MINUTES = 30
"""Default slot step; an hour-only service starts on the hour (``Product.slot_step_minutes``)."""
DEFAULT_EDUCATOR_TZ = 'Africa/Johannesburg'
"""The zone an educator's weekly hours are read in when they have not saved one.
(The platform's ``TIME_ZONE`` is UTC for storage; the coaches work in SAST.)
"""


class BookingError(Exception):
    """The slot cannot be booked; the message is for the student."""


def educator_tz(educator):
    """The zone ``educator``'s weekly hours are written in."""
    from zoneinfo import ZoneInfo

    from django.conf import settings

    zone = ''
    try:
        from apps.accounts.models import UserSettings
        zone = (UserSettings.objects.filter(user_id=educator.user_id)
                .values_list('timezone', flat=True).first() or '')
    except Exception:  # pragma: no cover
        zone = ''
    for name in (zone, getattr(settings, 'BOOKING_TIME_ZONE', DEFAULT_EDUCATOR_TZ)):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    return timezone.get_current_timezone()


def _local_day(educator, moment, tz=None):
    """The calendar date of ``moment`` in the educator's zone (or ``tz``)."""
    return moment.astimezone(tz or educator_tz(educator)).date()


def _aware(day, clock, tz):
    """An aware datetime for ``day`` at ``clock`` in ``tz``."""
    return timezone.make_aware(datetime.combine(day, clock), tz)


def _windows(educator, day, tz=None):
    """The educator's working windows on their local ``day``, as aware (start, end) pairs."""
    tz = tz or educator_tz(educator)
    windows = [(_aware(day, w.start_time, tz), _aware(day, w.end_time, tz))
               for w in EducatorAvailability.objects.filter(educator=educator, weekday=day.weekday())
               if w.end_time > w.start_time]
    return sorted(windows)


def _step(product, step=None):
    """The slot step in minutes: ``step``, else the product's, else :data:`SLOT_STEP_MINUTES`."""
    return int(step or getattr(product, 'slot_step_minutes', 0) or SLOT_STEP_MINUTES)


def _on_step(cursor, step):
    """``cursor`` moved forward to the next start on a ``step``-minute grid."""
    cursor = cursor.replace(second=0, microsecond=0)
    past = (cursor.hour * 60 + cursor.minute) % step
    return cursor + timedelta(minutes=step - past) if past else cursor


def busy_spans(educator, start, end, *, exclude_booking=None, include_time_off=True,
               include_bookings=True):
    """Everything that occupies ``educator`` between ``start`` and ``end``."""
    from apps.communication.models import MeetingRoom

    spans = []
    user = educator.user
    if user is not None:
        sessions = (MeetingRoom.objects
                    .filter(is_active=True, scheduled_start__lt=end, scheduled_end__gt=start)
                    .filter(Q(host=user) | Q(participants__user=user,
                                             participants__role__in=('host', 'cohost')))
                    .distinct())
        spans += [(m.scheduled_start, m.scheduled_end or m.scheduled_start + timedelta(hours=1))
                  for m in sessions]
        try:
            from apps.livesessions import sources
            spans += [(e.start, e.end or e.start + timedelta(hours=1))
                      for e in sources.entries_for(user, start, end, kinds=('external',))
                      if e.busy and e.start and not e.all_day]
        except Exception:  # pragma: no cover — a feed problem must not hide every slot
            logger.exception('shop: could not read calendar feeds for %s', educator)

    if include_time_off:
        spans += time_off_spans(educator, start, end)
    if not include_bookings:
        return spans

    now = timezone.now()
    bookings = (Booking.objects.filter(educator=educator, start__lt=end, end__gt=start,
                                       status__in=Booking.BLOCKING)
                .filter(Q(status=Booking.STATUS_CONFIRMED) | Q(hold_expires_at__gt=now)))
    if exclude_booking is not None:
        bookings = bookings.exclude(pk=exclude_booking.pk)
    spans += [(b.start, b.end) for b in bookings]
    return spans


def blocking_bookings(educator, start, end, *, exclude_booking=None):
    """Bookings that hold ``educator``'s time: paid, or held in a live cart."""
    qs = (Booking.objects.filter(educator=educator, start__lt=end, end__gt=start,
                                 status__in=Booking.BLOCKING)
          .filter(Q(status=Booking.STATUS_CONFIRMED) | Q(hold_expires_at__gt=timezone.now())))
    if exclude_booking is not None:
        qs = qs.exclude(pk=exclude_booking.pk)
    return list(qs)


def time_off_spans(educator, start, end):
    """``(start, end)`` of the educator's time off overlapping the range."""
    return [(t.start, t.end) for t in
            EducatorTimeOff.objects.filter(educator=educator, start__lt=end, end__gt=start)]


def free_slots(rate, day, minutes, *, now=None, exclude_booking=None, step=None):
    """Start times on the educator's local ``day`` when they can take a ``minutes`` session.

    The buffer keeps sessions apart; it does not pad leave, so time off is added
    as-is. Starts are aligned to the service's step so the picker reads
    14:00, 14:30 … (or 14:00, 15:00 … for an on-the-hour service).
    """
    product = rate.product
    now = now or timezone.now()
    earliest = now + timedelta(hours=product.min_notice_hours)
    latest = now + timedelta(days=product.booking_window_days)
    windows = _windows(rate.educator, day)
    if not windows:
        return []

    day_start, day_end = windows[0][0], windows[-1][1]
    buffer = timedelta(minutes=product.buffer_minutes)
    busy = [(s - buffer, e + buffer) for s, e in
            busy_spans(rate.educator, day_start - buffer, day_end + buffer,
                       exclude_booking=exclude_booking, include_time_off=False)]
    busy += time_off_spans(rate.educator, day_start, day_end)
    length = timedelta(minutes=minutes)
    step_minutes = _step(product, step)
    step = timedelta(minutes=step_minutes)

    slots = []
    for win_start, win_end in windows:
        cursor = _on_step(win_start, step_minutes)
        while cursor + length <= win_end:
            end = cursor + length
            clash = any(s < end and e > cursor for s, e in busy)
            if not clash and earliest <= cursor <= latest:
                slots.append(cursor)
            cursor += step
    return slots


def week_of_slots(rate, minutes, start_day, *, days=14, now=None, exclude_booking=None):
    """``days`` rows of ``{'day', 'slots'}`` for the picker; full days kept.
    ``day`` is the educator's local date."""
    rows = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        rows.append({'day': day, 'slots': free_slots(rate, day, minutes, now=now,
                                                     exclude_booking=exclude_booking)})
    return rows


def is_free(rate, start, minutes, *, exclude_booking=None, now=None):
    """Whether ``start`` is one of the free slots for a ``minutes`` session that day."""
    day = _local_day(rate.educator, start)
    return any(slot == start for slot in
               free_slots(rate, day, minutes, now=now, exclude_booking=exclude_booking))


CELL_FREE, CELL_BOOKED, CELL_BUSY, CELL_CLOSED, CELL_OFF = 'free', 'booked', 'busy', 'closed', 'off'
"""Cell states on the week grid (``CELL_FREE`` … ``CELL_OFF``). ``free`` is bookable;
``booked`` (red) is a paid or held booking — anyone's; ``busy`` is other
commitments (classes, their own calendar, leave); ``closed`` is inside their
hours but too soon or too far ahead; ``off`` is outside their hours.
"""


def week_grid(rate, monday, *, viewer=None, minutes=60, now=None, viewer_tz=None):
    """A Mon–Sun × hourly grid of ``rate``'s educator, in the viewer's zone.

    ``monday`` is the viewer-local Monday the week starts on. Returns
    ``{'days': [date × 7], 'rows': [{'time', 'cells': [cell × 7]}], 'free': n}``
    where a cell is ``{'state', 'start', 'mine'}`` (``start`` aware, or None for
    an ``off`` cell). The states agree with :func:`free_slots`: a ``free`` cell
    is exactly a start :func:`hold_slot` will accept.

    Candidates are every on-the-hour start inside the educator's hours that
    week; the hours between the first and last start are filled in so the rows
    read evenly.
    """
    product = rate.product
    educator = rate.educator
    viewer_tz = viewer_tz or timezone.get_current_timezone()
    edu_tz = educator_tz(educator)
    now = now or timezone.now()
    earliest = now + timedelta(hours=product.min_notice_hours)
    latest = now + timedelta(days=product.booking_window_days)
    length = timedelta(minutes=minutes)
    step = 60

    range_start = timezone.make_aware(datetime.combine(monday, datetime.min.time()), viewer_tz)
    range_end = timezone.make_aware(datetime.combine(monday + timedelta(days=7),
                                                     datetime.min.time()), viewer_tz)

    candidates = []
    day = range_start.astimezone(edu_tz).date()
    while day <= range_end.astimezone(edu_tz).date():
        for win_start, win_end in _windows(educator, day, edu_tz):
            cursor = _on_step(win_start, step)
            while cursor + length <= win_end:
                if range_start <= cursor < range_end:
                    candidates.append(cursor)
                cursor += timedelta(minutes=step)
        day += timedelta(days=1)

    buffer = timedelta(minutes=product.buffer_minutes)
    span_start, span_end = range_start - buffer - length, range_end + buffer + length
    other = [(s - buffer, e + buffer) for s, e in
             busy_spans(educator, span_start, span_end, include_time_off=False,
                        include_bookings=False)]
    other += time_off_spans(educator, span_start, span_end)
    bookings = blocking_bookings(educator, span_start, span_end)
    viewer_id = getattr(viewer, 'pk', None)

    cells = {}
    for start in candidates:
        end = start + length
        hit = next((b for b in bookings if b.start < end and b.end > start), None)
        if hit is not None:
            state = CELL_BOOKED
        elif any(b.start - buffer < end and b.end + buffer > start for b in bookings) \
                or any(s < end and e > start for s, e in other):
            state = CELL_BUSY
        elif not (earliest <= start <= latest):
            state = CELL_CLOSED
        else:
            state = CELL_FREE
        local = start.astimezone(viewer_tz)
        cells[(local.date(), local.time())] = {
            'state': state, 'start': start,
            'mine': bool(hit is not None and viewer_id and hit.student_id == viewer_id)}

    days = [monday + timedelta(days=i) for i in range(7)]
    times = sorted({t for _d, t in cells})
    if times:
        first = datetime.combine(monday, times[0])
        last = datetime.combine(monday, times[-1])
        filled, cursor = [], first
        while cursor <= last:
            filled.append(cursor.time())
            cursor += timedelta(minutes=step)
        times = sorted(set(filled) | set(times))
    rows = [{'time': t, 'cells': [cells.get((d, t), {'state': CELL_OFF, 'start': None, 'mine': False})
                                  for d in days]} for t in times]
    return {'days': days, 'rows': rows,
            'free': sum(1 for c in cells.values() if c['state'] == CELL_FREE)}


def hold_slot(*, user, rate, start, minutes, note=''):
    """Hold ``start`` for ``user`` and put the session in their cart."""
    if minutes not in rate.product.lengths:
        raise BookingError('That session length is not offered.')
    with transaction.atomic():
        EducatorRate.objects.select_for_update().get(pk=rate.pk)
        if not is_free(rate, start, minutes):
            raise BookingError('Sorry — that time has just been taken. Please pick another.')
        cart = Order.get_cart(user)
        price = rate.price_for(minutes)
        item = OrderItem.objects.create(order=cart, product=rate.product, quantity=1, unit_price=price)
        booking = Booking.objects.create(
            product=rate.product, educator=rate.educator, student=user,
            start=start, end=start + timedelta(minutes=minutes), minutes=minutes, price=price,
            hold_expires_at=timezone.now() + timedelta(minutes=HOLD_MINUTES),
            order_item=item, note=note[:2000])
        cart.recalc_total()
    return booking


def refresh_hold(booking, *, hours=None, minutes=None):
    """Keep a held slot for longer — re-checking it is still free if it lapsed."""
    if booking.status != Booking.STATUS_HELD:
        return booking
    now = timezone.now()
    if booking.hold_expires_at and booking.hold_expires_at <= now:
        rate = EducatorRate.objects.filter(product=booking.product, educator=booking.educator).first()
        if rate is None or not is_free(rate, booking.start, booking.minutes, exclude_booking=booking):
            raise BookingError(f'The {booking.label} slot is no longer available — '
                               'please remove it and choose another time.')
    delta = timedelta(hours=hours) if hours else timedelta(minutes=minutes or HOLD_MINUTES)
    booking.hold_expires_at = now + delta
    booking.save(update_fields=['hold_expires_at', 'updated_at'])
    return booking


def _session_title(booking):
    """Title of the meeting room created for a booking."""
    return f'1-on-1: {booking.product.name}'


def confirm(booking):
    """The booking is paid for: create the live session and tell both sides."""
    if booking.status == Booking.STATUS_CONFIRMED:
        return booking
    from apps.communication.services import notify
    from apps.livesessions import services as live

    student_name = booking.student.get_full_name() or booking.student.get_username()
    meeting = None
    try:
        meeting = live.create_session(
            host=booking.educator.user, title=_session_title(booking),
            start=booking.start, end=booking.end, audience='private', session_kind='consult',
            description=f'1-on-1 with {student_name}.' + (f'\n\n{booking.note}' if booking.note else ''),
            invitees=[booking.student], notify=False)
    except Exception:  # pragma: no cover — the money is real; staff can fix the room
        logger.exception('shop: could not create the session for booking %s', booking.pk)

    booking.status = Booking.STATUS_CONFIRMED
    booking.hold_expires_at = None
    booking.meeting = meeting
    booking.save(update_fields=['status', 'hold_expires_at', 'meeting', 'updated_at'])

    url = _bookings_url()
    notify(booking.student, title='Your 1-on-1 is booked',
           body=f'{booking.label} with {booking.educator}. The join link is under Online sessions.',
           verb='booking confirmed', level='success', url=url, meeting=meeting)
    if booking.educator.user_id:
        notify(booking.educator.user, title='New 1-on-1 booked',
               body=f'{student_name} booked {booking.product.name} · {booking.label}.',
               verb='booking', level='info', url=url, meeting=meeting, actor=booking.student)
    return booking


def settle_paid(booking):
    """Payment landed for ``booking``: confirm it, unless the slot was lost.

    A hold lapses if payment takes longer than :data:`CHECKOUT_HOLD_HOURS` (an
    EFT over a long weekend). If someone else booked the time meanwhile, the
    student is not double-booked onto it: the money becomes account credit and
    they are asked to choose another time.
    """
    if booking.status in (Booking.STATUS_CONFIRMED, Booking.STATUS_COMPLETED, Booking.STATUS_CANCELLED):
        return booking
    rate = EducatorRate.objects.filter(product=booking.product, educator=booking.educator).first()
    lapsed = booking.status == Booking.STATUS_EXPIRED or (
        booking.hold_expires_at is not None and booking.hold_expires_at <= timezone.now())
    if lapsed and (rate is None or not _slot_still_open(rate, booking)):
        with transaction.atomic():
            booking.status = Booking.STATUS_CANCELLED
            booking.cancelled_reason = 'The time was taken before payment arrived.'
            booking.save(update_fields=['status', 'cancelled_reason', 'updated_at'])
            CreditEntry.objects.create(user=booking.student, amount=booking.price, booking=booking,
                                       reason=f'Payment for {booking.label} arrived after the slot was taken')
        from apps.communication.services import notify
        notify(booking.student, title='Please choose a new time',
               body=(f'Your payment for {booking.product.name} arrived after {booking.label} was booked '
                     f'by someone else. R{booking.price:,.2f} is on your account as credit — book any '
                     'free time and it will be used automatically.'),
               verb='booking needs a time', level='warning',
               url=booking.product.get_absolute_url(), email=True)
        return booking
    return confirm(booking)


def _slot_still_open(rate, booking):
    """Free apart from this booking — notice period ignored, the student paid."""
    past_notice = timezone.localtime(booking.start) - timedelta(hours=booking.product.min_notice_hours)
    return any(slot == booking.start for slot in free_slots(
        rate, _local_day(rate.educator, booking.start), booking.minutes,
        now=min(timezone.now(), past_notice), exclude_booking=booking))


def reschedule(booking, *, start, by):
    """Move a confirmed booking to ``start`` (same educator, same length)."""
    if not booking.can_reschedule() and not _is_staff(by):
        raise BookingError('This session is too close to move. Please contact us.')
    rate = EducatorRate.objects.filter(product=booking.product, educator=booking.educator).first()
    if rate is None:
        raise BookingError('This educator no longer offers this session.')
    with transaction.atomic():
        EducatorRate.objects.select_for_update().get(pk=rate.pk)
        if not is_free(rate, start, booking.minutes, exclude_booking=booking):
            raise BookingError('That time is not free. Please pick another.')
        old_label = booking.label
        booking.start, booking.end = start, start + timedelta(minutes=booking.minutes)
        booking.reschedule_count += 1
        booking.save(update_fields=['start', 'end', 'reschedule_count', 'updated_at'])

    if booking.meeting is not None:
        try:
            from apps.livesessions import services as live
            booking.meeting.scheduled_start, booking.meeting.scheduled_end = booking.start, booking.end
            live.update_session(booking.meeting, fields=['scheduled_start', 'scheduled_end'],
                                reschedule=True)
        except Exception:  # pragma: no cover
            logger.exception('shop: could not move the session for booking %s', booking.pk)

    from apps.communication.services import notify
    for user in {booking.student, booking.educator.user} - {None}:
        notify(user, title='1-on-1 moved', body=f'{old_label} → {booking.label}.',
               verb='booking moved', level='info', url=_bookings_url(), meeting=booking.meeting,
               actor=by)
    return booking


def cancel(booking, *, by, reason=''):
    """Staff / the educator cancel a paid session: the student gets it as credit.

    Students cannot cancel for money back (they may reschedule instead), so this
    is only reachable by staff and the booking's own educator — enforced here as
    well as in the view.
    """
    if not (_is_staff(by) or booking.educator.user_id == getattr(by, 'pk', None)):
        raise BookingError('Only staff or the educator can cancel a session.')
    if booking.status not in Booking.BLOCKING:
        raise BookingError('This session is not active.')
    was_paid = booking.status == Booking.STATUS_CONFIRMED
    with transaction.atomic():
        booking.status = Booking.STATUS_CANCELLED
        booking.cancelled_by = by
        booking.cancelled_reason = reason[:255]
        booking.save(update_fields=['status', 'cancelled_by', 'cancelled_reason', 'updated_at'])
        if was_paid and booking.price > 0:
            CreditEntry.objects.create(
                user=booking.student, amount=booking.price, booking=booking, created_by=by,
                reason=f'Cancelled 1-on-1: {booking.product.name} ({booking.label})')

    if booking.meeting is not None:
        try:
            from apps.livesessions import services as live
            live.cancel_session(booking.meeting, by=by, reason=reason)
        except Exception:  # pragma: no cover
            logger.exception('shop: could not cancel the session for booking %s', booking.pk)

    from apps.communication.services import notify
    body = f'{booking.label} with {booking.educator} has been cancelled.'
    if was_paid and booking.price > 0:
        body += f' R{booking.price:,.2f} has been added to your account credit for your next purchase.'
    if reason:
        body += f' Reason: {reason}'
    notify(booking.student, title='1-on-1 cancelled', body=body, verb='booking cancelled',
           level='warning', url=_bookings_url(), actor=by, email=True)
    return booking


def expire_holds(now=None):
    """Release holds that ran out. Run by the scheduler every few minutes.

    A hold in an open cart just lapses (checkout re-checks the slot). A hold on
    a placed but unpaid order is expired; the invoice stays, and paying it later
    reports the slot as needing a new time.
    """
    now = now or timezone.now()
    lapsed = Booking.objects.filter(status=Booking.STATUS_HELD, hold_expires_at__lte=now)
    placed = lapsed.filter(order_item__order__checked_out=True)
    count = placed.update(status=Booking.STATUS_EXPIRED)
    return f'{count} unpaid booking(s) expired'


def complete_past(now=None):
    """Mark confirmed bookings that have ended as completed; returns how many."""
    now = now or timezone.now()
    return Booking.objects.filter(status=Booking.STATUS_CONFIRMED, end__lt=now).update(
        status=Booking.STATUS_COMPLETED)


def run_housekeeping():
    """Scheduler entry point: expire unpaid holds, close sessions that have ended."""
    message = expire_holds()
    done = complete_past()
    from .one_on_one import follow_up
    followed = follow_up()
    return f'{message}; {done} session(s) completed; {followed}'


def apply_credit(invoice, user):
    """Spend the user's account credit on ``invoice``. Returns the amount used."""
    from decimal import Decimal

    from apps.finance import services as finance

    with transaction.atomic():
        balance = CreditEntry.balance_for(user)
        use = min(balance, invoice.balance)
        if use <= 0:
            return Decimal('0')
        CreditEntry.objects.create(user=user, amount=-use, invoice=invoice,
                                   reason=f'Used on invoice {invoice.number}')
    finance.settle_payment(invoice, use, method='credit', reference='Account credit',
                           gateway_ref=f'CREDIT-{invoice.number}')
    return use


def _is_staff(user):
    """Whether ``user`` is Django staff/superuser or an admin/staff person."""
    if user is None:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return getattr(getattr(user, 'profile', None), 'user_type', '') in ('admin', 'staff')


def _bookings_url():
    """Where a student finds their 1-on-1s: the Online sessions page."""
    from django.urls import reverse
    return reverse('communication:meetings') + '#one-on-one'
