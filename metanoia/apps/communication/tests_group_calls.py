"""Group video calls on Microsoft Teams, and the study group's own chat.

Graph is the mocked one from apps.msteams.tests_teams_flow (FakeGraph): no
test here reaches Microsoft.
"""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.msteams import graph
from apps.msteams.tests_teams_flow import GUID, JOIN, TEAMS_ON, FakeGraph, teams_routes

from . import models, study_groups
from .tests_api_security import LOCMEM_CACHE, make_user


@override_settings(CACHES=LOCMEM_CACHE, **TEAMS_ON)
class GroupCallTests(TestCase):

    """Starting and joining a study group's Teams call from its chat."""
    def setUp(self):
        cache.clear()
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com')
        self.eve = make_user('eve@example.com')
        self.group = models.ChatGroup.objects.create(
            kind=models.ChatGroup.KIND_CUSTOM, name='Study circle', created_by=self.alice)
        self.group.add_member(self.alice, role='owner')
        self.group.add_member(self.bob)
        self.fake = FakeGraph(teams_routes())
        graph._user_id_cache.clear()
        for p in (mock.patch.object(graph, '_get_token', return_value='tok'),
                  mock.patch('requests.request', self.fake)):
            p.start()
            self.addCleanup(p.stop)
        self.client.force_login(self.alice)

    def start(self):
        """Press the group chat's call button as the logged-in user."""
        return self.client.post(reverse('communication:chat-call', args=[self.group.pk]))

    def test_a_call_is_a_teams_meeting_owned_by_the_platform_account(self):
        """The meeting belongs to the platform account and everyone is let in.

        Every member is on the invite list, so the hand-off lets them in, and the
        group is told about the call with the way in.
        """
        response = self.start()
        meeting = models.MeetingRoom.objects.get(group=self.group)
        self.assertRedirects(response, reverse('livesessions:session-join', args=[meeting.pk]),
                             fetch_redirect_response=False)
        self.assertTrue(meeting.is_teams)
        self.assertEqual(meeting.teams_join_url, JOIN)
        self.assertEqual(meeting.organizer_upn, 'admin@metanoia.co.za')
        sent = self.fake.find('POST', f'/users/{GUID}/onlineMeetings')[0]['json']
        self.assertEqual(sent['lobbyBypassSettings']['scope'], 'everyone')
        self.assertFalse(sent['recordAutomatically'])
        self.assertEqual(set(meeting.participants.values_list('user_id', flat=True)),
                         {self.alice.pk, self.bob.pk})
        msg = self.group.messages.latest('created_at')
        self.assertIn(reverse('communication:chat-call-join', args=[self.group.pk, meeting.pk]), msg.body)

    def test_a_second_press_joins_the_running_call(self):
        self.start()
        self.client.force_login(self.bob)
        self.start()
        self.assertEqual(models.MeetingRoom.objects.filter(group=self.group).count(), 1)
        self.assertEqual(len(self.fake.find('POST', '/onlineMeetings')), 1)

    def test_a_member_who_joined_later_gets_in_through_the_group(self):
        self.start()
        meeting = models.MeetingRoom.objects.get(group=self.group)
        carol = make_user('carol@example.com')
        self.group.add_member(carol)
        self.client.force_login(carol)
        response = self.client.get(reverse('communication:chat-call-join', args=[self.group.pk, meeting.pk]))
        self.assertRedirects(response, reverse('livesessions:session-join', args=[meeting.pk]),
                             fetch_redirect_response=False)
        response = self.client.get(reverse('livesessions:session-join', args=[meeting.pk]))
        self.assertEqual(response['Location'], JOIN)

    def test_an_outsider_cannot_start_or_join(self):
        self.start()
        meeting = models.MeetingRoom.objects.get(group=self.group)
        self.client.force_login(self.eve)
        self.start()
        self.client.get(reverse('communication:chat-call-join', args=[self.group.pk, meeting.pk]))
        self.assertFalse(meeting.participants.filter(user=self.eve).exists())
        self.assertEqual(models.MeetingRoom.objects.filter(group=self.group).count(), 1)

    def test_when_teams_refuses_the_person_is_told_and_nothing_is_saved(self):
        self.fake.routes[('POST', f'/users/{GUID}/onlineMeetings')] = (403, {'error': {'code': 'Forbidden'}})
        response = self.client.post(reverse('communication:chat-call', args=[self.group.pk]), follow=True)
        self.assertContains(response, 'The video call could not be started')
        self.assertFalse(models.MeetingRoom.objects.filter(group=self.group).exists())

    def test_the_group_header_offers_video_only(self):
        html = self.client.get(reverse('communication:chat-room', args=[self.group.pk])).content.decode()
        self.assertIn('id="videoCallBtn"', html)
        self.assertNotIn('voiceCallBtn', html)
        self.assertNotIn('kind=audio', html)
        self.start()
        html = self.client.get(reverse('communication:chat-room', args=[self.group.pk])).content.decode()
        self.assertIn('Join call', html)

    def test_a_direct_chat_has_no_video_call(self):
        from . import services
        direct = services.get_or_create_direct_chat(self.alice, self.bob)
        self.client.post(reverse('communication:chat-call', args=[direct.pk]))
        self.assertFalse(models.MeetingRoom.objects.filter(group=direct).exists())
        self.assertEqual(self.fake.calls, [])


@override_settings(CACHES=LOCMEM_CACHE, MS_TEAMS_ENABLED=False)
class StudyGroupChatTests(TestCase):

    """A study group's own chat follows the group's membership."""
    def setUp(self):
        cache.clear()
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com')
        self.eve = make_user('eve@example.com')
        self.ws = models.Workspace.objects.create(name='Tax circle', created_by=self.alice)
        self.ws.members.add(self.bob)

    def test_the_group_gets_its_own_chat_with_its_members(self):
        chat = study_groups.ensure_chat(self.ws)
        self.assertEqual(chat.kind, models.ChatGroup.KIND_CUSTOM)
        self.assertEqual(chat.name, 'Tax circle')
        self.assertEqual(set(chat.memberships.values_list('user_id', flat=True)),
                         {self.alice.pk, self.bob.pk})
        self.assertEqual(chat.linked_study_group, self.ws)
        self.assertTrue(chat.can_video_call)

    def test_members_follow_the_group(self):
        chat = study_groups.ensure_chat(self.ws)
        self.ws.members.remove(self.bob)
        self.ws.members.add(self.eve)
        study_groups.sync_chat(self.ws)
        self.assertEqual(set(chat.memberships.values_list('user_id', flat=True)),
                         {self.alice.pk, self.eve.pk})
        self.ws.is_active = False
        self.ws.save()
        study_groups.sync_chat(self.ws)
        chat.refresh_from_db()
        self.assertFalse(chat.is_active)

    def test_the_group_page_embeds_its_chat(self):
        self.client.force_login(self.bob)
        response = self.client.get(reverse('communication:workspace-detail', args=[self.ws.pk]))
        self.assertEqual(response.status_code, 200)
        self.ws.refresh_from_db()
        self.assertIsNotNone(self.ws.chat_group)
        self.assertContains(response, 'Group chat')
        self.assertContains(response, 'id="msgs"')
        self.assertContains(response, 'id="videoCallBtn"')

    def test_leaving_the_group_leaves_its_chat(self):
        chat = study_groups.ensure_chat(self.ws)
        self.client.force_login(self.bob)
        self.client.post(reverse('communication:workspace-detail', args=[self.ws.pk]), {'action': 'leave'})
        self.assertFalse(chat.memberships.filter(user=self.bob).exists())

    def test_an_outsider_is_not_put_in_the_chat(self):
        study_groups.ensure_chat(self.ws)
        self.client.force_login(self.eve)
        self.client.get(reverse('communication:workspace-detail', args=[self.ws.pk]))
        self.assertFalse(self.ws.chat_group.memberships.filter(user=self.eve).exists())
