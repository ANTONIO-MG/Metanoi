"""Database backups and restore points.

A backup is a PostgreSQL custom-format dump (``pg_dump -Fc``) in
``settings.BACKUP_ROOT`` (``backups/db/`` by default, git-ignored), with a small
JSON file beside it describing it — when, why, by whom, size, checksum.

The description lives on disk, not in the database, on purpose: restoring the
database rewinds every table, so a list of backups kept *in* the database would
forget the backups made after the point being restored. The folder never lies.

Kinds:

``scheduled``    the automatic job (``database-backup``, every 3 hours) — kept for
                 :data:`SCHEDULED_KEEP_DAYS` days (3), never fewer than the newest one;
``manual``       "Back up now" on the Backups page — kept until deleted;
``pre-restore``  taken automatically before every restore — the last 10 are
                 kept, so a restore can always be undone;
``uploaded``     a dump brought in from elsewhere — kept until deleted.

A restore runs in its own process (``manage.py restore_database``) because it
drops the site's own database connections part-way. It takes a safety backup,
ends other sessions on the database, then ``pg_restore --clean --if-exists
--single-transaction`` — all-or-nothing, so a failed restore leaves the
database as it was. Progress is written to ``restore-status.json`` for the page
to poll.

Retention settings:

:data:`RETENTION`
    How many of each kind are kept; ``None`` keeps them all until someone
    deletes them. Automatic backups are kept by age instead (see
    :func:`scheduled_keep_days`).
:data:`SCHEDULED_KEEP_DAYS`
    Automatic (scheduled) backups older than this many days are deleted — here
    and in the OneDrive copy. ``settings.BACKUP_RETENTION_DAYS`` overrides it.

Dumps hold everyone's data, so the folder is ``0700`` and every dump and
description file ``0600`` (owner-only).
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import connection

logger = logging.getLogger('apps')

KIND_SCHEDULED, KIND_MANUAL, KIND_PRE_RESTORE, KIND_UPLOADED = 'scheduled', 'manual', 'pre-restore', 'uploaded'
KINDS = (KIND_SCHEDULED, KIND_MANUAL, KIND_PRE_RESTORE, KIND_UPLOADED)
RETENTION = {KIND_SCHEDULED: None, KIND_PRE_RESTORE: 10, KIND_MANUAL: None, KIND_UPLOADED: None}
SCHEDULED_KEEP_DAYS = 3

_NAME_RE = re.compile(r'^metanoia-\d{8}-\d{6}-(scheduled|manual|pre-restore|uploaded)(-\d+)?\.dump$')
STATUS_FILE = 'restore-status.json'


class BackupError(Exception):
    """A backup or restore step failed; ``code`` is its error-log code."""

    def __init__(self, message, code='BKP-5001', detail=''):
        super().__init__(message)
        self.code, self.detail = code, detail


def backup_root():
    """The backup folder (``settings.BACKUP_ROOT`` or ``backups/db/``), created owner-only.

    Dumps hold everyone's data, so the folder is chmod ``0700`` every time.
    """
    root = Path(getattr(settings, 'BACKUP_ROOT', '') or Path(settings.BASE_DIR) / 'backups' / 'db')
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:     # pragma: no cover
        pass
    return root


def _tool(name):
    """Path to PostgreSQL client ``name`` (from ``PG_BIN_DIR`` or ``PATH``); ``BKP-7001`` if missing."""
    bin_dir = getattr(settings, 'PG_BIN_DIR', '') or os.environ.get('PG_BIN_DIR', '')
    path = os.path.join(bin_dir, name) if bin_dir else shutil.which(name)
    if not path or not os.path.exists(path):
        raise BackupError(f'{name} is not installed on this server.', code='BKP-7001')
    return path


def _db():
    """Connection details for the default database, and an env carrying the password."""
    conf = connection.settings_dict
    env = dict(os.environ)
    if conf.get('PASSWORD'):
        env['PGPASSWORD'] = str(conf['PASSWORD'])
    args = ['-h', conf.get('HOST') or '127.0.0.1', '-p', str(conf.get('PORT') or 5432),
            '-U', conf.get('USER') or '']
    return conf['NAME'], args, env


def path_for(name):
    """The path of backup ``name`` — refusing anything that is not one of ours."""
    if not _NAME_RE.match(name or ''):
        raise BackupError('That is not a backup name this system made.', code='BKP-1001')
    path = backup_root() / name
    if not path.exists():
        raise BackupError('That backup no longer exists.', code='BKP-1001')
    return path


def _meta_path(path):
    """The ``.json`` description file beside dump ``path``."""
    return path.with_suffix('.json')


def read_meta(path):
    """Description of dump ``path``, filling gaps from its name and file stats.

    Adds ``size`` (bytes, from disk) and ``created`` (a datetime) to whatever the
    ``.json`` file holds; a missing or broken description file is not an error.
    """
    meta = {}
    try:
        meta = json.loads(_meta_path(path).read_text())
    except (OSError, ValueError):
        pass
    stat = path.stat()
    meta.setdefault('name', path.name)
    meta.setdefault('kind', (_NAME_RE.match(path.name) or [None, 'manual'])[1])
    meta.setdefault('created_at', datetime.fromtimestamp(stat.st_mtime, dt_timezone.utc).isoformat())
    meta['size'] = stat.st_size
    meta['created'] = datetime.fromisoformat(meta['created_at'])
    return meta


def list_backups():
    """Every backup, newest first."""
    rows = [read_meta(p) for p in backup_root().glob('metanoia-*.dump') if _NAME_RE.match(p.name)]
    return sorted(rows, key=lambda m: m['created'], reverse=True)


def disk_usage():
    """Bytes used by dumps, plus free and total bytes on the backup disk."""
    root = backup_root()
    used = sum(p.stat().st_size for p in root.glob('*.dump'))
    total, _used, free = shutil.disk_usage(root)
    return {'backups_bytes': used, 'disk_free': free, 'disk_total': total}


def _sha256(path):
    """Hex SHA-256 of a file, read in 1 MB chunks."""
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _new_name(kind):
    """A fresh ``metanoia-<UTC stamp>-<kind>[-n].dump`` name not yet on disk."""
    stamp = datetime.now(dt_timezone.utc).strftime('%Y%m%d-%H%M%S')
    name, n = f'metanoia-{stamp}-{kind}.dump', 1
    while (backup_root() / name).exists():
        n += 1
        name = f'metanoia-{stamp}-{kind}-{n}.dump'
    return name


def _git_head():
    """Short commit hash of the running code, or ``''`` (recorded in each backup)."""
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=settings.BASE_DIR,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:   # pragma: no cover
        return ''


def create_backup(kind=KIND_MANUAL, *, by='', note=''):
    """Dump the database to a new restore point and return its metadata."""
    if kind not in KINDS:
        raise ValueError(kind)
    pg_dump = _tool('pg_dump')
    dbname, args, env = _db()
    name = _new_name(kind)
    path = backup_root() / name
    tmp = path.with_suffix('.partial')
    started = time.monotonic()
    proc = subprocess.run([pg_dump, *args, '-Fc', '-Z', '6', '--no-owner', '-f', str(tmp), dbname],
                          env=env, capture_output=True, text=True, timeout=60 * 60)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise BackupError('The database could not be backed up.', detail=proc.stderr[-2000:])
    tmp.rename(path)
    os.chmod(path, 0o600)
    meta = {
        'name': name, 'kind': kind, 'by': by, 'note': note[:300],
        'created_at': datetime.now(dt_timezone.utc).isoformat(),
        'database': dbname, 'size': path.stat().st_size, 'sha256': _sha256(path),
        'duration_s': round(time.monotonic() - started, 1), 'code_version': _git_head(),
    }
    _meta_path(path).write_text(json.dumps(meta, indent=2))
    os.chmod(_meta_path(path), 0o600)
    removed = prune()
    meta = read_meta(path)
    meta['pruned'] = removed
    return meta


def scheduled_keep_days():
    """Days automatic backups are kept: ``BACKUP_RETENTION_DAYS`` (at least 1) or the default."""
    try:
        return max(1, int(getattr(settings, 'BACKUP_RETENTION_DAYS', SCHEDULED_KEEP_DAYS)
                          or SCHEDULED_KEEP_DAYS))
    except (TypeError, ValueError):
        return SCHEDULED_KEEP_DAYS


def prune(*, now=None):
    """Apply the retention rules; returns the names removed.

    * automatic backups older than :func:`scheduled_keep_days` days go — but the
      newest automatic backup is never deleted, however old, so a server whose
      backups have been failing for a week still has its last good one;
    * the count limits in :data:`RETENTION` (safety backups: the last 10);
    * manual and uploaded backups are never removed automatically.
    """
    from datetime import timedelta

    now = now or datetime.now(dt_timezone.utc)
    removed = []
    by_kind = {}
    for meta in list_backups():
        by_kind.setdefault(meta['kind'], []).append(meta)
    cutoff = now - timedelta(days=scheduled_keep_days())
    for meta in by_kind.get(KIND_SCHEDULED, [])[1:]:
        if meta['created'] < cutoff:
            delete_backup(meta['name'])
            removed.append(meta['name'])
    for kind, keep in RETENTION.items():
        if keep is None:
            continue
        for meta in by_kind.get(kind, [])[keep:]:
            if meta['name'] in removed:
                continue
            delete_backup(meta['name'])
            removed.append(meta['name'])
    return removed


def delete_backup(name):
    """Delete backup ``name`` and its description (the name is validated first)."""
    path = path_for(name)
    path.unlink(missing_ok=True)
    _meta_path(path).unlink(missing_ok=True)


def validate_dump(path):
    """True if ``pg_restore`` can read it — the test that it is a real dump."""
    proc = subprocess.run([_tool('pg_restore'), '--list', str(path)], capture_output=True, text=True,
                          timeout=300)
    return proc.returncode == 0


def import_upload(uploaded, *, by=''):
    """Keep an uploaded dump as a restore point (after checking it is one)."""
    name = _new_name(KIND_UPLOADED)
    path = backup_root() / name
    tmp = path.with_suffix('.partial')
    with open(tmp, 'wb') as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)
    if not validate_dump(tmp):
        tmp.unlink(missing_ok=True)
        raise BackupError('That file is not a PostgreSQL backup (pg_restore cannot read it).',
                          code='BKP-1001')
    tmp.rename(path)
    os.chmod(path, 0o600)
    meta = {'name': name, 'kind': KIND_UPLOADED, 'by': by,
            'note': f'Uploaded as {getattr(uploaded, "name", "")}'[:300],
            'created_at': datetime.now(dt_timezone.utc).isoformat(),
            'size': path.stat().st_size, 'sha256': _sha256(path)}
    _meta_path(path).write_text(json.dumps(meta, indent=2))
    return read_meta(path)


def status():
    """The last restore's progress from ``restore-status.json`` (``{}`` if none)."""
    try:
        return json.loads((backup_root() / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _set_status(**fields):
    """Merge ``fields`` into the restore status file, stamping ``updated_at``."""
    data = status()
    data.update(fields, updated_at=datetime.now(dt_timezone.utc).isoformat())
    (backup_root() / STATUS_FILE).write_text(json.dumps(data, indent=2))
    return data


def restore_in_progress():
    """True while a restore is starting, taking its safety backup or restoring."""
    s = status()
    return s.get('state') in ('starting', 'safety-backup', 'restoring')


def start_restore(name, *, by=''):
    """Launch the restore in its own process and return immediately.

    The name is validated (:func:`path_for`) before anything happens; output of
    the child process goes to ``restore.log`` in the backup folder.
    """
    path_for(name)
    if restore_in_progress():
        raise BackupError('A restore is already running.', code='BKP-2001')
    _set_status(state='starting', backup=name, by=by, started_at=datetime.now(dt_timezone.utc).isoformat(),
                safety_backup='', error='')
    log = open(backup_root() / 'restore.log', 'a')
    subprocess.Popen([sys.executable, str(Path(settings.BASE_DIR) / 'manage.py'), 'restore_database', name,
                      '--by', by or 'unknown'],
                     cwd=settings.BASE_DIR, stdout=log, stderr=log, start_new_session=True)


def restore_backup(name, *, by=''):
    """Restore ``name`` over the live database. Runs in ``manage.py restore_database``.

    Takes a ``pre-restore`` safety backup first, then ends everyone else's
    sessions on this database so the restore is not blocked behind their locks
    (the site reconnects on its next request). Returns the safety backup's meta.
    """
    path = path_for(name)
    if not validate_dump(path):
        raise BackupError('That backup cannot be read, so nothing was changed.', code='BKP-1001')
    _set_status(state='safety-backup', backup=name, by=by)
    safety = create_backup(KIND_PRE_RESTORE, by=by, note=f'Automatic, before restoring {name}')
    _set_status(state='restoring', safety_backup=safety['name'])

    dbname, args, env = _db()
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                       'WHERE datname = current_database() AND pid <> pg_backend_pid()')
    connection.close()
    proc = subprocess.run([_tool('pg_restore'), *args, '-d', dbname, '--clean', '--if-exists',
                           '--no-owner', '--no-privileges', '--single-transaction', '--exit-on-error',
                           str(path)], env=env, capture_output=True, text=True, timeout=60 * 60)
    if proc.returncode != 0:
        _set_status(state='failed', error=proc.stderr[-2000:], finished_at=datetime.now(dt_timezone.utc).isoformat())
        raise BackupError('The restore failed and was rolled back; the database is as it was.',
                          code='BKP-5002', detail=proc.stderr[-2000:])
    _set_status(state='done', finished_at=datetime.now(dt_timezone.utc).isoformat())
    return safety


def update_meta(name, **fields):
    """Merge ``fields`` into backup ``name``'s .json description; returns the new meta."""
    path = path_for(name)
    meta = {}
    try:
        meta = json.loads(_meta_path(path).read_text())
    except (OSError, ValueError):
        meta = {'name': name}
    meta.update(fields)
    _meta_path(path).write_text(json.dumps(meta, indent=2))
    try:
        os.chmod(_meta_path(path), 0o600)
    except OSError:     # pragma: no cover
        pass
    return read_meta(path)


def scheduled_backup():
    """Scheduler entry point (``database-backup`` job, every 3 hours).

    The local restore point comes first and is complete on its own (and prunes
    automatic backups older than 3 days). Then, if the OneDrive copy is on, the
    dump goes to the "metanoia DB BCKP" folder — and nothing that happens to that
    copy can fail this job (see :mod:`.onedrive_backup`). The outer ``except`` is
    belt and braces: :func:`onedrive_backup.upload_backup` never raises.
    """
    meta = create_backup(KIND_SCHEDULED, by='scheduler', note='Automatic restore point (every 3 hours)')
    summary = f'backed up to {meta["name"]} ({meta["size"] // 1024} KB)'
    if meta.get('pruned'):
        summary += f'; {len(meta["pruned"])} old backup(s) deleted here'
    try:
        from . import onedrive_backup
        if onedrive_backup.is_enabled():
            outcome = onedrive_backup.upload_backup(meta['name'])
            summary += f'; OneDrive copy {outcome.get("status")}'
            if outcome.get('status') == 'failed':
                summary += f' ({outcome.get("error", "")[:120]})'
    except Exception:       # pragma: no cover
        logger.exception('backups: OneDrive copy step crashed for %s', meta['name'])
        summary += '; OneDrive copy failed'
    return summary
