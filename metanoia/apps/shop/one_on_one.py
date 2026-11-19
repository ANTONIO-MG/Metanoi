"""The one-on-one track: private coaching bought by the hour.

The rules (owner, 2026-11-10):

* A student is on **one** track for everything (``Person.learning_track``):
  group classes billed per module per month, or one-on-one.
* One-on-one costs :func:`hourly_rate` (R300) **per module per hour**. Hours are
  bought per module; one session is for one module and uses its hours.
* One-on-one modules are always open — the schedule, documents, mocks, their
  own session recordings — whatever is owed. Group classes and class
  recordings are not part of the track and stay hidden.
* Hours are paid before the student can book. Staff or the educator may also
  schedule a session and send an invoice; the student can still attend it,
  and the invoice is sent again after the session.
* A session that runs more than :data:`OVERTIME_GRACE_MINUTES` past its booked
  length (an hour → past 1h30) adds an :data:`OVERTIME_FEE` (R100) invoice.
* While a session or overtime invoice is unpaid the account is **not settled**:
  nobody can book or schedule the next session (an admin may override).
* Paid hours never expire. A student who cancels at least
  :data:`CANCEL_NOTICE_HOURS` before the start gets the hour back; a later
  cancel or a no-show uses it. A cancel by staff or the educator always gives
  it back.
* The student has a private chat with their educators and the Metanoia team
  (:func:`ensure_coaching_group`) instead of the cohort's module chats.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import Booking, EducatorRate, HourPurchase, HourPurchaseLine, Product, SessionHours

logger = logging.getLogger('apps')

DEFAULT_HOURLY_RATE = Decimal('300')
"""What an hour costs when the catalogue item has no price of its own."""
OVERTIME_FEE = Decimal('100')
"""The extra charge when a session runs long."""
OVERTIME_GRACE_MINUTES = 30
"""How far past the booked length a session may run before the extra charge."""
CANCEL_NOTICE_HOURS = 24
"""A student cancelling at least this long before the start gets the hour back."""
OVERTIME_WAIT_HOURS = 48
"""How long to wait for Teams' attendance report before giving up on overtime."""
MAX_HOURS_PER_MODULE = 20
"""Most hours one module can be bought in one go."""
BOOKING_SKU = 'SHOP-1ON1-HOUR'
"""The bookable shop service whose weekly calendar one-on-one sessions use
(``manage.py ensure_one_on_one_rates``).
"""


class OneOnOneError(Exception):
    """The request breaks a one-on-one rule; the message is for the person."""


def person_of(user):
    """The :class:`Person` profile of ``user``, or ``None``."""
    return getattr(user, 'profile', None) if user is not None else None


def is_one_on_one(user):
    """Whether ``user`` is on the one-on-one track."""
    person = person_of(user)
    return bool(person is not None and person.is_one_on_one)


def hourly_rate():
    """Rand per module per hour: the ``SVC-1ON1-HOUR`` catalogue price, else R300."""
    from .catalogue import SKU_SESSION
    price = Product.objects.filter(sku=SKU_SESSION).values_list('price', flat=True).first()
    return Decimal(str(price)) if price else DEFAULT_HOURLY_RATE


def _session_product():
    """The invoice-only 1-on-1 hour catalogue item, creating the standard items if missing."""
    from .catalogue import SKU_SESSION, ensure_standard_items
    product = Product.objects.filter(sku=SKU_SESSION).first()
    if product is None:
        ensure_standard_items()
        product = Product.objects.filter(sku=SKU_SESSION).first()
    return product


def is_staff(user):
    """Whether ``user`` counts as staff for the one-on-one tools."""
    from .booking import _is_staff
    return _is_staff(user)


def is_admin(user):
    """Whether ``user`` is a superuser or an admin person."""
    if user is None:
        return False
    return bool(user.is_superuser or getattr(person_of(user), 'user_type', '') == 'admin')


def student_modules(user):
    """The modules a one-on-one student is registered for (General excluded)."""
    from apps.learning.models import ProgrammeModule
    person = person_of(user)
    if person is None:
        return ProgrammeModule.objects.none()
    return (ProgrammeModule.objects.without_general()
            .filter(enrolments__person=person, is_active=True)
            .select_related('programme__institution', 'module').distinct()
            .order_by('programme__name', 'order', 'pk'))


def teaches(user, programme_module):
    """Whether ``user`` is one of ``programme_module``'s educators."""
    person = person_of(user)
    return bool(person is not None
                and programme_module.educators.filter(pk=person.pk).exists())


def balances(user):
    """``{programme_module_id: hours}`` left to book, never negative."""
    rows = (SessionHours.objects.filter(user=user).values('programme_module_id')
            .annotate(total=Sum('hours')))
    return {r['programme_module_id']: max(r['total'] or Decimal('0'), Decimal('0')) for r in rows}


def balance(user, programme_module):
    """Hours ``user`` has left to book on ``programme_module``."""
    return balances(user).get(programme_module.pk, Decimal('0'))


def outstanding_invoices(user):
    """Unpaid session and overtime invoices — what keeps the account unsettled.

    Unpaid *hour* invoices are not debt: the hours simply are not bought yet.

    Every session ever booked counts: a late cancel still owes its invoice, and
    an overtime charge stands whatever happened to the session afterwards.
    """
    from apps.finance.models import Invoice
    ids = set()
    for invoice_id, overtime_id in (Booking.objects.filter(student=user)
                                    .values_list('invoice_id', 'overtime_invoice_id')):
        ids.update(i for i in (invoice_id, overtime_id) if i)
    if not ids:
        return Invoice.objects.none()
    return (Invoice.objects.filter(pk__in=ids)
            .exclude(status__in=[Invoice.STATUS_PAID, Invoice.STATUS_CANCELLED, Invoice.STATUS_DRAFT])
            .order_by('issue_date', 'pk'))


def amount_owed(user):
    """Total still owed on ``user``'s one-on-one invoices."""
    return sum((inv.balance for inv in outstanding_invoices(user)), Decimal('0'))


def account_settled(user):
    """Whether ``user`` owes nothing on one-on-one invoices."""
    return not outstanding_invoices(user).exists()


def unsettled_message(user):
    """The message shown when a new session cannot be booked until earlier ones are paid."""
    owed = amount_owed(user)
    return (f'There is R{owed:,.2f} outstanding on earlier one-on-one sessions. Please settle '
            'it first — the next session can be booked as soon as the payment is recorded.')


def _raise_invoice(user, lines, *, by=None, due=None):
    """One finance invoice for ``user`` with ``lines`` of
    ``(description, quantity, unit_price)``, marked sent."""
    from apps.finance.models import Invoice, InvoiceItem
    product = _session_product()
    invoice = Invoice.objects.create(
        customer=user, created_by=by if getattr(by, 'pk', None) else user,
        status=Invoice.STATUS_SENT, due_date=due or timezone.localdate())
    for description, quantity, unit_price in lines:
        InvoiceItem.objects.create(invoice=invoice, product=product, description=description[:255],
                                   quantity=quantity, unit_price=unit_price)
    invoice.recalc_total()
    invoice.refresh_status()
    return invoice


def send_invoice(invoice):
    """E-mail the pay-link for ``invoice``. Best effort."""
    try:
        from apps.finance.emails import send_invoice_email
        send_invoice_email(invoice)
        return True
    except Exception:  # pragma: no cover - the invoice stands either way
        logger.exception('one-on-one: invoice e-mail failed for %s', invoice.number)
        return False


def _notify(user, title, body, *, url='', level='info', email=False, actor=None, meeting=None):
    """Send a one-on-one notification; failures are logged, never raised."""
    try:
        from apps.communication.services import notify
        notify(user, title=title, body=body, verb='one-on-one', level=level, url=url,
               email=email, actor=actor, meeting=meeting)
    except Exception:  # pragma: no cover
        logger.exception('one-on-one: notice failed for %s', user)


def hours_url():
    """URL of the student's one-on-one hours page."""
    from django.urls import reverse
    return reverse('shop:o2o-hours')


def _clean_lines(user, lines):
    """Validate ``(programme_module, hours)`` purchase lines for ``user``.

    Zero-hour lines are dropped; a module the student is not on, or more than
    :data:`MAX_HOURS_PER_MODULE` hours, raises :class:`OneOnOneError`.
    """
    allowed = {pm.pk: pm for pm in student_modules(user)}
    out = []
    for programme_module, hours in lines:
        hours = int(hours or 0)
        if hours <= 0:
            continue
        if programme_module.pk not in allowed:
            raise OneOnOneError(f'{programme_module.code} is not one of your modules.')
        if hours > MAX_HOURS_PER_MODULE:
            raise OneOnOneError(f'At most {MAX_HOURS_PER_MODULE} hours per module at a time.')
        out.append((allowed[programme_module.pk], hours))
    if not out:
        raise OneOnOneError('Tick at least one module and choose how many hours.')
    return out


@transaction.atomic
def create_purchase(user, lines, *, by=None, send=True):
    """Invoice ``user`` for hours: ``lines`` is ``[(programme_module, hours)]``.

    The hours are credited when the invoice is paid. Returns the HourPurchase.
    """
    lines = _clean_lines(user, lines)
    rate = hourly_rate()
    invoice = _raise_invoice(user, [
        (f'One-on-one · {pm.display_name} ({pm.code}) — {hours} h', hours, rate)
        for pm, hours in lines], by=by)
    purchase = HourPurchase.objects.create(user=user, invoice=invoice, created_by=by)
    for pm, hours in lines:
        HourPurchaseLine.objects.create(purchase=purchase, programme_module=pm, hours=hours,
                                        unit_price=rate)
    if send:
        transaction.on_commit(lambda: send_invoice(invoice))
    return purchase


def credit_paid_purchase(invoice):
    """The invoice behind an hour purchase is paid: add its hours. Idempotent."""
    with transaction.atomic():
        purchase = (HourPurchase.objects.select_for_update()
                    .filter(invoice=invoice).first())
        if purchase is None or purchase.credited_at is not None:
            return None
        for line in purchase.lines.select_related('programme_module'):
            SessionHours.objects.create(
                user=purchase.user, programme_module=line.programme_module, hours=line.hours,
                kind=SessionHours.KIND_PURCHASE, purchase=purchase,
                reason=f'Bought on invoice {invoice.number}')
        purchase.credited_at = timezone.now()
        purchase.save(update_fields=['credited_at', 'updated_at'])
    summary = ', '.join(f'{line.programme_module.code} {line.hours} h' for line in purchase.lines.all())
    _notify(purchase.user, 'Your one-on-one hours are ready',
            f'Payment received — {summary}. Book your session whenever suits you.',
            url=hours_url(), level='success', email=True)
    return purchase


def adjust_hours(user, programme_module, hours, *, by, reason):
    """Staff add (or take away) hours by hand, with the reason on record."""
    if not is_staff(by):
        raise OneOnOneError('Only staff can adjust hours.')
    hours = Decimal(str(hours))
    if not hours:
        raise OneOnOneError('Enter the number of hours to add or remove.')
    if not (reason or '').strip():
        raise OneOnOneError('Please give a reason — the student sees it on their statement.')
    if hours < 0 and balance(user, programme_module) + hours < 0:
        raise OneOnOneError('That would take the balance below zero.')
    return SessionHours.objects.create(user=user, programme_module=programme_module, hours=hours,
                                       kind=SessionHours.KIND_ADJUST, reason=reason.strip()[:255],
                                       created_by=by)


def settled_invoice(invoice):
    """A session or overtime invoice was paid: tell the student when the account
    is clear again, so they know they can book."""
    booking = (Booking.objects.filter(Q(invoice=invoice) | Q(overtime_invoice=invoice))
               .select_related('student').first())
    if booking is None:
        return
    if account_settled(booking.student):
        _notify(booking.student, 'Your account is settled',
                'Thank you — your payment is recorded. You can book your next one-on-one session.',
                url=hours_url(), level='success')


def open_module(enrolment):
    """One-on-one access: open-ended, nothing monthly to pay."""
    now = timezone.now()
    enrolment.status = enrolment.STATUS_ACTIVE
    enrolment.paid_until = None
    enrolment.paid_from = enrolment.paid_from or timezone.localdate()
    enrolment.started_at = enrolment.started_at or now
    enrolment.trial_ends_at = None
    enrolment.price_at_enrolment = 0
    enrolment.invoice_uid = None
    enrolment.save(update_fields=['status', 'paid_until', 'paid_from', 'started_at',
                                  'trial_ends_at', 'price_at_enrolment', 'invoice_uid', 'updated_at'])
    return enrolment


@transaction.atomic
def enrol(person, programme, module_ids, cohort_id=None):
    """Register ``person`` on the one-on-one track for ``module_ids``."""
    from apps.learning import enrolment as learning_enrolment
    if person.learning_track != person.TRACK_ONE_ON_ONE:
        person.learning_track = person.TRACK_ONE_ON_ONE
        person.save(update_fields=['learning_track'])
    rows = learning_enrolment.register_modules(person, programme, module_ids, cohort_id=cohort_id)
    for row in rows:
        open_module(row)
    transaction.on_commit(lambda: refresh_rooms(person))
    return rows


@transaction.atomic
def switch_track(person, track, *, by):
    """Move a student between group classes and one-on-one (staff only).

    To one-on-one: every module opens, open-ended. To classes: every module
    locks until the monthly fee is paid (staff then use Grant access as usual).
    """
    if not is_staff(by):
        raise OneOnOneError('Only staff can change a student\'s track.')
    if track not in (person.TRACK_CLASS, person.TRACK_ONE_ON_ONE):
        raise OneOnOneError('Unknown track.')
    if person.learning_track == track:
        return person
    from apps.learning.models import ModuleEnrolment
    rows = list(ModuleEnrolment.objects.filter(person=person).select_related('programme_module'))
    person.learning_track = track
    person.save(update_fields=['learning_track'])
    if track == person.TRACK_ONE_ON_ONE:
        for row in rows:
            open_module(row)
    else:
        for row in rows:
            row.status = ModuleEnrolment.STATUS_LOCKED
            row.paid_until = None
            row.price_at_enrolment = row.programme_module.price_per_month or 0
            row.save(update_fields=['status', 'paid_until', 'price_at_enrolment', 'updated_at'])
    transaction.on_commit(lambda: refresh_rooms(person))
    return person


def refresh_rooms(person):
    """Put the student in the right conversations for their track: their module
    chats follow enrolment (one-on-one students are left out), and a one-on-one
    student gets their private space. Best effort."""
    try:
        from apps.communication.services import sync_module_chat_members
        for offering in {e.programme_module for e in
                         person.module_enrolments.select_related('programme_module')}:
            sync_module_chat_members(offering)
    except Exception:  # pragma: no cover
        logger.exception('one-on-one: module chat sync failed for %s', person)
    if person.is_one_on_one and person.user_id:
        try:
            ensure_coaching_group(person.user)
        except Exception:  # pragma: no cover
            logger.exception('one-on-one: coaching group failed for %s', person)


def coaching_members(user):
    """The student, every educator of their modules, and the Metanoia team."""
    from django.contrib.auth import get_user_model

    from .fulfilment import staff_recipients
    User = get_user_model()
    ids = {user.pk}
    for pm in student_modules(user):
        ids.update(pm.educators.values_list('user_id', flat=True))
    ids.update(staff_recipients().values_list('pk', flat=True))
    ids.discard(None)
    return list(User.objects.filter(pk__in=ids, is_active=True))


def ensure_coaching_group(user):
    """The one-on-one student's private space, with its members brought up to date."""
    from apps.communication.models import ChatGroup
    from apps.communication.services import _reconcile_members
    from core.utils import display_name
    group = ChatGroup.objects.filter(coaching_for=user).first()
    if group is None:
        group = ChatGroup.objects.create(
            kind=ChatGroup.KIND_CUSTOM, coaching_for=user,
            name=f'One-on-one · {display_name(user)}'[:200],
            description='Your private space with your educators and the Metanoia team.')
    if not group.is_active:
        group.is_active = True
        group.save(update_fields=['is_active', 'updated_at'])
    _reconcile_members(group, coaching_members(user))
    return group


def booking_rates(programme_module=None):
    """The educators' bookable hourly rates, the module's own educators first."""
    rates = (EducatorRate.objects.filter(is_active=True, product__sku=BOOKING_SKU,
                                         product__status='active', educator__user__is_active=True)
             .select_related('product', 'educator__user'))
    if programme_module is None:
        return list(rates)
    teaching = set(programme_module.educators.values_list('pk', flat=True))
    return sorted(rates, key=lambda r: (r.educator_id not in teaching, r.pk))


def _hours_for(minutes):
    """``minutes`` as hours, to two decimal places."""
    return (Decimal(minutes) / Decimal(60)).quantize(Decimal('0.01'))


def _check_length(minutes):
    """Sessions are whole hours, at most four."""
    if minutes <= 0 or minutes % 60:
        raise OneOnOneError('Sessions are booked in whole hours.')
    if minutes > 4 * 60:
        raise OneOnOneError('A single session can be at most 4 hours.')


def _create_booking(*, student, rate_or_educator, product, programme_module, start, minutes,
                    paid_by, price, note, by):
    """Create the (held) :class:`Booking` row for a one-on-one session."""
    educator = getattr(rate_or_educator, 'educator', rate_or_educator)
    return Booking.objects.create(
        product=product, educator=educator, student=student, start=start,
        end=start + timedelta(minutes=minutes), minutes=minutes, price=price,
        status=Booking.STATUS_HELD, hold_expires_at=None, paid_by=paid_by,
        programme_module=programme_module, note=(note or '')[:2000], booked_by=by)


def _attach_module(booking):
    """File the session under its module, so it shows on the module page."""
    meeting = booking.meeting
    if meeting is None or booking.programme_module_id is None:
        return
    try:
        meeting.module = booking.programme_module
        meeting.programme = booking.programme_module.programme
        meeting.institution = booking.programme_module.programme.institution
        meeting.save(update_fields=['module', 'programme', 'institution'])
    except Exception:  # pragma: no cover
        logger.exception('one-on-one: could not file session %s under its module', meeting.pk)


def _confirm(booking):
    """Confirm the booking (meeting room and all) and attach it to its module."""
    from . import booking as booking_service
    booking_service.confirm(booking)
    _attach_module(booking)
    return booking


def book_with_hours(user, rate, programme_module, start, minutes=60, *, note=''):
    """The student books a free slot with hours they have already paid for.

    The student's ledger is locked as well as the rate, so two tabs cannot spend one hour twice.
    """
    from . import booking as booking_service

    if not is_one_on_one(user):
        raise OneOnOneError('Hour booking is for one-on-one students.')
    _check_length(minutes)
    if not student_modules(user).filter(pk=programme_module.pk).exists():
        raise OneOnOneError('Choose one of your own modules.')
    if not account_settled(user):
        raise OneOnOneError(unsettled_message(user))
    hours = _hours_for(minutes)
    with transaction.atomic():
        EducatorRate.objects.select_for_update().get(pk=rate.pk)
        list(SessionHours.objects.select_for_update().filter(user=user,
                                                             programme_module=programme_module))
        if balance(user, programme_module) < hours:
            raise OneOnOneError(f'You have {balance(user, programme_module):g} hour(s) left on '
                                f'{programme_module.code}. Buy more hours first.')
        if not booking_service.is_free(rate, start, minutes):
            raise OneOnOneError('Sorry — that time is not free. Please pick another.')
        booking = _create_booking(student=user, rate_or_educator=rate, product=rate.product,
                                  programme_module=programme_module, start=start, minutes=minutes,
                                  paid_by=Booking.PAID_HOURS, price=hourly_rate() * hours,
                                  note=note, by=user)
        SessionHours.objects.create(user=user, programme_module=programme_module, hours=-hours,
                                    kind=SessionHours.KIND_BOOKING, booking=booking,
                                    reason=f'Session {booking.label}')
    return _confirm(booking)


def may_schedule(by, programme_module):
    """Whether ``by`` may schedule sessions on ``programme_module`` (staff or its educator)."""
    return is_staff(by) or teaches(by, programme_module)


def schedule_session(student, educator, programme_module, start, minutes=60, *, by, note='',
                     override=False, ignore_availability=False):
    """Staff or the educator put a session in the diary.

    Uses the student's hours on the module when they have enough; otherwise the
    session is booked **on account** and an invoice is sent (the student may
    attend before paying). Refused while the account is unsettled, unless an
    admin overrides.

    Staff are not held to the notice period or booking window.
    """
    from . import booking as booking_service

    if not may_schedule(by, programme_module):
        raise OneOnOneError('Only staff or the module\'s educator can schedule this session.')
    if not is_one_on_one(student):
        raise OneOnOneError('This student is not on the one-on-one track.')
    _check_length(minutes)
    if not student_modules(student).filter(pk=programme_module.pk).exists():
        raise OneOnOneError('The student is not registered for that module.')
    if start <= timezone.now():
        raise OneOnOneError('Choose a time in the future.')
    if not account_settled(student) and not (override and is_admin(by)):
        raise OneOnOneError(unsettled_message(student) + (
            '' if is_admin(by) else ' An admin can override this.'))

    hours = _hours_for(minutes)
    rate = (EducatorRate.objects.filter(educator=educator, product__sku=BOOKING_SKU)
            .select_related('product').first())
    product = rate.product if rate is not None else _session_product()
    with transaction.atomic():
        if rate is not None:
            EducatorRate.objects.select_for_update().get(pk=rate.pk)
        list(SessionHours.objects.select_for_update().filter(user=student,
                                                             programme_module=programme_module))
        end = start + timedelta(minutes=minutes)
        clash = bool(booking_service.blocking_bookings(educator, start, end))
        if clash:
            raise OneOnOneError(f'{educator} already has a one-on-one at that time.')
        if (not ignore_availability and rate is not None
                and not booking_service.is_free(
                    rate, start, minutes,
                    now=start - timedelta(hours=rate.product.min_notice_hours))):
            raise OneOnOneError('That time is outside the educator\'s open hours or clashes with '
                                'their calendar. Tick "Ignore open hours" to book it anyway.')
        use_hours = balance(student, programme_module) >= hours
        booking = _create_booking(
            student=student, rate_or_educator=educator, product=product,
            programme_module=programme_module, start=start, minutes=minutes,
            paid_by=Booking.PAID_HOURS if use_hours else Booking.PAID_ACCOUNT,
            price=hourly_rate() * hours, note=note, by=by)
        if use_hours:
            SessionHours.objects.create(user=student, programme_module=programme_module,
                                        hours=-hours, kind=SessionHours.KIND_BOOKING,
                                        booking=booking, created_by=by,
                                        reason=f'Session {booking.label}')
        else:
            booking.invoice = _raise_invoice(student, [(
                f'One-on-one · {programme_module.display_name} ({programme_module.code}) — '
                f'{booking.label}', int(hours) or 1, hourly_rate())],
                by=by, due=timezone.localdate(start))
            booking.save(update_fields=['invoice', 'updated_at'])
    _confirm(booking)
    if booking.invoice is not None:
        send_invoice(booking.invoice)
        _notify(student, 'Invoice for your one-on-one session',
                f'{booking.label} ({programme_module.code}) is booked. Your invoice for '
                f'R{booking.invoice.total:,.2f} has been e-mailed — please pay it so you can book '
                'your next session.', url=hours_url(), email=False, actor=by)
    return booking


def _late(booking, now=None):
    """Whether cancelling now is inside the :data:`CANCEL_NOTICE_HOURS` notice period."""
    now = now or timezone.now()
    return booking.start - now < timedelta(hours=CANCEL_NOTICE_HOURS)


def cancel_booking(booking, *, by, reason=''):
    """Cancel a one-on-one session under the 24-hour rule (see the module doc).

    Shop-checkout bookings keep their own rule (:func:`apps.shop.booking.cancel`).

    With notice, an already-paid session's money becomes hours to book again,
    and an unpaid invoice is cancelled. A late cancel keeps its invoice: the
    session is still owed.
    """
    from . import booking as booking_service

    if booking.paid_by == Booking.PAID_CART:
        return booking_service.cancel(booking, by=by, reason=reason)
    is_student = getattr(by, 'pk', None) == booking.student_id
    is_coach = booking.educator.user_id == getattr(by, 'pk', None)
    if not (is_student or is_coach or is_staff(by)):
        raise OneOnOneError('You cannot cancel this session.')
    if booking.status not in Booking.BLOCKING:
        raise OneOnOneError('This session is not active.')
    if booking.start <= timezone.now():
        raise OneOnOneError('This session has already started.')
    gives_back = not (is_student and not is_staff(by) and _late(booking))
    hours = _hours_for(booking.minutes)
    note = ''
    with transaction.atomic():
        booking.status = Booking.STATUS_CANCELLED
        booking.cancelled_by = by
        booking.cancelled_reason = (reason or '')[:255]
        booking.save(update_fields=['status', 'cancelled_by', 'cancelled_reason', 'updated_at'])
        if booking.paid_by == Booking.PAID_HOURS and gives_back and booking.programme_module_id:
            SessionHours.objects.create(user=booking.student, programme_module=booking.programme_module,
                                        hours=hours, kind=SessionHours.KIND_RETURN, booking=booking,
                                        created_by=by, reason=f'Cancelled {booking.label}')
            note = f' {hours:g} hour(s) are back on your balance.'
        elif booking.paid_by == Booking.PAID_ACCOUNT and booking.invoice_id:
            invoice = booking.invoice
            if gives_back:
                if invoice.amount_paid > 0 and booking.programme_module_id:
                    SessionHours.objects.create(
                        user=booking.student, programme_module=booking.programme_module,
                        hours=hours, kind=SessionHours.KIND_RETURN, booking=booking,
                        created_by=by, reason=f'Cancelled {booking.label} (paid)')
                    note = f' You had paid, so {hours:g} hour(s) are on your balance.'
                elif invoice.status != invoice.STATUS_CANCELLED:
                    invoice.status = invoice.STATUS_CANCELLED
                    invoice.save(update_fields=['status', 'updated_at'])
                    note = f' Invoice {invoice.number} is cancelled.'
        if not gives_back:
            note = (f' It was cancelled less than {CANCEL_NOTICE_HOURS} hours before the start, '
                    'so the hour is used.')
    if booking.meeting is not None:
        try:
            from apps.livesessions import services as live
            live.cancel_session(booking.meeting, by=by, reason=reason)
        except Exception:  # pragma: no cover
            logger.exception('one-on-one: could not cancel the session for booking %s', booking.pk)
    body = f'{booking.label} with {booking.educator} is cancelled.{note}'
    if reason:
        body += f' Reason: {reason}'
    for user in {booking.student, booking.educator.user} - {None, by}:
        _notify(user, 'One-on-one cancelled', body, url=hours_url(), level='warning',
                email=True, actor=by)
    return booking


def measured_minutes(booking):
    """How long the Teams call ran, from the attendance report: the longest
    time anyone spent in it. ``None`` until Teams has reported."""
    if booking.meeting_id is None:
        return None
    from django.db.models import Max

    from apps.communication.models import Attendance
    seconds = (Attendance.objects.filter(session__meeting_id=booking.meeting_id,
                                         source=Attendance.SOURCE_TEAMS)
               .aggregate(m=Max('seconds_attended'))['m'])
    return round(seconds / 60) if seconds else None


def charge_overtime(booking, *, by=None):
    """Raise and send the overtime invoice for ``booking`` (once)."""
    if booking.overtime_invoice_id:
        return booking.overtime_invoice
    label = booking.programme_module.code if booking.programme_module_id else booking.product.name
    invoice = _raise_invoice(booking.student, [(
        f'One-on-one overtime · {label} — {booking.label} ran past '
        f'{booking.minutes + OVERTIME_GRACE_MINUTES} min', 1, OVERTIME_FEE)], by=by)
    booking.overtime_invoice = invoice
    booking.save(update_fields=['overtime_invoice', 'updated_at'])
    send_invoice(invoice)
    _notify(booking.student, 'Overtime on your one-on-one',
            f'{booking.label} ran longer than {booking.minutes + OVERTIME_GRACE_MINUTES} minutes, so '
            f'an extra R{OVERTIME_FEE:,.0f} invoice has been e-mailed. Your next session can be '
            'booked once it is paid.', url=hours_url(), level='warning')
    return invoice


def follow_up(now=None):
    """After each one-on-one session: send an unpaid invoice again, and charge
    overtime once Teams reports how long the call ran. Run by the scheduler.

    A session Teams has not reported on yet is skipped until :data:`OVERTIME_WAIT_HOURS` have passed.
    """
    now = now or timezone.now()
    done = Booking.objects.filter(
        paid_by__in=(Booking.PAID_HOURS, Booking.PAID_ACCOUNT),
        status__in=(Booking.STATUS_CONFIRMED, Booking.STATUS_COMPLETED),
        end__lte=now - timedelta(minutes=15)).select_related('invoice', 'student', 'product',
                                                              'programme_module')
    resent = charged = 0
    for booking in done.filter(paid_by=Booking.PAID_ACCOUNT, invoice__isnull=False,
                               invoice_resent_at__isnull=True):
        if booking.invoice.status not in (booking.invoice.STATUS_PAID, booking.invoice.STATUS_CANCELLED):
            send_invoice(booking.invoice)
            _notify(booking.student, 'Thank you for your session — invoice attached',
                    f'Your invoice {booking.invoice.number} for {booking.label} is still open: '
                    f'R{booking.invoice.balance:,.2f}. Please settle it to book your next session.',
                    url=hours_url(), level='info')
            resent += 1
        booking.invoice_resent_at = now
        booking.save(update_fields=['invoice_resent_at', 'updated_at'])
    for booking in done.filter(overtime_checked_at__isnull=True):
        minutes = measured_minutes(booking)
        if minutes is None and booking.end > now - timedelta(hours=OVERTIME_WAIT_HOURS):
            continue
        booking.measured_minutes = minutes
        booking.overtime_checked_at = now
        booking.save(update_fields=['measured_minutes', 'overtime_checked_at', 'updated_at'])
        if minutes and minutes > booking.minutes + OVERTIME_GRACE_MINUTES:
            charge_overtime(booking)
            charged += 1
    return f'{resent} invoice(s) re-sent, {charged} overtime charge(s)'


def statement(user):
    """What the hours page shows: per-module balances and the account state."""
    bal = balances(user)
    modules = [{'module': pm, 'hours': bal.get(pm.pk, Decimal('0'))} for pm in student_modules(user)]
    upcoming = (Booking.objects.filter(student=user, status=Booking.STATUS_CONFIRMED,
                                       end__gte=timezone.now())
                .select_related('educator__user', 'programme_module', 'meeting', 'invoice')
                .order_by('start'))
    past = (Booking.objects.filter(student=user, end__lt=timezone.now())
            .exclude(status=Booking.STATUS_HELD)
            .select_related('educator__user', 'programme_module', 'invoice', 'overtime_invoice')
            .order_by('-start')[:20])
    pending = (HourPurchase.objects.filter(user=user, credited_at__isnull=True)
               .select_related('invoice').prefetch_related('lines__programme_module')
               .exclude(invoice__status='cancelled'))
    return {
        'modules': modules, 'rate': hourly_rate(), 'overtime_fee': OVERTIME_FEE,
        'grace': OVERTIME_GRACE_MINUTES, 'notice_hours': CANCEL_NOTICE_HOURS,
        'outstanding': list(outstanding_invoices(user)), 'owed': amount_owed(user),
        'settled': account_settled(user), 'upcoming': list(upcoming), 'past': list(past),
        'pending': [p for p in pending if p.invoice_id],
        'ledger': list(SessionHours.objects.filter(user=user).select_related('programme_module')[:30]),
        'max_hours': MAX_HOURS_PER_MODULE, 'any_hours': any(m['hours'] > 0 for m in modules),
    }
