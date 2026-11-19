"""Invoice-wide discounts, voiding by deletion, and free "pay later" access.

* An invoice carries ONE discount, in rand, for the whole invoice; lines are
  billed gross. "All 4 modules" is applied automatically when all four are on it.
* Voiding an invoice deletes it (undoably) and leaves the student's module
  access exactly as it was.
* Staff can give a student 7 or 30 days of access without a payment.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person, UndoAction
from apps.learning import enrolment as enrol
from apps.learning.models import ModuleEnrolment, PaidPeriod
from apps.shop.models import Discount, Product
from core.testing import make_module, make_programme

from . import access, discounts, documents, models

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


class _Base(TestCase):
    """Shared fixtures: a programme of four modules priced by ``apply_module_pricing``
    (R750 each, R800 off all four), an administrator and a student.
    """
    def setUp(self):
        from apps.learning.models import Institution
        self.programme = make_programme('MILPARK', 'PGDA')
        Institution.objects.filter(pk=self.programme.institution_id).update(name='Milpark Education')
        self.mods = [make_module(name, code, programme=self.programme, price=500)
                     for name, code in [('Management Accounting', 'MACF'),
                                        ('Financial Reporting', 'FREP'),
                                        ('Taxation', 'TAXA'),
                                        ('Governance and Auditing', 'CGAU')]]
        call_command('apply_module_pricing', stdout=StringIO())
        self.admin = User.objects.create_user('fin-admin', 'fin-admin@example.com', 'pw-12345!x',
                                              is_staff=True)
        _person(self.admin, 'admin')
        self.student = User.objects.create_user('thandi', 'thandi@example.com', 'pw-12345!x',
                                                first_name='Thandi')
        self.person = _person(self.student, 'student')
        self.client.force_login(self.admin)

    def fee(self, pm):
        """The module-fee product for ``pm``."""
        return Product.objects.get(unlocks=Product.UNLOCKS_MODULE, module=pm)


class InvoiceDiscountTests(_Base):
    """One discount for the whole invoice: bundles, custom amounts and promotions."""
    def _post(self, url, products, **extra):
        """POST the invoice editor with one line per product."""
        data = {'customer': self.student.pk, 'issue_date': timezone.localdate().isoformat(),
                'due_date': '', 'summary': '', 'po_number': '', 'notes': '', 'footer': '',
                'discount_choice': 'auto', 'discount_amount': '0', 'discount_label': '',
                'lines-TOTAL_FORMS': str(len(products)), 'lines-INITIAL_FORMS': '0',
                'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '1000', 'then': 'save'}
        for i, product in enumerate(products):
            data.update({f'lines-{i}-product': product.pk, f'lines-{i}-description': product.name,
                         f'lines-{i}-detail': '', f'lines-{i}-quantity': '1',
                         f'lines-{i}-unit_price': str(product.price)})
        data.update(extra)
        return self.client.post(url, data)

    def test_editor_has_no_per_line_discount(self):
        response = self.client.get(reverse('finance:invoice-new'))
        self.assertNotContains(response, 'Disc. %')
        self.assertNotContains(response, 'name="lines-__prefix__-discount_percent"')
        self.assertContains(response, 'Discount on this invoice')

    def test_all_four_modules_get_the_bundle_automatically(self):
        self._post(reverse('finance:invoice-new'), [self.fee(pm) for pm in self.mods])
        invoice = models.Invoice.objects.get()
        self.assertEqual(invoice.discount_amount, Decimal('800.00'))
        self.assertEqual(invoice.discount_label, 'All 4 modules')
        self.assertEqual(invoice.total, Decimal('2200.00'))
        self.assertEqual(invoice.gross_subtotal, Decimal('3000.00'))
        self.assertFalse(invoice.items.filter(discount_amount__gt=0).exists())

    def test_three_modules_get_no_bundle(self):
        self._post(reverse('finance:invoice-new'), [self.fee(pm) for pm in self.mods[:3]])
        invoice = models.Invoice.objects.get()
        self.assertEqual(invoice.discount_amount, Decimal('0'))
        self.assertEqual(invoice.total, Decimal('2250.00'))

    def test_custom_rand_discount_on_the_whole_invoice(self):
        self._post(reverse('finance:invoice-new'), [self.fee(pm) for pm in self.mods[:2]],
                   discount_choice='custom', discount_amount='150', discount_label='Bursary')
        invoice = models.Invoice.objects.get()
        self.assertFalse(invoice.discount_auto)
        self.assertEqual((invoice.discount_amount, invoice.discount_label), (Decimal('150.00'), 'Bursary'))
        self.assertEqual(invoice.total, Decimal('1350.00'))
        self.assertEqual(invoice.discount_total, Decimal('150.00'))

    def test_picked_promotion_is_charged_in_rand(self):
        promo = Discount.objects.create(name='Loyalty', discount_type=Discount.TYPE_PERCENT,
                                        value=Decimal('10'))
        self._post(reverse('finance:invoice-new'), [self.fee(pm) for pm in self.mods[:2]],
                   discount_choice=f'promo:{promo.pk}')
        invoice = models.Invoice.objects.get()
        self.assertEqual((invoice.discount_amount, invoice.discount_label), (Decimal('150.00'), 'Loyalty'))
        self.assertEqual(invoice.total, Decimal('1350.00'))

    def test_discount_never_goes_below_zero(self):
        self._post(reverse('finance:invoice-new'), [self.fee(self.mods[0])],
                   discount_choice='custom', discount_amount='5000', discount_label='Waiver')
        self.assertEqual(models.Invoice.objects.get().total, Decimal('0'))

    def test_registration_invoice_carries_one_bundle_discount(self):
        """Lines without a product still count towards the bundle (matched by name)."""
        enrolments = enrol.register_modules(self.person, self.programme, [pm.pk for pm in self.mods])
        invoice = enrol.invoice_for_modules(self.person, enrolments)
        self.assertEqual((invoice.discount_amount, invoice.discount_label), (Decimal('800.00'), 'All 4 modules'))
        self.assertEqual(invoice.total, Decimal('2200.00'))
        self.assertEqual(discounts.bundle_discount(list(invoice.items.all())),
                         (Decimal('800.00'), 'All 4 modules'))

    def test_report_lines_add_up_to_the_invoice(self):
        enrolments = enrol.register_modules(self.person, self.programme, [pm.pk for pm in self.mods])
        invoice = enrol.invoice_for_modules(self.person, enrolments)
        items = list(invoice.items.all())
        self.assertEqual(sum(i.net_amount for i in items), invoice.total)
        self.assertEqual(sum(i.total_discount for i in items), Decimal('800.00'))


class VoidTests(_Base):
    """Voiding an invoice deletes it, keeps access, and can be undone."""
    def test_void_deletes_the_invoice_and_keeps_access(self):
        enrolments = enrol.register_modules(self.person, self.programme, [self.mods[0].pk])
        invoice = enrol.invoice_for_modules(self.person, enrolments)
        enrolment = ModuleEnrolment.objects.get(person=self.person)
        before = (enrolment.status, enrolment.trial_ends_at, enrolment.paid_until)

        response = self.client.post(reverse('finance:invoice-action', args=[invoice.pk]), {'action': 'void'})
        self.assertRedirects(response, reverse('finance:invoices'), fetch_redirect_response=False)
        self.assertFalse(models.Invoice.all_objects.filter(pk=invoice.pk).exists())
        enrolment.refresh_from_db()
        self.assertEqual((enrolment.status, enrolment.trial_ends_at, enrolment.paid_until), before)

        action = UndoAction.objects.get(kind='delete')
        self.client.post(reverse('accounts:undo', args=[action.pk]), {'reason': 'Voided by mistake'})
        self.assertTrue(models.Invoice.all_objects.filter(pk=invoice.pk, number=invoice.number).exists())

    def test_a_paid_invoice_cannot_be_voided(self):
        invoice = models.Invoice.objects.create(customer=self.student, status='sent')
        models.InvoiceItem.objects.create(invoice=invoice, description='Fees', unit_price=100)
        models.InvoicePayment.objects.create(invoice=invoice, amount=100, status='completed')
        with self.assertRaises(ValueError):
            documents.void_invoice(invoice, by=self.admin)
        self.assertTrue(models.Invoice.objects.filter(pk=invoice.pk).exists())


class ExtendAccessTests(_Base):
    """Free 7- and 30-day access extensions from the student page."""
    def setUp(self):
        super().setUp()
        self.enrolment = ModuleEnrolment.objects.create(
            person=self.person, programme_module=self.mods[0], price_at_enrolment=Decimal('750'))
        self.url = reverse('staffdesk:student-extend', args=[self.person.pk])

    def test_student_page_shows_the_buttons(self):
        response = self.client.get(reverse('staffdesk:student', args=[self.person.pk]))
        self.assertContains(response, 'Extend 7 days')
        self.assertContains(response, 'Extend 30 days')

    def test_locked_module_opens_for_seven_days_without_payment(self):
        self.client.post(self.url, {'days': '7', 'module': self.enrolment.pk})
        self.enrolment.refresh_from_db()
        self.assertTrue(self.enrolment.is_unlocked)
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_TRIAL)
        self.assertAlmostEqual((self.enrolment.trial_ends_at - timezone.now()).total_seconds(),
                               timedelta(days=7).total_seconds(), delta=60)
        self.assertFalse(models.InvoicePayment.objects.exists())
        self.assertFalse(models.ProofOfPayment.objects.exists())

    def test_live_trial_is_extended_from_its_end(self):
        self.enrolment.start_trial()
        ends = self.enrolment.trial_ends_at
        self.client.post(self.url, {'days': '30'})
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.trial_ends_at, ends + timedelta(days=30))

    def test_paid_module_has_its_end_date_pushed_out_without_a_paid_period(self):
        self.enrolment.activate()
        until = self.enrolment.paid_until
        self.client.post(self.url, {'days': '30', 'module': self.enrolment.pk})
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.paid_until, until + timedelta(days=30))
        self.assertEqual(PaidPeriod.objects.filter(enrolment=self.enrolment).count(), 1)

    def test_payment_later_still_unlocks_normally(self):
        self.client.post(self.url, {'days': '7'})
        self.enrolment.refresh_from_db()
        self.enrolment.activate()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_ACTIVE)
        self.assertEqual(self.enrolment.paid_from, timezone.localdate())

    def test_extension_can_be_undone(self):
        self.client.post(self.url, {'days': '7'})
        action = UndoAction.objects.get(kind='access_extension')
        self.client.post(reverse('accounts:undo', args=[action.pk]), {'reason': 'Wrong student'})
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_LOCKED)
        self.assertIsNone(self.enrolment.trial_ends_at)

    def test_only_seven_or_thirty_days(self):
        self.client.post(self.url, {'days': '365'})
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_LOCKED)

    def test_students_cannot_extend(self):
        self.client.force_login(self.student)
        self.client.post(self.url, {'days': '30'})
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.status, ModuleEnrolment.STATUS_LOCKED)

    def test_open_ended_modules_are_not_extendable(self):
        self.enrolment.status, self.enrolment.paid_until = ModuleEnrolment.STATUS_ACTIVE, None
        self.enrolment.save()
        self.assertFalse(access.can_extend(self.enrolment))
