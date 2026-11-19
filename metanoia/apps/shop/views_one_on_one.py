"""One-on-one track pages (rules in :mod:`apps.shop.one_on_one`).

* ``/shop/one-on-one/hours/`` — the student's hours, invoices and sessions; buy
  hours; cancel a session.
* ``/shop/one-on-one/students/`` — staff and educators: one-on-one students.
* ``/shop/one-on-one/students/<id>/`` — one student: schedule a session, send
  an hours invoice, adjust hours, overtime, change track.
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import one_on_one as o2o
from .models import Booking

User = get_user_model()


def _hour_lines(request, modules):
    """``[(programme_module, hours)]`` from ``hours_<module id>`` fields."""
    lines = []
    for pm in modules:
        raw = (request.POST.get(f'hours_{pm.pk}') or '').strip()
        if raw.isdigit() and int(raw) > 0:
            lines.append((pm, int(raw)))
    return lines


@login_required
def hours(request):
    """A one-on-one student's hours page.

    Staff and educators are redirected to the students list; other users to the
    one-on-one landing page. A POST with ``action=buy`` creates and e-mails an
    hours invoice (``pay=now`` goes straight to payment). Each module row gets a
    ``book`` link to the first bookable rate for it (see :func:`_book_url`).
    """
    if not o2o.is_one_on_one(request.user):
        if o2o.is_staff(request.user) or getattr(o2o.person_of(request.user), 'user_type', '') == 'educator':
            return redirect('shop:o2o-students')
        messages.info(request, 'One-on-one hours are for students on the one-on-one plan.')
        return redirect('shop:one-on-one')

    if request.method == 'POST' and request.POST.get('action') == 'buy':
        try:
            purchase = o2o.create_purchase(request.user, _hour_lines(request, o2o.student_modules(request.user)),
                                           by=request.user, send=True)
        except o2o.OneOnOneError as exc:
            messages.error(request, str(exc))
            return redirect('shop:o2o-hours')
        invoice = purchase.invoice
        if request.POST.get('pay') == 'now':
            return redirect('finance:pay', public_id=invoice.public_id)
        messages.success(request, f'Invoice {invoice.number} for R{invoice.total:,.2f} has been e-mailed. '
                                  'Your hours are added as soon as it is paid.')
        return redirect('shop:o2o-hours')

    ctx = o2o.statement(request.user)
    for row in ctx['modules']:
        row['book'] = _book_url(o2o.booking_rates(row['module']), row['module'])
    ctx['page_title'] = 'My one-on-one sessions'
    return render(request, 'shop/o2o_hours.html', ctx)


def _book_url(rates, programme_module):
    """Where a module's "Book" button goes: the first rate's educator week, or ``''`` if none."""
    if not rates:
        return ''
    rate = rates[0]
    return (reverse('shop:one-on-one-educator', args=[rate.educator_id])
            + f'?service={rate.pk}&module={programme_module.pk}')


@login_required
@require_POST
def cancel(request, pk):
    """Cancel a one-on-one session (POST); permission rules live in :func:`o2o.cancel_booking`.

    Any error is shown as a message: besides :class:`OneOnOneError` the cart path
    can raise ``booking.BookingError``, hence the broad catch. Redirects to a
    local ``next`` URL when given, else the hours page.
    """
    booking = get_object_or_404(Booking.objects.select_related('educator__user', 'invoice'), pk=pk)
    try:
        o2o.cancel_booking(booking, by=request.user, reason=request.POST.get('reason', '').strip())
        messages.success(request, 'Session cancelled.')
    except o2o.OneOnOneError as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, str(exc))
    nxt = request.POST.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith('//'):
        return redirect(nxt)
    return redirect('shop:o2o-hours')


def _may_manage(user):
    """Staff and educators may manage one-on-one students."""
    person = o2o.person_of(user)
    return o2o.is_staff(user) or getattr(person, 'user_type', '') == 'educator'


def _students_for(user):
    """Active one-on-one students ``user`` may see: all for staff, own module students for educators."""
    qs = (User.objects.filter(is_active=True, profile__learning_track='one_on_one')
          .select_related('profile'))
    if not o2o.is_staff(user):
        person = o2o.person_of(user)
        qs = qs.filter(profile__module_enrolments__programme_module__educators=person)
    return qs.distinct().order_by('first_name', 'last_name')


@login_required
def students(request):
    """Staff/educator list of one-on-one students with hours, amount owed and next session.

    ``?q=`` filters on every word against name and e-mail; at most 300 rows.
    """
    if not _may_manage(request.user):
        raise PermissionDenied
    term = (request.GET.get('q') or '').strip()[:80]
    qs = _students_for(request.user)
    if term:
        for word in term.split():
            qs = qs.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word)
                           | Q(email__icontains=word))
    rows = []
    for student in qs[:300]:
        bal = o2o.balances(student)
        rows.append({
            'user': student, 'hours': sum(bal.values()) if bal else 0,
            'owed': o2o.amount_owed(student),
            'next': (Booking.objects.filter(student=student, status=Booking.STATUS_CONFIRMED,
                                            start__gte=timezone.now()).order_by('start').first()),
        })
    return render(request, 'shop/o2o_students.html', {
        'page_title': 'One-on-one students', 'rows': rows, 'search': term,
        'is_staff': o2o.is_staff(request.user),
    })


def _parse_local(date_value, time_value):
    """Aware datetime from ``YYYY-MM-DD`` and ``HH:MM`` in the current time zone, or ``None``."""
    try:
        moment = datetime.strptime(f'{date_value} {time_value}', '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(moment, timezone.get_current_timezone())


def _coaches_for(programme_modules):
    """Active educators of these modules plus everyone with a booking rate."""
    from apps.accounts.models import Person
    ids = set()
    for pm in programme_modules:
        ids.update(pm.educators.values_list('pk', flat=True))
    rate_people = {r.educator_id for r in o2o.booking_rates()}
    return (Person.objects.filter(Q(pk__in=ids) | Q(pk__in=rate_people), user__is_active=True)
            .select_related('user').distinct().order_by('first_name', 'last_name'))


@login_required
def student(request, user_id):
    """One one-on-one student's management page (staff and the student's educators).

    POST ``action`` is one of ``schedule`` (book a session, paid from hours or
    invoiced), ``invoice`` (send an hours invoice), ``adjust`` (change hours),
    ``overtime`` (staff only: invoice overtime on a booking) or ``track``
    (switch learning track; leaving one-on-one returns to the list).
    """
    if not _may_manage(request.user):
        raise PermissionDenied
    target = get_object_or_404(_students_for(request.user) if not o2o.is_staff(request.user)
                               else User.objects.select_related('profile'), pk=user_id)
    person = o2o.person_of(target)
    if person is None:
        raise PermissionDenied
    staff = o2o.is_staff(request.user)
    modules = list(o2o.student_modules(target))
    here = reverse('shop:o2o-student', args=[target.pk])

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'schedule':
                pm = next((m for m in modules if str(m.pk) == request.POST.get('module')), None)
                coach = _coaches_for(modules).filter(pk=request.POST.get('educator') or 0).first()
                start = _parse_local(request.POST.get('date'), request.POST.get('time'))
                if pm is None or coach is None or start is None:
                    raise o2o.OneOnOneError('Choose the module, the educator, the date and the time.')
                minutes = int(request.POST.get('hours') or 1) * 60
                booking = o2o.schedule_session(
                    target, coach, pm, start, minutes, by=request.user,
                    note=request.POST.get('note', ''),
                    override=bool(request.POST.get('override')),
                    ignore_availability=bool(request.POST.get('ignore_hours')))
                how = ('paid from their hours' if booking.paid_by == Booking.PAID_HOURS
                       else f'invoice {booking.invoice.number} sent')
                messages.success(request, f'Scheduled {booking.label} ({pm.code}) — {how}.')
            elif action == 'invoice':
                purchase = o2o.create_purchase(target, _hour_lines(request, modules), by=request.user)
                messages.success(request, f'Invoice {purchase.invoice.number} for '
                                          f'R{purchase.invoice.total:,.2f} sent to the student.')
            elif action == 'adjust':
                pm = next((m for m in modules if str(m.pk) == request.POST.get('module')), None)
                if pm is None:
                    raise o2o.OneOnOneError('Choose a module.')
                o2o.adjust_hours(target, pm, request.POST.get('hours') or 0, by=request.user,
                                 reason=request.POST.get('reason', ''))
                messages.success(request, 'Hours adjusted.')
            elif action == 'overtime':
                if not staff:
                    raise PermissionDenied
                booking = get_object_or_404(Booking, pk=request.POST.get('booking'), student=target)
                invoice = o2o.charge_overtime(booking, by=request.user)
                messages.success(request, f'Overtime invoice {invoice.number} sent.')
            elif action == 'track':
                o2o.switch_track(person, request.POST.get('track'), by=request.user)
                messages.success(request, f'{target.get_full_name() or target.username} is now on '
                                          f'{person.get_learning_track_display().lower()}.')
                if not person.is_one_on_one:
                    return redirect('shop:o2o-students')
            else:
                messages.error(request, 'Unknown action.')
        except o2o.OneOnOneError as exc:
            messages.error(request, str(exc))
        except (TypeError, ValueError):
            messages.error(request, 'Please check the numbers you entered.')
        return redirect(here)

    ctx = o2o.statement(target)
    ctx.update({
        'page_title': f'One-on-one · {target.get_full_name() or target.username}',
        'student': target, 'person': person, 'is_staff': staff,
        'is_admin': o2o.is_admin(request.user),
        'coaches': _coaches_for(modules),
        'today': timezone.localdate(),
    })
    return render(request, 'shop/o2o_student.html', ctx)
