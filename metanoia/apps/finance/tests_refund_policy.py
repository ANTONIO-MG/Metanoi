"""The pay page's refund-policy click-to-accept.

PayFast requires the buyer to accept the refund policy on the pay screen with an
affirmative click, and the ECTA cooling-off exclusion needs the buyer's consent
to immediate access. So the PayFast form is only handed over after the tick box
has been posted, and the acceptance is stamped on the invoice.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from apps.accounts.forms import TERMS_VERSION

from . import models

User = get_user_model()


@override_settings(PAYMENTS_ENABLED=True, DEBUG=False, PAYFAST_SANDBOX=False,
                   PAYFAST_MERCHANT_ID='10000100', PAYFAST_MERCHANT_KEY='key',
                   PAYFAST_PASSPHRASE='pass', SECURE_SSL_REDIRECT=False)
class RefundPolicyGateTests(TestCase):
    def setUp(self):
        self.client = Client()
        customer = User.objects.create_user(
            username='rbuyer', email='rbuyer@example.com', password='pw12345!')
        self.invoice = models.Invoice.objects.create(
            customer=customer, status=models.Invoice.STATUS_SENT,
            subtotal=Decimal('900.00'), total=Decimal('900.00'))
        self.url = f'/finance/pay/{self.invoice.public_id}/'

    def test_the_page_shows_the_policy_and_no_payfast_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Refund policy')
        self.assertContains(response, 'name="accept_refund_policy"')
        self.assertNotContains(response, 'payfast.co.za/eng/process')

    def test_paying_without_ticking_the_box_is_refused(self):
        response = self.client.post(self.url, {})
        self.assertContains(response, 'Please tick the box')
        self.assertNotContains(response, 'payfast.co.za/eng/process')
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.refund_policy_accepted_at)

    def test_accepting_records_it_and_hands_over_the_payfast_form(self):
        response = self.client.post(self.url, {'accept_refund_policy': '1'},
                                    REMOTE_ADDR='196.25.1.1')
        self.assertContains(response, 'https://www.payfast.co.za/eng/process')
        self.invoice.refresh_from_db()
        self.assertIsNotNone(self.invoice.refund_policy_accepted_at)
        self.assertEqual(self.invoice.refund_policy_version, TERMS_VERSION)
        self.assertEqual(self.invoice.refund_policy_ip, '196.25.1.1')
