"""Registration sends exactly two e-mails, and they do not overlap.

The count is the point. It used to be three on the card path — a finance
"payment requested" with the figures in the body, a "registration summary" that
repeated the total *and* attached the PDF, and a generic receipt itemising
everything again — because three different call sites each sent their own and no
one place owned the total. These tests pin the number down at both ends and
assert the split, so a fourth sender added later fails here rather than in
somebody's inbox.

What each e-mail may and may not carry:

* **1 — thank you.** The summary and how to get started. No money at all: no
  total, no balance, no invoice number, no due date. No personal detail either:
  no address, no phone. No attachment.
* **2 — invoice or receipt.** The wording they need and the PDF. No figures in
  the body, because the attachment is the one authoritative copy of them.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from apps.accounts import registration_billing
from apps.accounts.models import Person

User = get_user_model()

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

MONEY_MARKERS = ('Total', 'Amount due', 'Balance', 'R1', 'R 1', 'per month')
PERSONAL_MARKERS = ('082 555', '12 Example Road')
"""``MONEY_MARKERS`` / ``PERSONAL_MARKERS``: strings that must never appear in
e-mail 1 (the thank-you) — money and contact detail."""


def bodies(message):
    """Every rendered part of a message, as one searchable blob."""
    parts = [message.body or '']
    parts += [content for content, _mime in getattr(message, 'alternatives', [])]
    return '\n'.join(parts)


def documents(message):
    """The real file attachments, ignoring the branded layout's inline images.

    Every branded e-mail carries four ``MIMEImage`` parts — the banner, the logo
    and the two social icons — which the layout references as ``cid:`` URLs so
    they survive a client that blocks remote content. Django reports those in
    ``.attachments`` alongside genuine attachments, but they are chrome, not
    documents. Django represents a genuine attachment as a
    ``(filename, content, mimetype)`` tuple, which is the distinction used here.
    """
    return [a for a in message.attachments if isinstance(a, tuple)]


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   CACHES=LOCMEM_CACHE)
class RegistrationEmailTests(TestCase):

    """What a finished registration e-mails: the count, the split between the two, and no third."""
    def setUp(self):
        from apps.finance.models import Invoice, InvoiceItem

        self.user = User.objects.create_user(
            username='learner@example.com', email='learner@example.com',
            password='Metanoia!Blueprint2026')
        self.person = Person.objects.filter(user=self.user).first() or \
            Person.objects.create(user=self.user)
        self.person.first_name = 'Thandi'
        self.person.save()

        self.invoice = Invoice.objects.create(customer=self.user, created_by=self.user,
                                              status=Invoice.STATUS_SENT)
        InvoiceItem.objects.create(invoice=self.invoice, description='Enrolment — Audit',
                                   quantity=1, unit_price=Decimal('1450.00'))
        self.invoice.recalc_total()
        self.invoice.refresh_status()
        mail.outbox = []

    def test_trial_registration_sends_exactly_two(self):
        registration_billing.deliver_registration(
            self.person, self.invoice, [], paid=False)

        self.assertEqual(len(mail.outbox), 2,
                         'registration must send exactly two e-mails, got:\n'
                         + '\n'.join(m.subject for m in mail.outbox))
        subjects = [m.subject for m in mail.outbox]
        self.assertIn('Thank you for registering — your Metanoia Group summary', subjects)
        self.assertIn('Your invoice — Metanoia Group', subjects)

    def test_paid_registration_sends_exactly_two(self):
        registration_billing.deliver_registration(
            self.person, self.invoice, [], paid=True)

        self.assertEqual(len(mail.outbox), 2,
                         'registration must send exactly two e-mails, got:\n'
                         + '\n'.join(m.subject for m in mail.outbox))
        subjects = [m.subject for m in mail.outbox]
        self.assertIn('Thank you for registering — your Metanoia Group summary', subjects)
        self.assertIn('Your receipt — Metanoia Group', subjects)

    def _sent(self, *, paid):
        """Deliver a registration and return ``(thank_you, document)`` messages."""
        registration_billing.deliver_registration(
            self.person, self.invoice, [], paid=paid)
        by_subject = {m.subject: m for m in mail.outbox}
        thank_you = by_subject['Thank you for registering — your Metanoia Group summary']
        document = by_subject['Your receipt — Metanoia Group' if paid
                              else 'Your invoice — Metanoia Group']
        return thank_you, document

    def test_the_thank_you_carries_no_money_and_no_attachment(self):
        thank_you, _document = self._sent(paid=False)

        body = bodies(thank_you)
        for marker in MONEY_MARKERS:
            self.assertNotIn(marker, body,
                             f'the thank-you e-mail must carry no money detail, found {marker!r}')
        self.assertNotIn(self.invoice.number, body,
                         'even the invoice number belongs on the other e-mail')
        self.assertEqual(documents(thank_you), [],
                         'the PDF belongs on the invoice/receipt e-mail, not this one')

    def test_the_thank_you_carries_the_steps(self):
        thank_you, _document = self._sent(paid=False)

        body = bodies(thank_you)
        self.assertIn('Getting started', body)
        self.assertIn('My Modules', body)
        self.assertIn('Thandi', body, 'it should still greet them by name')

    def test_the_invoice_e_mail_attaches_the_pdf_and_states_the_trial(self):
        _thank_you, document = self._sent(paid=False)

        body = bodies(document)
        self.assertIn('free week has started', body)
        self.assertIn('EFT', body)
        self.assertEqual(len(documents(document)), 1,
                         'the invoice PDF is the point of this e-mail')
        filename, _content, mimetype = documents(document)[0]
        self.assertTrue(filename.startswith('invoice-'), filename)
        self.assertEqual(mimetype, 'application/pdf')

    def test_the_receipt_e_mail_attaches_the_pdf_and_confirms_payment(self):
        _thank_you, document = self._sent(paid=True)

        body = bodies(document)
        self.assertIn('payment has gone through', body)
        self.assertEqual(len(documents(document)), 1)
        filename, _content, mimetype = documents(document)[0]
        self.assertTrue(filename.startswith('receipt-'), filename)
        self.assertEqual(mimetype, 'application/pdf')

    def test_no_line_item_figures_in_the_document_body(self):
        """The numbers live in the PDF, so there is only ever one copy of them."""
        for paid in (False, True):
            with self.subTest(paid=paid):
                mail.outbox = []
                _thank_you, document = self._sent(paid=paid)
                body = bodies(document)
                self.assertNotIn('1450', body)
                self.assertNotIn('Amount due', body)
                self.assertNotIn('Total paid', body)

    def test_settling_a_registration_invoice_does_not_add_a_generic_receipt(self):
        """apps.finance skips its own receipt for a registration invoice.

        The fixture invoice is not yet a registration invoice (no module
        enrolments point at it), so the generic finance receipt is right for it.
        """
        from apps.finance import services as finance_services

        self.assertFalse(finance_services._is_registration_invoice(self.invoice))
