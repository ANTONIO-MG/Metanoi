"""The nightly YouTube backup (youtube_backup.run).

YouTube is a backup: at most six recordings a night, oldest first, each filed
into its institution + programme playlist, and nothing a student sees changes.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.communication.models import MeetingRoom

from . import youtube, youtube_backup
from .models import LiveSessionSettings, SessionArtifact

CREDS = dict(YOUTUBE_CLIENT_ID='id', YOUTUBE_CLIENT_SECRET='secret', YOUTUBE_REFRESH_TOKEN='tok')


@override_settings(**CREDS)
class NightlyBackupTests(TestCase):
    """Tests for :func:`youtube_backup.run`: the nightly cap, quota, playlists and switch."""
    def setUp(self):
        """Switch the backup on with ample quota and create eight published, unbacked recordings, oldest first."""
        conf = LiveSessionSettings.load()
        conf.youtube_enabled = True
        conf.youtube_daily_quota = 10000
        conf.save()
        self.artifacts = []
        for i in range(8):
            meeting = MeetingRoom.objects.create(
                title=f'Class {i}', provider=MeetingRoom.PROVIDER_TEAMS,
                scheduled_start=timezone.now() - timedelta(days=10 - i))
            self.artifacts.append(SessionArtifact.objects.create(
                meeting=meeting, state=SessionArtifact.STATE_PUBLISHED,
                recording_item_id=f'item{i}'))

    def _run(self):
        """Run the backup with OneDrive, Graph and YouTube mocked.

        Returns ``(uploaded_titles, summary, ensure_playlist_mock, add_to_playlist_mock)``.
        """
        uploaded = []

        def fake_upload(**kwargs):
            """Record the uploaded title and return a fresh video id with no error."""
            uploaded.append(kwargs['title'])
            return f'vid{len(uploaded)}', ''

        with mock.patch('apps.livesessions.pipeline.folder_ref',
                        return_value={'drive_id': 'd'}), \
             mock.patch('apps.msteams.graph.download_item', return_value=b'mp4'), \
             mock.patch.object(youtube, 'upload', side_effect=fake_upload), \
             mock.patch.object(youtube, 'ensure_playlist', return_value=('PL1', '')) as ensure, \
             mock.patch.object(youtube, 'add_to_playlist', return_value='') as add:
            summary = youtube_backup.run()
        return uploaded, summary, ensure, add

    def test_six_a_night_oldest_first_each_filed_in_its_playlist(self):
        uploaded, summary, ensure, add = self._run()
        self.assertEqual(len(uploaded), 6)
        self.assertTrue(uploaded[0].endswith('Class 0'))
        self.assertEqual(add.call_count, 6)
        self.assertIn('2 still waiting', summary)
        backed_up = SessionArtifact.objects.exclude(youtube_video_id='')
        self.assertEqual(backed_up.count(), 6)
        self.assertNotIn(self.artifacts[7].pk, backed_up.values_list('pk', flat=True))

    def test_the_quota_check_stops_the_run(self):
        conf = LiveSessionSettings.load()
        conf.youtube_daily_quota = 2 * youtube.BACKUP_COST
        conf.save()
        uploaded, *_ = self._run()
        self.assertEqual(len(uploaded), 2)

    def test_students_keep_watching_from_onedrive(self):
        self._run()
        artifact = SessionArtifact.objects.exclude(youtube_video_id='').first()
        self.assertEqual(artifact.embed_url, '')

    def test_switched_off_means_nothing_is_uploaded(self):
        conf = LiveSessionSettings.load()
        conf.youtube_enabled = False
        conf.save()
        uploaded, summary, *_ = self._run()
        self.assertEqual(uploaded, [])
        self.assertIn('off', summary)

    @staticmethod
    def _programme(name):
        """Return a programme stand-in whose ``name`` is ``name``.

        The attribute is set after construction because ``Mock(name=...)`` names the mock itself
        rather than setting ``.name``.
        """
        programme = mock.Mock()
        programme.name = name
        return programme

    def test_playlist_is_named_after_institution_and_programme(self):
        meeting = self.artifacts[0].meeting
        with mock.patch.object(MeetingRoom, 'resolved_institution',
                               new_callable=mock.PropertyMock,
                               return_value=mock.Mock(display_name='Milpark Education')), \
             mock.patch.object(MeetingRoom, 'resolved_programme',
                               new_callable=mock.PropertyMock,
                               return_value=self._programme('PGDA')):
            self.assertEqual(youtube_backup.playlist_title(meeting), 'Milpark Education — PGDA')
