"""The shop shows people only what belongs to their institution and programme
(plus general items), on every page — the storefront, a product page reached
by its id, add to cart, and the one-on-one calendar. Admin and staff see all.
"""
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from apps.learning.models import Institution, Programme, ProgrammeEnrolment

from .models import EducatorAvailability, EducatorRate, Order, Product
from .scoping import visible_products

User = get_user_model()


def _user(email, role='student', **extra):
    """Create a registered user with ``role``."""
    user = User.objects.create_user(email, email, 'pw12345!', **extra)
    Person.objects.filter(user=user).update(user_type=role, registered=True, profile_status=True,
                                            first_name='Test', last_name=role.title())
    user.refresh_from_db()
    return user


@override_settings(VAT_REGISTERED=False, BOOKING_TIME_ZONE='UTC', MS_TEAMS_ENABLED=False)
class ShopScopingTests(TestCase):
    """The visibility rule itself, and the pages and POSTs that must honour it."""

    @classmethod
    def setUpTestData(cls):
        """Institutions, programmes, one product per scope and people in each role.

        The products are: a general hoodie, a Milpark-wide diary, a Milpark PGDA pack,
        a Milpark CTA pack and a UNISA PGDA pack.
        """
        cls.milpark = Institution.objects.create(code='MILPARK', name='Milpark Education')
        cls.unisa = Institution.objects.create(code='UNISA', name='UNISA')
        cls.other_inst = Institution.objects.create(code='OTHER', name='Other institution')
        cls.pgda = Programme.objects.create(institution=cls.milpark, code='PGDA', name='PGDA')
        cls.cta = Programme.objects.create(institution=cls.milpark, code='CTA', name='CTA')
        cls.upgda = Programme.objects.create(institution=cls.unisa, code='UPGDA', name='PGDA')

        def product(name, **kw):
            """A plain-fee product called ``name`` in the given scope."""
            return Product.objects.create(name=name, price=Decimal('100'),
                                          fulfilment=Product.FULFIL_NONE, **kw)
        cls.hoodie = product('Metanoia hoodie')
        cls.milpark_all = product('Milpark diary', institution=cls.milpark)
        cls.milpark_pgda = product('PGDA pack', institution=cls.milpark, programme=cls.pgda)
        cls.milpark_cta = product('CTA pack', institution=cls.milpark, programme=cls.cta)
        cls.unisa_pack = product('UNISA pack', institution=cls.unisa, programme=cls.upgda)

        cls.student = _user('stu@example.com')
        ProgrammeEnrolment.objects.create(person=cls.student.profile, programme=cls.pgda)

    def names(self, user):
        """Names of the products ``user`` may see."""
        return set(visible_products(user).values_list('name', flat=True))

    def test_student_sees_own_institution_and_programme_plus_general(self):
        self.assertEqual(self.names(self.student), {'Metanoia hoodie', 'Milpark diary', 'PGDA pack'})

    def test_community_member_sees_what_they_picked_at_join(self):
        member = _user('member@example.com', 'community')
        Person.objects.filter(user=member).update(community_institution=self.unisa,
                                                  community_programme=self.upgda)
        member.refresh_from_db()
        self.assertEqual(self.names(member), {'Metanoia hoodie', 'UNISA pack'})

    def test_member_with_institution_only_sees_institution_wide_items(self):
        member = _user('inst@example.com', 'community')
        Person.objects.filter(user=member).update(community_institution=self.milpark)
        member.refresh_from_db()
        self.assertEqual(self.names(member), {'Metanoia hoodie', 'Milpark diary'})

    def test_nobody_in_particular_sees_general_only(self):
        self.assertEqual(self.names(_user('new@example.com')), {'Metanoia hoodie'})
        from django.contrib.auth.models import AnonymousUser
        self.assertEqual(self.names(AnonymousUser()), {'Metanoia hoodie'})

    def test_educator_sees_the_programmes_they_teach(self):
        from core.testing import make_module
        educator = _user('edu@example.com', 'educator')
        module = make_module('Tax', 'TAX', programme=self.upgda)
        module.educators.add(educator.profile)
        self.assertEqual(self.names(educator), {'Metanoia hoodie', 'UNISA pack'})

    def test_admin_and_staff_see_everything(self):
        everything = set(Product.objects.values_list('name', flat=True))
        self.assertEqual(self.names(_user('admin@example.com', 'admin')), everything)
        self.assertEqual(self.names(_user('staff@example.com', 'staff')), everything)
        self.assertEqual(self.names(_user('flag@example.com', is_staff=True)), everything)

    def test_storefront_lists_only_visible_items(self):
        """A hand-typed facet cannot widen the catalogue."""
        self.client.force_login(self.student)
        r = self.client.get(reverse('shop:storefront'))
        self.assertContains(r, 'PGDA pack')
        self.assertNotContains(r, 'UNISA pack')
        self.assertNotContains(r, 'CTA pack')
        r = self.client.get(reverse('shop:storefront') + f'?institution={self.unisa.pk}')
        self.assertNotContains(r, 'UNISA pack')

    def test_product_page_by_url_is_404_outside_the_catalogue(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse('shop:product-detail', args=[self.unisa_pack.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('shop:product-detail', args=[self.milpark_pgda.pk])).status_code, 200)

    def test_admin_role_opens_any_product_page(self):
        self.client.force_login(_user('admin2@example.com', 'admin'))
        self.assertEqual(self.client.get(reverse('shop:product-detail', args=[self.unisa_pack.pk])).status_code, 200)

    def test_add_to_cart_by_post_is_refused_outside_the_catalogue(self):
        self.client.force_login(self.student)
        r = self.client.post(reverse('shop:add-to-cart', args=[self.unisa_pack.pk]))
        self.assertEqual(r.status_code, 404)
        self.assertFalse(Order.objects.filter(buyer=self.student, items__product=self.unisa_pack).exists())
        r = self.client.post(reverse('shop:add-to-cart', args=[self.milpark_pgda.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Order.objects.filter(buyer=self.student, items__product=self.milpark_pgda).exists())

    def test_one_on_one_booking_is_scoped_too(self):
        coach = _user('coach@example.com', 'educator')
        Person.objects.filter(user=coach).update(first_name='Unisa', last_name='Coach')
        service = Product.objects.create(name='UNISA 1-on-1', fulfilment=Product.FULFIL_BOOKING,
                                         price=Decimal('300'), institution=self.unisa)
        EducatorRate.objects.create(product=service, educator=coach.profile, hourly_rate=Decimal('300'))
        day = timezone.localdate() + timedelta(days=3)
        EducatorAvailability.objects.create(educator=coach.profile, weekday=day.weekday(),
                                            start_time=time(9), end_time=time(12))
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse('shop:one-on-one')), 'Unisa Coach')
        self.assertEqual(self.client.get(reverse('shop:one-on-one-educator',
                                                 args=[coach.profile.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('shop:product-detail', args=[service.pk])).status_code, 404)
