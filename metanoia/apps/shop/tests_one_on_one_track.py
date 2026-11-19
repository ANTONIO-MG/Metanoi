"""The one-on-one track (apps.shop.one_on_one) — owner's rules of 2026-11-10.

Registration on the one-on-one path, modules always open, hours bought per
module per hour, booking only with paid hours and a settled account, staff
scheduling on account, the 24-hour cancel rule, re-sending the invoice after
the session, the R100 overtime charge, and the private feed.
"""
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.communication.models import Attendance, ChatGroup, ClassSession, MeetingRoom
from apps.finance import services as finance_services
from apps.learning.access import Gate
from apps.learning.models import ModuleEnrolment
from core.testing import make_module, make_programme

from . import one_on_one as o2o
from .models import Booking, EducatorAvailability, EducatorRate, HourPurchase, Product, SessionHours

User = get_user_model()


def _person(username, user_type='student', **extra):
    """Create a registered user of ``user_type``; returns ``(user, person)``."""
    user = User.objects.create_user(username, f'{username}@example.com', 'pw12345!',
                                    first_name=username.title(), **extra)
    person = user.profile
    person.user_type = user_type
    person.first_name = username.title()
    person.registered = person.profile_status = True
    person.save()
    return user, person


@override_settings(TIME_ZONE='UTC', MS_TEAMS_ENABLED=False, VAT_REGISTERED=False,
                   PAYMENTS_ENABLED=True, BOOKING_TIME_ZONE='UTC')
@mock.patch('apps.shop.one_on_one.send_invoice', lambda invoice: True)
@mock.patch('apps.communication.welcome.send_welcome_pack', lambda *a, **k: None)
class OneOnOneTrackTests(TestCase):
    """Registration, access, booking, cancelling and follow-up on the one-on-one track.

    Invoices are not really sent and welcome packs are skipped (both patched).
    """

    def setUp(self):
        """An IAC-like programme (modules compulsory for classes) with two modules, a
        coach teaching both, staff, an admin, a student and the hourly booking
        service with the coach's hours on the test day.
        """
        self.programme = make_programme('O2OINST', 'IACO')
        self.programme.modules_mandatory = True
        self.programme.save()
        self.frep = make_module('Financial Reporting', 'OFREP', programme=self.programme, price=750)
        self.tax = make_module('Taxation', 'OTAX', programme=self.programme, price=750)
        self.coach_user, self.coach = _person('coach', 'educator')
        self.frep.educators.add(self.coach)
        self.tax.educators.add(self.coach)
        self.staff_user, _ = _person('staffer', 'staff')
        self.admin_user, _ = _person('boss', 'admin')
        self.student, self.person = _person('thandi')

        self.service = Product.objects.create(
            sku=o2o.BOOKING_SKU, name='One-on-one session', fulfilment=Product.FULFIL_BOOKING,
            price=Decimal('300'), session_lengths='60', buffer_minutes=0, min_notice_hours=1,
            status='active')
        self.rate = EducatorRate.objects.create(product=self.service, educator=self.coach,
                                                hourly_rate=Decimal('300'))
        self.day = timezone.localdate() + timedelta(days=3)
        EducatorAvailability.objects.create(educator=self.coach, weekday=self.day.weekday(),
                                            start_time=time(8), end_time=time(17))

    def at(self, hour, day=None):
        """An aware datetime at ``hour`` on ``day`` (default the test day)."""
        return timezone.make_aware(timezone.datetime.combine(day or self.day, time(hour)),
                                   timezone.get_current_timezone())

    def enrol(self):
        """Put the student on the one-on-one track for both modules."""
        o2o.enrol(self.person, self.programme, [self.frep.pk, self.tax.pk])
        self.person.refresh_from_db()

    def pay(self, invoice):
        """Settle ``invoice`` in full by EFT."""
        finance_services.settle_payment(invoice, invoice.balance, reference='EFT')
        invoice.refresh_from_db()

    def give_hours(self, module, hours):
        """Buy and pay for ``hours`` on ``module`` for the student."""
        purchase = o2o.create_purchase(self.student, [(module, hours)], by=self.student, send=False)
        self.pay(purchase.invoice)
        return purchase

    def _unregistered(self):
        """Mark the student as not yet registered."""
        self.person.registered = False
        self.person.save(update_fields=['registered'])

    def test_registration_one_on_one_path(self):
        """Compulsory modules are a group-class rule, so the one-on-one path keeps the
        choice. Hours are credited only once paid, and paying again (a replayed
        webhook) never doubles them.
        """
        self._unregistered()
        self.client.force_login(self.student)
        response = self.client.post(reverse('accounts:register-course'), {
            'track': 'one_on_one', 'institution_id': str(self.programme.institution_id),
            'programme_id': str(self.programme.pk), 'cohort_id': 'year',
            'module_ids': [str(self.frep.pk)], f'hours_{self.frep.pk}': '2'})
        self.assertRedirects(response, reverse('accounts:register-review'), fetch_redirect_response=False)
        reg = self.client.session['reg']
        self.assertEqual(reg['module_ids'], [self.frep.pk])
        self.assertEqual(reg['track'], 'one_on_one')
        page = self.client.get(reverse('accounts:register-review'))
        self.assertContains(page, 'one-on-one')
        self.assertEqual(page.context['total'], Decimal('600'))

        response = self.client.post(reverse('accounts:register-review'), {'action': 'invoice'})
        self.assertEqual(response.status_code, 302)
        self.person.refresh_from_db()
        self.assertTrue(self.person.is_one_on_one)
        self.assertTrue(self.person.registered)
        enrolment = ModuleEnrolment.objects.get(person=self.person, programme_module=self.frep)
        self.assertEqual(enrolment.status, ModuleEnrolment.STATUS_ACTIVE)
        self.assertIsNone(enrolment.paid_until)
        self.assertEqual(enrolment.price_at_enrolment, 0)
        purchase = HourPurchase.objects.get(user=self.student)
        self.assertEqual(purchase.invoice.total, Decimal('600'))
        self.assertEqual(o2o.balance(self.student, self.frep), 0)
        self.pay(purchase.invoice)
        self.assertEqual(o2o.balance(self.student, self.frep), 2)
        o2o.credit_paid_purchase(purchase.invoice)
        self.assertEqual(o2o.balance(self.student, self.frep), 2)

    def test_class_path_still_enforces_compulsory_modules(self):
        self._unregistered()
        self.client.force_login(self.student)
        self.client.post(reverse('accounts:register-course'), {
            'institution_id': str(self.programme.institution_id),
            'programme_id': str(self.programme.pk), 'cohort_id': 'year',
            'module_ids': [str(self.frep.pk)]})
        self.assertEqual(sorted(self.client.session['reg']['module_ids']),
                         sorted([self.frep.pk, self.tax.pk]))
        self.assertEqual(self.client.session['reg']['track'], 'class')

    def test_modules_stay_open_and_class_sessions_hidden(self):
        self.enrol()
        gate = Gate(self.student, self.frep)
        self.assertTrue(gate.module_open)
        self.assertTrue(gate.one_on_one)
        klass = MeetingRoom.objects.create(
            title='FREP class', host=self.coach_user, audience=MeetingRoom.AUDIENCE_MODULE,
            module=self.frep, scheduled_start=self.at(9), scheduled_end=self.at(10))
        everyone = MeetingRoom.objects.create(
            title='Welcome briefing', host=self.coach_user, audience=MeetingRoom.AUDIENCE_EVERYONE,
            scheduled_start=self.at(11), scheduled_end=self.at(12))
        from apps.livesessions.audience import visible_sessions
        seen = set(visible_sessions(self.student).values_list('pk', flat=True))
        self.assertNotIn(klass.pk, seen)
        self.assertIn(everyone.pk, seen)

    def test_feed_is_private(self):
        from apps.communication.community import programme_ids_for
        from apps.communication.services import module_chat_users
        self.enrol()
        self.assertEqual(programme_ids_for(self.student), set())
        self.assertNotIn(self.student, list(module_chat_users(self.frep)))
        group = o2o.ensure_coaching_group(self.student)
        members = set(group.memberships.values_list('user_id', flat=True))
        self.assertTrue({self.student.pk, self.coach_user.pk, self.staff_user.pk,
                         self.admin_user.pk} <= members)
        self.assertEqual(ChatGroup.objects.filter(coaching_for=self.student).count(), 1)
        other, _ = _person('classmate')
        self.assertNotIn(other.pk, members)

    def test_book_needs_paid_hours(self):
        """Once the hour is spent, a second booking is refused."""
        self.enrol()
        with self.assertRaises(o2o.OneOnOneError):
            o2o.book_with_hours(self.student, self.rate, self.frep, self.at(9))
        self.give_hours(self.frep, 1)
        booking = o2o.book_with_hours(self.student, self.rate, self.frep, self.at(9))
        self.assertEqual(booking.status, Booking.STATUS_CONFIRMED)
        self.assertEqual(booking.paid_by, Booking.PAID_HOURS)
        self.assertEqual(o2o.balance(self.student, self.frep), 0)
        self.assertIsNotNone(booking.meeting)
        self.assertEqual(booking.meeting.module_id, self.frep.pk)
        with self.assertRaises(o2o.OneOnOneError):
            o2o.book_with_hours(self.student, self.rate, self.frep, self.at(11))

    def test_booking_page_books_with_hours(self):
        self.enrol()
        self.give_hours(self.tax, 1)
        self.client.force_login(self.student)
        url = reverse('shop:one-on-one-educator', args=[self.coach.pk])
        page = self.client.get(url + f'?module={self.tax.pk}')
        self.assertContains(page, 'Book with my hours')
        response = self.client.post(url, {'rate': self.rate.pk, 'module': self.tax.pk,
                                          'start': self.at(10).isoformat()})
        self.assertRedirects(response, reverse('shop:o2o-hours'), fetch_redirect_response=False)
        self.assertTrue(Booking.objects.filter(student=self.student, programme_module=self.tax,
                                               status=Booking.STATUS_CONFIRMED).exists())

    def test_staff_schedule_on_account_blocks_next_booking(self):
        """A session on account is confirmed (the student may attend before paying).
        While it is unsettled neither the student, the educator nor staff can book
        the next one — unless an admin overrides; paying settles the account again.
        """
        self.enrol()
        booking = o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), by=self.staff_user)
        self.assertEqual(booking.paid_by, Booking.PAID_ACCOUNT)
        self.assertEqual(booking.invoice.total, Decimal('300'))
        self.assertEqual(booking.status, Booking.STATUS_CONFIRMED)
        self.assertFalse(o2o.account_settled(self.student))
        self.give_hours(self.tax, 1)
        with self.assertRaises(o2o.OneOnOneError):
            o2o.book_with_hours(self.student, self.rate, self.tax, self.at(11))
        with self.assertRaises(o2o.OneOnOneError):
            o2o.schedule_session(self.student, self.coach, self.frep, self.at(13), by=self.coach_user)
        o2o.schedule_session(self.student, self.coach, self.frep, self.at(13), by=self.admin_user,
                             override=True)
        for invoice in o2o.outstanding_invoices(self.student):
            self.pay(invoice)
        self.assertTrue(o2o.account_settled(self.student))
        o2o.book_with_hours(self.student, self.rate, self.tax, self.at(15))

    def test_staff_schedule_uses_hours_first(self):
        self.enrol()
        self.give_hours(self.frep, 2)
        booking = o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), 120,
                                       by=self.coach_user)
        self.assertEqual(booking.paid_by, Booking.PAID_HOURS)
        self.assertIsNone(booking.invoice)
        self.assertEqual(o2o.balance(self.student, self.frep), 0)

    def test_educator_cannot_schedule_module_they_do_not_teach(self):
        self.enrol()
        _, stranger = _person('stranger', 'educator')
        with self.assertRaises(o2o.OneOnOneError):
            o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), by=stranger.user)

    def test_cancel_24_hour_rule(self):
        """Early cancel gives the hour back; a late cancel uses it; an educator
        cancelling always gives it back.
        """
        self.enrol()
        self.give_hours(self.frep, 2)
        early = o2o.book_with_hours(self.student, self.rate, self.frep, self.at(9))
        o2o.cancel_booking(early, by=self.student)
        self.assertEqual(o2o.balance(self.student, self.frep), 2)

        soon_day = timezone.localdate() + timedelta(days=1)
        EducatorAvailability.objects.create(educator=self.coach, weekday=soon_day.weekday(),
                                            start_time=time(0), end_time=time(23))
        start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=5)
        late = o2o.schedule_session(self.student, self.coach, self.frep, start, by=self.staff_user,
                                    ignore_availability=True)
        self.assertEqual(o2o.balance(self.student, self.frep), 1)
        o2o.cancel_booking(late, by=self.student)
        self.assertEqual(o2o.balance(self.student, self.frep), 1)

        again = o2o.schedule_session(self.student, self.coach, self.frep, start + timedelta(hours=2),
                                     by=self.staff_user, ignore_availability=True)
        o2o.cancel_booking(again, by=self.coach_user)
        self.assertEqual(o2o.balance(self.student, self.frep), 1)

    def test_cancel_on_account_session_cancels_invoice(self):
        self.enrol()
        booking = o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), by=self.staff_user)
        o2o.cancel_booking(booking, by=self.student)
        booking.invoice.refresh_from_db()
        self.assertEqual(booking.invoice.status, booking.invoice.STATUS_CANCELLED)
        self.assertTrue(o2o.account_settled(self.student))

    def _past_session(self, minutes_attended):
        """A session that ended two hours ago, attended for ``minutes_attended`` per Teams."""
        booking = o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), by=self.staff_user)
        Booking.objects.filter(pk=booking.pk).update(
            start=timezone.now() - timedelta(hours=3), end=timezone.now() - timedelta(hours=2))
        booking.refresh_from_db()
        session = ClassSession.objects.create(module=self.frep, meeting=booking.meeting)
        Attendance.objects.create(session=session, student=self.student,
                                  source=Attendance.SOURCE_TEAMS, seconds_attended=minutes_attended * 60)
        return booking

    def test_follow_up_resends_invoice_and_charges_overtime(self):
        """Running the follow-up again changes nothing."""
        self.enrol()
        sent = []
        with mock.patch('apps.shop.one_on_one.send_invoice', lambda inv: sent.append(inv.pk) or True):
            booking = self._past_session(minutes_attended=95)
            sent.clear()
            o2o.follow_up()
        booking.refresh_from_db()
        self.assertIsNotNone(booking.invoice_resent_at)
        self.assertIn(booking.invoice_id, sent)
        self.assertEqual(booking.measured_minutes, 95)
        self.assertIsNotNone(booking.overtime_invoice)
        self.assertEqual(booking.overtime_invoice.total, Decimal('100'))
        self.assertIn(booking.overtime_invoice_id, sent)
        o2o.follow_up()
        self.assertEqual(Booking.objects.get(pk=booking.pk).overtime_invoice_id, booking.overtime_invoice_id)

    def test_no_overtime_within_half_an_hour(self):
        self.enrol()
        booking = self._past_session(minutes_attended=85)
        o2o.follow_up()
        booking.refresh_from_db()
        self.assertIsNone(booking.overtime_invoice)
        self.assertIsNotNone(booking.overtime_checked_at)

    def test_overtime_waits_for_teams_report(self):
        self.enrol()
        booking = o2o.schedule_session(self.student, self.coach, self.frep, self.at(9), by=self.staff_user)
        Booking.objects.filter(pk=booking.pk).update(
            start=timezone.now() - timedelta(hours=3), end=timezone.now() - timedelta(hours=2))
        o2o.follow_up()
        self.assertIsNone(Booking.objects.get(pk=booking.pk).overtime_checked_at)

    def test_pages_render(self):
        self.enrol()
        self.give_hours(self.frep, 1)
        self.client.force_login(self.student)
        page = self.client.get(reverse('shop:o2o-hours'))
        self.assertContains(page, 'Hours left')
        response = self.client.post(reverse('shop:o2o-hours'), {
            'action': 'buy', 'pay': 'eft', f'hours_{self.tax.pk}': '3'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(HourPurchase.objects.filter(user=self.student).count(), 2)
        self.client.force_login(self.staff_user)
        self.assertContains(self.client.get(reverse('shop:o2o-students')), 'Thandi')
        url = reverse('shop:o2o-student', args=[self.student.pk])
        self.assertContains(self.client.get(url), 'Schedule a session')
        response = self.client.post(url, {
            'action': 'schedule', 'module': self.frep.pk, 'educator': self.coach.pk,
            'date': self.day.isoformat(), 'time': '09:00', 'hours': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(o2o.balance(self.student, self.frep), 0)
        self.client.post(url, {'action': 'adjust', 'module': self.frep.pk, 'hours': '2',
                               'reason': 'Goodwill'})
        self.assertEqual(o2o.balance(self.student, self.frep), 2)
        self.assertEqual(SessionHours.objects.filter(kind=SessionHours.KIND_ADJUST).count(), 1)

    def test_class_student_cannot_open_hours_page(self):
        other, _ = _person('classy')
        self.client.force_login(other)
        self.assertRedirects(self.client.get(reverse('shop:o2o-hours')), reverse('shop:one-on-one'),
                             fetch_redirect_response=False)

    def test_switch_track(self):
        self.enrol()
        o2o.switch_track(self.person, 'class', by=self.staff_user)
        row = ModuleEnrolment.objects.get(person=self.person, programme_module=self.frep)
        self.assertEqual(row.status, ModuleEnrolment.STATUS_LOCKED)
        o2o.switch_track(self.person, 'one_on_one', by=self.staff_user)
        row.refresh_from_db()
        self.assertEqual(row.status, ModuleEnrolment.STATUS_ACTIVE)
        with self.assertRaises(o2o.OneOnOneError):
            o2o.switch_track(self.person, 'class', by=self.student)

    def test_session_form_refuses_one_on_one_invitee(self):
        from apps.livesessions.forms import SessionForm
        self.enrol()
        form = SessionForm(user=self.staff_user)
        form.cleaned_data = {'target': SessionForm.TARGET_PEOPLE, 'invitees': [self.student],
                             'scheduled_start': self.at(9), 'scheduled_end': self.at(10)}
        form._errors = {}
        with mock.patch('django.forms.ModelForm.clean', lambda f: f.cleaned_data):
            form.clean()
        self.assertIn('invitees', form.errors)
