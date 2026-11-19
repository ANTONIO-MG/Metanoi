"""Web meeting pages follow the same rules as ``/api/communication/meetings/``.

* ``meetings/`` lists only what the calendar would show you
  (``livesessions.audience.visible_sessions``) plus calls in your own chats —
  not every active meeting on the platform (each row carries the join link);
* ``meetings/new/`` schedules a standalone meeting, which is for the roles the
  calendar lets schedule (``audience.can_manage``: admin, staff, educator);
  anyone may still start a call in a chat they belong to;
* a meeting can only be attached to a conversation you are in.
"""

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from . import models
from .tests_api_security import ApiSecurityFixture, make_user


@override_settings(MS_TEAMS_ENABLED=False)
class MeetingWebSecurityTests(ApiSecurityFixture):
    """Meeting list and scheduling pages, with Teams switched off so no test calls Microsoft Graph."""
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.private = models.MeetingRoom.objects.create(
            title='Alice one-to-one', host=self.alice, scheduled_start=now,
            audience=models.MeetingRoom.AUDIENCE_PRIVATE)
        self.public = models.MeetingRoom.objects.create(
            title='Open lecture', host=self.alice, scheduled_start=now,
            audience=models.MeetingRoom.AUDIENCE_EVERYONE)
        self.chat_call = models.MeetingRoom.objects.create(
            title='Study circle call', host=self.alice, scheduled_start=now,
            group=self.group, audience=models.MeetingRoom.AUDIENCE_PRIVATE)
        self.educator = make_user('teach@example.com', role='educator')

    def listed(self, user):
        """Return the ids of the meetings the list page shows ``user``."""
        self.login(user)
        r = self.client.get(reverse('communication:meetings'))
        self.assertEqual(r.status_code, 200)
        return {m.pk for m in r.context['meetings']}

    def test_outsider_sees_only_what_the_calendar_shows(self):
        ids = self.listed(self.eve)
        self.assertIn(self.public.pk, ids)
        self.assertNotIn(self.private.pk, ids)
        self.assertNotIn(self.chat_call.pk, ids)

    def test_chat_member_sees_the_call_in_their_chat(self):
        ids = self.listed(self.bob)
        self.assertIn(self.chat_call.pk, ids)
        self.assertNotIn(self.private.pk, ids)

    def test_host_and_staff_see_their_meetings(self):
        self.assertIn(self.private.pk, self.listed(self.alice))
        self.assertTrue({self.private.pk, self.public.pk, self.chat_call.pk} <= self.listed(self.staff))

    def test_student_cannot_schedule_a_meeting(self):
        self.login(self.eve)
        before = models.MeetingRoom.objects.count()
        r = self.client.get(reverse('communication:meeting-create'))
        self.assertRedirects(r, reverse('communication:meetings'), fetch_redirect_response=False)
        r = self.client.post(reverse('communication:meeting-create'), {'title': 'Party'})
        self.assertRedirects(r, reverse('communication:meetings'), fetch_redirect_response=False)
        self.assertEqual(models.MeetingRoom.objects.count(), before)

    def test_student_list_has_no_schedule_button(self):
        self.login(self.eve)
        r = self.client.get(reverse('communication:meetings'))
        self.assertNotContains(r, reverse('communication:meeting-create'))

    def test_educator_can_schedule_but_not_into_someone_elses_chat(self):
        self.login(self.educator)
        r = self.client.get(reverse('communication:meeting-create'))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.group, list(r.context['form'].fields['group'].queryset))

        r = self.client.post(reverse('communication:meeting-create'),
                             {'title': 'Sneaky', 'group': self.group.pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(models.MeetingRoom.objects.filter(title='Sneaky').exists())

        r = self.client.post(reverse('communication:meeting-create'), {'title': 'Office hours'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(models.MeetingRoom.objects.filter(title='Office hours', host=self.educator).exists())

    def test_a_call_without_teams_is_refused_with_a_reason_not_a_fallback(self):
        self.login(self.bob)
        r = self.client.post(reverse('communication:chat-call', args=[self.group.pk]), follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'could not be started')
        self.assertFalse(models.MeetingRoom.objects.filter(host=self.bob, group=self.group).exists())
