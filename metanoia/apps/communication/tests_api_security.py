"""``/api/communication/`` must not let one person see or change another's things.

``/api/`` is exempt from the site's login / management / parent middlewares, so
the viewsets carry the rules themselves. These tests pin them from the point of
view of an ordinary student who is *not* in the conversation / meeting:

* chat groups: invisible, so not readable, editable or deletable; a member who
  is not the owner cannot rename or delete the group either;
* messages: invisible outside your conversations, and ``reply_to`` /
  ``mention_ids`` cannot reach into another conversation (the reply preview
  would otherwise echo a foreign message's text);
* meetings: only what the calendar would show you, and only the host or a
  manager may edit, delete or invite;
* chat files go through the same type/size validators as the web upload.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Person

from . import models

User = get_user_model()

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
IN_MEMORY_FILES = {**settings.STORAGES,
                   'files': {'BACKEND': 'django.core.files.storage.InMemoryStorage'}}

API = '/api/communication/'


def results(response):
    """Return the result list of an API response, paginated or not."""
    body = response.json()
    return body['results'] if isinstance(body, dict) and 'results' in body else body


def make_user(email, role='student', **extra):
    """Create a registered user with a profile of the given ``role``."""
    user = User.objects.create_user(username=email, email=email, password='pw12345!', **extra)
    Person.objects.update_or_create(user=user, defaults={
        'user_type': role, 'registered': True, 'profile_status': True,
        'first_name': email.split('@')[0].title()})
    return user


@override_settings(CACHES=LOCMEM_CACHE, STORAGES=IN_MEMORY_FILES)
class ApiSecurityFixture(TestCase):
    """A custom chat group owned by Alice with Bob as a plain member, and Eve as the outsider.

    Eve also owns a group of her own, so she has somewhere legitimate to post
    from; Staff is a staff user who may manage any group.
    """
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com')
        self.eve = make_user('eve@example.com')
        self.staff = make_user('staff@example.com', role='staff', is_staff=True)

        self.group = models.ChatGroup.objects.create(
            kind=models.ChatGroup.KIND_CUSTOM, name='Study circle', created_by=self.alice)
        self.group.add_member(self.alice, role='owner')
        self.group.add_member(self.bob)
        self.secret = models.Message.objects.create(
            group=self.group, sender=self.alice, body='the secret answer is 42')

        self.eve_group = models.ChatGroup.objects.create(
            kind=models.ChatGroup.KIND_CUSTOM, name='Eve corner', created_by=self.eve)
        self.eve_group.add_member(self.eve, role='owner')

    def login(self, user):
        """Log the test client in as ``user``."""
        self.client.force_login(user)


class ChatGroupApiSecurityTests(ApiSecurityFixture):
    """Who may list, retrieve, rename or delete chat groups through the API."""
    def test_outsider_cannot_list_or_retrieve_the_group(self):
        self.login(self.eve)
        ids = [g['id'] for g in results(self.client.get(f'{API}chat-groups/'))]
        self.assertNotIn(self.group.pk, ids)
        self.assertEqual(self.client.get(f'{API}chat-groups/{self.group.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'{API}chat-groups/{self.group.pk}/members/').status_code, 404)

    def test_outsider_cannot_edit_or_delete_the_group(self):
        self.login(self.eve)
        r = self.client.patch(f'{API}chat-groups/{self.group.pk}/', {'name': 'pwned'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.delete(f'{API}chat-groups/{self.group.pk}/').status_code, 404)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, 'Study circle')

    def test_plain_member_cannot_rename_or_delete_the_group(self):
        self.login(self.bob)
        r = self.client.patch(f'{API}chat-groups/{self.group.pk}/', {'name': 'mine now'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.delete(f'{API}chat-groups/{self.group.pk}/').status_code, 403)
        self.assertTrue(models.ChatGroup.objects.filter(pk=self.group.pk, name='Study circle').exists())

    def test_owner_can_rename_but_not_change_the_kind(self):
        self.login(self.alice)
        r = self.client.patch(f'{API}chat-groups/{self.group.pk}/',
                              {'name': 'Renamed', 'kind': 'module'}, content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, 'Renamed')
        self.assertEqual(self.group.kind, models.ChatGroup.KIND_CUSTOM)

    def test_nobody_but_staff_renames_a_direct_thread(self):
        direct = models.ChatGroup.objects.create(kind=models.ChatGroup.KIND_DIRECT)
        direct.add_member(self.alice, role='owner')
        direct.add_member(self.bob)
        self.login(self.alice)
        r = self.client.patch(f'{API}chat-groups/{direct.pk}/', {'name': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_staff_can_manage_any_group(self):
        self.login(self.staff)
        self.assertEqual(self.client.delete(f'{API}chat-groups/{self.group.pk}/').status_code, 204)

    def test_creating_a_group_still_works_for_the_chat_page(self):
        """chat.html's "New group" button POSTs {name} and reads back an id."""
        self.login(self.eve)
        r = self.client.post(f'{API}chat-groups/', {'name': 'New'}, content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content[:300])
        group = models.ChatGroup.objects.get(pk=r.json()['id'])
        self.assertTrue(group.memberships.filter(user=self.eve, role='owner').exists())


class MessageApiSecurityTests(ApiSecurityFixture):
    """Who may read, post, edit or delete messages, and what replies and mentions may reference."""
    def test_outsider_cannot_read_messages_or_snippets(self):
        self.login(self.eve)
        self.assertEqual(results(self.client.get(f'{API}messages/?group={self.group.pk}')), [])
        self.assertEqual(results(self.client.get(f'{API}messages/')), [])
        self.assertEqual(self.client.get(f'{API}messages/{self.secret.pk}/').status_code, 404)
        body = self.client.get(f'{API}chat-groups/').content.decode()
        self.assertNotIn('secret answer', body)

    def test_outsider_cannot_edit_or_delete_messages(self):
        self.login(self.eve)
        r = self.client.patch(f'{API}messages/{self.secret.pk}/', {'body': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.delete(f'{API}messages/{self.secret.pk}/').status_code, 404)
        self.secret.refresh_from_db()
        self.assertEqual(self.secret.body, 'the secret answer is 42')
        self.assertFalse(self.secret.is_deleted)

    def test_outsider_cannot_post_into_the_group(self):
        self.login(self.eve)
        r = self.client.post(f'{API}messages/', {'group': self.group.pk, 'body': 'hi'})
        self.assertEqual(r.status_code, 403)

    def test_member_cannot_edit_someone_elses_message(self):
        self.login(self.bob)
        r = self.client.patch(f'{API}messages/{self.secret.pk}/', {'body': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_reply_to_cannot_quote_another_conversation(self):
        """The reply preview echoes the quoted body — a foreign id would leak it."""
        self.login(self.eve)
        r = self.client.post(f'{API}messages/', {
            'group': self.eve_group.pk, 'body': 'peek', 'reply_to': self.secret.pk})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn('secret answer', r.content.decode())
        self.assertFalse(models.Message.objects.filter(group=self.eve_group).exists())

    def test_mentions_are_limited_to_members(self):
        self.login(self.eve)
        r = self.client.post(f'{API}messages/', {
            'group': self.eve_group.pk, 'body': 'hey', 'mention_ids': [self.alice.pk]})
        self.assertEqual(r.status_code, 400)

    def test_an_edit_cannot_move_a_message_into_another_group(self):
        mine = models.Message.objects.create(group=self.eve_group, sender=self.eve, body='mine')
        self.login(self.eve)
        r = self.client.patch(f'{API}messages/{mine.pk}/', {'group': self.group.pk},
                              content_type='application/json')
        self.assertEqual(r.status_code, 400)
        mine.refresh_from_db()
        self.assertEqual(mine.group_id, self.eve_group.pk)

    def test_member_still_reads_and_replies(self):
        self.login(self.bob)
        listed = results(self.client.get(f'{API}messages/?group={self.group.pk}&ordering=created_at'))
        self.assertEqual([m['id'] for m in listed], [self.secret.pk])
        r = self.client.post(f'{API}messages/', {
            'group': self.group.pk, 'body': 'thanks', 'reply_to': self.secret.pk})
        self.assertEqual(r.status_code, 201, r.content[:300])


class ChatFileApiTests(ApiSecurityFixture):
    """Chat file uploads: type and size limits."""
    def post_file(self, name, content=b'data', content_type='application/octet-stream'):
        """Upload a file to the group as the logged-in user."""
        self.login(self.alice)
        upload = SimpleUploadedFile(name, content, content_type=content_type)
        return self.client.post(f'{API}messages/', {
            'group': self.group.pk, 'body': '', 'files': [upload]})

    def test_disallowed_file_types_are_refused(self):
        for name in ('evil.html', 'run.exe', 'page.svgz', 'script.js'):
            with self.subTest(name=name):
                r = self.post_file(name, b'<script>alert(1)</script>', 'text/html')
                self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertFalse(models.MessageAttachment.objects.exists())

    def test_oversized_files_are_refused(self):
        r = self.post_file('big.pdf', b'0' * (25 * 1024 * 1024 + 1), 'application/pdf')
        self.assertEqual(r.status_code, 400, r.content[:300])
        self.assertFalse(models.MessageAttachment.objects.exists())

    def test_an_allowed_file_is_stored(self):
        r = self.post_file('notes.pdf', b'%PDF-1.4 hello', 'application/pdf')
        self.assertEqual(r.status_code, 201, r.content[:300])
        self.assertEqual(models.MessageAttachment.objects.count(), 1)


class MeetingApiSecurityTests(ApiSecurityFixture):
    """Meetings through the API: visibility follows the calendar, changes need the host or staff."""
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.private = models.MeetingRoom.objects.create(
            title='Alice 1:1', host=self.alice, scheduled_start=now,
            audience=models.MeetingRoom.AUDIENCE_PRIVATE)
        self.public = models.MeetingRoom.objects.create(
            title='Open lecture', host=self.alice, scheduled_start=now,
            audience=models.MeetingRoom.AUDIENCE_EVERYONE)
        self.chat_call = models.MeetingRoom.objects.create(
            title='Study circle call', host=self.alice, scheduled_start=now,
            group=self.group, audience=models.MeetingRoom.AUDIENCE_PRIVATE)

    def ids(self):
        """Return the ids of the meetings the API lists for the logged-in user."""
        return {m['id'] for m in results(self.client.get(f'{API}meetings/'))}

    def test_outsider_sees_only_what_the_calendar_shows(self):
        self.login(self.eve)
        ids = self.ids()
        self.assertIn(self.public.pk, ids)
        self.assertNotIn(self.private.pk, ids)
        self.assertNotIn(self.chat_call.pk, ids)
        self.assertEqual(self.client.get(f'{API}meetings/{self.private.pk}/').status_code, 404)

    def test_outsider_cannot_edit_delete_or_invite(self):
        """A meeting visible to everyone is still not editable by an outsider."""
        self.login(self.eve)
        r = self.client.patch(f'{API}meetings/{self.private.pk}/', {'title': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.delete(f'{API}meetings/{self.private.pk}/').status_code, 404)
        r = self.client.patch(f'{API}meetings/{self.public.pk}/', {'title': 'x'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.delete(f'{API}meetings/{self.public.pk}/').status_code, 403)
        r = self.client.post(f'{API}meetings/{self.public.pk}/invite/',
                             {'user_ids': [self.bob.pk]}, content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertTrue(models.MeetingRoom.objects.filter(pk=self.public.pk, title='Open lecture').exists())

    def test_group_members_see_their_chat_call(self):
        """The chat page polls meetings/ to ring when a call in the chat goes live."""
        self.login(self.bob)
        self.assertIn(self.chat_call.pk, self.ids())
        self.assertNotIn(self.private.pk, self.ids())

    def test_cannot_attach_a_meeting_to_someone_elses_chat(self):
        self.login(self.eve)
        r = self.client.post(f'{API}meetings/', {'title': 'Crash', 'group': self.group.pk},
                             content_type='application/json')
        self.assertEqual(r.status_code, 403)

    def test_host_and_staff_can_change_it(self):
        self.login(self.alice)
        r = self.client.patch(f'{API}meetings/{self.private.pk}/', {'title': 'Moved'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.login(self.staff)
        self.assertIn(self.private.pk, self.ids())
        self.assertEqual(self.client.delete(f'{API}meetings/{self.private.pk}/').status_code, 204)
