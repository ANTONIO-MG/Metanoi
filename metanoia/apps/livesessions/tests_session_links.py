"""Class links stay on our domain: recording, transcript, summary — and no AI errors.

Students used to be handed SharePoint links (a transcript's lands on a Microsoft
sign-in page) and, while the Anthropic account was out of credit, a "summary"
that was the raw API error. Graph and Claude are mocked throughout.

:mod:`.tests_summaries` is imported as a module, not its test class: importing the class here would
make the runner collect and re-run its tests.
"""

import datetime as dt
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import tests_summaries as base

VTT = """WEBVTT

0f1c-1
00:00:03.120 --> 00:00:06.000
<v Coach Smith>Welcome back, everyone.</v>

0f1c-2
00:00:06.000 --> 00:00:09.500
<v Coach Smith>Today is deferred tax.</v>

0f1c-3
00:01:02.000 --> 00:01:05.000
<v Thabo M>Does IAS 12 apply to assessed losses?</v>
"""
SHARE_TRANSCRIPT = 'https://tenant-my.sharepoint.com/:u:/g/personal/x/TRANSCRIPT'
SHARE_RECORDING = 'https://tenant-my.sharepoint.com/:v:/g/personal/x/RECORDING'
SHARE_SUMMARY = 'https://tenant-my.sharepoint.com/:t:/g/personal/x/SUMMARY'
AI_ERROR = ("The admin AI hit an error: Error code: 400 - {'type': 'error', 'error': {'message': "
            "'Your credit balance is too low to access the Anthropic API.'}}")


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example',
                   SITE_URL='https://learn.example.org')
class SessionLinkTests(TestCase):
    """Tests that every class link a student meets stays on our domain.

    The tests run in four groups: the recording (students watch it on the session page and do not
    download it — the owner's rule of 2026-11-09; downloading is for the team only), the transcript
    (rendered as a page on our domain), the summary (the lesson or recap, never an AI error) and what
    the class is sent (absolute, in-domain links). Reuses the fixtures of
    :class:`~.tests_summaries.SummaryPipelineTests`.
    """
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def _session(self, **artifact):
        """Create a published P26F module session whose artifact carries SharePoint links, overridden by ``artifact``."""
        from apps.communication.models import MeetingRoom
        from apps.livesessions.models import SessionArtifact
        end = timezone.now() - dt.timedelta(hours=2)
        room = MeetingRoom.objects.create(
            title='MP PGDA P26F | TAX', host=self.coach, provider=MeetingRoom.PROVIDER_TEAMS,
            teams_meeting_id='M', organizer_upn='coach@metanoia.example', module=self.pm, cohort=self.p26f,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=end - dt.timedelta(hours=2),
            scheduled_end=end)
        defaults = dict(state=SessionArtifact.STATE_PUBLISHED, teams_item_id='ITEM', teams_drive_id='DRIVE',
                        recording_share_url=SHARE_RECORDING, transcript_item_id='TR',
                        recording_drive_id='DRIVE', transcript_share_url=SHARE_TRANSCRIPT,
                        transcript_web_url='https://tenant-my.sharepoint.com/personal/x/t.vtt')
        defaults.update(artifact)
        SessionArtifact.objects.create(meeting=room, **defaults)
        return room

    def test_opening_the_video_url_in_a_tab_goes_to_the_player(self):
        room = self._session()
        self.client.force_login(self.mine)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_url') as dl:
            response = self.client.get(f'/calendar/sessions/{room.pk}/video/', secure=True,
                                       HTTP_SEC_FETCH_DEST='document', HTTP_ACCEPT='text/html')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], f'/calendar/sessions/{room.pk}/#recording')
        dl.assert_not_called()

    def test_the_player_and_the_download_button_still_get_the_file(self):
        """The player, an old browser's range request and a staff download (downloading is for the team only) all get the file."""
        room = self._session()
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_url', return_value='https://tenant.sharepoint.com/dl?t=1'):
            self.client.force_login(self.mine)
            player = self.client.get(f'/calendar/sessions/{room.pk}/video/', secure=True,
                                     HTTP_SEC_FETCH_DEST='video', HTTP_RANGE='bytes=0-')
            old_browser = self.client.get(f'/calendar/sessions/{room.pk}/video/', secure=True,
                                          HTTP_RANGE='bytes=0-', HTTP_ACCEPT='*/*')
            self.client.force_login(self.coach)
            download = self.client.get(f'/calendar/sessions/{room.pk}/video/?download=1', secure=True,
                                       HTTP_SEC_FETCH_DEST='document', HTTP_ACCEPT='text/html')
        for response in (player, download, old_browser):
            self.assertEqual((response.status_code, response['Location']),
                             (302, 'https://tenant.sharepoint.com/dl?t=1'))

    def test_recording_links_are_the_session_page_even_for_youtube(self):
        room = self._session(youtube_video_id='abc123')
        self.assertEqual(room.artifact.watch_url, f'/calendar/sessions/{room.pk}/#recording')

    def test_a_student_cannot_download_the_recording(self):
        """Students watch, they do not download (the owner's rule of 2026-11-09)."""
        room = self._session()
        self.client.force_login(self.mine)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_url', return_value='https://tenant.sharepoint.com/dl') as dl:
            response = self.client.get(f'/calendar/sessions/{room.pk}/video/?download=1', secure=True,
                                       HTTP_SEC_FETCH_DEST='document', HTTP_ACCEPT='text/html')
            fetched = self.client.get(f'/calendar/sessions/{room.pk}/video/?download=1', secure=True,
                                      HTTP_RANGE='bytes=0-', HTTP_ACCEPT='*/*')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(fetched.status_code, 403)
        dl.assert_not_called()

    def test_parent_cannot_download_either(self):
        from apps.livesessions import audience
        parent = self._user('parent@example.com', 'parent')
        self.assertFalse(audience.can_download_recording(parent))
        self.assertFalse(audience.can_download_recording(self.mine))
        self.assertTrue(audience.can_download_recording(self.coach))
        educator = self._user('edu@example.com', 'educator')
        self.assertTrue(audience.can_download_recording(educator))

    def test_student_session_page_has_no_download_or_raw_links(self):
        """The student page plays the video in place, with no download control and no raw YouTube or OneDrive address."""
        room = self._session(youtube_video_id='abcdefghijk')
        self.client.force_login(self.mine)
        page = self.client.get(f'/calendar/sessions/{room.pk}/', secure=True).content.decode()
        self.assertIn('id="sessionVideo"', page)
        self.assertIn('controlslist="nodownload"', page)
        self.assertNotIn('?download=1', page)
        recording = page[page.index('id="recording"'):]
        recording = recording[:recording.index('</div></div>')]
        self.assertNotIn('bi-download', recording)
        self.assertNotIn('download', recording.replace('nodownload', ''))
        for raw in ('youtube.com', 'youtu.be', 'abcdefghijk', 'sharepoint.com', 'onedrive'):
            self.assertNotIn(raw, page.lower())

    def test_staff_keep_the_download_and_the_youtube_link(self):
        room = self._session(youtube_video_id='abcdefghijk')
        self.client.force_login(self.coach)
        page = self.client.get(f'/calendar/sessions/{room.pk}/', secure=True).content.decode()
        self.assertIn(f'/calendar/sessions/{room.pk}/video/?download=1', page)
        self.assertIn('https://www.youtube.com/watch?v=abcdefghijk', page)
        self.assertNotIn('controlslist="nodownload"', page)

    def test_youtube_library_plays_inline_without_the_youtube_address(self):
        from apps.livesessions.models import YouTubePlaylist, YouTubeVideo
        playlist = YouTubePlaylist.objects.create(youtube_id='PL1', programme_module=self.pm)
        YouTubeVideo.objects.create(youtube_id='vidvidvid01', playlist=playlist, title='Debrief',
                                    published_at=timezone.now())
        self.client.force_login(self.mine)
        page = self.client.get('/calendar/sessions/past/', secure=True).content.decode()
        self.assertIn('youtube-nocookie.com/embed/vidvidvid01', page)
        for raw in ('youtube.com/watch', 'youtu.be', 'youtube.com/playlist', 'Open on YouTube'):
            self.assertNotIn(raw, page)

    def test_a_youtube_material_plays_on_the_platform_for_students(self):
        from apps.learning.models import ModuleMaterial
        material = ModuleMaterial.objects.create(
            phase=self.week.phase, week=self.week, kind=ModuleMaterial.KIND_RECORDING,
            title='Class recording', url='https://www.youtube.com/watch?v=vidvidvid02', is_preview=True)
        self.client.force_login(self.mine)
        response = self.client.get(f'/learning/materials/{material.pk}/open/', secure=True)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('youtube-nocookie.com/embed/vidvidvid02', page)
        self.assertNotIn('youtube.com/watch', page)
        self.client.force_login(self.coach)
        response = self.client.get(f'/learning/materials/{material.pk}/open/', secure=True)
        self.assertEqual((response.status_code, response['Location']),
                         (302, 'https://www.youtube.com/watch?v=vidvidvid02'))

    def test_a_recording_material_of_a_session_opens_its_player(self):
        from apps.learning.models import ModuleMaterial
        room = self._session()
        material = ModuleMaterial.objects.create(
            phase=self.week.phase, week=self.week, kind=ModuleMaterial.KIND_RECORDING,
            title='Class recording', meeting=room, is_preview=True)
        self.client.force_login(self.mine)
        response = self.client.get(f'/learning/materials/{material.pk}/open/', secure=True)
        self.assertEqual(response['Location'], f'/calendar/sessions/{room.pk}/#recording')

    def test_the_transcript_reads_as_a_page_on_our_domain(self):
        """The transcript reads as a page on our domain.

        One speaker's consecutive cues are merged, cue ids are not shown as content, and the staff OneDrive
        link is for managers only.
        """
        room = self._session()
        self.client.force_login(self.mine)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_item', return_value=VTT.encode()) as fetch:
            response = self.client.get(f'/calendar/sessions/{room.pk}/transcript/', secure=True)
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with('DRIVE', 'TR')
        page = response.content.decode()
        self.assertIn('Welcome back, everyone. Today is deferred tax.', page)
        self.assertIn('Thabo M', page)
        self.assertIn('1:02', page)
        self.assertNotIn('sharepoint.com', page)
        self.assertNotIn('0f1c-1', page)

    def test_staff_also_see_the_onedrive_copy(self):
        room = self._session()
        self.client.force_login(self.coach)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_item', return_value=VTT.encode()):
            response = self.client.get(f'/calendar/sessions/{room.pk}/transcript/', secure=True)
        self.assertContains(response, 'Open the file in OneDrive')

    def test_someone_outside_the_class_cannot_read_the_transcript(self):
        room = self._session()
        self.client.force_login(self.other_module)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_item', return_value=VTT.encode()) as fetch:
            response = self.client.get(f'/calendar/sessions/{room.pk}/transcript/', secure=True)
        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_a_missing_transcript_is_a_friendly_page_not_json(self):
        room = self._session()
        self.client.force_login(self.mine)
        with mock.patch('apps.msteams.graph.is_configured', return_value=False):
            unconfigured = self.client.get(f'/calendar/sessions/{room.pk}/transcript/', secure=True)
        with mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.msteams.graph.download_item', return_value=None), \
                mock.patch('apps.livesessions.pipeline.transcript_text', return_value=''):
            missing = self.client.get(f'/calendar/sessions/{room.pk}/transcript/', secure=True)
        for response in (unconfigured, missing):
            self.assertEqual(response.status_code, 404)
            self.assertIn('text/html', response['Content-Type'])
            self.assertContains(response, 'Back to the session page', status_code=404)

    def test_the_summary_link_opens_the_summary_lesson(self):
        from apps.learning.models import Lesson
        room = self._session()
        lesson = Lesson.objects.create(title='Session summary', slug='s-1', module=self.pm)
        room.artifact.summary_lesson = lesson
        room.artifact.save()
        self.client.force_login(self.mine)
        response = self.client.get(f'/calendar/sessions/{room.pk}/summary/', secure=True)
        self.assertEqual((response.status_code, response['Location']), (302, f'/learning/lessons/{lesson.pk}/view/'))

    def test_without_a_lesson_the_recap_is_shown_but_never_an_ai_error(self):
        from apps.ai_assistant.models import AiReport
        room = self._session(summary_item_id='S')
        report = AiReport.objects.create(linked_ref=f'meeting:{room.pk}', kind=AiReport.KIND_MEETING_RECAP,
                                         title='Recap', content='# T\n\n## Key points\n\nDeferred tax on losses.')
        self.client.force_login(self.mine)
        response = self.client.get(f'/calendar/sessions/{room.pk}/summary/', secure=True)
        self.assertContains(response, 'Deferred tax on losses.')

        report.content = AI_ERROR
        report.save()
        response = self.client.get(f'/calendar/sessions/{room.pk}/summary/', secure=True)
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, 'credit balance', status_code=404)
        page = self.client.get(f'/calendar/sessions/{room.pk}/', secure=True)
        self.assertNotContains(page, 'credit balance')

    def test_the_session_page_gives_students_no_sharepoint_links(self):
        room = self._session(summary_share_url=SHARE_SUMMARY, summary_item_id='S')
        self.client.force_login(self.mine)
        page = self.client.get(f'/calendar/sessions/{room.pk}/', secure=True).content.decode()
        self.assertNotIn('sharepoint.com', page)
        self.assertIn(f'/calendar/sessions/{room.pk}/transcript/', page)
        self.assertIn(f'/calendar/sessions/{room.pk}/summary/', page)

    def test_session_links_are_absolute_and_in_domain(self):
        from apps.livesessions import ai_followup
        room = self._session()
        links = dict(ai_followup._links(room.artifact))
        self.assertEqual(links, {
            'Watch the recording': f'https://learn.example.org/calendar/sessions/{room.pk}/#recording',
            'Read the transcript': f'https://learn.example.org/calendar/sessions/{room.pk}/transcript/',
        })

    def test_an_ai_failure_never_reaches_the_class(self):
        """An AI failure never reaches the class.

        A recap filed while the AI was failing (before the fix) must not leak either: the fallback message
        carries the links and a neutral line, not the error, while ``last_error`` keeps the reason for staff.
        """
        from apps.ai_assistant.models import AiReport
        from apps.livesessions import ai_followup
        room = self._session()
        artifact = room.artifact
        AiReport.objects.create(linked_ref=f'meeting:{room.pk}', kind=AiReport.KIND_MEETING_RECAP,
                                title='Recap', content=f'# {room.title} — class recap\n\n{AI_ERROR}')
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.admin_ai.ask',
                           return_value=(AI_ERROR, {'error': 'credit balance is too low'})), \
                mock.patch('apps.msteams.graph.is_configured', return_value=True), \
                mock.patch('apps.livesessions.pipeline.transcript_text', return_value=VTT * 10), \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            for _ in range(ai_followup.MAX_ATTEMPTS):
                ai_followup.process(artifact)
        artifact.refresh_from_db()
        self.assertTrue(artifact.ai_followup_done)
        self.assertIn('credit balance', artifact.last_error)
        self.assertTrue(notify.called)
        for call in notify.call_args_list:
            body = call.kwargs['body']
            self.assertNotIn('admin AI', body)
            self.assertNotIn('credit balance', body)
            self.assertIn(ai_followup.SUMMARY_PENDING, body)
            self.assertIn('/transcript/', body)

    def test_a_failed_claude_recap_falls_back_to_the_extractive_one(self):
        from apps.msteams import recap
        cues = recap.parse_vtt(VTT)
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.admin_ai.ask', return_value=(AI_ERROR, {'error': 'x'})):
            result = recap.claude_recap(cues, title='Class')
        self.assertEqual(result['engine'], 'extractive')
        self.assertNotIn('admin AI', recap.to_markdown(result, title='Class'))


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example',
                   SITE_URL='https://learn.example.org')
class RepairSessionLinksTests(TestCase):
    """Tests for the ``repair_session_links`` management command, dry run and ``--apply``."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user
    _session = SessionLinkTests._session

    def _rows(self):
        """Create a session plus every kind of row the repair touches, and one unmatchable notification.

        Returns ``(room, lesson, resource, note, watch, stranger, material)``.
        """
        from apps.ai_assistant.models import AiReport
        from apps.communication.models import Notification
        from apps.learning.models import Lesson, LessonBlock, LessonResource, ModuleMaterial
        room = self._session()
        lesson = Lesson.objects.create(title='Session summary', slug='s-1', module=self.pm)
        room.artifact.summary_lesson = lesson
        room.artifact.summary_source = 'claude'
        room.artifact.save()
        LessonBlock.objects.create(lesson=lesson, order=0, block_type=LessonBlock.TYPE_TEXT,
                                   data={'html': f'<p>{AI_ERROR}</p>'})
        resource = LessonResource.objects.create(lesson=lesson, kind='link', title='Open the summary on OneDrive',
                                                 url=SHARE_SUMMARY)
        AiReport.objects.create(linked_ref=f'meeting:{room.pk}', kind=AiReport.KIND_MEETING_RECAP,
                                title='Recap', content=f'# T — class recap\n\n{AI_ERROR}')
        note = Notification.objects.create(
            recipient=self.mine, verb='session summary', title='Session material', meeting=room,
            body=f'T — class recap\n\n{AI_ERROR}\n\nRead the transcript: {SHARE_TRANSCRIPT}')
        watch = Notification.objects.create(
            recipient=self.mine, verb='session summary', title='Recap', meeting=room,
            body=f'• Watch the recording: {SHARE_RECORDING}.')
        stranger = Notification.objects.create(
            recipient=self.mine, verb='x', title='Other',
            body='See https://tenant-my.sharepoint.com/:x:/g/unknown')
        phase = room.week.phase if room.week_id else self.pm.phases.first()
        material = ModuleMaterial.objects.create(phase=phase, kind=ModuleMaterial.KIND_RECORDING, meeting=room,
                                                 title='Recording — class', url=SHARE_RECORDING)
        return room, lesson, resource, note, watch, stranger, material

    def test_dry_run_reports_and_changes_nothing(self):
        """A dry run reports counts and lists unmatched links but changes nothing."""
        _room, _lesson, resource, note, _watch, _stranger, material = self._rows()
        out = StringIO()
        call_command('repair_session_links', stdout=out)
        text = out.getvalue()
        self.assertIn('DRY RUN', text)
        self.assertIn('Notifications with SharePoint links: 3 · rewritten 2', text)
        self.assertIn('Notifications with AI error text: 1', text)
        self.assertIn('/unknown', text)
        before = (resource.url, note.body, material.url)
        for row in (resource, note, material):
            row.refresh_from_db()
        self.assertEqual((resource.url, note.body, material.url), before)

    def test_apply_rewrites_matched_rows_and_neutralises_ai_errors(self):
        """``--apply`` rewrites matched rows and neutralises AI errors.

        Unmatchable SharePoint links are left alone, the summary source is cleared so a rebuilt summary may
        replace it, and the command is idempotent: a second run finds nothing left to do.
        """
        from apps.ai_assistant.models import AiReport
        from apps.livesessions.ai_followup import SUMMARY_PENDING
        room, lesson, resource, note, watch, stranger, material = self._rows()
        call_command('repair_session_links', '--apply', stdout=StringIO())

        note.refresh_from_db()
        self.assertIn(f'Read the transcript: https://learn.example.org/calendar/sessions/{room.pk}/transcript/',
                      note.body)
        self.assertNotIn('admin AI', note.body)
        self.assertIn(SUMMARY_PENDING, note.body)
        watch.refresh_from_db()
        self.assertEqual(watch.body,
                         f'• Watch the recording: https://learn.example.org/calendar/sessions/{room.pk}/#recording.')
        stranger.refresh_from_db()
        self.assertIn('sharepoint.com/:x:/g/unknown', stranger.body)

        resource.refresh_from_db()
        self.assertEqual((resource.title, resource.url),
                         ('Recording and transcript (session page)', f'/calendar/sessions/{room.pk}/'))
        material.refresh_from_db()
        self.assertEqual(material.url, f'/calendar/sessions/{room.pk}/#recording')

        html = lesson.blocks.get().data['html']
        self.assertEqual(html, f'<p>{SUMMARY_PENDING}</p>')
        report = AiReport.objects.get(linked_ref=f'meeting:{room.pk}')
        self.assertNotIn('admin AI', report.content)
        room.artifact.refresh_from_db()
        self.assertEqual(room.artifact.summary_source, '')

        out = StringIO()
        call_command('repair_session_links', stdout=out)
        self.assertIn('Notifications with AI error text: 0', out.getvalue())
        self.assertIn('Lesson resources with SharePoint links: 0', out.getvalue())
