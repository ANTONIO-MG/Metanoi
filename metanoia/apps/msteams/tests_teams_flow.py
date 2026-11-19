"""The Teams live-session flow end to end, against a mocked Microsoft Graph.

No test here reaches Microsoft: ``requests`` is replaced by :class:`FakeGraph`,
which answers like Graph and records every call, so the assertions are about
exactly what the LMS *sends* — the meeting, the calendar event and its times,
the update and the cancellation — and about what the people involved receive.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.communication.models import MeetingRoom, Notification, NotificationPreference
from apps.learning.models import (Institution, Module, ModuleEnrolment, ModuleMaterial,
                                  Programme, ProgrammeModule)
from apps.livesessions import reminders, services, sources
from apps.livesessions.models import SessionReminder
from apps.livesessions.tests import make_user

from . import graph

GUID = '11111111-2222-3333-4444-555555555555'
JOIN = 'https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0'
BASE = 'https://graph.microsoft.com/v1.0'

TEAMS_ON = dict(
    MS_TEAMS_ENABLED=True, MEETING_PROVIDER='teams',
    MS_GRAPH_TENANT_ID='tenant', MS_GRAPH_CLIENT_ID='client', MS_GRAPH_CLIENT_SECRET='secret',
    MS_GRAPH_DEFAULT_ORGANIZER='admin@metanoia.co.za', MS_GRAPH_BASE=BASE,
    SITE_URL='https://lms.example.org', TIME_ZONE='Africa/Johannesburg',
)


class FakeResponse:
    """A minimal :class:`requests.Response`: status, JSON body and text."""

    def __init__(self, status=200, data=None):
        """A response with ``status`` and the JSON body ``data`` (``None`` for an empty body)."""
        self.status_code = status
        self._data = data
        self.content = b'' if data is None else json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        """The JSON body; raises :class:`ValueError` when there is none, like ``requests``."""
        if self._data is None:
            raise ValueError('no body')
        return self._data


class FakeGraph:
    """Answers Graph calls from a route table and keeps a log of every call.

    ``routes`` maps ``(METHOD, path-suffix)`` → ``(status, body)``; the first
    route whose suffix ends the URL path wins. Unrouted calls answer 404.
    """

    def __init__(self, routes=None):
        """Start with ``routes`` and an empty call log."""
        self.calls = []
        self.routes = dict(routes or {})

    def __call__(self, method, url, params=None, json=None, timeout=None, headers=None,
                 data=None, **_kw):
        """Stand in for :func:`requests.request`: log the call and answer from the route table."""
        path = url.split('?', 1)[0]
        if path.startswith(BASE):
            path = path[len(BASE):]
        self.calls.append({'method': method.upper(), 'path': path, 'json': json,
                           'params': params, 'headers': headers or {}, 'data': data})
        for (m, suffix), (status, body) in self.routes.items():
            if m == method.upper() and path.endswith(suffix):
                return FakeResponse(status, body)
        return FakeResponse(404, {'error': {'code': 'itemNotFound'}})

    def put(self, url, data=None, headers=None, timeout=None, **kw):
        """Stand in for :func:`requests.put`."""
        return self('PUT', url, data=data, headers=headers, timeout=timeout)

    def delete(self, url, timeout=None, **kw):
        """Stand in for :func:`requests.delete`."""
        return self('DELETE', url, timeout=timeout)

    def find(self, method, suffix):
        """The logged calls with ``method`` whose path ends with ``suffix``."""
        return [c for c in self.calls if c['method'] == method and c['path'].endswith(suffix)]


def teams_routes():
    """Routes for the happy path: the organiser resolves, and the meeting and calendar event are created, updated and cancelled."""
    return {
        ('GET', '/users/admin@metanoia.co.za'): (200, {'id': GUID}),
        ('POST', f'/users/{GUID}/onlineMeetings'): (201, {'id': 'MEET1', 'joinWebUrl': JOIN}),
        ('PATCH', f'/users/{GUID}/onlineMeetings/MEET1'): (200, {'id': 'MEET1'}),
        ('DELETE', f'/users/{GUID}/onlineMeetings/MEET1'): (204, None),
        ('POST', '/users/admin@metanoia.co.za/events'): (201, {'id': 'EV1'}),
        ('PATCH', '/users/admin@metanoia.co.za/events/EV1'): (200, {'id': 'EV1'}),
        ('POST', '/users/admin@metanoia.co.za/events/EV1/cancel'): (202, None),
    }


class GraphHelpersTests(TestCase):
    """Graph helpers: event times, attendees, calendar events, cancelling and large uploads."""

    def test_event_times_are_sent_as_utc_wall_clock(self):
        """14:00 in Johannesburg is 12:00 UTC.

        Sending "14:00+02:00" with timeZone UTC would put it in Outlook at
        14:00 UTC — two hours late.
        """
        local = datetime(2026, 11, 20, 14, 0, tzinfo=ZoneInfo('Africa/Johannesburg'))
        self.assertEqual(graph.graph_datetime(local),
                         {'dateTime': '2026-11-20T12:00:00', 'timeZone': 'UTC'})
        self.assertEqual(graph.graph_datetime('2026-11-20T14:00:00+02:00')['dateTime'],
                         '2026-11-20T12:00:00')

    def test_attendees_are_deduplicated_and_blank_addresses_dropped(self):
        out = graph.event_attendees([('A@x.org', 'A'), ('a@x.org', 'A again'), ('', 'nobody')])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['emailAddress']['address'], 'A@x.org')

    @override_settings(**TEAMS_ON)
    def test_calendar_event_points_at_the_existing_meeting_not_a_new_one(self):
        fake = FakeGraph({('POST', '/users/org@x.org/events'): (201, {'id': 'E'})})
        with mock.patch.object(graph, '_get_token', return_value='tok'), \
                mock.patch('requests.request', fake):
            graph.create_calendar_event('org@x.org', subject='S', start_iso='2026-11-20T14:00:00+02:00',
                                        end_iso='2026-11-20T15:00:00+02:00', join_url=JOIN)
        body = fake.find('POST', '/events')[0]['json']
        self.assertFalse(body['isOnlineMeeting'])
        self.assertNotIn('onlineMeeting', body)
        self.assertEqual(body['location']['locationUri'], JOIN)
        self.assertEqual(body['start'], {'dateTime': '2026-11-20T12:00:00', 'timeZone': 'UTC'})

    @override_settings(**TEAMS_ON)
    def test_cancel_falls_back_to_delete_when_cancel_is_refused(self):
        fake = FakeGraph({('POST', '/events/E/cancel'): (400, {'error': {'code': 'x'}}),
                          ('DELETE', '/events/E'): (204, None)})
        with mock.patch.object(graph, '_get_token', return_value='tok'), \
                mock.patch('requests.request', fake):
            self.assertTrue(graph.cancel_calendar_event('org@x.org', 'E', 'Sorry'))
        self.assertEqual(len(fake.find('DELETE', '/events/E')), 1)

    @override_settings(**TEAMS_ON)
    def test_large_upload_goes_in_ranged_fragments(self):
        import os
        import tempfile
        size = 5 * 1024 * 1024 + 3
        fd, path = tempfile.mkstemp()
        os.write(fd, b'x' * size)
        os.close(fd)
        self.addCleanup(os.unlink, path)
        chunk = 8 * 320 * 1024
        upload_url = 'https://upload.example/session/1'
        fake = FakeGraph({('POST', ':/createUploadSession'): (200, {'uploadUrl': upload_url})})

        def put(url, data=None, headers=None, timeout=None, **kw):
            """Record a fragment PUT; the last fragment answers 201 with the drive item."""
            fake.calls.append({'method': 'PUT', 'path': url, 'headers': headers, 'data': data,
                               'json': None, 'params': None})
            last = headers['Content-Range'].endswith(f'-{size - 1}/{size}')
            return FakeResponse(201 if last else 202,
                                {'id': 'ITEM', 'size': size} if last else {'nextExpectedRanges': []})

        with mock.patch.object(graph, '_get_token', return_value='tok'), \
                mock.patch('requests.request', fake), mock.patch('requests.put', put):
            item = graph.upload_large_file('D', 'F', 'big.dump', path, chunk_size=chunk)
        self.assertEqual(item['id'], 'ITEM')
        puts = [c for c in fake.calls if c['method'] == 'PUT']
        self.assertEqual([c['headers']['Content-Range'] for c in puts], [
            f'bytes 0-{chunk - 1}/{size}', f'bytes {chunk}-{2 * chunk - 1}/{size}',
            f'bytes {2 * chunk}-{size - 1}/{size}'])
        self.assertTrue(all('Authorization' not in c['headers'] for c in puts),
                        'upload-session fragments must not carry the bearer token')


@override_settings(**TEAMS_ON)
class TeamsSessionFlowTests(TestCase):
    """Admin schedules → Teams + calendars → LMS calendar → notices → reminders → edits → cancel."""

    @classmethod
    def setUpTestData(cls):
        """A module with an admin organiser, its educator, an enrolled student and an outsider."""
        institution = Institution.objects.create(code='MILPARKT', name='Milpark')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='FRT', name='Financial Reporting')
        cls.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                    code='FREPT')
        cls.admin = make_user('owner', 'admin', is_staff=True)
        cls.admin.email = 'admin@metanoia.co.za'
        cls.admin.save()
        cls.educator = make_user('lecturer', 'educator')
        cls.educator.profile.taught_modules.add(cls.module)
        cls.student = make_user('pupil')
        ModuleEnrolment.objects.create(person=cls.student.profile, programme_module=cls.module)
        cls.outsider = make_user('stranger')

    def setUp(self):
        """Route Graph calls to a :class:`FakeGraph`; the class starts in two days."""
        self.fake = FakeGraph(teams_routes())
        graph._user_id_cache.clear()
        patches = [mock.patch.object(graph, '_get_token', return_value='tok'),
                   mock.patch('requests.request', self.fake)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.start = (timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)

    def _schedule(self, **kw):
        """Schedule a one-hour module session through :func:`apps.livesessions.services.create_session`."""
        args = dict(host=self.admin, title='Deferred tax workshop', start=self.start,
                    end=self.start + timedelta(hours=1), audience=MeetingRoom.AUDIENCE_MODULE,
                    module=self.module, description='Bring IAS 12.')
        args.update(kw)
        return services.create_session(**args)

    def test_admin_schedules_a_teams_meeting_with_a_calendar_event(self):
        """The module's educator is invited (Outlook + Teams calendar).

        The student joins anonymously from the LMS, and the organiser is not their
        own attendee.
        """
        meeting = self._schedule()

        self.assertTrue(meeting.is_teams)
        self.assertEqual(meeting.teams_meeting_id, 'MEET1')
        self.assertEqual(meeting.teams_join_url, JOIN)
        self.assertEqual(meeting.teams_calendar_event_id, 'EV1')
        self.assertEqual(meeting.organizer_upn, 'admin@metanoia.co.za')

        created = self.fake.find('POST', '/onlineMeetings')[0]['json']
        self.assertEqual(created['subject'], 'Deferred tax workshop')
        self.assertTrue(created['recordAutomatically'])

        event = self.fake.find('POST', '/users/admin@metanoia.co.za/events')[0]['json']
        start_utc = self.start.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        self.assertEqual(event['start'], {'dateTime': start_utc, 'timeZone': 'UTC'})
        self.assertFalse(event['isOnlineMeeting'])
        self.assertIn(JOIN, event['body']['content'])
        invited = {a['emailAddress']['address'] for a in event['attendees']}
        self.assertEqual(invited, {self.educator.email})

    def test_a_host_outside_the_tenant_is_invited_to_their_own_session(self):
        coach = make_user('visitingcoach', 'staff', is_staff=True)
        coach.email = 'coach@school.example'
        coach.save()
        meeting = self._schedule(host=coach)
        self.assertEqual(meeting.organizer_upn, 'admin@metanoia.co.za')
        event = self.fake.find('POST', '/users/admin@metanoia.co.za/events')[0]['json']
        invited = {a['emailAddress']['address'] for a in event['attendees']}
        self.assertEqual(invited, {coach.email, self.educator.email})

    def test_session_shows_on_the_lms_calendar_and_joins_into_teams(self):
        """The session is also on the module schedule before it happens."""
        meeting = self._schedule()
        entries = sources.entries_for(self.student, self.start - timedelta(hours=1),
                                      self.start + timedelta(hours=2))
        self.assertIn('Deferred tax workshop', [e.title for e in entries])
        outsider = sources.entries_for(self.outsider, self.start - timedelta(hours=1),
                                       self.start + timedelta(hours=2))
        self.assertNotIn('Deferred tax workshop', [e.title for e in outsider])

        self.assertTrue(ModuleMaterial.objects.filter(
            meeting=meeting, kind=ModuleMaterial.KIND_LIVE).exists())

        self.client.force_login(self.student)
        page = self.client.get(reverse('livesessions:session-detail', args=[meeting.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, reverse('livesessions:session-join', args=[meeting.pk]))
        joined = self.client.get(reverse('livesessions:session-join', args=[meeting.pk]))
        self.assertRedirects(joined, JOIN, fetch_redirect_response=False)

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(
            reverse('livesessions:session-join', args=[meeting.pk])).status_code, 404)

    def test_the_audience_is_notified_with_an_invite_email(self):
        """The invite links to the session page, whose Join button goes into Teams."""
        pref = NotificationPreference.for_user(self.student)
        pref.email_reminders = True
        pref.save()
        mail.outbox.clear()

        meeting = self._schedule()

        told = Notification.objects.filter(meeting=meeting, verb='session scheduled')
        self.assertEqual(set(told.values_list('recipient_id', flat=True)),
                         {self.student.pk, self.educator.pk, self.admin.pk})
        self.assertFalse(told.filter(recipient=self.outsider).exists())
        invites = [m for m in mail.outbox if self.student.email in m.to]
        self.assertEqual(len(invites), 1)
        body = invites[0].body + ''.join(str(a[0]) for a in getattr(invites[0], 'alternatives', []))
        self.assertIn(f'https://lms.example.org{meeting.get_absolute_url()}', body)

    def test_reminders_fan_out_once_per_person_and_lead(self):
        """Each person gets one reminder per lead: 24 hours and 30 minutes."""
        meeting = self._schedule()
        sent = reminders.run(now=self.start - timedelta(minutes=20))
        self.assertIn('session reminder', sent)
        ledger = SessionReminder.objects.filter(meeting=meeting)
        self.assertEqual(set(ledger.filter(lead_minutes=30).values_list('recipient_id', flat=True)),
                         {self.student.pk, self.educator.pk, self.admin.pk})
        note = Notification.objects.filter(recipient=self.student, verb='session reminder')
        self.assertEqual(note.count(), 2)
        self.assertEqual(note.first().url, meeting.get_absolute_url())

        reminders.run(now=self.start - timedelta(minutes=10))
        self.assertEqual(Notification.objects.filter(recipient=self.student,
                                                     verb='session reminder').count(), 2)

    def test_rescheduling_patches_teams_and_the_calendar_and_tells_people(self):
        """The reminder ledger is cleared, so reminders are sent again against the new time."""
        meeting = self._schedule()
        reminders.run(now=self.start - timedelta(minutes=20))
        self.fake.calls.clear()
        mail.outbox.clear()

        new_start = self.start + timedelta(days=1, hours=2)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('livesessions:session-edit', args=[meeting.pk]), {
            'target': 'class', 'title_suffix': 'Deferred tax workshop (moved)',
            'description': meeting.description,
            'session_kind': meeting.session_kind,
            'scheduled_start': timezone.localtime(new_start).strftime('%Y-%m-%dT%H:%M'),
            'scheduled_end': timezone.localtime(new_start + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            'institution': self.module.programme.institution_id,
            'programme': self.module.programme_id,
            'module': self.module.pk,
            'record_automatically': 'on',
        })
        form = (response.context or {}).get('form') if response.status_code == 200 else None
        self.assertEqual(response.status_code, 302, form.errors if form else response.status_code)
        meeting.refresh_from_db()

        online = self.fake.find('PATCH', '/onlineMeetings/MEET1')[0]['json']
        self.assertEqual(online['subject'], meeting.title)
        self.assertTrue(meeting.title.startswith('MILPARKT PGDA FREPT · '))
        self.assertTrue(meeting.title.endswith(' | Deferred tax workshop (moved)'))
        self.assertIn('startDateTime', online)
        event = self.fake.find('PATCH', '/events/EV1')[0]['json']
        self.assertEqual(event['start']['dateTime'],
                         new_start.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(event['subject'], meeting.title)
        self.assertEqual({a['emailAddress']['address'] for a in event['attendees']},
                         {self.educator.email})

        moved = Notification.objects.filter(meeting=meeting, verb='session updated')
        self.assertTrue(moved.filter(recipient=self.student).exists())
        self.assertFalse(moved.filter(recipient=self.admin).exists(), 'the editor knows already')
        self.assertIn('moved', moved.get(recipient=self.student).title.lower())
        self.assertFalse(SessionReminder.objects.filter(meeting=meeting).exists())
        self.assertTrue(any(self.student.email in m.to for m in mail.outbox),
                        'a change made by a person is e-mailed like a cancellation')

    def test_an_edit_nobody_planned_around_sends_no_notice(self):
        meeting = self._schedule()
        services.update_session(meeting, changed=['record_automatically'], by=self.admin,
                                notify=True)
        self.assertFalse(Notification.objects.filter(meeting=meeting, verb='session updated').exists())

    def test_cancelling_cancels_the_event_deletes_the_meeting_and_tells_everyone(self):
        """No reminders are sent for a cancelled class."""
        meeting = self._schedule()
        self.fake.calls.clear()

        services.cancel_session(meeting, by=self.admin, reason='The lecturer is ill.')

        cancel = self.fake.find('POST', '/events/EV1/cancel')
        self.assertEqual(len(cancel), 1)
        self.assertIn('ill', cancel[0]['json']['comment'])
        self.assertEqual(len(self.fake.find('DELETE', f'/onlineMeetings/MEET1')), 1)
        meeting.refresh_from_db()
        self.assertFalse(meeting.is_active)
        self.assertFalse(ModuleMaterial.objects.filter(
            meeting=meeting, kind=ModuleMaterial.KIND_LIVE).exists())
        told = Notification.objects.filter(meeting=meeting, verb='session cancelled')
        self.assertTrue(told.filter(recipient=self.student).exists())
        self.assertTrue(told.filter(recipient=self.educator).exists())

        reminders.run(now=self.start - timedelta(minutes=20))
        self.assertFalse(SessionReminder.objects.filter(meeting=meeting).exists())

    def test_graph_refusal_keeps_the_session_without_a_link_and_says_so(self):
        """It is still a Teams session — there is no other engine — with no link yet.

        The meeting is made on the first join (see ``tests_seeded_sessions``), and
        everyone is still told about the class.
        """
        self.fake.routes[('POST', f'/users/{GUID}/onlineMeetings')] = (
            403, {'error': {'code': 'Forbidden'}})
        self.client.force_login(self.admin)
        response = self.client.post(reverse('livesessions:session-create'), {
            'target': 'class', 'title_suffix': 'Refused', 'session_kind': MeetingRoom.KIND_CLASS,
            'scheduled_start': timezone.localtime(self.start).strftime('%Y-%m-%dT%H:%M'),
            'scheduled_end': timezone.localtime(self.start + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            'institution': self.module.programme.institution_id,
            'programme': self.module.programme_id, 'module': self.module.pk,
        }, follow=True)
        meeting = MeetingRoom.objects.get(title__endswith=' | Refused')
        self.assertTrue(meeting.is_teams)
        self.assertEqual(meeting.teams_join_url, '')
        self.assertEqual(meeting.teams_calendar_event_id, '')
        self.assertContains(response, 'Microsoft Teams could not create the meeting')
        self.assertTrue(Notification.objects.filter(meeting=meeting, recipient=self.student,
                                                    verb='session scheduled').exists())

    @override_settings(MS_TEAMS_ENABLED=False)
    def test_teams_off_never_calls_graph(self):
        meeting = self._schedule()
        self.assertTrue(meeting.is_teams)
        self.assertEqual(meeting.teams_join_url, '')
        self.assertEqual(self.fake.calls, [])


class MeetingOwnerTests(TestCase):
    """A host outside the Microsoft tenant still gets a Teams meeting, owned by the default organiser."""

    @override_settings(**TEAMS_ON)
    def test_owner_falls_back_to_the_default_organiser(self):
        """``coach@school.example`` is not in the tenant (Graph answers 404)."""
        from apps.msteams import services
        host = get_user_model().objects.create_user('coach@school.example', 'coach@school.example', 'pw', is_staff=True)
        graph._user_id_cache.clear()
        fake = FakeGraph(teams_routes())
        with mock.patch.object(graph, '_get_token', return_value='tok'), \
                mock.patch('requests.request', fake):
            self.assertEqual(services.meeting_owner_for(host), 'admin@metanoia.co.za')
            room = services.create_class_meeting(host=host, title='Class',
                                                 start=timezone.now() + timezone.timedelta(days=1))
        self.assertEqual(room.provider, room.PROVIDER_TEAMS)
        self.assertEqual(room.organizer_upn, 'admin@metanoia.co.za')
        self.assertEqual(room.teams_join_url, JOIN)
        self.assertEqual(services.organizer_upn_for(host), 'coach@school.example',
                         'attendee invites still go to their own address')
