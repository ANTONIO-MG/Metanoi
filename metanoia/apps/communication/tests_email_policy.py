"""Which notices still e-mail, the links inside e-mails, and the unsubscribe flow."""

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.livesessions.tests import make_user
from core.utils import email_link

from . import email_prefs
from .emails import send_branded_email
from .models import NotificationPreference
from .services import notify

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
SITE = 'https://learn.metanoia.test'


@override_settings(SITE_URL=SITE, ALLOWED_HOSTS=['learn.metanoia.test', '10.0.0.100', 'testserver'])
class EmailLinkTests(TestCase):

    """Links in e-mails point at the public website."""
    def test_site_paths_get_the_public_host(self):
        self.assertEqual(email_link('/myhub/'), f'{SITE}/myhub/')
        self.assertEqual(email_link('learning/modules/3/'), f'{SITE}/learning/modules/3/')

    def test_internal_hosts_are_moved_onto_the_public_site(self):
        self.assertEqual(email_link('http://10.0.0.100:8000/calendar/x/?a=1#r'), f'{SITE}/calendar/x/?a=1#r')
        self.assertEqual(email_link('http://localhost/shop/'), f'{SITE}/shop/')

    def test_outside_links_and_mailto_pass_through(self):
        for url in ('https://payhip.com/b/39PgN', 'https://wa.me/27813139321',
                    'mailto:info@metanoiagroup.co.za', f'{SITE}/myhub/', ''):
            self.assertEqual(email_link(url), url)


@override_settings(CACHES=LOCMEM, SITE_URL=SITE,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EmailPolicyTests(TestCase):

    """Which notification categories still e-mail, and the unsubscribe footer and headers."""
    @classmethod
    def setUpTestData(cls):
        cls.student = make_user('stu', 'student')
        cls.coach = make_user('coach', 'educator')

    def test_reminders_are_emailed_by_default_updates_are_not(self):
        """Updates are not e-mailed, but the bell still has them."""
        for category in ('deadlines',):
            note = notify(self.student, title=f'Auto {category}', url='/myhub/', category=category, email=True)
            self.assertTrue(note.emailed)
        for category in ('meetings', 'content', 'weekly', 'assessments'):
            note = notify(self.student, title=f'Auto {category}', url='/myhub/', category=category, email=True)
            self.assertIsNotNone(note)
            self.assertFalse(note.emailed)
        self.assertEqual(sorted(m.subject for m in mail.outbox), ['Auto deadlines'])

    def test_switching_reminders_off_stops_the_email(self):
        pref = NotificationPreference.for_user(self.student)
        pref.email_reminders = False
        pref.save()
        notify(self.student, title='Due soon', category='deadlines', email=True)
        notify(self.student, title='New blueprint', category='content', email=True)
        self.assertEqual(mail.outbox, [])

    def test_a_person_sending_it_still_emails(self):
        notify(self.student, title='We miss you', category='deadlines', email=True, actor=self.coach)
        self.assertEqual(len(mail.outbox), 1)

    def test_content_notice_with_an_uploader_is_still_automated(self):
        notify(self.student, title='New on Metanoia', category='content', email=True,
               actor=self.coach, email_kind='updates')
        self.assertEqual(mail.outbox, [])

    def test_account_mail_is_untouched(self):
        """A notice without a category is account business and always e-mails."""
        notify(self.student, title='1-on-1 cancelled', email=True)
        self.assertEqual(len(mail.outbox), 1)

    def test_notification_email_button_is_absolute(self):
        notify(self.student, title='Hi', url='/learning/x/', email=True, actor=self.coach)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(f'href="{SITE}/learning/x/"', html)
        self.assertNotIn('href="/learning/x/"', html)

    def test_automated_mail_carries_unsubscribe_footer_and_headers(self):
        send_branded_email('Your receipt', self.student.email, 'general', {'heading': 'Hi', 'body': 'x'})
        msg = mail.outbox[0]
        html = msg.alternatives[0][0]
        links = email_prefs.links_for(self.student)
        self.assertIn(links['unsubscribe_url'], html)
        self.assertIn('Terms &amp; conditions', html)
        self.assertIn('Privacy policy', html)
        self.assertEqual(msg.extra_headers['List-Unsubscribe'], f'<{links["one_click_url"]}>')
        self.assertEqual(msg.extra_headers['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')

    def test_personal_mail_has_no_unsubscribe(self):
        send_branded_email('Note', self.student.email, 'general', {'heading': 'Hi', 'body': 'x'}, automated=False)
        msg = mail.outbox[0]
        self.assertNotIn('Unsubscribe</a>', msg.alternatives[0][0])
        self.assertNotIn('List-Unsubscribe', msg.extra_headers)
        self.assertIn('Privacy policy', msg.alternatives[0][0])

    def test_banner_links_to_the_public_website(self):
        send_branded_email('Hi', self.student.email, 'general', {'heading': 'Hi', 'body': 'x'})
        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn('metanoiacoach.co.za"', html.replace('metanoiagroup.co.za"', ''))
        self.assertIn('href="https://metanoiagroup.co.za"', html)


@override_settings(CACHES=LOCMEM, SECURE_SSL_REDIRECT=False)
class UnsubscribePageTests(TestCase):

    """The signed e-mail preferences page reached from the unsubscribe link."""
    @classmethod
    def setUpTestData(cls):
        cls.student = make_user('stu', 'student')

    def setUp(self):
        pref = NotificationPreference.for_user(self.student)
        pref.email_reminders = pref.email_updates = pref.email_enabled = True
        pref.save()
        self.url = reverse('communication:email-preferences', args=[email_prefs.token_for(self.student)])

    def pref(self):
        """Return the student's preferences fresh from the database."""
        return NotificationPreference.objects.get(user=self.student)

    def test_opens_signed_out_and_a_get_changes_nothing(self):
        res = self.client.get(self.url + '?unsubscribe=1')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, 'Unsubscribe from optional e-mails?')
        self.assertTrue(self.pref().email_reminders)

    def test_unsubscribe_turns_every_optional_email_off(self):
        self.client.post(self.url, {'action': 'unsubscribe'})
        pref = self.pref()
        self.assertFalse(pref.email_reminders or pref.email_updates or pref.email_enabled)
        self.assertIsNotNone(pref.email_unsubscribed_at)

    def test_switches_save(self):
        self.client.post(self.url, {'email_updates': 'on'})
        pref = self.pref()
        self.assertEqual((pref.email_reminders, pref.email_updates, pref.email_enabled), (False, True, False))

    def test_one_click_needs_no_csrf(self):
        client = self.client_class(enforce_csrf_checks=True)
        res = client.post(reverse('communication:email-unsubscribe-one-click',
                                  args=[email_prefs.token_for(self.student)]))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.pref().email_reminders)

    def test_a_forged_token_is_refused(self):
        res = self.client.get(reverse('communication:email-preferences', args=['nope:abc']))
        self.assertEqual(res.status_code, 404)
