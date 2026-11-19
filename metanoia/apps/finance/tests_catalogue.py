"""Products & services: the tuition catalogue, the pickers' search, and paid
tuition lines unlocking modules."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.learning.models import (Institution, Module, ModuleEnrolment, Programme,
                                  ProgrammeEnrolment, ProgrammeModule)
from apps.shop import catalogue
from apps.shop.models import Product
from apps.shop.scoping import visible_products

from . import models, services

User = get_user_model()


def _spine():
    """Create two institutions with programmes and modules; return them by key."""
    milpark = Institution.objects.create(name='Milpark', code='MILPARK')
    unisa = Institution.objects.create(name='UNISA', code='UNISA')
    pgda = Programme.objects.create(institution=milpark, name='PGDA', code='PGDA')
    upgda = Programme.objects.create(institution=unisa, name='PGDA', code='PGDA')
    fr = Module.objects.create(name='Financial Reporting', code='FR')
    tx = Module.objects.create(name='Taxation', code='TX')
    frep = ProgrammeModule.objects.create(programme=pgda, module=fr, code='FREP', price_per_month=Decimal('500'))
    taxa = ProgrammeModule.objects.create(programme=pgda, module=tx, code='TAXA', price_per_month=Decimal('450'))
    ProgrammeModule.objects.create(programme=upgda, module=tx, code='TAX', price_per_month=Decimal('400'))
    return {'milpark': milpark, 'unisa': unisa, 'pgda': pgda, 'upgda': upgda, 'frep': frep, 'taxa': taxa}


@override_settings(VAT_REGISTERED=False)
class CatalogueSyncTests(TestCase):
    """Keeping tuition items in step with the learning spine (:mod:`apps.shop.catalogue`)."""
    def setUp(self):
        self.s = _spine()

    def test_modules_and_programmes_become_invoice_only_items(self):
        """Syncing is idempotent."""
        catalogue.sync_all()
        frep = Product.objects.get(unlocks='module', module=self.s['frep'])
        self.assertEqual(frep.price, Decimal('500'))
        self.assertEqual((frep.kind, frep.unit, frep.shop_listed), ('product', 'month', False))
        self.assertEqual(frep.institution, self.s['milpark'])
        pgda = Product.objects.get(unlocks='programme', programme=self.s['pgda'])
        self.assertEqual(pgda.price, Decimal('950.00'))
        self.assertTrue(Product.objects.filter(sku=catalogue.SKU_SESSION, kind='service', unit='hour').exists())
        hoodie = Product.objects.get(sku=catalogue.SKU_HOODIE)
        self.assertTrue(hoodie.shop_listed)
        self.assertEqual(hoodie.variants.count(), 5)
        before = Product.objects.count()
        catalogue.sync_all()
        self.assertEqual(Product.objects.count(), before)

    def test_fee_change_follows_until_staff_set_their_own(self):
        catalogue.sync_all()
        self.s['frep'].price_per_month = Decimal('600')
        self.s['frep'].save()
        self.assertEqual(Product.objects.get(unlocks='module', module=self.s['frep']).price, Decimal('600'))
        self.assertEqual(Product.objects.get(unlocks='programme', programme=self.s['pgda']).price, Decimal('1050.00'))
        item = Product.objects.get(unlocks='module', module=self.s['frep'])
        item.price_from_modules, item.price = False, Decimal('999')
        item.save()
        self.s['frep'].price_per_month = Decimal('700')
        self.s['frep'].save()
        self.assertEqual(Product.objects.get(pk=item.pk).price, Decimal('999'))

    def test_inactive_module_item_is_inactive_and_comes_back(self):
        catalogue.sync_all()
        self.s['frep'].is_active = False
        self.s['frep'].save()
        item = Product.objects.get(unlocks='module', module=self.s['frep'])
        self.assertEqual(item.status, 'inactive')
        self.s['frep'].is_active = True
        self.s['frep'].save()
        item.refresh_from_db()
        self.assertEqual(item.status, 'active')

    def test_invoice_only_items_stay_out_of_the_shop(self):
        catalogue.sync_all()
        staff = User.objects.create_user('st', 'st@example.com', 'pw', is_staff=True)
        visible = visible_products(staff)
        self.assertFalse(visible.filter(shop_listed=False).exists())
        self.assertTrue(visible.filter(sku=catalogue.SKU_HOODIE).exists())


@override_settings(VAT_REGISTERED=False)
class LookupTests(TestCase):
    """The customer and item picker endpoints."""
    def setUp(self):
        self.s = _spine()
        catalogue.sync_all()
        self.staff = User.objects.create_user('staff', 'staff@example.com', 'pw12345!', is_staff=True)
        self.thandi = User.objects.create_user('thandi', 'thandi@example.com', 'pw', first_name='Thandi', last_name='Mokoena')
        self.sipho = User.objects.create_user('sipho', 'sipho@work.co.za', 'pw', first_name='Sipho', last_name='Dlamini')
        self.sipho.profile.phone = '082 555 1234'
        self.sipho.profile.save()
        ProgrammeEnrolment.objects.create(person=self.thandi.profile, programme=self.s['pgda'])
        ProgrammeEnrolment.objects.create(person=self.sipho.profile, programme=self.s['upgda'])
        self.client.force_login(self.staff)

    def _customers(self, **params):
        """GET the customer picker with ``params`` and return the JSON."""
        response = self.client.get(reverse('finance:lookup-customers'), params)
        self.assertEqual(response.status_code, 200)
        return [row['email'] for row in response.json()['results']]

    def _items(self, **params):
        """GET the item picker with ``params`` and return the JSON."""
        response = self.client.get(reverse('finance:lookup-items'), params)
        self.assertEqual(response.status_code, 200)
        return [row['name'] for row in response.json()['results']]

    def test_customer_search_by_words_field_and_filters(self):
        self.assertEqual(self._customers(q='thandi moko'), ['thandi@example.com'])
        self.assertEqual(self._customers(q='555', field='phone'), ['sipho@work.co.za'])
        self.assertEqual(self._customers(q='thandi', field='email'), ['thandi@example.com'])
        self.assertEqual(self._customers(q='sipho', field='phone'), [])
        self.assertEqual(self._customers(institution=self.s['unisa'].pk), ['sipho@work.co.za'])
        self.assertEqual(self._customers(programme=self.s['pgda'].pk), ['thandi@example.com'])
        self.assertIn('staff@example.com', self._customers(role='student') + self._customers())
        row = self.client.get(reverse('finance:lookup-customers'), {'q': 'thandi'}).json()['results'][0]
        self.assertEqual(row['studies'], ['MILPARK PGDA'])

    def test_item_search_by_words_and_filters(self):
        names = self._items(q='milpark tax')
        self.assertTrue(names and all('Tax' in n or 'PGDA' in n for n in names), names)
        self.assertEqual(len(self._items(source='module', institution=self.s['milpark'].pk)), 2)
        self.assertEqual(self._items(source='programme', programme=self.s['upgda'].pk),
                         [Product.objects.get(unlocks='programme', programme=self.s['upgda']).name])
        self.assertIn('Metanoia Coach Hoodie', self._items(institution='general', type='product'))
        self.assertEqual(self._items(q='HOODIE', field='sku'), ['Metanoia Coach Hoodie'])
        self.assertEqual(self._items(q='hoodie', field='description'), ['Metanoia Coach Hoodie'])
        self.assertNotIn('Private 1-on-1 session', self._items(type='product'))
        self.assertEqual(self._items(type='service'), ['Private 1-on-1 session'])

    def test_lookups_are_staff_only(self):
        self.client.force_login(self.thandi)
        response = self.client.get(reverse('finance:lookup-items'))
        self.assertNotEqual(response.status_code, 200)

    def test_quick_create_returns_the_item(self):
        response = self.client.post(reverse('finance:item-quick'),
                                    {'name': 'Workshop', 'kind': 'service', 'price': '750', 'unit': 'session'})
        self.assertEqual(response.status_code, 201)
        item = Product.objects.get(pk=response.json()['item']['id'])
        self.assertEqual((item.kind, item.shop_listed, item.fulfilment), ('service', False, 'none'))
        self.assertEqual(self.client.post(reverse('finance:item-quick'), {'name': ''}).status_code, 400)

    def test_items_page_and_editor_render(self):
        self.assertContains(self.client.get(reverse('finance:items'), {'source': 'module'}), 'FREP')
        self.assertEqual(self.client.get(reverse('finance:item-add')).status_code, 200)
        response = self.client.get(reverse('finance:invoice-new'), {'customer': self.thandi.pk})
        self.assertContains(response, 'Thandi Mokoena · thandi@example.com')

    def test_item_form_fills_scope_and_checks_it(self):
        response = self.client.post(reverse('finance:item-add'), {
            'kind': 'service', 'name': 'FREP revision day', 'price': '300', 'unit': 'session',
            'module': self.s['frep'].pk, 'status': 'active'})
        self.assertRedirects(response, reverse('finance:items'), fetch_redirect_response=False)
        item = Product.objects.get(name='FREP revision day')
        self.assertEqual((item.programme, item.institution, item.fulfilment), (self.s['pgda'], self.s['milpark'], 'none'))
        response = self.client.post(reverse('finance:item-add'), {
            'kind': 'service', 'name': 'Wrong', 'price': '1', 'status': 'active',
            'institution': self.s['unisa'].pk, 'programme': self.s['pgda'].pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(name='Wrong').exists())


@override_settings(VAT_REGISTERED=False)
class UnlockOnPaymentTests(TestCase):
    """Paying for a catalogue item opens what it unlocks."""
    def setUp(self):
        self.s = _spine()
        catalogue.sync_all()
        self.staff = User.objects.create_user('staff', 'staff@example.com', 'pw12345!', is_staff=True)
        self.student = User.objects.create_user('stu', 'stu@example.com', 'pw')

    def test_editor_line_links_product_and_payment_unlocks_programme(self):
        """Two months of access are two periods of 33 days each."""
        programme_item = Product.objects.get(unlocks='programme', programme=self.s['pgda'])
        self.client.force_login(self.staff)
        self.client.post(reverse('finance:invoice-new'), {
            'customer': self.student.pk, 'issue_date': '2026-10-01', 'due_date': '2026-10-08',
            'summary': '', 'po_number': '', 'notes': '', 'footer': '',
            'lines-TOTAL_FORMS': '1', 'lines-INITIAL_FORMS': '0', 'lines-MIN_NUM_FORMS': '0', 'lines-MAX_NUM_FORMS': '1000',
            'lines-0-product': programme_item.pk, 'lines-0-description': programme_item.name,
            'lines-0-quantity': '2', 'lines-0-unit_price': '950', 'lines-0-discount_percent': '0', 'then': 'save'})
        invoice = models.Invoice.objects.get()
        self.assertEqual(invoice.items.get().product, programme_item)
        self.assertEqual(invoice.total, Decimal('1900.00'))

        services.settle_payment(invoice, invoice.total, method='bank')
        rows = ModuleEnrolment.objects.filter(person=self.student.profile)
        self.assertEqual(rows.count(), 2)
        expected = timezone.localdate() + timedelta(days=2 * 33)
        for row in rows:
            self.assertEqual(row.status, ModuleEnrolment.STATUS_ACTIVE)
            self.assertEqual(row.paid_until, expected)

    def test_plain_lines_unlock_nothing(self):
        invoice = models.Invoice.objects.create(customer=self.student, status=models.Invoice.STATUS_SENT)
        models.InvoiceItem.objects.create(invoice=invoice, description='Printing', unit_price=Decimal('50'),
                                          product=Product.objects.get(sku=catalogue.SKU_HOODIE))
        invoice.refresh_from_db()
        services.settle_payment(invoice, invoice.total, method='bank')
        self.assertFalse(ModuleEnrolment.objects.filter(person=self.student.profile).exists())
