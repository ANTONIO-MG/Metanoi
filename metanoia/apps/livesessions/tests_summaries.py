"""Session summaries: Teams calendar import → summary → OneDrive + course + students.

Graph is mocked throughout — nothing here talks to Microsoft.
"""

import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Person
from apps.msteams.calendar_import import parse_subject

NOW = dt.datetime(2026, 10, 14, 10, 0, tzinfo=dt.timezone.utc)
CLASS_START = dt.datetime(2026, 10, 13, 16, 0, tzinfo=dt.timezone.utc)
CLASS_END = dt.datetime(2026, 10, 13, 18, 0, tzinfo=dt.timezone.utc)


def _event(eid, subject, start, end, *, cancelled=False):
    """Return a Graph calendar event dict for an online Teams meeting, as ``calendar_view`` yields it."""
    return {'id': eid, 'subject': subject, 'isCancelled': cancelled, 'isOnlineMeeting': True,
            'onlineMeeting': {'joinUrl': f'https://teams.microsoft.com/l/meetup-join/{eid}'},
            'start': {'dateTime': start.strftime('%Y-%m-%dT%H:%M:%S.0000000'), 'timeZone': 'UTC'},
            'end': {'dateTime': end.strftime('%Y-%m-%dT%H:%M:%S.0000000'), 'timeZone': 'UTC'}}


class SubjectTests(TestCase):
    """Tests for :func:`~apps.msteams.calendar_import.parse_subject`."""
    def test_group_classes_are_placed_and_one_on_ones_are_not(self):
        """Group classes parse to programme/intake/module/topic (B-intakes are BCTA); one-on-ones parse to ``None``."""
        p = parse_subject('MP PGDA P26F | TAX Debrief Session')
        self.assertEqual((p.programme, p.cohort, p.module, p.topic), ('PGDA', 'P26F', 'TAX', 'Debrief Session'))
        self.assertEqual(parse_subject(' MP PGDA B26S | CGAU').programme, 'BCTA')
        self.assertEqual(parse_subject('MP P26S Revision').module, '')
        self.assertEqual(parse_subject('PGDA Consult Exam Prep - P26F').cohort, 'P26F')
        for private in ('Tayisha Pillay | Individual Coaching', 'Gugu - MACF - Costing / Std Costing',
                        'Lona NMU | Individual Coaching', 'Accounting 1B | Zach'):
            self.assertIsNone(parse_subject(private), private)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class SummaryPipelineTests(TestCase):
    """End-to-end tests of the calendar import and the session-summary pipeline, Graph mocked."""
    def setUp(self):
        """Seed the academic spine and a PGDA TAXA set-up with three students.

        ``mine`` is in P26F and on TAXA, ``other_intake`` is in P26S and on TAXA, and ``other_module`` is in
        P26F but not on TAXA, so only ``mine`` belongs to the imported class.
        """
        from apps.learning.models import (Cohort, ModulePhase, ModuleWeek, ProgrammeEnrolment,
                                          ProgrammeModule)
        from apps.learning.models import Module
        from core.testing import enrol, make_programme
        cache.clear()
        self.pm = ProgrammeModule.objects.create(
            programme=make_programme('MILPARK', 'PGDA'), code='TAXA', price_per_month=500,
            module=Module.objects.get_or_create(code='TX', defaults={'name': 'Taxation'})[0])
        self.p26f, _ = Cohort.objects.get_or_create(programme=self.pm.programme, code='P26F')
        self.p26s, _ = Cohort.objects.get_or_create(programme=self.pm.programme, code='P26S')
        phase = ModulePhase.objects.create(programme_module=self.pm, cohort=self.p26f, kind='exam', sequence=1,
                                           starts_on=dt.date(2026, 9, 24), ends_on=dt.date(2026, 10, 22))
        self.week = ModuleWeek.objects.create(phase=phase, number=3, starts_on=dt.date(2026, 10, 12),
                                              ends_on=dt.date(2026, 10, 18))
        self.coach = self._user('coach@metanoia.example', 'admin')
        Person.objects.filter(user=self.coach).update(ms_upn='coach@metanoia.example')
        self.mine = self._user('p26f@example.com', 'student')
        self.other_intake = self._user('p26s@example.com', 'student')
        self.other_module = self._user('frep@example.com', 'student')
        for user, cohort in ((self.mine, self.p26f), (self.other_intake, self.p26s), (self.other_module, self.p26f)):
            ProgrammeEnrolment.objects.update_or_create(person=user.profile, programme=self.pm.programme,
                                                        defaults={'cohort': cohort})
        enrol(self.mine.profile, self.pm)
        enrol(self.other_intake.profile, self.pm)

    def _user(self, email, role):
        """Create a registered user with the given ``user_type``."""
        user = get_user_model().objects.create_user(username=email, email=email, password='x')
        Person.objects.filter(user=user).update(user_type=role, registered=True, profile_status=True)
        user.refresh_from_db()
        return user

    def _import(self):
        """Import a fixed set of Teams calendar events: one group class, a one-on-one, a future and a cancelled class."""
        from apps.msteams import calendar_import
        events = [
            _event('evt-class', 'MP PGDA P26F | TAX Debrief Session', CLASS_START, CLASS_END),
            _event('evt-1on1', 'Tayisha Pillay | Individual Coaching', CLASS_START, CLASS_END),
            _event('evt-future', 'MP PGDA P26F | TAX', NOW + dt.timedelta(days=1), NOW + dt.timedelta(days=1, hours=2)),
            _event('evt-gone', 'MP PGDA P26F | TAX', CLASS_START, CLASS_END, cancelled=True),
        ]
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.calendar_view', return_value=events), \
                mock.patch('apps.msteams.graph.find_online_meeting', return_value={'id': 'MEETING-1'}):
            return calendar_import.import_sessions(now=NOW)

    def test_import_places_the_group_class_only_and_is_idempotent(self):
        """Only the group class is placed (in its week, flagged for the pipeline to file and publish); a re-run creates nothing."""
        from apps.communication.models import MeetingRoom
        report = self._import()
        self.assertEqual((report.created, report.skipped_private, report.skipped_future, report.skipped_cancelled),
                         (1, 1, 1, 1))
        room = MeetingRoom.objects.get()
        self.assertEqual((room.cohort, room.module, room.week), (self.p26f, self.pm, self.week))
        self.assertEqual(room.session_kind, MeetingRoom.KIND_TEST_REVIEW)
        self.assertTrue(room.publish_recording)
        self.assertEqual(room.storage_path_parts, ['MILPARK', 'PGDA', 'TAXA', 'P26F', 'Exam 1', 'Week 3'])
        self.assertEqual(self._import().created, 0)
        self.assertEqual(MeetingRoom.objects.count(), 1)

    def test_recording_pipeline_picks_up_imported_classes(self):
        """The recording pipeline picks up imported classes.

        The class passes the "may this be published?" gate, but it is days old with nothing recorded, so the
        pipeline stops waiting for Teams and skips it.
        """
        from apps.livesessions import pipeline
        from apps.livesessions.models import SessionArtifact
        self._import()
        with mock.patch('apps.livesessions.pipeline.graph') as graph:
            graph.is_configured.return_value = True
            graph.list_recordings.return_value = []
            graph.list_transcripts.return_value = []
            pipeline.run()
            graph.list_recordings.assert_called()
        artifact = SessionArtifact.objects.get()
        self.assertEqual(artifact.state, SessionArtifact.STATE_SKIPPED)
        self.assertEqual(artifact.last_error, 'Teams published no recording or transcript.')

    def _process(self, *, insights=(403, []), transcripts=(403, []), detail=None, followup=False):
        """Run :func:`summaries.process` on the imported room with Graph, OneDrive, the AI follow-up and notifications mocked.

        Returns ``(status, ensure_path_mock, notify_mock)``.
        """
        from apps.communication.models import MeetingRoom
        from apps.livesessions import summaries
        room = MeetingRoom.objects.get()
        folder = {'drive_id': 'D', 'folder_id': 'F', 'path': 'Metanoia Sessions/x', 'web_url': '', 'owner_upn': 'c'}
        with mock.patch('apps.msteams.graph.ai_insights_with_status', return_value=insights), \
                mock.patch('apps.msteams.graph.get_ai_insight', return_value=detail), \
                mock.patch('apps.msteams.graph.transcripts_with_status', return_value=transcripts), \
                mock.patch('apps.msteams.onedrive.ensure_path', return_value=folder) as ensure, \
                mock.patch('apps.msteams.onedrive.put_text', return_value={'id': 'ITEM', 'webUrl': 'https://x/item'}), \
                mock.patch('apps.msteams.onedrive.anonymous_link', return_value='https://1drv.ms/anon'), \
                mock.patch('apps.livesessions.ai_followup.is_enabled', return_value=followup), \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            status = summaries.process(room, now=NOW)
        return status, ensure, notify

    INSIGHT = {'id': 'ins-1', 'createdDateTime': '2026-10-13T18:05:00Z',
               'meetingNotes': [{'title': 'Deferred tax on assessed losses', 'text': 'When a DTA is recognised.',
                                 'subpoints': [{'title': 'IAS 12.34', 'text': 'probable future taxable profit'}]}],
               'actionItems': [{'title': 'Redo Q2', 'text': 'Karoo Highlands', 'ownerDisplayName': 'Class'}]}

    def test_no_summary_yet_records_why(self):
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, _ensure, notify = self._process()
        self.assertEqual(status, 'waiting')
        note = SessionArtifact.objects.get().summary_note
        self.assertIn('transcript access is disabled', note)
        self.assertIn('OnlineMeetingAiInsight.Read.All', note)
        notify.assert_not_called()

    def test_teams_recap_is_filed_published_and_announced_to_the_intake_only(self):
        """A Teams recap is filed, published and announced to the intake only.

        The intake's student and the educator (who has action items too) are told — not the P26S student and
        not the off-module one. Checking again finds nothing new and tells nobody twice.
        """
        from apps.ai_assistant.models import AiReport
        from apps.learning.models import ModuleMaterial
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, ensure, notify = self._process(insights=(200, [self.INSIGHT]), detail=self.INSIGHT)
        self.assertEqual(status, 'published')
        ensure.assert_called_once_with(['MILPARK', 'PGDA', 'TAXA', 'P26F', 'Exam 1', 'Week 3', 'Session summaries'])
        artifact = SessionArtifact.objects.get()
        self.assertEqual((artifact.summary_source, artifact.summary_share_url),
                         ('teams_ai', 'https://1drv.ms/anon'))
        lesson = artifact.summary_lesson
        self.assertEqual(lesson.status, 'published')
        self.assertIn('IAS 12.34', ''.join(b.data.get('html', '') for b in lesson.blocks.all()))
        material = ModuleMaterial.objects.get(source_ref__startswith='session-summary/')
        self.assertEqual((material.week, material.is_published, material.lesson), (self.week, True, lesson))
        self.assertTrue(AiReport.objects.filter(linked_ref=f'meeting:{artifact.meeting_id}').exists())
        notified = {c.args[0] for c in notify.call_args_list}
        self.assertEqual(notified, {self.mine, artifact.meeting.host})
        self.assertEqual(artifact.meeting.host, self.coach)

        status, _e, notify = self._process(insights=(200, [self.INSIGHT]), detail=self.INSIGHT)
        self.assertEqual(status, 'unchanged')
        notify.assert_not_called()

    def test_with_the_ai_follow_up_on_the_summary_is_published_but_not_announced(self):
        """The class hears once — from the session pack — not from every job."""
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, _ensure, notify = self._process(insights=(200, [self.INSIGHT]), detail=self.INSIGHT,
                                                followup=True)
        self.assertEqual(status, 'published')
        self.assertTrue(SessionArtifact.objects.get().summary_published_at)
        notify.assert_not_called()

    def test_a_same_rank_rewrite_is_not_an_upgrade(self):
        """Claude words the same class differently each time; that must not
        rewrite (and re-bill) the published summary on every run."""
        self._import()
        vtt = 'WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Coach>Today we cover deferred tax on assessed losses.</v>\n'
        transcripts = (200, [{'id': 't1', 'createdDateTime': '2026-10-13T18:01:00Z'}])
        answers = iter([('{"overview": "first wording"}', {}), ('{"overview": "second wording"}', {})])
        with mock.patch('apps.msteams.graph.get_transcript_content', return_value=vtt), \
                mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.admin_ai.ask', side_effect=lambda *a: next(answers)):
            self.assertEqual(self._process(transcripts=transcripts)[0], 'published')
            self.assertEqual(self._process(transcripts=transcripts)[0], 'unchanged')

    def test_another_occurrence_of_a_recurring_class_is_ignored(self):
        self._import()
        last_week = dict(self.INSIGHT, id='ins-0', createdDateTime='2026-10-06T18:05:00Z')
        status, _e, _n = self._process(insights=(200, [last_week]))
        self.assertEqual(status, 'waiting')

    def test_a_better_summary_replaces_a_weaker_one_quietly(self):
        from apps.livesessions.models import SessionArtifact
        self._import()
        vtt = 'WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Coach>Today we cover deferred tax on assessed losses in detail.</v>\n'
        with mock.patch('apps.msteams.graph.get_transcript_content', return_value=vtt), \
                mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=False):
            status, _e, notify = self._process(
                transcripts=(200, [{'id': 't1', 'createdDateTime': '2026-10-13T18:01:00Z'}]))
        self.assertEqual(status, 'published')
        self.assertEqual(SessionArtifact.objects.get().summary_source, 'extractive')
        self.assertEqual({c.args[0] for c in notify.call_args_list}, {self.mine, self.coach})
        status, _e, notify = self._process(insights=(200, [self.INSIGHT]), detail=self.INSIGHT)
        self.assertEqual(status, 'upgraded')
        self.assertEqual(SessionArtifact.objects.get().summary_source, 'teams_ai')
        notify.assert_not_called()

    def test_daily_backfill_finds_an_old_class_and_says_it_is_now_available(self):
        from apps.communication.models import MeetingRoom
        from apps.livesessions import summaries
        old_start, old_end = CLASS_START - dt.timedelta(days=21), CLASS_END - dt.timedelta(days=21)
        insight = dict(self.INSIGHT, createdDateTime=(old_end + dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        folder = {'drive_id': 'D', 'folder_id': 'F', 'path': 'p', 'web_url': '', 'owner_upn': 'c'}
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.calendar_view',
                           return_value=[_event('evt-old', 'MP PGDA P26F | TAX', old_start, old_end)]), \
                mock.patch('apps.msteams.graph.find_online_meeting', return_value={'id': 'MEETING-OLD'}), \
                mock.patch('apps.msteams.graph.ai_insights_with_status', return_value=(200, [insight])), \
                mock.patch('apps.msteams.graph.get_ai_insight', return_value=insight), \
                mock.patch('apps.msteams.graph.transcripts_with_status', return_value=(403, [])), \
                mock.patch('apps.msteams.onedrive.ensure_path', return_value=folder), \
                mock.patch('apps.msteams.onedrive.put_text', return_value={'id': 'I', 'webUrl': 'u'}), \
                mock.patch('apps.msteams.onedrive.anonymous_link', return_value=''), \
                mock.patch('apps.livesessions.ai_followup.is_enabled', return_value=False), \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            self.assertEqual(summaries.run(now=NOW).split('·')[-1].strip(), 'summaries: nothing to check')
            result = summaries.run_backfill(now=NOW)
        self.assertIn('1 published', result)
        self.assertTrue(MeetingRoom.objects.filter(external_event_id='evt-old').exists())
        self.assertTrue(notify.call_args.kwargs['title'].startswith('Now available: summary of'))
