"""``/api/myhub/events/`` — your reminders are yours; institution events are staff's.

An Event with no owner is institution-wide (everyone sees it); one with an owner
is that person's private calendar entry. A student must not be able to read,
edit or delete someone else's entry, edit an institution-wide one, or plant an
entry on another person's calendar.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Person

from . import models

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
API = '/api/myhub/events/'


def make_user(email, role='student', **extra):
    """A user whose profile has role ``role``."""
    user = User.objects.create_user(username=email, email=email, password='pw12345!', **extra)
    Person.objects.update_or_create(user=user, defaults={'user_type': role})
    return user


def results(response):
    """The result list of a (possibly paginated) API response."""
    body = response.json()
    return body['results'] if isinstance(body, dict) and 'results' in body else body


@override_settings(CACHES=LOCMEM_CACHE)
class EventApiSecurityTests(TestCase):
    """Who may see and change which calendar entries through the events API."""

    def setUp(self):
        """Two students with a private reminder each, a staff member (staff by profile only) and one institution-wide event."""
        cache.clear()
        self.addCleanup(cache.clear)
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com')
        self.staff = make_user('staff@example.com', role='staff')
        now = timezone.now()
        self.public = models.Event.objects.create(title='Open day', start=now)
        self.alice_reminder = models.Event.objects.create(
            title='Alice dentist', start=now, owner=self.alice,
            category=models.Event.CATEGORY_REMINDER, description='private')
        self.bob_reminder = models.Event.objects.create(
            title='Bob gym', start=now, owner=self.bob, category=models.Event.CATEGORY_REMINDER)

    def ids(self):
        """Ids of the events the signed-in user can list."""
        return {e['id'] for e in results(self.client.get(API))}

    def test_student_sees_institution_events_and_their_own_only(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.ids(), {self.public.pk, self.bob_reminder.pk})
        self.assertEqual(self.client.get(f'{API}{self.alice_reminder.pk}/').status_code, 404)

    def test_student_cannot_edit_or_delete_someone_elses_reminder(self):
        self.client.force_login(self.bob)
        r = self.client.patch(f'{API}{self.alice_reminder.pk}/', {'title': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.delete(f'{API}{self.alice_reminder.pk}/').status_code, 404)
        self.alice_reminder.refresh_from_db()
        self.assertEqual(self.alice_reminder.title, 'Alice dentist')

    def test_student_cannot_edit_or_delete_institution_events(self):
        self.client.force_login(self.bob)
        r = self.client.patch(f'{API}{self.public.pk}/', {'title': 'Cancelled!'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.delete(f'{API}{self.public.pk}/').status_code, 403)
        self.assertTrue(models.Event.objects.filter(pk=self.public.pk, title='Open day').exists())

    def test_student_entries_are_always_their_own(self):
        """A student cannot plant an entry on someone else, nor hand one over afterwards."""
        self.client.force_login(self.bob)
        r = self.client.post(API, {'title': 'Planted', 'start': timezone.now().isoformat(),
                                   'owner': self.alice.pk}, content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content[:300])
        event = models.Event.objects.get(pk=r.json()['id'])
        self.assertEqual(event.owner_id, self.bob.pk)
        r = self.client.patch(f'{API}{event.pk}/', {'owner': self.alice.pk},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.owner_id, self.bob.pk)

    def test_student_can_manage_their_own_reminder(self):
        self.client.force_login(self.bob)
        r = self.client.patch(f'{API}{self.bob_reminder.pk}/', {'title': 'Bob run'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete(f'{API}{self.bob_reminder.pk}/').status_code, 204)

    def test_staff_see_and_manage_everything(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.ids(), {self.public.pk, self.alice_reminder.pk, self.bob_reminder.pk})
        r = self.client.patch(f'{API}{self.public.pk}/', {'title': 'Open day (moved)'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete(f'{API}{self.alice_reminder.pk}/').status_code, 204)
