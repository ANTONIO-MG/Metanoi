"""A personal reminder (an ``Event`` with an ``owner``) is its owner's alone.

Mirrors ``myhub.api.EventViewSet``: everyone sees institution-wide events (no
owner) plus their own; admin/staff see every entry. The web detail page, the
navbar search and the dashboard's upcoming-events list follow the same rule —
the detail page used to serve any event by id.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person

from . import models

User = get_user_model()


def make_user(email, role='student', **extra):
    user = User.objects.create_user(username=email, email=email, password='pw12345!', **extra)
    Person.objects.update_or_create(user=user, defaults={
        'user_type': role, 'registered': True, 'profile_status': True,
        'first_name': email.split('@')[0].title()})
    return user


class EventVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user('alice@example.com')
        cls.eve = make_user('eve@example.com')
        cls.staff = make_user('staff@example.com', role='staff', is_staff=True)
        soon = timezone.now() + timedelta(days=1)
        cls.reminder = models.Event.objects.create(
            title='Alice dentist zebra', start=soon, owner=cls.alice,
            category=models.Event.CATEGORY_REMINDER)
        cls.public = models.Event.objects.create(title='Open day zebra', start=soon)

    def detail(self, user, event):
        self.client.force_login(user)
        return self.client.get(reverse('myhub:event-detail', args=[event.pk]))

    def test_owner_sees_their_reminder(self):
        self.assertEqual(self.detail(self.alice, self.reminder).status_code, 200)

    def test_someone_else_gets_a_404(self):
        self.assertEqual(self.detail(self.eve, self.reminder).status_code, 404)

    def test_institution_wide_events_are_for_everyone(self):
        self.assertEqual(self.detail(self.eve, self.public).status_code, 200)

    def test_staff_see_everything(self):
        self.assertEqual(self.detail(self.staff, self.reminder).status_code, 200)

    def test_visible_to(self):
        self.assertEqual(set(models.Event.visible_to(self.eve)), {self.public})
        self.assertEqual(set(models.Event.visible_to(self.alice)), {self.public, self.reminder})
        self.assertEqual(set(models.Event.visible_to(self.staff)), {self.public, self.reminder})

    def test_search_does_not_reveal_other_peoples_reminders(self):
        from django.test import RequestFactory

        from . import search
        request = RequestFactory().get('/')
        request.user = self.eve
        labels = {row['label'] for row in search._events(request, 'zebra', None)}
        self.assertIn('Open day zebra', labels)
        self.assertNotIn('Alice dentist zebra', labels)

    def test_dashboard_upcoming_events_are_scoped(self):
        from .context_processors import get_upcoming_events
        self.assertEqual(set(get_upcoming_events(self.eve)), {self.public})
