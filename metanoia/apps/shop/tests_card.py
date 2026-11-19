"""The store card: the website's Blueprint poster, drawn from the product."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import ProductForm
from .models import Product

User = get_user_model()


def _onboarded(user):
    """Make ``user`` a registered, profile-complete student and return them."""
    person = user.profile
    person.user_type, person.registered, person.profile_status = 'student', True, True
    person.save()
    return user


class StoreCardTests(TestCase):
    """The poster-style store card: derived blanks, markup, escaping and its buttons."""

    def setUp(self):
        """Sign in as an onboarded buyer."""
        self.buyer = _onboarded(User.objects.create_user('buyer', 'buyer@example.com', 'pw12345!'))
        self.client.force_login(self.buyer)

    def test_blanks_are_derived_from_the_product(self):
        pack = Product.objects.create(name='FREP Strategic Blueprint', price=Decimal('150'),
                                      description='Built from every Test 3 paper since P20S.')
        card = pack.card
        self.assertEqual(card['cta'], 'Buy now')
        self.assertEqual(card['blurb'], 'Built from every Test 3 paper since P20S.')
        self.assertIn('<em>Blueprint</em><span class="tb-dot">.</span>', card['headline'])
        self.assertIn('FREP Strategic', card['headline'])
        self.assertTrue(card['points'])

    def test_marked_accent_points_and_labels(self):
        pack = Product.objects.create(
            name='CGAU pack', price=Decimal('150'), card_tag='Test 3 preparation bundle',
            card_headline='CGAU Test 3. Win the first *fifteen*', card_price_label='Full bundle',
            card_points='One\n\n- Two\nThree\nFour', card_cta='Get the bundle')
        card = pack.card
        self.assertEqual(card['points'], ['One', 'Two', 'Three'])
        self.assertIn('CGAU Test 3<span class="tb-dot">.</span> Win the first', card['headline'])
        self.assertIn('<em>fifteen</em>', card['headline'])

    def test_headline_is_escaped(self):
        pack = Product.objects.create(name='<b>x</b> pack', price=1)
        self.assertNotIn('<b>', pack.card['headline'])

    def test_storefront_draws_the_poster_with_a_working_buy_button(self):
        """The poster's button is the existing add-to-cart (buy now) flow."""
        pack = Product.objects.create(
            name='CGAU pack', price=Decimal('150'), card_tag='Test 3 preparation bundle',
            card_headline='CGAU Test 3. Win the first *fifteen*', card_cta='Get the bundle')
        page = self.client.get(reverse('shop:storefront')).content.decode()
        self.assertIn('class="tb-poster"', page)
        self.assertIn('Test 3 preparation bundle', page)
        self.assertIn('<em>fifteen</em>', page)
        self.assertIn('R150', page)
        self.assertIn('Structure.', page)
        self.assertIn(f'action="{reverse("shop:add-to-cart", args=[pack.pk])}"', page)
        response = self.client.post(reverse('shop:add-to-cart', args=[pack.pk]))
        self.assertEqual(response.status_code, 302)

    def test_booking_card_links_to_the_item_page(self):
        session = Product.objects.create(name='Coaching hour', price=Decimal('650'),
                                         fulfilment=Product.FULFIL_BOOKING)
        self.assertEqual(session.card['cta'], 'Book now')
        self.assertEqual(session.card['price_note'], 'per hour')

    def test_uploaded_image_is_the_card_only_when_asked(self):
        pack = Product.objects.create(name='Pack', price=1, image='shop/products/cover.png')
        self.assertFalse(pack.card['use_image'])
        pack.card_use_image = True
        pack.save()
        pack = Product.objects.get(pk=pack.pk)
        self.assertTrue(pack.card['use_image'])
        page = self.client.get(reverse('shop:storefront')).content.decode()
        self.assertIn('shop/products/cover.png', page)

    def test_card_fields_are_on_the_product_form(self):
        form = ProductForm()
        for name in ('card_tag', 'card_headline', 'card_price_label', 'card_points', 'card_cta',
                     'card_use_image'):
            self.assertIn(name, form.fields)
            self.assertFalse(form.fields[name].required)
