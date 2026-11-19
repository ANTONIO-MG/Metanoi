"""A "pay now" registrant whose payment never clears still gets the free week.

Registration offers two buttons: pay now (PayFast) or start the 7-day free week
and pay by EFT. Only the second used to start the trial, so a student who chose
to pay and whose payment failed, was cancelled, was abandoned or is still
pending had every module LOCKED — worse off than somebody who never tried to
pay. The rule now: both paths start the same seven days at sign-up; a payment
that clears turns the modules ACTIVE; an unpaid week ends exactly like a trial.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from apps.learning import access
from apps.learning import enrolment as enrol
from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment
from core.testing import make_module, make_programme

User = get_user_model()
REVIEW_URL = reverse('accounts:register-review')


def _student(email):
    """A student with a finished profile, not yet registered; returns ``(user, person)``."""
    user = User.objects.create_user(username=email, email=email, password='x',
                                    first_name='Pay', last_name='Er')
    person = Person.objects.get(user=user)
    person.user_type = 'student'
    person.profile_status = True
    person.save()
    return user, person


@override_settings(PAYMENTS_ENABLED=True)
@mock.patch('apps.communication.welcome.send_welcome_pack', lambda *a, **k: None)
@mock.patch('apps.accounts.registration_billing.deliver_registration',
            lambda *a, **k: {'sent': [], 'failed': [], 'trial_ends': timezone.now().date()})
class PayNowStartsTheFreeWeekTests(TestCase):

    """Choosing "pay now" at registration review starts the seven-day free week too."""
    def setUp(self):
        self.programme = make_programme('PAYINST', 'PGDA')
        self.frep = make_module('Financial Reporting', 'PFREP', programme=self.programme, price=500)
        self.tax = make_module('Taxation', 'PTAXA', programme=self.programme, price=500)

    def _register(self, email, action):
        """Register both modules from the review page with ``action``; returns ``(user, person, response)``."""
        user, person = _student(email)
        self.client.force_login(user)
        session = self.client.session
        session['reg'] = {'programme_id': self.programme.pk,
                          'module_ids': [self.frep.pk, self.tax.pk], 'cohort_id': 'year'}
        session.save()
        response = self.client.post(REVIEW_URL, {'action': action})
        return user, person, response

    def _rows(self, person):
        """Every enrolment row of ``person``."""
        return list(ModuleEnrolment.objects.filter(person=person))

    def test_pay_now_starts_the_seven_day_trial_at_sign_up(self):
        before = timezone.now()
        user, person, response = self._register('payer@example.com', 'pay')

        rows = self._rows(person)
        self.assertEqual(len(rows), 2)
        self.assertRedirects(
            response, reverse('finance:pay', args=[rows[0].invoice_uid]),
            fetch_redirect_response=False)
        for row in rows:
            self.assertEqual(row.status, ModuleEnrolment.STATUS_TRIAL)
            self.assertTrue(row.is_trial_valid)
            self.assertGreaterEqual(row.trial_ends_at, before + timedelta(days=7))
            self.assertLessEqual(row.trial_ends_at, timezone.now() + timedelta(days=7))

        gate = access.Gate(user, self.frep)
        self.assertTrue(gate.module_open)
        self.assertEqual(gate.reason, access.REASON_TRIAL)
        self.assertEqual(gate.module_status_label, 'Open during your free week')

    def test_pay_now_and_free_week_get_the_same_window(self):
        _, payer, _ = self._register('payer2@example.com', 'pay')
        _, trialist, _ = self._register('trialist@example.com', 'trial')
        pay_end = self._rows(payer)[0].trial_ends_at
        trial_end = self._rows(trialist)[0].trial_ends_at
        self.assertLess(abs(trial_end - pay_end), timedelta(minutes=1))
        self.assertEqual(self._rows(trialist)[0].status, ModuleEnrolment.STATUS_TRIAL)

    def test_cancelled_payment_leaves_the_free_week_in_place(self):
        user, person, _ = self._register('cancel@example.com', 'pay')
        invoice_uid = self._rows(person)[0].invoice_uid
        self.client.get(reverse('finance:payfast-cancel') + f'?inv={invoice_uid}')
        for row in self._rows(person):
            self.assertTrue(row.is_unlocked)
        self.assertTrue(access.Gate(user, self.tax).module_open)

    def test_payment_that_clears_turns_the_modules_active(self):
        user, person, _ = self._register('paid@example.com', 'pay')
        from apps.finance.models import Invoice
        invoice = Invoice.objects.get(public_id=self._rows(person)[0].invoice_uid)
        enrol.activate_modules_for_invoice(invoice)
        for row in self._rows(person):
            self.assertEqual(row.status, ModuleEnrolment.STATUS_ACTIVE)
        gate = access.Gate(user, self.frep)
        self.assertTrue(gate.module_open)
        self.assertEqual(gate.reason, access.REASON_PAID)

    def test_unpaid_week_ends_exactly_like_a_trial(self):
        payer_user, payer, _ = self._register('lapsed@example.com', 'pay')
        trial_user, trialist, _ = self._register('lapsed-trial@example.com', 'trial')
        past = timezone.now() - timedelta(minutes=1)
        ModuleEnrolment.objects.filter(person__in=[payer, trialist]).update(trial_ends_at=past)
        for user in (payer_user, trial_user):
            gate = access.Gate(user, self.frep)
            self.assertFalse(gate.module_open)
            self.assertEqual(gate.reason, access.LOCK_EXPIRED)
            self.assertEqual(gate.module_status_label, 'Access expired')

    def test_the_trial_ending_reminder_chases_an_unpaid_payer(self):
        from apps.accounts.trial_reminders import due_enrolments
        _, person, _ = self._register('remind@example.com', 'pay')
        row = self._rows(person)[0]
        due = due_enrolments(now=row.trial_ends_at - timedelta(hours=12))
        self.assertIn(row.invoice_uid, due)


class BackfillRegistrationTrialsTests(TestCase):
    """The repair for "pay now" registrants created before the fix."""

    def setUp(self):
        self.programme = make_programme('BFINST', 'PGDA')
        self.offering = make_module('Financial Reporting', 'BFREP', programme=self.programme, price=500)
        self.other = make_module('Taxation', 'BTAXA', programme=self.programme, price=500)

    def _old_pay_now(self, email, *, registered_ago=timedelta(days=1)):
        """What the pay-now path used to leave behind: LOCKED rows + an unpaid invoice."""
        _, person = _student(email)
        rows = enrol.register_modules(person, self.programme, [self.offering.pk])
        invoice = enrol.invoice_for_modules(person, rows)
        when = timezone.now() - registered_ago
        ProgrammeEnrolment.objects.filter(person=person).update(created_at=when)
        ModuleEnrolment.objects.filter(person=person).update(created_at=when)
        return person, invoice

    def _run(self, *args):
        """Run ``backfill_registration_trials`` with ``args``; return its output."""
        out = StringIO()
        call_command('backfill_registration_trials', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        person, _ = self._old_pay_now('dry@example.com')
        output = self._run()
        self.assertIn('1 enrolment(s) to start', output)
        self.assertEqual(ModuleEnrolment.objects.get(person=person).status,
                         ModuleEnrolment.STATUS_LOCKED)

    def test_apply_starts_the_week_counted_from_registration(self):
        person, _ = self._old_pay_now('apply@example.com', registered_ago=timedelta(days=2))
        self._run('--apply')
        row = ModuleEnrolment.objects.get(person=person)
        self.assertEqual(row.status, ModuleEnrolment.STATUS_TRIAL)
        self.assertEqual(row.trial_ends_at, row.created_at + timedelta(days=7))
        self.assertTrue(row.is_unlocked)

    def test_a_week_already_over_is_left_locked(self):
        person, _ = self._old_pay_now('late@example.com', registered_ago=timedelta(days=8))
        output = self._run('--apply')
        self.assertIn('1 enrolment(s) whose week has already passed', output)
        self.assertEqual(ModuleEnrolment.objects.get(person=person).status,
                         ModuleEnrolment.STATUS_LOCKED)

    def test_paid_or_cancelled_invoices_are_skipped(self):
        person, invoice = self._old_pay_now('cancelled@example.com')
        invoice.status = invoice.STATUS_CANCELLED
        invoice.save(update_fields=['status'])
        self._run('--apply')
        self.assertEqual(ModuleEnrolment.objects.get(person=person).status,
                         ModuleEnrolment.STATUS_LOCKED)

    def test_a_module_unlocked_later_is_not_a_registration(self):
        """A module unlocked days after registering is not part of the registration and stays locked.

        The unlock flow now starts the free week itself, so the test puts the row
        back the way that flow used to leave it — which is what the backfill has to
        skip.
        """
        person, _ = self._old_pay_now('later@example.com', registered_ago=timedelta(days=3))
        later, _ = enrol.unlock_single_module(person, self.other)
        ModuleEnrolment.objects.filter(pk=later.pk).update(
            status=ModuleEnrolment.STATUS_LOCKED, started_at=None, trial_ends_at=None)
        self._run('--apply')
        later.refresh_from_db()
        self.assertEqual(later.status, ModuleEnrolment.STATUS_LOCKED)
        self.assertEqual(ModuleEnrolment.objects.get(person=person, programme_module=self.offering).status,
                         ModuleEnrolment.STATUS_TRIAL)
