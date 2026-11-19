"""Seeded Teams sessions: joining, pre-creating, editing, importing, recordings.

The IAC timetable was seeded as Teams sessions with no Microsoft link. These
tests pin down what happens to such a session — against the mocked Graph from
:mod:`apps.msteams.tests_teams_flow`, so nothing reaches Microsoft.
"""

from datetime import timedelta, timezone as dt_timezone
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.communication.models import MeetingRoom
from apps.learning.models import (Institution, Module, ModuleEnrolment, Programme,
                                  ProgrammeModule)
from apps.livesessions import pipeline, provisioning, services
from apps.livesessions.models import SessionArtifact
from apps.livesessions.tests import make_user

from . import calendar_import, graph
from .tests_teams_flow import GUID, JOIN, TEAMS_ON, FakeGraph, teams_routes

SHARED = 'https://teams.microsoft.com/l/meetup-join/19%3ameeting_shared%40thread.v2/0'


def _event(subject, start, minutes=90, join=SHARED, event_id='HANDMADE'):
    """A Graph calendar event for a hand-made Teams meeting starting at ``start``."""
    fmt = '%Y-%m-%dT%H:%M:%S.0000000'
    utc = start.astimezone(dt_timezone.utc)
    return {'id': event_id, 'subject': subject, 'isOnlineMeeting': True, 'isCancelled': False,
            'onlineMeeting': {'joinUrl': join},
            'start': {'dateTime': utc.strftime(fmt), 'timeZone': 'UTC'},
            'end': {'dateTime': (utc + timedelta(minutes=minutes)).strftime(fmt), 'timeZone': 'UTC'},
            'attendees': [{'emailAddress': {'address': 'pupil@example.com'}}]}


@override_settings(**TEAMS_ON)
class SeededSessionTests(TestCase):
    """Seeded Teams sessions with no link yet: joining, editing, pre-creating, rebuilding and importing them."""

    @classmethod
    def setUpTestData(cls):
        """A module with an admin host and one enrolled student."""
        institution = Institution.objects.create(code='SAICAT', name='SAICA')
        cls.programme = Programme.objects.create(institution=institution, code='IAC', name='IAC')
        canonical = Module.objects.create(code='FRX', name='Financial Reporting')
        cls.module = ProgrammeModule.objects.create(programme=cls.programme, module=canonical,
                                                    code='FREP')
        cls.host = make_user('coach', 'admin', is_staff=True)
        cls.host.email = 'admin@metanoia.co.za'
        cls.host.save()
        cls.student = make_user('pupil', first_name='Ada', last_name='Lovelace')
        ModuleEnrolment.objects.create(person=cls.student.profile, programme_module=cls.module)

    def setUp(self):
        """Route Graph calls to a :class:`FakeGraph`; classes start in three days."""
        self.fake = FakeGraph(teams_routes())
        graph._user_id_cache.clear()
        for p in (mock.patch.object(graph, '_get_token', return_value='tok'),
                  mock.patch('requests.request', self.fake)):
            p.start()
            self.addCleanup(p.stop)
        self.start = (timezone.now() + timedelta(days=3)).replace(second=0, microsecond=0)

    def _seeded(self, **kw):
        """A session as the programme seeder writes it: Teams, but no link."""
        fields = dict(title='FREP live session · Unit 2', host=self.host,
                      provider=MeetingRoom.PROVIDER_TEAMS, module=self.module,
                      programme=self.programme, institution=self.programme.institution,
                      audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=self.start,
                      scheduled_end=self.start + timedelta(hours=2),
                      external_event_id=f'seed:iac:{MeetingRoom.objects.count()}')
        fields.update(kw)
        return MeetingRoom.objects.create(**fields)

    def test_join_creates_the_missing_meeting_and_goes_straight_to_teams(self):
        """A second click uses the same meeting rather than minting another."""
        room = self._seeded()
        self.client.force_login(self.student)
        response = self.client.get(reverse('livesessions:session-join', args=[room.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], JOIN)
        room.refresh_from_db()
        self.assertEqual((room.teams_meeting_id, room.teams_join_url), ('MEET1', JOIN))
        self.assertEqual(len(self.fake.find('POST', '/onlineMeetings')), 1)
        self.client.get(reverse('livesessions:session-join', args=[room.pk]))
        self.assertEqual(len(self.fake.find('POST', '/onlineMeetings')), 1)

    def test_a_teams_session_never_falls_back_to_another_engine(self):
        room = self._seeded()
        self.fake.routes[('POST', f'/users/{GUID}/onlineMeetings')] = (500, {'error': {}})
        self.client.force_login(self.student)
        response = self.client.get(reverse('livesessions:session-join', args=[room.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('livesessions:session-detail', args=[room.pk]))

    def test_the_host_sees_how_to_start_the_class(self):
        room = self._seeded(teams_join_url=JOIN, teams_meeting_id='MEET1',
                            organizer_upn='admin@metanoia.co.za')
        self.client.force_login(self.host)
        page = self.client.get(reverse('livesessions:session-detail', args=[room.pk]))
        self.assertContains(page, 'Start or join the class in Teams')
        self.assertContains(page, 'signed in to Microsoft Teams')
        self.client.force_login(self.student)
        page = self.client.get(reverse('livesessions:session-detail', args=[room.pk]))
        self.assertNotContains(page, 'Start or join the class in Teams')

    def test_editing_a_seeded_session_creates_its_meeting(self):
        room = self._seeded()
        room.scheduled_start += timedelta(hours=1)
        room.scheduled_end += timedelta(hours=1)
        services.update_session(room, reschedule=True)
        room.refresh_from_db()
        self.assertEqual(room.teams_join_url, JOIN)
        sent = self.fake.find('POST', '/onlineMeetings')[0]['json']
        self.assertEqual(sent['startDateTime'], services._iso(room.scheduled_start))
        self.assertEqual(sent['lobbyBypassSettings']['scope'], 'everyone')
        self.assertTrue(self.fake.find('POST', '/events'))

    def test_moving_a_session_off_a_shared_hand_made_meeting(self):
        room = self._seeded(teams_join_url=SHARED, teams_meeting_id='SHARED1',
                            organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        room.scheduled_start += timedelta(days=1)
        room.scheduled_end += timedelta(days=1)
        services.update_session(room, reschedule=True)
        room.refresh_from_db()
        self.assertEqual(room.teams_join_url, JOIN)
        self.assertFalse(room.teams_meeting_external)
        self.assertFalse([c for c in self.fake.calls if 'SHARED1' in c['path']],
                         'the shared hand-made meeting must never be patched')

    def test_a_failed_move_keeps_the_shared_link(self):
        room = self._seeded(teams_join_url=SHARED, teams_meeting_id='SHARED1',
                            organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        self.fake.routes[('POST', f'/users/{GUID}/onlineMeetings')] = (500, {'error': {}})
        room.scheduled_start += timedelta(days=1)
        services.update_session(room, reschedule=True)
        room.refresh_from_db()
        self.assertEqual((room.teams_join_url, room.teams_meeting_external), (SHARED, True))

    def test_renaming_a_session_on_a_shared_meeting_leaves_teams_alone(self):
        room = self._seeded(teams_join_url=SHARED, teams_meeting_id='SHARED1',
                            organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        room.title = 'Renamed'
        services.update_session(room, reschedule=False)
        self.assertFalse([c for c in self.fake.calls if c['method'] in ('POST', 'PATCH', 'DELETE')])

    def test_cancelling_a_session_on_a_shared_meeting_keeps_the_meeting(self):
        room = self._seeded(teams_join_url=SHARED, teams_meeting_id='SHARED1',
                            organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        services.cancel_session(room)
        self.assertFalse(self.fake.find('DELETE', '/onlineMeetings/SHARED1'))

    def test_editing_moves_the_register_too(self):
        room = self._seeded()
        services.ensure_class_session(room)
        room.scheduled_start += timedelta(days=1)
        room.scheduled_end += timedelta(days=1)
        services.update_session(room, reschedule=True)
        room.class_session.refresh_from_db()
        self.assertEqual(room.class_session.starts_at, room.scheduled_start)

    def _calendar(self, events):
        """Make the organiser's calendar return ``events``."""
        self.fake.routes[('GET', f'/users/{GUID}/calendarView')] = (200, {'value': events})
        self.fake.routes[('GET', f'/users/{GUID}/onlineMeetings')] = (200, {'value': [{'id': 'SHARED1'}]})

    def test_prepare_links_the_hand_made_meeting_for_the_slot(self):
        room = self._seeded()
        other = self._seeded(title='FREP open session', scheduled_start=self.start + timedelta(days=1),
                             scheduled_end=self.start + timedelta(days=1, hours=1))
        self._calendar([_event('Metanoia | IAC Board Exam Prep - FREP', self.start)])
        plan = provisioning.prepare([room, other], organizer='admin@metanoia.co.za', apply=True)
        room.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((len(plan.linked), len(plan.created), len(plan.failed)), (1, 1, 0))
        self.assertEqual((room.teams_join_url, room.teams_meeting_id), (SHARED, 'SHARED1'))
        self.assertTrue(room.teams_meeting_external)
        self.assertEqual(other.teams_join_url, JOIN)
        self.assertEqual(len(self.fake.find('POST', '/onlineMeetings')), 1)

    def test_prepare_needs_the_programme_and_module_in_the_subject(self):
        room = self._seeded()
        events = [_event('MP PGDA P26S | FREP', self.start),
                  _event('Metanoia | IAC Board Exam Prep - TAX', self.start)]
        self.assertIsNone(provisioning.matching_event(room, events))

    def test_prepare_dry_run_changes_nothing(self):
        room = self._seeded()
        self._calendar([])
        plan = provisioning.prepare([room], organizer='admin@metanoia.co.za', apply=False)
        room.refresh_from_db()
        self.assertEqual(len(plan.created), 1)
        self.assertEqual(room.teams_join_url, '')
        self.assertFalse(self.fake.find('POST', '/onlineMeetings'))

    def test_open_session_covering_two_modules_links_both(self):
        frep = self._seeded(title='FREP open', scheduled_start=self.start,
                            scheduled_end=self.start + timedelta(hours=1))
        canonical = Module.objects.create(code='TXX', name='Tax')
        tax_module = ProgrammeModule.objects.create(programme=self.programme, module=canonical, code='TAX')
        tax = self._seeded(title='TAX open', module=tax_module,
                           scheduled_start=self.start + timedelta(hours=1),
                           scheduled_end=self.start + timedelta(hours=2))
        events = [_event('Metanoia | IAC Board Exam Prep - Open Session (FREP then TAX)',
                         self.start, minutes=120)]
        self.assertIsNotNone(provisioning.matching_event(frep, events))
        self.assertIsNotNone(provisioning.matching_event(tax, events))

    def test_rebuild_moves_a_shared_session_to_its_own_titled_meeting(self):
        room = self._seeded(title='FREP live session · Unit 2: Group statements',
                            teams_join_url=SHARED, teams_meeting_id='SHARED1',
                            organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        result = provisioning.rebuild([room], apply=True)
        room.refresh_from_db()
        self.assertEqual((len(result.retitled), len(result.new_meeting), len(result.failed)), (1, 1, 0))
        self.assertTrue(room.title.startswith('SAICAT IAC FREP · '), room.title)
        self.assertTrue(room.title.endswith(' | Live session · Unit 2: Group statements'), room.title)
        self.assertEqual(room.teams_join_url, JOIN)
        sent = self.fake.find('POST', '/onlineMeetings')[0]['json']
        self.assertEqual(sent['subject'], room.title)
        self.assertFalse([c for c in self.fake.calls if 'SHARED1' in c['path']])

    def test_rebuild_renames_an_own_meeting_in_teams(self):
        room = self._seeded(title='FREP open session', teams_join_url=JOIN, teams_meeting_id='MEET1',
                            organizer_upn='admin@metanoia.co.za')
        provisioning.rebuild([room], apply=True)
        room.refresh_from_db()
        patch = self.fake.find('PATCH', '/onlineMeetings/MEET1')[0]['json']
        self.assertEqual(patch['subject'], room.title)
        self.assertFalse(self.fake.find('POST', '/onlineMeetings'))

    def test_rebuild_dry_run_and_second_run_change_nothing(self):
        room = self._seeded(title='FREP live session · Unit 3')
        provisioning.rebuild([room], apply=False)
        room.refresh_from_db()
        self.assertEqual(room.title, 'FREP live session · Unit 3')
        provisioning.rebuild([room], apply=True)
        room.refresh_from_db()
        calls = len(self.fake.calls)
        result = provisioning.rebuild([room], apply=True)
        self.assertEqual((len(result.retitled), len(result.new_meeting)), (0, 0))
        self.assertEqual(len(self.fake.calls), calls)

    def test_a_past_class_is_linked_never_given_a_new_meeting(self):
        past = timezone.now() - timedelta(hours=20)
        linked = self._seeded(scheduled_start=past, scheduled_end=past + timedelta(hours=2))
        lonely = self._seeded(scheduled_start=past - timedelta(days=1),
                              scheduled_end=past - timedelta(days=1) + timedelta(hours=2))
        self._calendar([_event('Metanoia | IAC Board Exam Prep - FREP', past)])
        provisioning.prepare([linked, lonely], organizer='admin@metanoia.co.za', apply=True)
        linked.refresh_from_db()
        lonely.refresh_from_db()
        self.assertEqual(linked.teams_join_url, SHARED)
        self.assertEqual(lonely.teams_join_url, '')
        self.assertFalse(self.fake.find('POST', '/onlineMeetings'))

    def test_importer_skips_a_slot_already_linked_on_the_platform(self):
        past = timezone.now() - timedelta(days=1)
        self._seeded(scheduled_start=past, scheduled_end=past + timedelta(hours=2),
                     teams_join_url=SHARED, teams_meeting_id='SHARED1',
                     organizer_upn='admin@metanoia.co.za', teams_meeting_external=True)
        self._calendar([_event('Metanoia | IAC Board Exam Prep - FREP', past)])
        before = MeetingRoom.objects.count()
        report = calendar_import.import_sessions(days=3)
        self.assertEqual(report.skipped_linked, 1)
        self.assertEqual(MeetingRoom.objects.count(), before)


class QuickRecordingRunTests(TestCase):
    """The five-minutely run: recent classes only, backing off after failures."""

    @classmethod
    def setUpTestData(cls):
        """An admin host."""
        cls.host = make_user('coach2', 'admin', is_staff=True)

    def _artifact(self, ended_hours_ago, **kw):
        """A polling pipeline row for a class that ended ``ended_hours_ago`` hours ago."""
        end = timezone.now() - timedelta(hours=ended_hours_ago)
        room = MeetingRoom.objects.create(title='Class', host=self.host,
                                          provider=MeetingRoom.PROVIDER_TEAMS,
                                          scheduled_start=end - timedelta(hours=1), scheduled_end=end,
                                          teams_meeting_id='M', teams_join_url=JOIN,
                                          organizer_upn='admin@metanoia.co.za')
        return SessionArtifact.objects.create(meeting=room, state=SessionArtifact.STATE_POLLING, **kw)

    def test_only_recent_classes_that_are_due(self):
        fresh = self._artifact(1)
        old = self._artifact(72)
        backing_off = self._artifact(2, attempts=2, last_attempt_at=timezone.now() - timedelta(minutes=20))
        seen = []
        with mock.patch.object(pipeline, 'step', side_effect=lambda a: seen.append(a.pk) and False):
            pipeline.run(recent_only=True)
        self.assertEqual(seen, [fresh.pk])
        self.assertNotIn(old.pk, seen)
        self.assertNotIn(backing_off.pk, seen)

    def test_a_backed_off_class_is_retried_later(self):
        retry = self._artifact(5, attempts=1, last_attempt_at=timezone.now() - timedelta(minutes=31))
        seen = []
        with mock.patch.object(pipeline, 'step', side_effect=lambda a: seen.append(a.pk) and False):
            pipeline.run(recent_only=True)
        self.assertEqual(seen, [retry.pk])
