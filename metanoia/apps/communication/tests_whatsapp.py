"""WhatsApp: the webhook, the bot on shared numbers, the staff inbox, the 24-hour
window, notification copies and number verification.

Meta is replaced by :class:`FakeMeta`, which records every payload the
transport would have POSTed, so these tests check the real payloads without a
network.
"""
import hashlib
import hmac
import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import PersonContact, PhoneVerification
from apps.livesessions.tests import make_user
from core import media_access
from core import whatsapp_templates as templates

from . import whatsapp_bot, whatsapp_inbox as inbox
from .models import NotificationPreference, WhatsAppMessage, WhatsAppThread
from .services import notify

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
SECRET = 'test-app-secret'
NUMBER = '27838000001'


class FakeMeta:
    """Stands in for the Graph API: records payloads, hands back message ids."""

    def __init__(self):
        self.sent = []

    def __call__(self, path, payload):
        """Record ``payload`` and return a Graph-style answer with a fresh message id."""
        self.sent.append(payload)
        return {'messages': [{'id': f'wamid.{len(self.sent)}'}]}

    def __enter__(self):
        """Switch WhatsApp on and route every POST to this recorder."""
        self._patches = [mock.patch('core.whatsapp.enabled', return_value=True),
                         mock.patch('core.whatsapp._post_json', side_effect=self)]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        """Undo the patches."""
        for p in self._patches:
            p.stop()

    def texts(self):
        """Return the bodies of the free-text messages sent."""
        return [p['text']['body'] for p in self.sent if p.get('type') == 'text']

    def templates(self):
        """Return the names of the templates sent."""
        return [p['template']['name'] for p in self.sent if p.get('type') == 'template']


def person_with_number(username, first, last, number='+27 838000001', *, verified=False, role='student'):
    """Create a user whose contact record holds ``number``, optionally already verified for WhatsApp."""
    user = make_user(username, role, first_name=first, last_name=last)
    PersonContact.objects.update_or_create(person=user.profile, defaults={'primary_phone': number})
    if verified:
        user.profile.whatsapp_number = NUMBER
        user.profile.save(update_fields=['whatsapp_number'])
    user.profile.refresh_from_db()
    return user


@override_settings(CACHES=LOCMEM, WHATSAPP_APP_SECRET=SECRET)
class WebhookTests(TestCase):

    """The signed webhook: welcomes, redelivery, media and delivery receipts."""
    def post(self, payload):
        """Post ``payload`` to the webhook with a valid ``X-Hub-Signature-256``."""
        body = json.dumps(payload).encode()
        signature = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post(reverse('communication:whatsapp-webhook'), body,
                                content_type='application/json', HTTP_X_HUB_SIGNATURE_256=signature)

    def envelope(self, message=None, status=None, name='Thabo'):
        """Wrap a message and/or status in Meta's webhook envelope."""
        value = {'contacts': [{'wa_id': NUMBER, 'profile': {'name': name}}]}
        if message:
            value['messages'] = [dict({'from': NUMBER, 'id': 'wamid.in.1'}, **message)]
        if status:
            value['statuses'] = [status]
        return {'entry': [{'changes': [{'value': value}]}]}

    def test_unsigned_post_is_refused(self):
        response = self.client.post(reverse('communication:whatsapp-webhook'), '{}',
                                    content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_unknown_number_opens_a_conversation_and_is_welcomed_once(self):
        with FakeMeta() as meta:
            self.post(self.envelope({'type': 'text', 'text': {'body': 'Hi, how much is the course?'}}))
            self.post(self.envelope({'id': 'wamid.in.2', 'type': 'text', 'text': {'body': 'Hello?'}}))
        thread = WhatsAppThread.objects.get(number=NUMBER)
        self.assertEqual(thread.status, WhatsAppThread.OPEN)
        self.assertEqual(thread.contact_name, 'Thabo')
        self.assertEqual(thread.unread, 2)
        self.assertEqual(len(meta.texts()), 1)
        self.assertIn('member of our team will reply', meta.texts()[0])

    def test_redelivered_message_is_handled_once(self):
        with FakeMeta() as meta:
            for _ in range(3):
                self.post(self.envelope({'type': 'text', 'text': {'body': 'Hi, a question'}}))
        self.assertEqual(WhatsAppMessage.objects.filter(direction='in').count(), 1)
        self.assertEqual(len(meta.sent), 1)

    def test_photo_is_stored_for_staff(self):
        with FakeMeta():
            self.post(self.envelope({'type': 'image', 'image': {'id': 'media.9', 'mime_type': 'image/jpeg',
                                                                 'caption': 'proof of payment'}}))
        message = WhatsAppMessage.objects.get(direction='in')
        self.assertEqual((message.kind, message.media_id, message.body), ('image', 'media.9', 'proof of payment'))
        self.assertEqual(message.thread.status, WhatsAppThread.OPEN)

    def test_delivery_receipts_only_move_forward(self):
        thread = inbox.thread_for(NUMBER)
        sent = inbox.log_outbound(NUMBER, 'hello', sent=True, wamid='wamid.out', thread=thread)
        self.post(self.envelope(status={'id': 'wamid.out', 'status': 'read'}))
        self.post(self.envelope(status={'id': 'wamid.out', 'status': 'delivered'}))
        sent.refresh_from_db()
        self.assertEqual(sent.status, WhatsAppMessage.STATUS_READ)


@override_settings(CACHES=LOCMEM)
class SharedNumberBotTests(TestCase):

    """The bot on a number shared by a parent and child."""
    @classmethod
    def setUpTestData(cls):
        cls.mother = person_with_number('mom', 'Lerato', 'Mokoena', role='parent')
        cls.child = person_with_number('kid', 'Thabo', 'Mokoena')

    def say(self, text, n=[0]):
        """Send ``text`` inbound from the shared number with a fresh message id."""
        n[0] += 1
        return whatsapp_bot.handle_inbound(NUMBER, text, wa_id=f'wamid.shared.{n[0]}')

    def test_people_for_number_finds_both_accounts(self):
        names = [p.first_name for p in inbox.people_for_number(NUMBER)]
        self.assertEqual(names, ['Lerato', 'Thabo'])

    def test_personal_command_asks_who_then_labels_the_answer(self):
        with FakeMeta() as meta:
            self.say('modules')
            self.assertIn('Who is asking', meta.texts()[-1])
            self.say('2')
            self.assertIn('Thabo Mokoena', meta.texts()[-1])
            self.say('modules')
        self.assertTrue(meta.texts()[-1].startswith('For *Thabo Mokoena*'))
        self.assertEqual(WhatsAppThread.objects.get(number=NUMBER).active_person, self.child.profile)

    def test_stop_silences_everyone_on_the_number(self):
        for user in (self.mother, self.child):
            pref = NotificationPreference.for_user(user)
            pref.whatsapp_enabled = True
            pref.save()
        with FakeMeta():
            self.say('STOP')
        for user in (self.mother, self.child):
            self.assertFalse(NotificationPreference.for_user(user).whatsapp_enabled)

    def test_own_words_go_to_staff_and_the_bot_then_stays_quiet(self):
        with FakeMeta() as meta:
            self.say('Hi, I paid yesterday but my module is still locked')
            self.say('Can someone check please')
        self.assertEqual(len(meta.texts()), 1)
        self.assertIn('passed your message to the Metanoia team', meta.texts()[0])
        self.assertEqual(WhatsAppThread.objects.get(number=NUMBER).status, WhatsAppThread.OPEN)

    def test_parse_only_reads_commands_from_short_messages(self):
        self.assertEqual(whatsapp_bot.parse('dates please'), 'dates')
        self.assertEqual(whatsapp_bot.parse('hi I need help with the payment for my course'), '')


@override_settings(CACHES=LOCMEM)
class WindowTests(TestCase):

    """Meta's 24-hour customer-service window: when replies go at once and when they queue."""
    @classmethod
    def setUpTestData(cls):
        cls.student = person_with_number('stu', 'Thabo', 'Mokoena', verified=True)
        cls.admin = make_user('boss', 'admin', first_name='Ropa', last_name='Ndlovu')

    def test_reply_outside_window_is_queued_then_delivered_when_they_answer(self):
        """Queued replies wait for the person to answer the template, which is sent only once.

        Their "Show message" answer releases the queue and gets no bot reply of its own.
        """
        thread = inbox.thread_for(NUMBER)
        with FakeMeta() as meta:
            first = inbox.send_staff_reply(thread, self.admin, text='Your module is open now.')
            inbox.send_staff_reply(thread, self.admin, text='Anything else?')
            self.assertEqual(first.status, WhatsAppMessage.STATUS_QUEUED)
            self.assertEqual(meta.templates(), [templates.NEW_MESSAGE])
            self.assertEqual(meta.texts(), [])
            whatsapp_bot.handle_inbound(NUMBER, 'Show message', wa_id='wamid.reply')
        self.assertEqual(meta.texts()[:2], ['Your module is open now.\n\n_Ropa, Metanoia Group_',
                                            'Anything else?\n\n_Ropa, Metanoia Group_'])
        self.assertEqual(len(meta.texts()), 2)
        first.refresh_from_db()
        self.assertEqual(first.status, WhatsAppMessage.STATUS_SENT)

    def test_reply_inside_window_goes_straight_away(self):
        thread = inbox.thread_for(NUMBER)
        with FakeMeta() as meta:
            whatsapp_bot.handle_inbound(NUMBER, 'agent', wa_id='wamid.a')
            message = inbox.send_staff_reply(thread, self.admin, text='Hello Thabo')
        self.assertEqual(message.status, WhatsAppMessage.STATUS_SENT)
        self.assertEqual(message.wa_id, f'wamid.{len(meta.sent)}')

    def test_notification_outside_window_uses_the_category_template_with_full_name(self):
        pref = NotificationPreference.for_user(self.student)
        pref.whatsapp_enabled = True
        pref.save()
        with FakeMeta() as meta:
            notify(self.student, title='FAC1501 Test 2', body='Friday 09:00', category='deadlines')
        payload = meta.sent[-1]
        self.assertEqual(payload['template']['name'], 'metanoia_deadline')
        params = [p['text'] for p in payload['template']['components'][0]['parameters']]
        self.assertEqual(params[:3], ['Thabo Mokoena', 'FAC1501 Test 2', 'Friday 09:00'])
        logged = WhatsAppMessage.objects.get(kind='notification')
        self.assertEqual(logged.person, self.student.profile)


@override_settings(CACHES=LOCMEM)
class InboxPageTests(TestCase):

    """The staff WhatsApp inbox pages and files."""
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('boss', 'admin', first_name='Ropa', last_name='Ndlovu')
        cls.student = person_with_number('stu', 'Thabo', 'Mokoena', verified=True)

    def setUp(self):
        with FakeMeta():
            whatsapp_bot.handle_inbound(NUMBER, 'I have a question about my invoice', wa_id='wamid.q')
        self.thread = WhatsAppThread.objects.get(number=NUMBER)

    def test_admin_sees_the_conversation_and_opening_it_clears_unread(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('staffdesk:whatsapp-thread', args=[self.thread.pk]))
        self.assertContains(response, 'Thabo Mokoena')
        self.assertContains(response, 'I have a question about my invoice')
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.unread, 0)

    def test_student_cannot_open_the_inbox(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('staffdesk:whatsapp'))
        self.assertEqual(response.status_code, 302)

    def test_sending_assigns_the_conversation_to_the_sender(self):
        self.client.force_login(self.admin)
        with FakeMeta() as meta:
            self.client.post(reverse('staffdesk:whatsapp-send', args=[self.thread.pk]), {'text': 'Checking now'})
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.assigned_to, self.admin)
        self.assertIn('Checking now', meta.texts()[-1])

    def test_unsupported_attachment_is_refused(self):
        self.client.force_login(self.admin)
        upload = SimpleUploadedFile('run.exe', b'MZ', content_type='application/x-msdownload')
        with FakeMeta() as meta:
            self.client.post(reverse('staffdesk:whatsapp-send', args=[self.thread.pk]), {'file': upload})
        self.assertFalse(WhatsAppMessage.objects.filter(sent_by=self.admin).exists())
        self.assertEqual(meta.sent, [])

    def test_poll_returns_new_messages_only(self):
        self.client.force_login(self.admin)
        last = self.thread.messages.order_by('-pk').first().pk
        with FakeMeta():
            whatsapp_bot.handle_inbound(NUMBER, 'Are you there?', wa_id='wamid.q2')
        data = self.client.get(reverse('staffdesk:whatsapp-poll', args=[self.thread.pk]),
                               {'after': last}).json()
        self.assertIn('Are you there?', data['html'])
        self.assertNotIn('question about my invoice', data['html'])

    def test_whatsapp_files_are_staff_only(self):
        self.assertFalse(media_access._may_read_private(self.student, 'whatsapp/2026/10/a.jpg'))
        self.assertTrue(media_access._may_read_private(self.admin, 'whatsapp/2026/10/a.jpg'))


@override_settings(CACHES=LOCMEM)
class VerificationTests(TestCase):

    """Verifying a WhatsApp number with a one-time code."""
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user('new', 'student', first_name='Thabo', last_name='Mokoena')

    def code_from(self, meta):
        """Return the code inside the last OTP template sent."""
        return meta.sent[-1]['template']['components'][0]['parameters'][0]['text']

    def test_code_round_trip_verifies_the_number(self):
        """The transcript records that a code went, never the code itself."""
        from apps.accounts import phone_verify
        with FakeMeta() as meta:
            ok, _ = phone_verify.send_code(self.user, '+27 838000001')
            self.assertTrue(ok)
            self.assertEqual(meta.templates(), [templates.OTP])
            code = self.code_from(meta)
            self.assertFalse(phone_verify.check_code(self.user, '+27 838000001', '000000' if code != '000000' else '111111')[0])
            self.assertTrue(phone_verify.check_code(self.user, '+27 838000001', code)[0])
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.whatsapp_number, NUMBER)
        self.assertNotIn(code, WhatsAppMessage.objects.get(kind='code').body)

    def test_codes_are_rate_limited(self):
        from apps.accounts import phone_verify
        with FakeMeta():
            self.assertTrue(phone_verify.send_code(self.user, NUMBER)[0])
            self.assertFalse(phone_verify.send_code(self.user, NUMBER)[0])

    def test_too_many_wrong_codes_lock_the_code(self):
        from apps.accounts import phone_verify
        with FakeMeta() as meta:
            phone_verify.send_code(self.user, NUMBER)
            code = self.code_from(meta)
            wrong = '000000' if code != '000000' else '111111'
            for _ in range(phone_verify.MAX_ATTEMPTS):
                phone_verify.check_code(self.user, NUMBER, wrong)
            self.assertFalse(phone_verify.check_code(self.user, NUMBER, code)[0])

    def test_contact_form_demands_a_verified_number_only_while_whatsapp_is_on(self):
        """With WhatsApp off, the form needs no code."""
        from apps.accounts.forms import RegContactForm
        data = {'contact-primary_phone': '082 000 0199', 'contact-phone_country': 'ZA',
                'contact-city': 'Pretoria'}

        form = RegContactForm(data, prefix='contact')
        form.person = self.user.profile
        self.assertTrue(form.is_valid(), form.errors)

        with FakeMeta():
            form = RegContactForm(data, prefix='contact')
            form.person = self.user.profile
            self.assertFalse(form.is_valid())
            self.assertIn('Verify this number', str(form.errors))

            self.user.profile.whatsapp_number = '27820000199'
            self.user.profile.save(update_fields=['whatsapp_number'])
            form = RegContactForm(data, prefix='contact')
            form.person = self.user.profile
            self.assertTrue(form.is_valid(), form.errors)

    def test_send_code_endpoint(self):
        self.client.force_login(self.user)
        with FakeMeta():
            response = self.client.post(reverse('accounts:phone-send-code'),
                                        {'number': '82 000 0199', 'country': 'ZA'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PhoneVerification.objects.get().number, '27820000199')


    def test_registration_page_shows_the_code_box_only_while_whatsapp_is_on(self):
        person = self.user.profile
        person.registered = person.profile_status = False
        person.save(update_fields=['registered', 'profile_status'])
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('accounts:register')), 'data-wa-verify')
        with FakeMeta(), self.settings(WHATSAPP_DISPLAY_NUMBER='+27 82 000 0000'):
            response = self.client.get(reverse('accounts:register'))
        self.assertContains(response, 'data-wa-verify')
        self.assertContains(response, 'data-wa-phone')
        self.assertContains(response, '+27 82 000 0000')


class TemplateDefinitionTests(TestCase):

    """The message template definitions are well-formed and cover every category."""
    def test_bodies_never_start_or_end_with_a_blank(self):
        for t in templates.TEMPLATES:
            if 'body' in t:
                body = t['body'].strip()
                self.assertFalse(body.startswith('{{'), t['name'])
                self.assertFalse(body.endswith('}}'), t['name'])
                self.assertEqual(body.count('{{'), len(t['example']), t['name'])

    def test_every_category_maps_to_a_known_template(self):
        for category in NotificationPreference.CATEGORIES + [None]:
            self.assertIn(templates.for_notification(category), templates.BY_NAME)
