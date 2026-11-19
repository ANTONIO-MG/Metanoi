"""Backups: real pg_dump restore points, retention, and the admin-only page."""

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import ActivityLog, Person
from apps.diagnostics.models import ErrorEvent
from apps.livesessions.tests import make_user

from . import backups

ROOT = tempfile.mkdtemp(prefix='metanoia-backup-test-')


@override_settings(BACKUP_ROOT=ROOT)
class BackupTests(TestCase):
    """Backups against a temporary ``BACKUP_ROOT``; the admin is a developer (backups are a developer tool)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('boss', 'admin')
        Person.objects.filter(user=cls.admin).update(is_developer=True)
        cls.admin.set_password('s3cret-pass!')
        cls.admin.save()
        cls.staff = make_user('staffer', 'staff')
        cls.plain_admin = make_user('plainboss', 'admin')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ROOT, ignore_errors=True)

    def tearDown(self):
        """Empty the backup folder between tests."""
        for p in Path(ROOT).glob('*'):
            p.unlink()

    def test_backup_is_a_real_readable_dump_with_metadata(self):
        meta = backups.create_backup(backups.KIND_MANUAL, by='Test', note='before import')
        path = backups.path_for(meta['name'])
        self.assertTrue(backups.validate_dump(path))
        self.assertEqual(meta['note'], 'before import')
        self.assertEqual(len(meta['sha256']), 64)
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual([m['name'] for m in backups.list_backups()], [meta['name']])

    def test_retention_keeps_the_newest_scheduled(self):
        with mock.patch.dict(backups.RETENTION, {backups.KIND_SCHEDULED: 2}):
            names = [backups.create_backup(backups.KIND_SCHEDULED)['name'] for _ in range(3)]
        kept = {m['name'] for m in backups.list_backups()}
        self.assertEqual(kept, set(names[1:]))

    def test_automatic_backups_older_than_three_days_are_deleted_never_the_newest(self):
        """Old automatic backups go; manual ones stay.

        When backups stopped for a week, the last automatic one survives anyway.
        """
        import json as _json
        from datetime import datetime, timedelta, timezone as dt_timezone

        def fake(kind, age_hours, n):
            """Write a fake dump of ``kind`` that is ``age_hours`` old."""
            name = f'metanoia-20260101-0000{n:02d}-{kind}.dump'
            path = Path(ROOT) / name
            path.write_bytes(b'x')
            created = datetime.now(dt_timezone.utc) - timedelta(hours=age_hours)
            path.with_suffix('.json').write_text(_json.dumps(
                {'name': name, 'kind': kind, 'created_at': created.isoformat()}))
            return name

        fresh = fake('scheduled', 2, 1)
        recent = fake('scheduled', 70, 2)
        old = fake('scheduled', 75, 3)
        manual = fake('manual', 24 * 30, 4)
        with override_settings(BACKUP_RETENTION_DAYS=3):
            removed = backups.prune()
        self.assertEqual(removed, [old])
        self.assertEqual({m['name'] for m in backups.list_backups()}, {fresh, recent, manual})

        for p in Path(ROOT).glob('*'):
            p.unlink()
        last = fake('scheduled', 24 * 7, 1)
        older = fake('scheduled', 24 * 8, 2)
        self.assertEqual(backups.prune(), [older])
        self.assertEqual([m['name'] for m in backups.list_backups()], [last])

    def test_names_outside_the_folder_are_refused(self):
        for bad in ('../../etc/passwd', 'metanoia-x.dump', '/tmp/a.dump', 'metanoia-20260101-000000-manual.dump'):
            with self.assertRaises(backups.BackupError):
                backups.path_for(bad)

    def test_upload_must_be_a_dump(self):
        with self.assertRaises(backups.BackupError) as ctx:
            backups.import_upload(SimpleUploadedFile('x.dump', b'not a dump'))
        self.assertEqual(ctx.exception.code, 'BKP-1001')

    def test_page_is_for_developers_only(self):
        for user in (self.staff, self.plain_admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse('staffdesk:backups')).status_code, 404)
        self.assertTrue(ErrorEvent.objects.filter(code='BKP-2001').exists())
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('staffdesk:backups')).status_code, 200)

    def test_back_up_now_download_and_audit(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('staffdesk:backup-create'), {'note': 'test'})
        name = backups.list_backups()[0]['name']
        r = self.client.get(reverse('staffdesk:backup-download', args=[name]))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ActivityLog.objects.filter(description__contains=f'downloaded {name}').exists())
        self.assertIn('downloaded', (Path(ROOT) / 'audit.log').read_text())

    def test_restore_needs_the_word_and_the_password(self):
        name = backups.create_backup()['name']
        self.client.force_login(self.admin)
        url = reverse('staffdesk:backup-restore', args=[name])
        with mock.patch.object(backups, 'start_restore') as start:
            self.client.post(url, {'confirm': 'restore', 'password': 's3cret-pass!'})
            self.client.post(url, {'confirm': 'RESTORE', 'password': 'wrong'})
            start.assert_not_called()
            self.client.post(url, {'confirm': 'RESTORE', 'password': 's3cret-pass!'})
            start.assert_called_once()

    def test_scheduled_job_entry_point(self):
        self.assertIn('backed up to metanoia-', backups.scheduled_backup())
