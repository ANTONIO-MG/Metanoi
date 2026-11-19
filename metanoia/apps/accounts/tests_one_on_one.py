"""Registration: the one-on-one request (any institution, any programme) and
the "All 4 modules" bundle on the review page."""
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import OneOnOneRequest, Person
from apps.communication.models import Notification
from core.testing import make_module, make_programme

User = get_user_model()


def _registrant(email):
    """A student mid-registration (profile done, not yet registered); returns ``(user, person)``."""
    user = User.objects.create_user(username=email, email=email, password='x',
                                    first_name='Nomsa', last_name='Dlamini')
    person = Person.objects.get(user=user)
    person.user_type = 'student'
    person.first_name, person.last_name = 'Nomsa', 'Dlamini'
    person.profile_status = True
    person.save()
    return user, person


def _team(email, role='admin'):
    """An onboarded team member of ``role``; returns ``(user, person)``."""
    user = User.objects.create_user(username=email, email=email, password='x')
    person = Person.objects.get(user=user)
    person.user_type, person.registered, person.profile_status = role, True, True
    person.first_name, person.last_name = 'Thandi', role.title()
    person.save()
    return user, person


@mock.patch('apps.communication.welcome.send_welcome_pack', lambda *a, **k: None)
class OneOnOneRequestTests(TestCase):

    """Requesting one-on-one coaching, and staff working the request queue."""
    def setUp(self):
        self.admin, _ = _team('admin@example.com')
        self.user, self.person = _registrant('nomsa@example.com')
        self.client.force_login(self.user)
        self.data = {'o2o-institution': 'University of Johannesburg',
                     'o2o-programme': 'BCom Accounting', 'o2o-start_date': '2027-01-15',
                     'o2o-end_date': '2027-06-30', 'o2o-modules': 'Financial Accounting 3B',
                     'o2o-notes': 'Evenings please'}

    def test_registration_page_offers_one_on_one(self):
        response = self.client.get(reverse('accounts:register-course'))
        self.assertContains(response, 'One-on-one coaching')
        self.assertContains(response, '/ hour')
        self.assertContains(response, 'name="o2o-institution_choice"')

    def test_request_is_recorded_and_staff_notified(self):
        response = self.client.post(reverse('accounts:register-one-on-one'), self.data)
        self.assertRedirects(response, reverse('accounts:register-complete') + '?mode=oneonone-request',
                             fetch_redirect_response=False)
        req = OneOnOneRequest.objects.get()
        self.assertEqual((req.person, req.institution, req.status),
                         (self.person, 'University of Johannesburg', OneOnOneRequest.STATUS_NEW))
        self.person.refresh_from_db()
        self.assertTrue(self.person.registered)
        self.assertTrue(Notification.objects.filter(recipient=self.admin,
                                                    title='New one-on-one request').exists())

    def test_end_before_start_is_rejected(self):
        self.data['o2o-end_date'] = '2026-12-01'
        response = self.client.post(reverse('accounts:register-one-on-one'), self.data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'cannot be before the start date', status_code=400)
        self.assertFalse(OneOnOneRequest.objects.exists())

    def test_staff_assign_and_mark_enrolled(self):
        """Staff assign an educator and mark the request enrolled; enrolling without an educator is refused."""
        _, educator = _team('coach@example.com', 'educator')
        req = OneOnOneRequest.objects.create(
            person=self.person, institution='UJ', programme='BCom', start_date='2027-01-01',
            end_date='2027-02-01')
        self.client.force_login(self.admin)
        page = self.client.get(reverse('staffdesk:ops-one-on-one'))
        self.assertContains(page, 'Nomsa Dlamini')
        url = reverse('staffdesk:ops-one-on-one-action', args=[req.pk])
        self.client.post(url, {'action': 'enrol', 'educator': ''})
        req.refresh_from_db()
        self.assertEqual(req.status, OneOnOneRequest.STATUS_NEW)
        self.client.post(url, {'action': 'enrol', 'educator': educator.pk, 'admin_notes': 'Tue 18:00'})
        req.refresh_from_db()
        self.assertEqual((req.status, req.educator, req.admin_notes),
                         (OneOnOneRequest.STATUS_ENROLLED, educator, 'Tue 18:00'))
        self.assertIsNotNone(req.enrolled_at)
        self.assertTrue(Notification.objects.filter(recipient=self.user).exists())

    def test_students_cannot_open_the_queue(self):
        self.person.registered = True
        self.person.save(update_fields=['registered'])
        response = self.client.get(reverse('staffdesk:ops-one-on-one'))
        self.assertRedirects(response, reverse('myhub:index'), fetch_redirect_response=False)


@override_settings(PAYMENTS_ENABLED=True)
@mock.patch('apps.communication.welcome.send_welcome_pack', lambda *a, **k: None)
@mock.patch('apps.accounts.registration_billing.deliver_registration',
            lambda *a, **k: {'sent': [], 'failed': [], 'trial_ends': timezone.now().date()})
class BundledRegistrationTests(TestCase):

    """Registration review with several modules applies the bundle pricing."""
    def setUp(self):
        self.programme = make_programme('MILPARK', 'PGDA')
        self.mods = [make_module(name, code, programme=self.programme, price=500)
                     for name, code in [('Management Accounting', 'MACF'),
                                        ('Financial Reporting', 'FREP'),
                                        ('Taxation', 'TAXA'),
                                        ('Governance and Auditing', 'CGAU')]]
        call_command('apply_module_pricing', stdout=StringIO())

    def _review(self, email, n):
        """Sign in a new registrant with ``n`` modules chosen in the session; returns the person."""
        user, person = _registrant(email)
        self.client.force_login(user)
        session = self.client.session
        session['reg'] = {'programme_id': self.programme.pk,
                          'module_ids': [m.pk for m in self.mods[:n]], 'cohort_id': 'year'}
        session.save()
        return person

    def test_review_shows_the_bundle_and_invoice_agrees(self):
        from apps.finance.models import Invoice
        from apps.learning.models import ModuleEnrolment

        person = self._review('four@example.com', 4)
        page = self.client.get(reverse('accounts:register-review'))
        self.assertEqual(page.context['total'], Decimal('2200.00'))
        self.assertContains(page, 'All 4 modules')
        self.client.post(reverse('accounts:register-review'), {'action': 'trial'})
        uid = ModuleEnrolment.objects.filter(person=person).first().invoice_uid
        invoice = Invoice.objects.get(public_id=uid)
        self.assertEqual(invoice.total, Decimal('2200.00'))
        self.assertEqual(invoice.discount_total, Decimal('800.00'))

    def test_three_modules_pay_the_module_fee(self):
        self._review('three@example.com', 3)
        page = self.client.get(reverse('accounts:register-review'))
        self.assertEqual(page.context['total'], Decimal('2250.00'))
        self.assertNotContains(page, 'All 4 modules')
