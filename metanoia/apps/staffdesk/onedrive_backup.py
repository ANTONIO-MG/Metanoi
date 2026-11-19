"""Off-site copies of the automatic database backup, in OneDrive.

After each successful automatic backup — every 3 hours —
(:func:`apps.staffdesk.backups.scheduled_backup`) the dump and its ``.json``
description are uploaded through Microsoft Graph to a folder in one person's
OneDrive (the default organiser's, unless ``MS_GRAPH_BACKUP_DRIVE_USER`` says
otherwise)::

    <owner's OneDrive>/metanoia DB BCKP/          (MS_GRAPH_BACKUP_FOLDER)
        metanoia-20261107-220000-scheduled.dump
        metanoia-20261107-220000-scheduled.json

The folder is created if it is missing. Dumps are well over Graph's 4 MB
single-request limit, so they go up through a resumable upload session in
10 MiB fragments. The same retention as on the server applies there: dumps
older than 3 days (``BACKUP_RETENTION_DAYS``) are deleted, never the newest.

Rules this module keeps:

* **On whenever Graph is configured** (tenant, client id and secret present),
  unless ``ONEDRIVE_BACKUP_ENABLED=false`` switches it off. Independent of
  ``MS_TEAMS_ENABLED`` — backups must not switch on and off with the classroom.
* **Never shared.** The dumps hold every account's personal data and password
  hashes. Nothing here creates a sharing link or invites anyone; every upload
  checks the folder's permissions and reports (``BKP-5004``) any sharing link
  or grant beyond the owner that someone added by hand.
* **Never breaks the local backup.** Every failure is caught, logged, recorded
  in the error log as ``BKP-5003`` and written into the backup's own ``.json``
  under ``"onedrive"``; the restore point on this server is untouched.
* The outcome is visible where backups are already looked at: the ``.json``
  beside the dump says ``uploaded``/``failed``, when, where, and why.

``_REMOTE_DUMP_RE`` matches the names this module uploads — and so the only
names it will ever prune remotely. :data:`DEFAULT_FOLDER` is the folder the
client asked for, in the owner's OneDrive.
"""

import logging
import re
from datetime import datetime, timezone as dt_timezone

from django.conf import settings

logger = logging.getLogger('apps')

_REMOTE_DUMP_RE = re.compile(r'^metanoia-(\d{8}-\d{6})-scheduled(-\d+)?\.dump$')


DEFAULT_FOLDER = 'metanoia DB BCKP'


def is_enabled():
    """``ONEDRIVE_BACKUP_ENABLED`` when it is set; otherwise on iff Graph is configured."""
    flag = getattr(settings, 'ONEDRIVE_BACKUP_ENABLED', None)
    if flag is None:
        from apps.msteams import graph
        return graph.credentials_present()
    return bool(flag)


def drive_user():
    """Whose OneDrive holds the copies: ``MS_GRAPH_BACKUP_DRIVE_USER``, else the default organiser."""
    return ((getattr(settings, 'MS_GRAPH_BACKUP_DRIVE_USER', '') or '').strip()
            or (getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip())


def folder_path():
    """The backup folder path in that OneDrive (``MS_GRAPH_BACKUP_FOLDER`` or :data:`DEFAULT_FOLDER`)."""
    return (getattr(settings, 'MS_GRAPH_BACKUP_FOLDER', '') or DEFAULT_FOLDER).strip('/ ')


def keep_days():
    """Days scheduled dumps are kept remotely — the same as on the server."""
    from . import backups
    return backups.scheduled_keep_days()


class UploadFailed(Exception):
    """A OneDrive copy step failed; the message says what to fix."""

    pass


def resolve_folder():
    """``(drive_id, folder_item)`` for the backup folder, creating it if needed."""
    from apps.msteams import graph

    owner = drive_user()
    if not owner:
        raise UploadFailed('No OneDrive owner: set MS_GRAPH_BACKUP_DRIVE_USER '
                           '(or MS_GRAPH_DEFAULT_ORGANIZER).')
    drive_id = graph.get_drive_id(owner)
    if not drive_id:
        raise UploadFailed(f'Could not open the OneDrive of {owner} (check Files.ReadWrite.All '
                           'consent and that the account has a OneDrive licence).')
    folder = graph.ensure_folder_path(drive_id, folder_path())
    if not folder:
        raise UploadFailed(f'Could not create or find the folder "{folder_path()}" in the '
                           f'OneDrive of {owner}.')
    return drive_id, folder


def upload_backup(name):
    """Upload backup ``name`` (dump + .json) to OneDrive; record and return the outcome.

    Returns the ``onedrive`` dict written into the backup's metadata, e.g.
    ``{'status': 'uploaded', 'folder': …, 'web_url': …}`` or
    ``{'status': 'failed', 'error': …}``. Never raises — not even when recording
    the failure in the error log fails, since the error log must not become the
    error. The ``.json`` description travels with the dump so a copy restored
    from OneDrive alone still says what it is and its checksum.
    """
    from apps.msteams import graph
    from . import backups

    if not is_enabled():
        return {'status': 'skipped', 'reason': 'OneDrive backup is off (ONEDRIVE_BACKUP_ENABLED, '
                                               'or Graph not configured)'}

    outcome = {'attempted_at': _now(), 'owner': drive_user(), 'folder': folder_path()}
    try:
        if not graph.credentials_present():
            raise UploadFailed('Microsoft Graph credentials are incomplete '
                               '(MS_GRAPH_TENANT_ID / CLIENT_ID / CLIENT_SECRET).')
        path = backups.path_for(name)
        sidecar = path.with_suffix('.json')
        with graph.without_teams_switch():
            drive_id, folder = resolve_folder()
            item = graph.upload_file(drive_id, folder['id'], path.name, str(path))
            if not item or not item.get('id'):
                raise UploadFailed(f'Graph did not accept the upload of {path.name} '
                                   '(see the msteams lines in the error log).')
            if sidecar.exists():
                graph.upload_small_file(drive_id, folder['id'], sidecar.name,
                                        sidecar.read_bytes(), 'application/json')
            removed = prune_remote(drive_id, folder['id'])
            shared = sharing_problems(drive_id, folder['id'])
        outcome.update(status='uploaded', uploaded_at=_now(), item_id=item.get('id', ''),
                       web_url=item.get('webUrl', ''), size=item.get('size'),
                       pruned=removed, shared=shared)
        logger.info('backups: %s copied to OneDrive (%s/%s)', name, drive_user(), folder_path())
    except Exception as exc:
        outcome.update(status='failed', error=str(exc)[:500])
        logger.warning('backups: OneDrive copy of %s failed: %s', name, exc)
        try:
            from core.errors import report
            report('BKP-5003', exc, context={'backup': name, 'owner': drive_user(),
                                             'folder': folder_path()})
        except Exception:   # pragma: no cover
            logger.exception('backups: could not record the OneDrive failure')

    if outcome.get('shared'):
        _report_shared(name, outcome['shared'])

    try:
        backups.update_meta(name, onedrive=outcome)
    except Exception:       # pragma: no cover
        logger.exception('backups: could not record the OneDrive outcome for %s', name)
    return outcome


def _stamp(name):
    """The UTC time in a dump's name (``metanoia-YYYYMMDD-HHMMSS-…``), or None."""
    match = _REMOTE_DUMP_RE.match(name or '')
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), '%Y%m%d-%H%M%S').replace(tzinfo=dt_timezone.utc)
    except ValueError:
        return None


def prune_remote(drive_id, folder_id, *, days=None, now=None):
    """Delete scheduled dumps (and their .json) older than ``days`` from the folder.

    Only files whose names this module itself writes are ever considered, so a
    file someone dropped into the folder by hand is never deleted. Age is read
    from the UTC timestamp in the name. The newest dump is always kept.
    """
    from datetime import timedelta

    from apps.msteams import graph

    days = days or keep_days()
    cutoff = (now or datetime.now(dt_timezone.utc)) - timedelta(days=days)
    children = graph.list_children(drive_id, folder_id)
    by_name = {c.get('name'): c for c in children if c.get('name')}
    dumps = sorted((n for n in by_name if _stamp(n)), reverse=True)
    removed = []
    for name in dumps[1:]:
        if _stamp(name) >= cutoff:
            continue
        if graph.delete_item(drive_id, by_name[name]['id']):
            removed.append(name)
        sidecar = by_name.get(name[:-len('.dump')] + '.json')
        if sidecar:
            graph.delete_item(drive_id, sidecar['id'])
    return removed


def sharing_problems(drive_id, item_id):
    """Who besides the owner can reach the backup folder: a list of short descriptions.

    Empty means private. Read only — the job never changes permissions, it
    reports them; an inherited or hand-made share is for a person to remove.
    """
    from apps.msteams import graph

    problems = []
    for perm in graph.list_permissions(drive_id, item_id) or []:
        roles = perm.get('roles') or []
        link = perm.get('link') or {}
        if link:
            problems.append(f"sharing link ({link.get('scope') or 'unknown scope'}, "
                            f"{link.get('type') or '?'})")
        elif 'owner' not in roles:
            who = (perm.get('grantedToV2') or perm.get('grantedTo') or {}).get('user') or {}
            problems.append(f"{', '.join(roles) or 'access'} for "
                            f"{who.get('email') or who.get('displayName') or 'someone else'}")
    return problems


def _report_shared(name, problems):
    """Log and report (``BKP-5004``) that the backup folder is shared."""
    message = (f'The OneDrive backup folder "{folder_path()}" is shared: {"; ".join(problems)}. '
               'Database dumps hold personal data and password hashes — remove the sharing.')
    logger.error('backups: %s', message)
    try:
        from core.errors import report
        report('BKP-5004', RuntimeError(message),
               context={'backup': name, 'owner': drive_user(), 'folder': folder_path()})
    except Exception:   # pragma: no cover
        logger.exception('backups: could not record the sharing warning')


def _now():
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(dt_timezone.utc).isoformat()
