"""Undo for payments: a mistaken payment can be taken back, fully.

The client recorded a payment against the wrong student and had no way back.
These tests pin what "undo" must mean: the payment is voided (kept, struck
out, with who/when/why), the invoice is owed again, and the access the payment
opened is closed again — status, paid dates and the paid periods that keep
weeks open — and only administrators / staff may do it.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ActivityLog, Person, UndoAction
from apps.learning.enrolment import invoice_for_modules
from apps.learning.models import ModuleEnrolment, PaidPeriod
from core.testing import make_module

from . import access, models, services

User = get_user_model()


def _person(user, user_type):
    """Set the user's profile type and onboarding flags; return the profile."""
    person = Person.all_objects.get(user=user)
    person.user_type = user_type
    person.registered = True
    person.profile_status = True
    person.first_name = user.username.title()
    person.save()
    return person


class PaymentUndoTests(TestCase):
    """Undoing payments, proofs, invoice voids and deletions (:mod:`apps.finance.undo`)."""
    def setUp(self):
        self.admin = User.objects.create_user('fin-admin', 'fin-admin@example.com', 'pw-12345!x',
                                              is_staff=True)
        _person(self.admin, 'admin')
        self.student = User.objects.create_user('thandi', 'thandi@example.com', 'pw-12345!x',
                                                first_name='Thandi')
        self.person = _person(self.student, 'student')
        self.offering = make_module('Financial Reporting', price=Decimal('500'))
        self.enrolment = ModuleEnrolment.objects.create(
            person=self.person, programme_module=self.offering, price_at_enrolment=Decimal('500'))
        self.invoice = invoice_for_modules(self.person, [self.enrolment])
        self.invoice.status = models.Invoice.STATUS_SENT
        self.invoice.due_date = timezone.localdate() + timedelta(days=7)
        self.invoice.save()
        self.person.pending_invoice_uid = self.invoice.public_id
        self.person.save(update_fields=['pending_invoice_uid'])
        self.client.force_login(self.admin)

    def _record_manual_payment(self):
        """Record a full manual payment on the invoice through the detail page."""
        response = self.client.post(reverse('finance:invoice-detail', args=[self.invoice.pk]), {
            'action': 'pay', 'amount': str(self.invoice.total), 'method': 'bank',
            'reference': 'EFT 123'}, follow=True)
        self.assertEqual(response.status_code, 200)
        return response

    def _undo(self, action, reason='Recorded against the wrong student', **extra):
        """POST the undo form for ``action``."""
        return self.client.post(reverse('accounts:undo', args=[action.pk]),
                                {'reason': reason, **extra})

    def test_recording_a_payment_offers_undo_in_the_message(self):
        response = self._record_manual_payment()
        action = UndoAction.objects.get(kind='payment')
        self.assertContains(response, reverse('accounts:undo', args=[action.pk]))
        self.assertContains(response, 'Undo')

    def test_undoing_a_manual_payment_restores_invoice_and_access(self):
        """The payment does everything a payment does, and the undo takes all of it back.
        Voided money no longer counts anywhere, and a second undo of the same thing is
        refused.
        """
        self._record_manual_payment()
        self.invoice.refresh_from_db()
        self.enrolment.refresh_from_db()
        self.person.refresh_from_db()
        self.assertEqual(self.invoice.status, models.Invoice.STATUS_PAID)
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_ACTIVE)
        self.assertIsNotNone(self.enrolment.paid_from)
        self.assertTrue(PaidPeriod.objects.filter(enrolment=self.enrolment).exists())
        self.assertIsNone(self.person.pending_invoice_uid)
        payment = self.invoice.payments.get()
        self.assertEqual(payment.recorded_by, self.admin)

        action = UndoAction.objects.get(kind='payment')
        page = self.client.get(reverse('accounts:undo', args=[action.pk]))
        self.assertContains(page, 'Why is this being undone?')
        response = self._undo(action)
        self.assertEqual(response.status_code, 302)

        payment.refresh_from_db()
        self.assertEqual(payment.status, models.InvoicePayment.STATUS_VOIDED)
        self.assertEqual(payment.voided_by, self.admin)
        self.assertEqual(payment.void_reason, 'Recorded against the wrong student')
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, models.Invoice.STATUS_SENT)
        self.assertEqual(self.invoice.balance, self.invoice.total)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_LOCKED)
        self.assertIsNone(self.enrolment.paid_from)
        self.assertIsNone(self.enrolment.paid_until)
        self.assertFalse(self.enrolment.is_unlocked)
        self.assertFalse(PaidPeriod.objects.filter(enrolment=self.enrolment).exists())
        self.person.refresh_from_db()
        self.assertEqual(self.person.pending_invoice_uid, self.invoice.public_id)
        self.assertFalse(models.ProofOfPayment.objects.filter(
            payment=payment, status=models.ProofOfPayment.STATUS_VERIFIED).exists())

        action.refresh_from_db()
        self.assertEqual(action.undone_by, self.admin)
        log = ActivityLog.objects.filter(action='undo').latest('timestamp')
        self.assertIn('Recorded against the wrong student', log.description)
        self.assertEqual(log.actor.user, self.admin)

        self.assertEqual(models.InvoicePayment.objects.counted().filter(
            invoice=self.invoice).count(), 0)
        self._undo(action)
        self.assertEqual(ActivityLog.objects.filter(action='undo').count(), 1)

    def test_a_reason_is_required(self):
        self._record_manual_payment()
        action = UndoAction.objects.get(kind='payment')
        response = self._undo(action, reason='  ')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.invoice.payments.get().status, models.InvoicePayment.STATUS_COMPLETED)

    def test_non_staff_cannot_undo(self):
        self._record_manual_payment()
        action = UndoAction.objects.get(kind='payment')
        for user_type in ('student', 'educator', 'parent'):
            other = User.objects.create_user(f'u-{user_type}', f'{user_type}@example.com', 'pw-12345!x')
            _person(other, user_type)
            self.client.force_login(other)
            self._undo(action)
            self.client.get(reverse('finance:payment-undo', args=[self.invoice.payments.get().pk]))
        payment = self.invoice.payments.get()
        self.assertEqual(payment.status, models.InvoicePayment.STATUS_COMPLETED)
        action.refresh_from_db()
        self.assertIsNone(action.undone_at)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_ACTIVE)

    def test_payfast_payment_needs_an_explicit_acknowledgement(self):
        """Without the acknowledgement the undo is refused."""
        payment = services.settle_payment(
            self.invoice, self.invoice.total, gateway=models.InvoicePayment.GATEWAY_PAYFAST,
            method='payfast', gateway_ref='pf-1', reference='pf-1')
        action = payment.undo_action
        page = self.client.get(reverse('accounts:undo', args=[action.pk]))
        self.assertContains(page, 'does NOT refund')
        self._undo(action)
        payment.refresh_from_db()
        self.assertEqual(payment.status, models.InvoicePayment.STATUS_COMPLETED)
        self._undo(action, acknowledge='1')
        payment.refresh_from_db()
        self.assertEqual(payment.status, models.InvoicePayment.STATUS_VOIDED)

    def test_manual_grant_without_invoice_is_undone_from_the_proof(self):
        self.invoice.status = models.Invoice.STATUS_CANCELLED
        self.invoice.save(update_fields=['status'])
        proof, payment, modules = access.grant_access(
            self.person, amount=Decimal('500'), months=2, granted_by=self.admin, invoice=None)
        self.assertIsNone(payment)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_ACTIVE)

        start = self.client.get(reverse('finance:proof-undo', args=[proof.pk]))
        self.assertEqual(start.status_code, 302)
        self._undo(proof.undo_action)
        proof.refresh_from_db()
        self.assertEqual(proof.status, models.ProofOfPayment.STATUS_VOIDED)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_LOCKED)
        self.assertIsNone(self.enrolment.paid_until)

    def test_a_later_change_is_not_clobbered(self):
        """The enrolment is extended by another paid month after the payment."""
        payment = services.settle_payment(self.invoice, self.invoice.total, recorded_by=self.admin)
        self.enrolment.refresh_from_db()
        self.enrolment.activate(months=1)
        self._undo(payment.undo_action)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_ACTIVE)
        payment.refresh_from_db()
        self.assertEqual(payment.status, models.InvoicePayment.STATUS_VOIDED)

    def test_older_payment_without_a_journal_can_still_be_voided(self):
        payment = models.InvoicePayment.objects.create(
            invoice=self.invoice, amount=self.invoice.total, method='cash')
        self.invoice.refresh_status()
        response = self.client.get(reverse('finance:payment-undo', args=[payment.pk]))
        action = UndoAction.objects.get(object_ref=f'finance.invoicepayment:{payment.pk}')
        self.assertRedirects(response, reverse('accounts:undo', args=[action.pk])
                             + '?next=' + reverse('finance:invoice-detail', args=[self.invoice.pk]),
                             fetch_redirect_response=False)
        self._undo(action)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, models.Invoice.STATUS_SENT)

    def test_deleting_an_invoice_can_be_undone(self):
        number, items = self.invoice.number, self.invoice.items.count()
        self.client.post(reverse('finance:delete-invoice', args=[self.invoice.pk]))
        self.assertFalse(models.Invoice.all_objects.filter(number=number).exists())
        action = UndoAction.objects.get(kind='delete')
        self._undo(action, reason='Wrong button')
        invoice = models.Invoice.all_objects.get(number=number)
        self.assertEqual(invoice.items.count(), items)

    def test_an_invoice_with_money_on_it_cannot_be_deleted(self):
        services.settle_payment(self.invoice, Decimal('100'), recorded_by=self.admin)
        self.client.post(reverse('finance:delete-invoice', args=[self.invoice.pk]))
        self.assertTrue(models.Invoice.objects.filter(pk=self.invoice.pk).exists())

    def test_voiding_an_invoice_can_be_undone(self):
        self.client.post(reverse('finance:invoice-action', args=[self.invoice.pk]),
                         {'action': 'void', 'reason': 'oops'})
        self.assertFalse(models.Invoice.all_objects.filter(pk=self.invoice.pk).exists())
        self._undo(UndoAction.objects.get(kind='delete'))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, models.Invoice.STATUS_SENT)

    def test_removing_a_module_enrolment_can_be_undone(self):
        self.enrolment.activate(months=1)
        self.client.post(reverse('accounts:member-enrol', args=[self.person.pk]), {'modules': []})
        self.assertFalse(ModuleEnrolment.objects.filter(pk=self.enrolment.pk).exists())
        self._undo(UndoAction.objects.get(kind='delete'), reason='Removed by mistake')
        restored = ModuleEnrolment.objects.get(pk=self.enrolment.pk)
        self.assertEqual(restored.status, ModuleEnrolment.STATUS_ACTIVE)
        self.assertTrue(PaidPeriod.objects.filter(enrolment=restored).exists())
