"""The "All 4 modules" bundle: R750 a module, R2 200 for all four.

A bundle is an automatic, fixed-amount promotion naming specific items. It
applies once, only when every named item is bought together — in a cart, on the
registration review page and on the module invoice alike.
"""
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.shop import catalogue, pricing
from apps.shop.models import Discount, Order, OrderItem, Product
from core.testing import make_module, make_programme

User = get_user_model()


class BundleTests(TestCase):
    """Module fees and the automatic four-module bundle, from the command to the cart and the invoice."""

    def setUp(self):
        """A four-module Milpark PGDA programme priced by ``apply_module_pricing``, and a buyer."""
        from apps.learning.models import Institution
        programme = make_programme('MILPARK', 'PGDA')
        Institution.objects.filter(pk=programme.institution_id).update(name='Milpark Education')
        self.programme = programme
        self.mods = [make_module(name, code, programme=programme, price=500)
                     for name, code in [('Management Accounting', 'MACF'),
                                        ('Financial Reporting', 'FREP'),
                                        ('Taxation', 'TAXA'),
                                        ('Governance and Auditing', 'CGAU')]]
        call_command('apply_module_pricing', stdout=StringIO())
        self.user = User.objects.create_user(username='b@example.com', email='b@example.com', password='x')

    def _cart(self, n):
        """An order holding the module-fee items of the first ``n`` modules."""
        order = Order.objects.create(buyer=self.user)
        for pm in self.mods[:n]:
            product = Product.objects.get(unlocks=Product.UNLOCKS_MODULE, module=pm)
            OrderItem.objects.create(order=order, product=product, quantity=1, unit_price=product.price)
        return order

    def test_command_sets_fees_and_bundle_idempotently(self):
        call_command('apply_module_pricing', stdout=StringIO())
        for pm in self.mods:
            pm.refresh_from_db()
            self.assertEqual(pm.price_per_month, Decimal('750.00'))
        bundle = Discount.objects.get(name='All 4 modules')
        self.assertEqual(Discount.objects.filter(name='All 4 modules').count(), 1)
        self.assertEqual(bundle.value, Decimal('800.00'))
        self.assertTrue(pricing.is_bundle(bundle))
        self.assertEqual(bundle.products.count(), 4)
        programme_item = Product.objects.get(unlocks=Product.UNLOCKS_PROGRAMME, programme=self.programme)
        self.assertEqual(programme_item.price, Decimal('2200.00'))

    def test_cart_totals_for_one_three_and_four_modules(self):
        for n, expected in [(1, '750.00'), (3, '2250.00'), (4, '2200.00')]:
            quote = pricing.price_cart(self._cart(n))
            self.assertEqual(quote['total'], Decimal(expected), n)
        quote = pricing.price_cart(self._cart(4))
        self.assertEqual(quote['discount'], Decimal('800.00'))
        self.assertEqual({line['label'] for line in quote['lines']}, {'All 4 modules'})

    def test_module_quote_matches_the_cart(self):
        for n, expected in [(1, '750.00'), (3, '2250.00'), (4, '2200.00')]:
            quote = pricing.quote_modules([(pm, pm.price_per_month) for pm in
                                           [type(pm).objects.get(pk=pm.pk) for pm in self.mods[:n]]])
            self.assertEqual(quote['total'], Decimal(expected), n)

    def test_module_invoice_carries_the_bundle_as_a_discount(self):
        """One discount on the whole invoice; the module lines are billed gross."""
        from apps.accounts.models import Person
        from apps.learning import enrolment as enrol

        person = Person.objects.get(user=self.user)
        enrolments = enrol.register_modules(person, self.programme, [pm.pk for pm in self.mods])
        invoice = enrol.invoice_for_modules(person, enrolments)
        self.assertEqual(invoice.total, Decimal('2200.00'))
        self.assertEqual(invoice.discount_total, Decimal('800.00'))
        self.assertEqual(invoice.gross_subtotal, Decimal('3000.00'))
        self.assertEqual(invoice.discount_amount, Decimal('800.00'))
        self.assertEqual(invoice.discount_label, 'All 4 modules')
        self.assertTrue(all(not item.discount_amount and item.amount == Decimal('750.00')
                            for item in invoice.items.all()))

        three = enrol.register_modules(
            Person.objects.get(user=User.objects.create_user(username='c', email='c@example.com')),
            self.programme, [pm.pk for pm in self.mods[:3]])
        invoice3 = enrol.invoice_for_modules(three[0].person, three)
        self.assertEqual(invoice3.total, Decimal('2250.00'))
        self.assertEqual(invoice3.discount_total, Decimal('0'))

    def test_an_inactive_bundle_does_not_apply(self):
        Discount.objects.filter(name='All 4 modules').update(is_active=False)
        self.assertEqual(pricing.price_cart(self._cart(4))['total'], Decimal('3000.00'))
        catalogue.sync_programme(self.programme)
        self.assertEqual(Product.objects.get(unlocks=Product.UNLOCKS_PROGRAMME,
                                             programme=self.programme).price, Decimal('3000.00'))
