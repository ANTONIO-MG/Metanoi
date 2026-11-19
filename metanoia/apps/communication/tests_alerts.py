"""Live alerts: the bell's redraw, desktop alerts (Web Push) and new-mail alerts."""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import webpush
from .models import Notification, WebPushSubscription
from .services import notify

User = get_user_model()

KEYS = dict(VAPID_PUBLIC_KEY='BPublicKeyForTests', VAPID_PRIVATE_KEY='private-key-for-tests',
            VAPID_SUBJECT='mailto:info@metanoia.test')


def _user(email='staff@metanoia.test'):
    """Create a staff user through the mailbox tests' factory."""
    from apps.mailbox.tests import make_user
    return make_user(email, 'staff')


@override_settings(SECURE_SSL_REDIRECT=False)
class BellTests(TestCase):
    """The header bell: live redraw and the ``go`` hand-off that opens and marks an alert read."""
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = _user()
        self.client.force_login(self.user)

    def test_bell_redraws_with_the_newest_alert(self):
        empty = self.client.get(reverse('communication:alerts-bell')).json()
        self.assertEqual(empty['total'], 0)
        self.assertIsNone(empty['latest'])
        notify(self.user, title='New email from Lerato', body='Timetable', url='/mail/?folder=1&m=5',
               alert_kind='mail')
        d = self.client.get(reverse('communication:alerts-bell')).json()
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['latest']['title'], 'New email from Lerato')
        self.assertEqual(d['latest']['kind'], 'mail')
        self.assertTrue(d['latest']['url'].endswith('?go=1'))
        self.assertIn('New email from Lerato', d['html'])

    def test_go_opens_the_email_and_marks_the_notification_read(self):
        note = notify(self.user, title='New email', url='/mail/?folder=1&m=5')
        r = self.client.get(reverse('communication:notification-detail', args=[note.pk]) + '?go=1')
        self.assertRedirects(r, '/mail/?folder=1&m=5', fetch_redirect_response=False)
        note.refresh_from_db()
        self.assertTrue(note.is_read)

    def test_go_never_leaves_the_site(self):
        note = notify(self.user, title='Odd', url='//evil.example/x')
        r = self.client.get(reverse('communication:notification-detail', args=[note.pk]) + '?go=1')
        self.assertEqual(r.status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False, **KEYS)
class DesktopAlertTests(TestCase):
    """Desktop alerts over Web Push: key, subscribe/unsubscribe and delivery (run inline)."""
    def setUp(self):
        webpush.RUN_IN_THREAD = False
        self.addCleanup(setattr, webpush, 'RUN_IN_THREAD', True)
        self.user = _user()
        self.client.force_login(self.user)

    def subscribe(self, endpoint='https://push.example/abc'):
        """Post a push subscription for ``endpoint`` as the logged-in user."""
        return self.client.post(reverse('communication:push-subscribe'), json.dumps(
            {'endpoint': endpoint, 'keys': {'p256dh': 'p-key', 'auth': 'a-key'}}), content_type='application/json')

    def test_key_subscribe_and_unsubscribe(self):
        """Subscribing twice is idempotent; an insecure endpoint is refused."""
        self.assertEqual(self.client.get(reverse('communication:push-key')).json()['key'], 'BPublicKeyForTests')
        self.assertTrue(self.subscribe().json()['ok'])
        self.assertTrue(self.subscribe().json()['ok'])
        self.assertEqual(WebPushSubscription.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.subscribe('http://insecure/x').status_code, 400)
        self.client.post(reverse('communication:push-unsubscribe'), json.dumps({'endpoint': 'https://push.example/abc'}),
                         content_type='application/json')
        self.assertFalse(WebPushSubscription.objects.exists())

    def test_one_browser_can_carry_several_accounts(self):
        self.subscribe()
        other = _user('accounts@metanoia.test')
        self.client.force_login(other)
        self.subscribe()
        self.assertEqual(WebPushSubscription.objects.filter(endpoint='https://push.example/abc').count(), 2)

    def test_desktop_notify_pushes_and_drops_dead_subscriptions(self):
        self.subscribe()
        with mock.patch('pywebpush.webpush') as push:
            note = notify(self.user, title='New email from Lerato', body='Timetable', url='/mail/?m=1',
                          alert_kind='mail', desktop=True)
        payload = json.loads(push.call_args.kwargs['data'])
        self.assertEqual(payload['title'], 'New email from Lerato')
        self.assertEqual(payload['kind'], 'mail')
        self.assertTrue(payload['url'].endswith(f'/{note.pk}/?go=1'))

        from pywebpush import WebPushException
        gone = WebPushException('gone', response=mock.Mock(status_code=410))
        with mock.patch('pywebpush.webpush', side_effect=gone):
            notify(self.user, title='Again', desktop=True)
        self.assertFalse(WebPushSubscription.objects.exists())

    def test_ordinary_notifications_do_not_push(self):
        self.subscribe()
        with mock.patch('pywebpush.webpush') as push:
            notify(self.user, title='Grade posted')
        push.assert_not_called()

    def test_service_worker_is_public_javascript(self):
        self.client.logout()
        r = self.client.get('/sw.js')
        self.assertEqual(r.status_code, 200)
        self.assertIn('javascript', r['Content-Type'])
        self.assertEqual(r['Service-Worker-Allowed'], '/')
        self.assertIn("addEventListener('push'", r.content.decode())


@override_settings(SECURE_SSL_REDIRECT=False)
class NewMailAlertTests(TestCase):
    """New mail found by the inbox sync raises a live alert and a desktop push."""
    def test_new_mail_alerts_as_mail_with_a_desktop_push(self):
        """The first sync only records history quietly; mail delivered after it alerts."""
        from apps.mailbox.tests import FakeServer, message
        from apps.mailbox import imap, sync
        from apps.mailbox.models import MailAccount, MailServer
        sync.RUN_IN_THREAD = False
        self.addCleanup(setattr, sync, 'RUN_IN_THREAD', True)
        server = MailServer.get()
        server.imap_host, server.smtp_host = 'imap.metanoia.test', 'smtp.metanoia.test'
        server.save()
        fake = FakeServer()
        user = _user('coach@metanoia.test')
        account = MailAccount(user=user, email_address='coach@metanoia.test')
        account.set_password('x')
        account.save()
        with mock.patch.object(imap, 'connect', side_effect=lambda a: imap.ImapClient(fake.conn())):
            sync.sync_inboxes()
            fake.deliver('INBOX', message('Timetable'))
            with mock.patch('apps.communication.realtime.push_alert') as alert, \
                    mock.patch('apps.communication.webpush.send_to_user') as desktop:
                self.assertIn('1 new', sync.sync_inboxes())
        self.assertEqual(alert.call_args.kwargs['kind'], 'mail')
        self.assertEqual(desktop.call_args.kwargs['kind'], 'mail')
        self.assertTrue(Notification.objects.filter(recipient=user, title__startswith='New email').exists())
