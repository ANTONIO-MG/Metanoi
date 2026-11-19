"""Each kind of e-mail goes out from the right mailbox (apps.communication.senders)."""

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from apps.communication import emails, senders

MAILBOXES = {
    'accounts': {'user': 'accounts@example.test', 'password': 'x'},
    'info': {'user': 'info@example.test', 'password': 'x'},
    'learninghub': {'user': 'learninghub@example.test', 'password': 'x'},
}


@override_settings(EMAIL_MAILBOXES=MAILBOXES, EMAIL_FROM_NAME='Metanoia')
class MailboxRoutingTests(TestCase):
    """Templates, notification categories and allauth mail each pick the configured mailbox."""
    def _from(self):
        """Return the sender address of the last e-mail sent."""
        return mail.outbox[-1].from_email

    def test_template_picks_mailbox(self):
        for template, box in [('invoice', 'accounts'), ('registration-document', 'accounts'),
                              ('session-invite', 'learninghub'), ('general', 'info')]:
            emails.send_branded_email('s', 'a@example.test', template, {})
            self.assertEqual(self._from(), f'Metanoia <{box}@example.test>', template)

    def test_notification_category_picks_mailbox(self):
        self.assertEqual(senders.mailbox_for_category('meetings'), 'learninghub')
        self.assertEqual(senders.mailbox_for_category('messages'), 'info')
        self.assertEqual(senders.mailbox_for_category(None), 'info')

    def test_allauth_mail_comes_from_accounts(self):
        """Verification and password-reset e-mails are built by allauth's adapter.

        They must come from the accounts mailbox and carry its own SMTP connection, so
        they sign in as ``accounts@`` rather than the default account.
        """
        from allauth.account.adapter import get_adapter
        from django.test import RequestFactory

        user = get_user_model().objects.create_user('pat', 'pat@example.test', 'pw-123456!')
        msg = get_adapter(RequestFactory().get('/')).render_mail(
            'account/email/password_reset_key', 'pat@example.test',
            {'user': user, 'password_reset_url': 'http://x/reset/', 'request': None})
        self.assertEqual(msg.from_email, 'Metanoia <accounts@example.test>')
        self.assertIsNotNone(msg.connection)

    @override_settings(EMAIL_MAILBOXES={}, DEFAULT_FROM_EMAIL='fallback@example.test')
    def test_unconfigured_mailbox_falls_back_to_default(self):
        emails.send_branded_email('s', 'a@example.test', 'invoice', {})
        self.assertEqual(self._from(), 'fallback@example.test')


@override_settings(EMAIL_MAILBOXES=MAILBOXES, EMAIL_FROM_NAME='Metanoia')
class SignatureTests(TestCase):
    """Every e-mail ends with a Brand Studio signature: the mailbox's (ringed
    tick) unless a person wrote it, then theirs (their own picture)."""

    def html(self):
        """Return the HTML body of the last e-mail sent."""
        return mail.outbox[-1].alternatives[0][0]

    def inline_ids(self):
        """Return the ``Content-ID`` values of the last e-mail's inline attachments."""
        return {part.get('Content-ID') for part in mail.outbox[-1].attachments if hasattr(part, 'get')}

    def test_a_mailbox_signs_with_its_own_name_and_address(self):
        emails.send_branded_email('s', 'a@example.test', 'invoice', {})
        self.assertIn('Metanoia Accounts', self.html())
        self.assertIn('accounts@metanoiagroup.co.za', self.html())
        self.assertIn('cid:banner', self.html())
        self.assertTrue({'<sig_avatar>', '<sig_logo>', '<banner>'} <= self.inline_ids())

    def test_a_person_signs_as_themselves(self):
        from PIL import Image
        import io
        from django.core.files.base import ContentFile
        user = get_user_model().objects.create_user('admin', email='admin@metanoiagroup.co.za', password='x')
        user.profile.first_name, user.profile.last_name = 'Thandi', 'Example'
        buf = io.BytesIO(); Image.new('RGB', (40, 40), (20, 40, 90)).save(buf, 'JPEG')
        user.profile.profile_picture.save('admin.jpg', ContentFile(buf.getvalue()), save=True)
        user.profile.save()
        emails.send_branded_email('s', 'a@example.test', 'general', {}, author=user)
        self.assertIn('Thandi Example', self.html())
        self.assertIn('Administrator', self.html())
        avatar = [p for p in mail.outbox[-1].attachments if hasattr(p, 'get') and p.get('Content-ID') == '<sig_avatar>']
        self.assertEqual(avatar[0].get_filename(), 'signature.png')

    def test_a_mailbox_account_as_author_keeps_the_mailbox_signature(self):
        user = get_user_model().objects.create_user('info', email='info@metanoiagroup.co.za', password='x')
        self.assertEqual(senders.signature('info', author=user)['name'], 'Metanoia Info')
