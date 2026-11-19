"""Off-site database backups in OneDrive, against a mocked Microsoft Graph.

``CONFIG`` keeps Teams deliberately off (``MS_TEAMS_ENABLED=False``): the backup
must not depend on the classroom switch.
"""

import json
from datetime import datetime, timedelta, timezone as dt_timezone
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from apps.diagnostics.models import ErrorEvent
from apps.msteams import graph
from apps.msteams.tests_teams_flow import BASE, FakeGraph, FakeResponse

from . import backups, onedrive_backup

ROOT = tempfile.mkdtemp(prefix='metanoia-onedrive-test-')
UPLOAD_URL = 'https://upload.example/session/42'

CONFIG = dict(
    BACKUP_ROOT=ROOT, MS_GRAPH_BASE=BASE,
    MS_TEAMS_ENABLED=False,
    MS_GRAPH_TENANT_ID='tenant', MS_GRAPH_CLIENT_ID='client', MS_GRAPH_CLIENT_SECRET='secret',
    MS_GRAPH_DEFAULT_ORGANIZER='admin@metanoia.co.za',
    MS_GRAPH_BACKUP_DRIVE_USER='', MS_GRAPH_BACKUP_FOLDER='metanoia DB BCKP',
    ONEDRIVE_BACKUP_ENABLED=True, BACKUP_RETENTION_DAYS=3,
)


def _fake_create_backup(size):
    """Stand in for pg_dump: write a dump of ``size`` bytes and its .json."""
    def create(kind=backups.KIND_MANUAL, *, by='', note=''):
        name = backups._new_name(kind)
        path = backups.backup_root() / name
        path.write_bytes(b'P' * size)
        path.with_suffix('.json').write_text(json.dumps({'name': name, 'kind': kind, 'by': by}))
        return backups.read_meta(path)
    return create


@override_settings(**CONFIG)
class OneDriveBackupTests(TestCase):
    """The OneDrive copy step of :func:`backups.scheduled_backup` against a fake Graph."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ROOT, ignore_errors=True)

    def tearDown(self):
        """Empty the backup folder between tests."""
        for p in Path(ROOT).glob('*'):
            p.unlink()

    def _graph(self, existing=()):
        """A drive with the backup folder missing, and ``existing`` files once it exists."""
        fake = FakeGraph({
            ('GET', '/users/admin@metanoia.co.za/drive'): (200, {'id': 'DRIVE'}),
            ('GET', '/drives/DRIVE/root'): (200, {'id': 'ROOT'}),
            ('POST', '/drives/DRIVE/items/ROOT/children'): (
                201, {'id': 'FOLDER', 'name': 'metanoia DB BCKP', 'folder': {}}),
            ('POST', ':/createUploadSession'): (200, {'uploadUrl': UPLOAD_URL}),
            ('GET', '/drives/DRIVE/items/FOLDER/children'): (
                200, {'value': [{'id': f'id-{n}', 'name': n} for n in existing]}),
            ('DELETE', ''): (204, None),
        })
        uploaded = []

        def put(url, data=None, headers=None, timeout=None, **kw):
            """Fake ``requests.put``: upload-session fragments, or a simple upload."""
            fake.calls.append({'method': 'PUT', 'path': url, 'headers': headers or {},
                               'data': data, 'json': None, 'params': None})
            if url.startswith(UPLOAD_URL):
                total = int(headers['Content-Range'].rsplit('/', 1)[1])
                done = headers['Content-Range'].split('-')[1].split('/')[0]
                if int(done) == total - 1:
                    uploaded.append(('session', total))
                    return FakeResponse(201, {'id': 'DUMP-ITEM', 'size': total,
                                              'webUrl': 'https://onedrive.example/dump'})
                return FakeResponse(202, {'nextExpectedRanges': [f'{int(done) + 1}-']})
            name = url.rsplit(':/content', 1)[0].rsplit(':/', 1)[1]
            uploaded.append(('simple', name))
            return FakeResponse(201, {'id': f'item-{name}', 'name': name})

        fake.put = put
        return fake, uploaded

    def _patched(self, fake):
        """Patches that route MSAL and ``requests`` to ``fake``."""
        graph._token_cache['value'] = None
        return [mock.patch.object(graph, '_msal_app', return_value=mock.Mock(
                    acquire_token_for_client=mock.Mock(
                        return_value={'access_token': 'tok', 'expires_in': 3600}))),
                mock.patch('requests.request', fake), mock.patch('requests.put', fake.put),
                mock.patch('requests.delete', fake.delete)]

    def _run(self, fake, size):
        """Run the scheduled backup with a fake ``size``-byte dump; returns its summary."""
        patches = self._patched(fake) + [
            mock.patch.object(backups, 'create_backup', _fake_create_backup(size))]
        for p in patches:
            p.start()
        try:
            return backups.scheduled_backup()
        finally:
            for p in reversed(patches):
                p.stop()
            graph._token_cache['value'] = None

    def test_large_dump_goes_up_through_an_upload_session_into_a_new_folder(self):
        """Dumps hold personal data and password hashes, so nothing may create a share or invite."""
        fake, uploaded = self._graph()
        summary = self._run(fake, size=5 * 1024 * 1024)

        self.assertIn('OneDrive copy uploaded', summary)
        self.assertEqual(len(fake.find('POST', '/drives/DRIVE/items/ROOT/children')), 1,
                         'the folder is created when it is missing')
        self.assertIn(('session', 5 * 1024 * 1024), uploaded)
        self.assertTrue(any(kind == 'simple' and name.endswith('.json') for kind, name in uploaded),
                        'the .json description travels with the dump')
        meta = backups.list_backups()[0]
        self.assertEqual(meta['onedrive']['status'], 'uploaded')
        self.assertEqual(meta['onedrive']['web_url'], 'https://onedrive.example/dump')
        self.assertEqual(meta['onedrive']['folder'], 'metanoia DB BCKP')
        self.assertEqual(fake.find('POST', '/drives/DRIVE/items/ROOT/children')[0]['json']['name'],
                         'metanoia DB BCKP')
        self.assertFalse([c for c in fake.calls if c['path'].endswith(('/createLink', '/invite'))])
        self.assertEqual(meta['onedrive']['shared'], [])

    def test_small_dump_is_a_single_put(self):
        fake, uploaded = self._graph()
        self._run(fake, size=1024)
        self.assertFalse(fake.find('POST', ':/createUploadSession'))
        self.assertTrue(any(kind == 'simple' and name.endswith('.dump') for kind, name in uploaded))

    def test_remote_dumps_older_than_three_days_are_deleted(self):
        """Only the scheduled dumps past 3 days, and their .json, go; files the job did
        not write are never touched.
        """
        now = datetime.now(dt_timezone.utc)
        stamp = lambda hours: (now - timedelta(hours=hours)).strftime('%Y%m%d-%H%M%S')
        fresh = [f'metanoia-{stamp(h)}-scheduled.dump' for h in (1, 30, 71)]
        stale = [f'metanoia-{stamp(h)}-scheduled.dump' for h in (73, 24 * 10)]
        sidecars = [n.replace('.dump', '.json') for n in fresh + stale]
        by_hand = ['notes.txt', f'metanoia-{stamp(24 * 30)}-manual.dump']
        fake, _ = self._graph(existing=fresh + stale + sidecars + by_hand)
        self._run(fake, size=1024)
        deleted = {c['path'].rsplit('/', 1)[1] for c in fake.calls if c['method'] == 'DELETE'}
        self.assertEqual(deleted, {f'id-{n}' for n in stale}
                         | {f'id-{n.replace(".dump", ".json")}' for n in stale})

    def test_the_newest_remote_dump_is_kept_however_old(self):
        fake, _ = self._graph(existing=['metanoia-20200101-000000-scheduled.dump',
                                        'metanoia-20190101-000000-scheduled.dump'])
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            with graph.without_teams_switch():
                removed = onedrive_backup.prune_remote('DRIVE', 'FOLDER')
        finally:
            for p in reversed(patches):
                p.stop()
            graph._token_cache['value'] = None
        self.assertEqual(removed, ['metanoia-20190101-000000-scheduled.dump'])

    def test_a_shared_folder_is_reported_not_changed(self):
        fake, _ = self._graph()
        fake.routes[('GET', '/drives/DRIVE/items/FOLDER/permissions')] = (200, {'value': [
            {'id': 'p0', 'roles': ['owner'], 'grantedToV2': {'user': {'email': 'admin@metanoia.co.za'}}},
            {'id': 'p1', 'roles': ['read'], 'link': {'scope': 'anonymous', 'type': 'view'}},
            {'id': 'p2', 'roles': ['write'], 'grantedToV2': {'user': {'email': 'someone@example.com'}}},
        ]})
        summary = self._run(fake, size=1024)
        self.assertIn('OneDrive copy uploaded', summary)
        meta = backups.list_backups()[0]
        self.assertEqual(len(meta['onedrive']['shared']), 2)
        self.assertIn('anonymous', meta['onedrive']['shared'][0])
        self.assertIn('someone@example.com', meta['onedrive']['shared'][1])
        self.assertTrue(ErrorEvent.objects.filter(code='BKP-5004').exists())
        self.assertFalse([c for c in fake.calls if '/permissions/' in c['path']],
                         'permissions are reported, never edited by the job')

    def test_a_failed_upload_never_breaks_the_local_backup(self):
        fake, _ = self._graph()
        fake.routes[('GET', '/users/admin@metanoia.co.za/drive')] = (
            403, {'error': {'code': 'accessDenied'}})
        summary = self._run(fake, size=1024)

        self.assertIn('backed up to metanoia-', summary)
        self.assertIn('OneDrive copy failed', summary)
        meta = backups.list_backups()[0]
        self.assertTrue((Path(ROOT) / meta['name']).exists(), 'the local restore point stays')
        self.assertEqual(meta['onedrive']['status'], 'failed')
        self.assertIn('OneDrive', meta['onedrive']['error'])
        self.assertTrue(ErrorEvent.objects.filter(code='BKP-5003').exists())

    def test_a_failed_fragment_cancels_the_session_and_is_recorded(self):
        fake, _ = self._graph()

        def broken_put(url, data=None, headers=None, timeout=None, **kw):
            """Every fragment fails with a 503."""
            fake.calls.append({'method': 'PUT', 'path': url, 'headers': headers or {},
                               'data': None, 'json': None, 'params': None})
            return FakeResponse(503, {'error': {'code': 'serviceNotAvailable'}})
        fake.put = broken_put
        self._run(fake, size=5 * 1024 * 1024)
        self.assertEqual(backups.list_backups()[0]['onedrive']['status'], 'failed')
        self.assertTrue(any(c['method'] == 'DELETE' and c['path'] == UPLOAD_URL
                            for c in fake.calls), 'the abandoned session is cancelled')

    @override_settings(ONEDRIVE_BACKUP_ENABLED=None)
    def test_on_by_default_when_graph_is_configured(self):
        self.assertTrue(onedrive_backup.is_enabled())
        with override_settings(MS_GRAPH_CLIENT_SECRET=''):
            self.assertFalse(onedrive_backup.is_enabled())

    @override_settings(ONEDRIVE_BACKUP_ENABLED=False)
    def test_switched_off_means_no_graph_calls(self):
        fake, _ = self._graph()
        summary = self._run(fake, size=1024)
        self.assertNotIn('OneDrive', summary)
        self.assertEqual(fake.calls, [])
        self.assertNotIn('onedrive', backups.list_backups()[0])

    @override_settings(MS_GRAPH_CLIENT_SECRET='')
    def test_missing_secret_is_a_recorded_failure_not_a_crash(self):
        fake, _ = self._graph()
        self._run(fake, size=1024)
        meta = backups.list_backups()[0]
        self.assertEqual(meta['onedrive']['status'], 'failed')
        self.assertIn('credentials', meta['onedrive']['error'])
        self.assertEqual(fake.calls, [])

    def test_the_backup_drive_user_overrides_the_organizer(self):
        fake, _ = self._graph()
        fake.routes[('GET', '/users/backups@metanoia.co.za/drive')] = (200, {'id': 'DRIVE'})
        with override_settings(MS_GRAPH_BACKUP_DRIVE_USER='backups@metanoia.co.za'):
            self._run(fake, size=1024)
        self.assertTrue(fake.find('GET', '/users/backups@metanoia.co.za/drive'))
        self.assertEqual(backups.list_backups()[0]['onedrive']['owner'], 'backups@metanoia.co.za')
