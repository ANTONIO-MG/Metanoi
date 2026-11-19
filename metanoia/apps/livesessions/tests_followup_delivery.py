"""After a class: the AI session pack, its one message, its tasks, and one-on-ones.

Graph, OneDrive and Claude are mocked throughout — nothing here talks to
Microsoft or spends tokens.

:mod:`.tests_summaries` is imported as a module, not its test class: importing the class here would
make the runner collect and re-run its tests.
"""

import datetime as dt
import json
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from . import tests_summaries as base
from .tests_summaries import CLASS_END, CLASS_START, NOW, _event

PACK = {
    'summary_markdown': 'We worked through deferred tax on assessed losses (IAS 12.34).',
    'next_agenda': ['Finish the Karoo Highlands question.', 'Start IAS 16 revaluations.'],
    'student_actions': [{'task': 'Redo question 2 under exam conditions', 'due_hint': 'before the next session',
                         'why': 'it is the part most of the class missed'}],
    'educator_actions': [{'task': 'Post the marked solution to Q2', 'due_hint': 'this week', 'why': ''}],
    'topics_covered': ['IAS 12'],
    'concerns': ['p26f@example.com seemed lost on DTA recognition'],
}
VTT = 'WEBVTT\n\n' + '\n\n'.join(
    f'00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\n<v Coach>Deferred tax point number {i} in some detail.</v>'
    for i in range(20))


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class SessionPackDeliveryTests(TestCase):
    """Tests for :func:`ai_followup.run`: the one message a class receives after the session pack is written.

    Reuses the fixtures of :class:`~.tests_summaries.SummaryPipelineTests`.
    """
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def _session(self, *, ended_ago=dt.timedelta(hours=2)):
        """Create a published module session for P26F that ended ``ended_ago``, with recording and transcript filed."""
        from apps.communication.models import MeetingRoom
        from apps.livesessions.models import SessionArtifact
        end = timezone.now() - ended_ago
        room = MeetingRoom.objects.create(
            title='MP PGDA P26F | TAX', host=self.coach, provider=MeetingRoom.PROVIDER_TEAMS,
            teams_meeting_id='MEETING-1', organizer_upn='coach@metanoia.example',
            scheduled_start=end - dt.timedelta(hours=2), scheduled_end=end,
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.pm, cohort=self.p26f,
            programme=self.pm.programme, publish_recording=True)
        artifact = SessionArtifact.objects.create(
            meeting=room, state=SessionArtifact.STATE_PUBLISHED,
            recording_item_id='REC', recording_share_url='https://1drv.ms/recording',
            transcript_item_id='TR', transcript_share_url='https://1drv.ms/transcript')
        return room, artifact

    def _run(self, *, transcript=VTT, answer=json.dumps(PACK)):
        """Run the AI follow-up with Claude, Graph, OneDrive and notifications mocked.

        Returns ``(result, notify_mock, ask_mock)``.
        """
        from apps.livesessions import ai_followup
        folder = {'drive_id': 'D', 'folder_id': 'F', 'path': 'p', 'web_url': '', 'owner_upn': 'c'}
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.admin_ai.ask', return_value=(answer, {})) as ask, \
                mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.livesessions.pipeline.transcript_text', return_value=transcript), \
                mock.patch('apps.msteams.onedrive.ensure_path', return_value=folder), \
                mock.patch('apps.msteams.onedrive.put_text', return_value={'id': 'S', 'webUrl': 'u'}), \
                mock.patch('apps.msteams.onedrive.anonymous_link', return_value='https://1drv.ms/summary'), \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            result = ai_followup.run()
        return result, notify, ask

    def test_the_class_gets_one_message_with_summary_agenda_actions_and_links(self):
        """The class gets one message with summary, agenda, actions and links.

        Only the intake's student and the coach are told (not the other intake, not off-module). The
        educator's actions are in the summary too, as the client asked for both. The body never carries a
        OneDrive link, staff-only material (concerns, addresses) stays out of what students receive, no
        per-row notices are created, and a second run sends nothing.
        """
        from apps.communication.models import AutoNotice
        from apps.livesessions.models import SessionArtifact
        room, _artifact = self._session()
        result, notify, _ask = self._run()
        self.assertIn('1 written', result)

        told = {c.args[0] for c in notify.call_args_list}
        self.assertEqual(told, {self.mine, self.coach})
        call = notify.call_args_list[0].kwargs
        self.assertTrue(call['email'])
        body = call['body']
        for expected in ('OVERVIEW', 'IAS 12.34', 'AGENDA FOR THE NEXT SESSION', 'Start IAS 16 revaluations.',
                         'ACTION ITEMS — STUDENTS', 'Should do: Redo question 2 under exam conditions',
                         'ACTION ITEMS — EDUCATOR', 'Will do: Post the marked solution',
                         f'/calendar/sessions/{room.pk}/#recording',
                         f'/calendar/sessions/{room.pk}/transcript/'):
            self.assertIn(expected, body)
        self.assertNotIn('1drv.ms', body)
        for private in ('seemed lost', 'p26f@example.com'):
            self.assertNotIn(private, body)

        artifact = SessionArtifact.objects.get(meeting=room)
        self.assertTrue(artifact.ai_followup_done and artifact.followup_sent_at)
        self.assertEqual(artifact.summary_source, 'claude')
        lesson_text = ''.join(b.data.get('html', '') for b in artifact.summary_lesson.blocks.all())
        self.assertIn('Redo question 2', lesson_text)
        self.assertNotIn('seemed lost', lesson_text)
        self.assertFalse(AutoNotice.objects.exists())

        _result, notify, ask = self._run()
        notify.assert_not_called()
        ask.assert_not_called()

    def test_an_old_class_is_filed_but_nobody_is_told(self):
        from apps.ai_assistant.models import AiReport
        room, _artifact = self._session(ended_ago=dt.timedelta(days=10))
        _result, notify, _ask = self._run()
        notify.assert_not_called()
        self.assertTrue(AiReport.objects.filter(linked_ref=f'session-followup:{room.pk}').exists())

    def test_no_transcript_still_tells_the_class_the_recording_is_up(self):
        self._session()
        result, notify, ask = self._run(transcript='')
        self.assertIn('no-material', result)
        ask.assert_not_called()
        self.assertEqual({c.args[0] for c in notify.call_args_list}, {self.mine, self.coach})
        self.assertIn('/#recording', notify.call_args.kwargs['body'])

    def test_claude_nonsense_is_retried_then_falls_back(self):
        from apps.livesessions.models import SessionArtifact
        room, _artifact = self._session()
        for _ in range(2):
            result, notify, _ask = self._run(answer='not json')
            self.assertIn('retry', result)
            notify.assert_not_called()
        result, notify, _ask = self._run(answer='not json')
        self.assertIn('fallback', result)
        self.assertTrue(SessionArtifact.objects.get(meeting=room).ai_followup_done)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class OneOnOneImportTests(TestCase):
    """Tests for importing one-on-one coaching events from the Teams calendar."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def _import(self, attendees):
        """Import a single one-on-one event with the given attendee addresses."""
        from apps.msteams import calendar_import
        event = _event('evt-1on1', 'Tayisha Pillay | Individual Coaching', CLASS_START, CLASS_END)
        event['attendees'] = [{'emailAddress': {'address': a}} for a in attendees]
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.calendar_view', return_value=[event]), \
                mock.patch('apps.msteams.graph.find_online_meeting', return_value={'id': 'MEETING-2'}):
            return calendar_import.import_sessions(now=NOW)

    def test_a_one_on_one_with_a_registered_student_reaches_only_that_student(self):
        """A one-on-one with a registered student reaches only that student.

        The coach's own student-type login, listed in ``coach_emails``, is not treated as the student.
        """
        from apps.communication.models import MeetingRoom
        from apps.livesessions.audience import class_students
        from apps.livesessions.models import LiveSessionSettings
        coach_gmail = self._user('coach.personal@gmail.com', 'student')
        conf = LiveSessionSettings.load()
        conf.coach_emails = 'Coach.Personal@gmail.com'
        conf.save()
        report = self._import(['coach.personal@gmail.com', 'P26F@example.com', 'stranger@example.com'])
        self.assertEqual((report.created, report.one_on_one), (1, 1))
        room = MeetingRoom.objects.get()
        self.assertEqual(room.audience, MeetingRoom.AUDIENCE_PRIVATE)
        self.assertIsNone(room.module)
        self.assertTrue(room.publish_recording)
        self.assertTrue(room.is_one_on_one)
        self.assertEqual(list(class_students(room)), [self.mine])
        self.assertNotIn(coach_gmail, class_students(room))

    def test_a_one_on_one_whose_student_has_no_account_is_left_out(self):
        from apps.communication.models import MeetingRoom
        report = self._import(['coach@metanoia.example', 'stranger@example.com'])
        self.assertEqual(report.skipped_private, 1)
        self.assertFalse(MeetingRoom.objects.exists())


class AfterClassScheduleTests(TestCase):
    """The after-class jobs run on their own twice-daily timer, and only there."""

    AFTER_CLASS = ['session-summaries', 'session-summaries-backfill', 'sync-teams-meetings',
                   'session-pipeline', 'session-ai-followup']

    def _names(self, timer):
        """Return the names of the jobs a scheduler tick on ``timer`` would run."""
        from apps.scheduler import runner
        with mock.patch('apps.scheduler.runner.run_job', return_value=(True, True, '')):
            return [name for name, *_rest in runner.tick(timer=timer)]

    def test_the_hourly_tick_never_runs_the_after_class_jobs(self):
        main = self._names('main')
        self.assertTrue(main)
        self.assertFalse(set(main) & set(self.AFTER_CLASS))

    def test_the_sessions_tick_runs_them_in_order_import_then_file_then_ai(self):
        self.assertEqual(self._names('sessions'), self.AFTER_CLASS)

    def test_a_job_can_still_be_run_by_name(self):
        from apps.scheduler import runner
        with mock.patch('apps.scheduler.runner.run_job', return_value=(True, True, '')):
            self.assertEqual([r[0] for r in runner.tick(only=['session-pipeline'])], ['session-pipeline'])


class RecordingCopyTests(TestCase):
    """Recordings are copied, so Teams keeps its own; the copy is waited for."""

    FOLDER = {'drive_id': 'D', 'folder_id': 'F', 'path': 'p', 'web_url': '', 'owner_upn': 'c'}
    RECORDING = {'driveItem': {'id': 'TEAMS-ITEM', 'parentReference': {'driveId': 'D'}}}

    def _meeting(self):
        """Return an unsaved Teams meeting for the P26F TAX class."""
        from apps.communication.models import MeetingRoom
        return MeetingRoom(title='MP PGDA P26F | TAX', scheduled_start=CLASS_START)

    def test_the_recording_is_copied_not_moved_by_default(self):
        from apps.msteams import onedrive
        children = iter([[], [], [{'name': '2026-10-13 MP PGDA P26F TAX.mp4', 'id': 'COPY'}]])
        with mock.patch('apps.msteams.graph.list_children', side_effect=lambda *a: next(children)), \
                mock.patch('apps.msteams.onedrive.find_child', wraps=onedrive.find_child), \
                mock.patch('apps.msteams.onedrive.artifact_stem', return_value='2026-10-13 MP PGDA P26F TAX'), \
                mock.patch('apps.msteams.graph.move_item') as move, \
                mock.patch('apps.msteams.graph.copy_item', return_value=True) as copy, \
                mock.patch('apps.msteams.onedrive.COPY_POLL_SECONDS', 0):
            item = onedrive.file_recording(self._meeting(), self.FOLDER, self.RECORDING, wait=5)
        move.assert_not_called()
        copy.assert_called_once()
        self.assertEqual(item['id'], 'COPY')

    def test_a_copy_already_in_the_folder_is_not_started_again(self):
        from apps.msteams import onedrive
        with mock.patch('apps.msteams.onedrive.find_child', return_value={'id': 'COPY'}), \
                mock.patch('apps.msteams.graph.copy_item') as copy:
            self.assertEqual(onedrive.file_recording(self._meeting(), self.FOLDER, self.RECORDING)['id'], 'COPY')
        copy.assert_not_called()

    def test_a_copy_in_flight_keeps_the_session_in_filing(self):
        from apps.communication.models import MeetingRoom
        from apps.livesessions import pipeline
        from apps.livesessions.models import SessionArtifact
        room = MeetingRoom.objects.create(title='Class', provider=MeetingRoom.PROVIDER_TEAMS,
                                          teams_meeting_id='M', organizer_upn='o@x',
                                          scheduled_start=CLASS_START, scheduled_end=CLASS_END)
        artifact = SessionArtifact.objects.create(meeting=room, state=SessionArtifact.STATE_FILING)
        rec = {'id': 'r1', 'createdDateTime': '2026-10-13T18:02:00Z'}
        with mock.patch('apps.msteams.onedrive.ensure_session_folder', return_value=self.FOLDER), \
                mock.patch('apps.livesessions.pipeline.session_recordings', return_value=[rec]), \
                mock.patch('apps.livesessions.pipeline.transcript_text', return_value=''), \
                mock.patch('apps.livesessions.pipeline._ensure_thumbnail'), \
                mock.patch('apps.msteams.onedrive.file_recording', return_value=None):
            self.assertFalse(pipeline.step(artifact))
        artifact.refresh_from_db()
        self.assertEqual(artifact.state, SessionArtifact.STATE_FILING)
        self.assertEqual(artifact.folder_id, 'F')
        self.assertIn('copying', artifact.last_error)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class ClassAudienceTests(TestCase):
    """Tests for :func:`~.audience.class_students` on a module class."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def test_a_class_reaches_its_intake_and_unplaced_students_not_other_intakes(self):
        """A class reaches its intake and unplaced students — not P26S, not off-module students."""
        from apps.communication.models import MeetingRoom
        from apps.learning.models import ProgrammeEnrolment
        from apps.livesessions.audience import class_students
        from core.testing import enrol
        unplaced = self._user('unplaced@example.com', 'student')
        ProgrammeEnrolment.objects.update_or_create(person=unplaced.profile, programme=self.pm.programme,
                                                    defaults={'cohort': None})
        enrol(unplaced.profile, self.pm)
        room = MeetingRoom.objects.create(title='MP PGDA P26F | TAX', host=self.coach, module=self.pm,
                                          cohort=self.p26f, audience=MeetingRoom.AUDIENCE_MODULE)
        self.assertEqual(set(class_students(room)), {self.mine, unplaced})


class RecordingLocatorTests(TestCase):
    """Graph's callRecording has no drive reference; the file is found by name."""

    def test_the_teams_file_is_matched_by_title_and_time(self):
        from apps.communication.models import MeetingRoom
        from apps.msteams import onedrive
        room = MeetingRoom(title='MP PGDA B26S | FREP', organizer_upn='o@x', scheduled_start=CLASS_START)
        files = [
            {'id': 'LAST-WEEK', 'name': 'MP PGDA B26S  FREP-20261014_180551-Meeting Recording.mp4'},
            {'id': 'OTHER', 'name': 'MP PGDA P26S  FREP-20261021_180600-Meeting Recording.mp4'},
            {'id': 'THIS', 'name': 'MP PGDA B26S  FREP-20261021_180551-Meeting Recording.mp4'},
        ]
        with mock.patch('apps.msteams.graph.get_drive_id', return_value='D'), \
                mock.patch('apps.msteams.graph.get_item_by_path', return_value={'id': 'REC'}), \
                mock.patch('apps.msteams.graph.list_children', return_value=files):
            found = onedrive.find_teams_recording(room, {'createdDateTime': '2026-10-21T16:06:11.0081384Z'})
            missing = onedrive.find_teams_recording(room, {'createdDateTime': '2026-10-28T16:06:11Z'})
        self.assertEqual((found['id'], found['drive_id']), ('THIS', 'D'))
        self.assertIsNone(missing)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class SessionVideoTests(TestCase):
    """Past sessions play in the platform's own player — never via a Graph URL."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def _session(self, **artifact):
        """Create a P26F module session with a Graph recording URL and an artifact built from ``artifact`` fields."""
        from apps.communication.models import MeetingRoom
        from apps.livesessions.models import SessionArtifact
        room = MeetingRoom.objects.create(
            title='MP PGDA P26F | TAX', host=self.coach, provider=MeetingRoom.PROVIDER_TEAMS,
            teams_meeting_id='M', organizer_upn='coach@metanoia.example', module=self.pm, cohort=self.p26f,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=CLASS_START, scheduled_end=CLASS_END,
            recording_url='https://graph.microsoft.com/v1.0/users/x/onlineMeetings/y/recordings/z/content')
        SessionArtifact.objects.create(meeting=room, state=SessionArtifact.STATE_WAITING, **artifact)
        return room

    def test_the_player_link_redirects_to_a_short_lived_download_url(self):
        room = self._session(teams_item_id='ITEM', teams_drive_id='DRIVE')
        self.client.force_login(self.mine)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_url', return_value='https://tenant.sharepoint.com/dl?t=1') as dl:
            response = self.client.get(f'/calendar/sessions/{room.pk}/video/', secure=True)
        self.assertEqual((response.status_code, response['Location']), (302, 'https://tenant.sharepoint.com/dl?t=1'))
        dl.assert_called_once_with('DRIVE', 'ITEM')

    def test_someone_outside_the_class_cannot_fetch_it(self):
        room = self._session(teams_item_id='ITEM', teams_drive_id='DRIVE')
        self.client.force_login(self.other_module)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True):
            response = self.client.get(f'/calendar/sessions/{room.pk}/video/', secure=True)
        self.assertEqual(response.status_code, 404)

    def test_links_never_point_at_the_graph_api(self):
        room = self._session(teams_item_id='ITEM', teams_drive_id='DRIVE')
        artifact = room.artifact
        self.assertTrue(artifact.is_watchable)
        self.assertNotIn('graph.microsoft.com', artifact.watch_url)
        self.assertEqual(artifact.video_url, f'/calendar/sessions/{room.pk}/video/')

    def test_without_a_recording_file_it_is_not_watchable(self):
        self.assertFalse(self._session().artifact.is_watchable)

    def test_the_module_live_tab_lists_the_recording_for_that_intake(self):
        """The module's live tab lists the recording for that intake and for unplaced students, not for another intake."""
        from apps.learning.module_feed import _session_videos
        room = self._session(teams_item_id='ITEM', teams_drive_id='DRIVE')
        student_gate = mock.Mock(can_author=False, one_on_one=False)
        self.assertEqual(_session_videos(self.pm, student_gate, self.p26f), [room])
        self.assertEqual(_session_videos(self.pm, student_gate, self.p26s), [])
        self.assertEqual(_session_videos(self.pm, student_gate, None), [room])

    def test_the_thumbnail_is_drawn_in_the_brand_with_its_details(self):
        """The thumbnail renders as a JPEG in the brand, with module, programme and intake as its kicker."""
        from apps.livesessions import thumbnails
        room = self._session()
        blob = thumbnails.render(room, topics=['IAS 12', 'Deferred tax'])
        self.assertTrue(blob and blob[:2] == b'\xff\xd8')
        self.assertEqual(thumbnails._kicker_parts(room), ['TAXA', 'PGDA', 'P26F'])


class RecordingNameVariantsTests(TestCase):
    """Series-named files, UTC stamps and second parts all match their session."""

    def _find(self, title, files, made):
        """Look up the Teams recording for a session titled ``title`` among ``files``, created at ``made``."""
        from apps.communication.models import MeetingRoom
        from apps.msteams import onedrive
        room = MeetingRoom(title=title, organizer_upn='o@x', scheduled_start=CLASS_START)
        with mock.patch('apps.msteams.graph.get_drive_id', return_value='D'), \
                mock.patch('apps.msteams.graph.get_item_by_path', return_value={'id': 'REC'}), \
                mock.patch('apps.msteams.graph.list_children', return_value=[{'id': n, 'name': n} for n in files]):
            return onedrive.find_teams_recording(room, {'createdDateTime': made})

    def test_a_renamed_occurrence_matches_the_series_file(self):
        found = self._find('MP PGDA P26F | MACF Debrief session',
                           ['MP PGDA P26F  MACF-20261013_193902-Meeting Recording.mp4'], '2026-10-13T17:39:31Z')
        self.assertEqual(found['id'], 'MP PGDA P26F  MACF-20261013_193902-Meeting Recording.mp4')

    def test_a_utc_stamp_and_a_numbered_part_match(self):
        self.assertIsNotNone(self._find('MP PGDA B26S | TAX', ['MP PGDA B26S  TAX-20260919_101445UTC-Meeting Recording.mp4'],
                                        '2026-09-19T10:16:02Z'))
        self.assertIsNotNone(self._find('MP PGDA P26S | FREP', ['MP PGDA P26S  FREP-20260919_101156-Meeting Recording 1.mp4'],
                                        '2026-09-19T08:12:00Z'))

    def test_a_renamed_session_takes_the_one_recording_in_its_window(self):
        found = self._find('MP PGDA B26S | MACF, FREP & TAX Planning sessions',
                           ['MP PGDA B26S  TAX-20261017_121514-Meeting Recording.mp4',
                            'MP PGDA B26S  TAX-20261017_123841-Meeting Recording.mp4'], '2026-10-17T10:15:34Z')
        self.assertEqual(found['id'], 'MP PGDA B26S  TAX-20261017_121514-Meeting Recording.mp4')

    def test_with_two_untitled_candidates_in_the_window_it_does_not_guess(self):
        self.assertIsNone(self._find('MP PGDA P26F | MACF',
                                     ['MP PGDA P26F  CGAU-20261013_193902-Meeting Recording.mp4',
                                      'MP PGDA P26S  CGAU-20261013_194000-Meeting Recording.mp4'],
                                     '2026-10-13T17:39:31Z'))
