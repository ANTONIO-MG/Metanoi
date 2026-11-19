"""Notifications page: tone filter (with pagination), the Notification centre
button for those who may send, and the centre's place in the sidebar."""

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.livesessions.tests import make_user

from . import views
from .models import Notification


def _note(user, level, title):
    """Create a notification for ``user`` with the given ``level`` (tone)."""
    return Notification.objects.create(recipient=user, title=title, level=level)


@override_settings(SECURE_SSL_REDIRECT=False)
class NotificationToneFilterTests(TestCase):
    """The tone filter chips, pagination with a filter, and the Notification centre entry points."""
    @classmethod
    def setUpTestData(cls):
        cls.student = make_user('tone-stu', 'student')
        cls.staff = make_user('tone-staff', 'staff')
        cls.admin = make_user('tone-admin', 'admin')
        cls.educator = make_user('tone-edu', 'educator')
        _note(cls.student, 'info', 'Plain update here')
        _note(cls.student, 'error', 'Urgent thing here')
        _note(cls.student, 'warning', 'Reminder thing here')

    def test_filter_by_tone(self):
        """The list narrows to one tone, and the chips are named as the composer names them.

        The header's bell dropdown still lists recent notifications of every tone,
        so the test checks the list itself.
        """
        self.client.force_login(self.student)
        url = reverse('communication:notifications')
        resp = self.client.get(url, {'tone': 'error'})
        self.assertEqual([n.title for n in resp.context['notifications']], ['Urgent thing here'])
        self.assertEqual(resp.context['tone'], 'error')
        for label in ('All', 'Update', 'Good news', 'Reminder', 'Urgent'):
            self.assertContains(resp, f'>{label}</a>')
        resp = self.client.get(url)
        self.assertEqual({n.title for n in resp.context['notifications']},
                         {'Plain update here', 'Urgent thing here', 'Reminder thing here'})

    def test_unknown_tone_shows_everything(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse('communication:notifications'), {'tone': 'bogus'})
        self.assertEqual(resp.context['tone'], '')
        self.assertEqual(len(resp.context['notifications']), 3)

    def test_filter_carries_across_pages(self):
        for i in range(views.NOTIFICATIONS_PER_PAGE + 5):
            _note(self.student, 'success', f'Good news number {i}')
        self.client.force_login(self.student)
        url = reverse('communication:notifications')
        resp = self.client.get(url, {'tone': 'success'})
        self.assertEqual(len(resp.context['notifications']), views.NOTIFICATIONS_PER_PAGE)
        self.assertContains(resp, '?tone=success&amp;page=2')
        resp = self.client.get(url, {'tone': 'success', 'page': 2})
        self.assertEqual(len(resp.context['notifications']), 5)
        self.assertTrue(all(n.level == 'success' for n in resp.context['notifications']))

    def test_notification_centre_button_follows_the_centres_rule(self):
        centre = reverse('communication:announcements')
        for user, sees in ((self.student, False), (self.educator, False),
                           (self.staff, True), (self.admin, True)):
            self.client.force_login(user)
            resp = self.client.get(reverse('communication:notifications'))
            self.assertEqual(resp.context['can_send_notifications'], sees, user)
            if sees:
                self.assertContains(resp, f'href="{centre}"')
                self.assertEqual(self.client.get(centre).status_code, 200)
            else:
                self.assertNotContains(resp, 'Notification centre</a>')
                self.assertNotEqual(self.client.get(centre).status_code, 200)

    def test_centre_is_in_the_administration_section(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('communication:notifications')).content.decode()
        admin_at = page.index('side-heading">Administration')
        ops_at = page.index('side-heading">Operations')
        centre_at = page.index('<span>Notifications centre</span>')
        self.assertTrue(admin_at < centre_at < ops_at)
        self.assertEqual(page.count('<span>Notifications centre</span>'), 1)
