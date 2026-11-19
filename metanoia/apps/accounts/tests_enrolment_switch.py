"""Switching a student's enrolment, and the free week every new module starts with.

The rules these tests pin down:

* When an admin moves a student from one module (or programme) to another, the
  old modules close at once and every new priced module starts the 7-day free
  week from that moment, with an invoice raised so the reminder and pay link go
  out as they do after registration.
* A module the student already trialled or paid for gets no second free week.
* The "unlock a locked module" flow also starts the free week first, whether the
  student then pays or the payment fails.
* Staff can open exactly the modules they tick on *Record payment & grant access*.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import Person
from apps.finance.models import Invoice, InvoicePayment
from apps.learning import enrolment as enrol
from apps.learning.models import Cohort, ModuleEnrolment, ProgrammeEnrolment
from core.testing import make_module, make_programme

User = get_user_model()


def _person(email, user_type='student'):
    """A registered, onboarded user and Person of ``user_type``; returns ``(user, person)``."""
    user = User.objects.create_user(username=email, email=email, password='x',
                                    first_name='Sam', last_name='Switch')
    person = Person.objects.get(user=user)
    person.user_type = user_type
    person.profile_status = True
    person.registered = True
    person.save()
    return user, person


class SwitchServiceTests(TestCase):

    """:func:`apps.accounts.services.set_module_membership` — switching modules and programmes."""
    def setUp(self):
        self.pgda = make_programme('SWINST', 'PGDA')
        self.iac = make_programme('SWINST', 'IAC')
        self.iac_intake = Cohort.objects.create(programme=self.iac, code='JAN27', name='Jan 2027')
        self.frep = make_module('Financial Reporting', 'SWFREP', programme=self.pgda, price=500)
        self.tax = make_module('Taxation', 'SWTAX', programme=self.pgda, price=400)
        self.iac_fr = make_module('IAC Financial Reporting', 'SWIACFR', programme=self.iac, price=750)
        self.free = make_module('Orientation', 'SWFREE', programme=self.iac, price=0)
        _, self.person = _person('student@example.com')

    def _row(self, offering):
        """The person's enrolment row for ``offering``, or ``None``."""
        return ModuleEnrolment.objects.filter(person=self.person, programme_module=offering).first()

    def test_switching_module_closes_the_old_and_starts_a_free_week_on_the_new(self):
        """Switching closes the old module and starts a free week on the new one.

        The new module gets its own invoice, so the reminder has a pay link, and the
        old module's unpaid invoice is cancelled.
        """
        old = enrol.register_modules(self.person, self.pgda, [self.frep.pk])
        enrol.start_trial(old)
        old_invoice = enrol.invoice_for_modules(self.person, old)

        before = timezone.now()
        change = services.set_module_membership(
            self.person, add_ids={self.tax.pk}, remove_ids={self.frep.pk})

        self.assertIsNone(self._row(self.frep))
        tax = self._row(self.tax)
        self.assertEqual(tax.status, ModuleEnrolment.STATUS_TRIAL)
        self.assertTrue(tax.is_unlocked)
        self.assertGreaterEqual(tax.trial_ends_at, before + timedelta(days=7))
        self.assertLessEqual(tax.trial_ends_at, timezone.now() + timedelta(days=7))
        self.assertEqual(tax.trial_days_left, 7)

        self.assertIsNotNone(change.invoice)
        self.assertEqual(tax.invoice_uid, change.invoice.public_id)
        self.assertEqual(change.invoice.total, Decimal('400'))
        old_invoice.refresh_from_db()
        self.assertEqual(old_invoice.status, Invoice.STATUS_CANCELLED)
        self.assertEqual(change.cancelled_invoices, [old_invoice])

    def test_switching_programme_deactivates_the_old_one_and_places_the_new_intake(self):
        enrol.register_modules(self.person, self.pgda, [self.frep.pk, self.tax.pk])

        services.set_module_membership(
            self.person, add_ids={self.iac_fr.pk}, remove_ids={self.frep.pk, self.tax.pk})

        old = ProgrammeEnrolment.objects.get(person=self.person, programme=self.pgda)
        new = ProgrammeEnrolment.objects.get(person=self.person, programme=self.iac)
        self.assertFalse(old.is_active)
        self.assertTrue(new.is_active)
        self.assertEqual(new.cohort, self.iac_intake)
        self.assertEqual(self._row(self.iac_fr).status, ModuleEnrolment.STATUS_TRIAL)

    def test_keeping_one_module_of_a_programme_keeps_that_programme_active(self):
        enrol.register_modules(self.person, self.pgda, [self.frep.pk, self.tax.pk])
        services.set_module_membership(self.person, remove_ids={self.frep.pk})
        self.assertTrue(ProgrammeEnrolment.objects.get(
            person=self.person, programme=self.pgda).is_active)

    def test_moving_back_into_a_programme_reactivates_it(self):
        enrol.register_modules(self.person, self.pgda, [self.frep.pk])
        services.set_module_membership(self.person, add_ids={self.iac_fr.pk},
                                       remove_ids={self.frep.pk})
        services.set_module_membership(self.person, add_ids={self.tax.pk},
                                       remove_ids={self.iac_fr.pk})
        self.assertTrue(ProgrammeEnrolment.objects.get(
            person=self.person, programme=self.pgda).is_active)
        self.assertFalse(ProgrammeEnrolment.objects.get(
            person=self.person, programme=self.iac).is_active)

    def test_an_invoice_with_money_on_it_is_never_cancelled_by_a_switch(self):
        rows = enrol.register_modules(self.person, self.pgda, [self.frep.pk])
        invoice = enrol.invoice_for_modules(self.person, rows)
        InvoicePayment.objects.create(invoice=invoice, amount=Decimal('100'),
                                      status=InvoicePayment.STATUS_COMPLETED,
                                      paid_at=timezone.now())
        invoice.refresh_status()

        services.set_module_membership(self.person, add_ids={self.tax.pk},
                                       remove_ids={self.frep.pk})
        invoice.refresh_from_db()
        self.assertNotEqual(invoice.status, Invoice.STATUS_CANCELLED)

    def test_an_invoice_still_billing_a_kept_module_is_not_cancelled(self):
        rows = enrol.register_modules(self.person, self.pgda, [self.frep.pk, self.tax.pk])
        invoice = enrol.invoice_for_modules(self.person, rows)
        services.set_module_membership(self.person, remove_ids={self.frep.pk})
        invoice.refresh_from_db()
        self.assertNotEqual(invoice.status, Invoice.STATUS_CANCELLED)

    def test_free_modules_get_no_trial_and_no_invoice(self):
        change = services.set_module_membership(self.person, add_ids={self.free.pk})
        self.assertEqual(change.trialled, [])
        self.assertIsNone(change.invoice)
        self.assertTrue(self.free.is_unlocked_for(self.person))

    def test_a_module_already_trialled_gets_no_second_free_week(self):
        row = ModuleEnrolment.objects.create(
            person=self.person, programme_module=self.tax, status=ModuleEnrolment.STATUS_TRIAL,
            started_at=timezone.now() - timedelta(days=20),
            trial_ends_at=timezone.now() - timedelta(days=13))
        change = services.set_module_membership(self.person, add_ids={self.tax.pk})
        row.refresh_from_db()
        self.assertEqual(change.trialled, [])
        self.assertFalse(row.is_unlocked)

    def test_a_paid_module_is_left_as_it_is(self):
        row = ModuleEnrolment.objects.create(person=self.person, programme_module=self.tax)
        row.activate(months=1)
        paid_until = row.paid_until
        services.set_module_membership(self.person, add_ids={self.tax.pk})
        row.refresh_from_db()
        self.assertEqual(row.status, ModuleEnrolment.STATUS_ACTIVE)
        self.assertEqual(row.paid_until, paid_until)

    def test_a_locked_row_that_never_started_gets_its_free_week(self):
        ModuleEnrolment.objects.create(person=self.person, programme_module=self.tax)
        change = services.set_module_membership(self.person, add_ids={self.tax.pk})
        self.assertEqual([e.programme_module for e in change.trialled], [self.tax])

    def test_the_student_is_told_in_app(self):
        from apps.communication.models import Notification
        with self.captureOnCommitCallbacks(execute=True):
            services.set_module_membership(self.person, add_ids={self.tax.pk})
        notice = Notification.objects.filter(recipient=self.person.user,
                                             verb='enrolment-changed').get()
        self.assertIn('free week has started', notice.body)
        self.assertIn('Taxation', notice.body)

    def test_the_trial_reminder_picks_up_a_switched_module(self):
        from apps.accounts import trial_reminders
        services.set_module_membership(self.person, add_ids={self.tax.pk})
        row = self._row(self.tax)
        due = trial_reminders.due_enrolments(now=row.trial_ends_at - timedelta(hours=12))
        self.assertIn(row.invoice_uid, due)


class MemberEnrolViewTests(TestCase):

    """The member-enrol page drives the same service."""
    def setUp(self):
        self.programme = make_programme('SWVINST', 'PGDA')
        self.frep = make_module('Financial Reporting', 'SWVFREP', programme=self.programme, price=500)
        self.tax = make_module('Taxation', 'SWVTAX', programme=self.programme, price=400)
        self.admin_user, _ = _person('admin@example.com', user_type='admin')
        _, self.student = _person('learner@example.com')
        enrol.register_modules(self.student, self.programme, [self.frep.pk])

    def test_admin_switch_from_the_enrol_page(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('accounts:member-enrol', args=[self.student.pk]),
            {'modules': [str(self.tax.pk)]}, follow=True)
        self.assertEqual(response.status_code, 200)
        rows = {e.programme_module_id: e for e in ModuleEnrolment.objects.filter(person=self.student)}
        self.assertEqual(set(rows), {self.tax.pk})
        self.assertTrue(rows[self.tax.pk].is_trial_valid)
        text = ' '.join(str(m) for m in response.context['messages'])
        self.assertIn('Closed: SWVFREP', text)
        self.assertIn('Free week started for SWVTAX', text)

    def test_a_student_cannot_switch_anyone(self):
        user, _ = _person('other@example.com')
        self.client.force_login(user)
        self.client.post(reverse('accounts:member-enrol', args=[self.student.pk]),
                         {'modules': [str(self.tax.pk)]})
        self.assertTrue(ModuleEnrolment.objects.filter(
            person=self.student, programme_module=self.frep).exists())


class UnlockFlowTrialTests(TestCase):

    """The unlock flow starts the free week."""
    def setUp(self):
        self.programme = make_programme('SWUINST', 'PGDA')
        self.tax = make_module('Taxation', 'SWUTAX', programme=self.programme, price=400)
        _, self.person = _person('unlock@example.com')

    def test_unlocking_a_new_module_starts_its_free_week_and_raises_the_invoice(self):
        row, invoice = enrol.unlock_single_module(self.person, self.tax)
        row.refresh_from_db()
        self.assertTrue(row.is_trial_valid)
        self.assertIsNotNone(invoice)
        self.assertEqual(row.invoice_uid, invoice.public_id)

    def test_a_failed_payment_leaves_the_free_week_running(self):
        row, invoice = enrol.unlock_single_module(self.person, self.tax)
        InvoicePayment.objects.create(invoice=invoice, amount=Decimal('400'),
                                      status=InvoicePayment.STATUS_FAILED, paid_at=timezone.now())
        invoice.refresh_status()
        row.refresh_from_db()
        self.assertTrue(row.is_unlocked)
        self.assertEqual(row.status, ModuleEnrolment.STATUS_TRIAL)

    def test_a_successful_payment_turns_it_active(self):
        row, invoice = enrol.unlock_single_module(self.person, self.tax)
        enrol.activate_modules_for_invoice(invoice)
        row.refresh_from_db()
        self.assertEqual(row.status, ModuleEnrolment.STATUS_ACTIVE)

    def test_an_expired_free_week_is_not_restarted_by_unlocking_again(self):
        row, _ = enrol.unlock_single_module(self.person, self.tax)
        row.trial_ends_at = timezone.now() - timedelta(days=1)
        row.save(update_fields=['trial_ends_at'])
        row, _ = enrol.unlock_single_module(self.person, self.tax)
        self.assertFalse(row.is_unlocked)


class GrantSpecificModulesTests(TestCase):

    """Granting access opens only the ticked modules of the chosen student."""
    def setUp(self):
        self.programme = make_programme('SWGINST', 'PGDA')
        self.frep = make_module('Financial Reporting', 'SWGFREP', programme=self.programme, price=500)
        self.tax = make_module('Taxation', 'SWGTAX', programme=self.programme, price=400)
        self.admin_user, _ = _person('granter@example.com', user_type='admin')
        _, self.student = _person('grantee@example.com')
        _, self.other = _person('someone@example.com')
        self.rows = enrol.register_modules(self.student, self.programme, [self.frep.pk, self.tax.pk])
        self.other_row = enrol.register_modules(self.other, self.programme, [self.frep.pk])[0]
        self.client.force_login(self.admin_user)

    def _post(self, modules, student=None):
        """Post the grant-access form for ``modules``."""
        return self.client.post(reverse('finance:grant-access'), {
            'student': (student or self.student).pk, 'amount': '0', 'months': '1',
            'method': 'bank', 'modules': [str(m.pk) for m in modules]})

    def _status(self, offering, person=None):
        """The enrolment status of ``offering`` for ``person`` (default: the test student)."""
        return ModuleEnrolment.objects.get(person=person or self.student,
                                           programme_module=offering).status

    def test_only_the_ticked_module_is_opened(self):
        frep = next(r for r in self.rows if r.programme_module == self.frep)
        response = self._post([frep])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status(self.frep), ModuleEnrolment.STATUS_ACTIVE)
        self.assertEqual(self._status(self.tax), ModuleEnrolment.STATUS_LOCKED)

    def test_nothing_ticked_opens_every_unpaid_module_as_before(self):
        self._post([])
        self.assertEqual(self._status(self.frep), ModuleEnrolment.STATUS_ACTIVE)
        self.assertEqual(self._status(self.tax), ModuleEnrolment.STATUS_ACTIVE)

    def test_another_students_module_is_refused(self):
        response = self._post([self.other_row])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status(self.frep, self.other), ModuleEnrolment.STATUS_LOCKED)
        self.assertEqual(self._status(self.frep), ModuleEnrolment.STATUS_LOCKED)

    def test_the_page_lists_the_chosen_students_modules(self):
        response = self.client.get(reverse('finance:grant-access') + f'?student={self.student.pk}')
        self.assertContains(response, 'SWGFREP')
        self.assertContains(response, 'SWGTAX')


class MandatoryModulesTests(TestCase):
    """IAC: picking the programme registers all four modules, whatever is ticked."""

    def setUp(self):
        from apps.learning.models import Institution, Programme
        inst = Institution.objects.create(code='SWSAICA', name='SAICA')
        self.iac = Programme.objects.create(institution=inst, code='IAC', name='IAC',
                                            modules_mandatory=True)
        self.mods = [make_module(n, c, programme=self.iac, price=750)
                     for n, c in (('FREP', 'SWIFR'), ('TAX', 'SWITX'), ('MACF', 'SWIMA'), ('CGAU', 'SWIGA'))]
        self.user, self.person = _person('iac@example.com')
        self.person.registered = False
        self.person.save()
        self.client.force_login(self.user)

    def test_ticking_one_module_registers_all_four(self):
        response = self.client.post(reverse('accounts:register-course'), {
            'institution_id': str(self.iac.institution_id), 'programme_id': str(self.iac.pk),
            'module_ids': [str(self.mods[0].pk)]})
        self.assertRedirects(response, reverse('accounts:register-review'),
                             fetch_redirect_response=False)
        self.assertEqual(sorted(self.client.session['reg']['module_ids']),
                         sorted(m.pk for m in self.mods))

    def test_ticking_nothing_still_registers_all_four(self):
        self.client.post(reverse('accounts:register-course'), {
            'institution_id': str(self.iac.institution_id), 'programme_id': str(self.iac.pk)})
        self.assertEqual(len(self.client.session['reg']['module_ids']), 4)

    def test_review_forces_all_four_even_if_the_session_was_tampered_with(self):
        session = self.client.session
        session['reg'] = {'programme_id': self.iac.pk, 'module_ids': [self.mods[0].pk],
                          'cohort_id': 'year'}
        session.save()
        response = self.client.get(reverse('accounts:register-review'))
        self.assertEqual(response.status_code, 200)
        for module in self.mods:
            self.assertContains(response, module.code)

    def test_other_programmes_still_choose_their_own(self):
        pgda = make_programme('SWOTHER', 'PGDA')
        a = make_module('A', 'SWOA', programme=pgda, price=500)
        make_module('B', 'SWOB', programme=pgda, price=500)
        self.client.post(reverse('accounts:register-course'), {
            'institution_id': str(pgda.institution_id), 'programme_id': str(pgda.pk),
            'module_ids': [str(a.pk)]})
        self.assertEqual(self.client.session['reg']['module_ids'], [a.pk])
