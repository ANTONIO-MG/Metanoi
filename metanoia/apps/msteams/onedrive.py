"""The OneDrive filing cabinet: one folder per session, under a stable tree.

Teams saves recordings flat in the organiser's ``Recordings/`` folder, so the
pipeline files them into a tree:

    <root>/<INSTITUTION>/<PROGRAMME>/<MODULE>/<YYYY-MM-DD Session title>/
        recording.mp4
        transcript.vtt
        summary.md
        thumbnail.jpg

Every function here is idempotent and returns ``None``/``''`` rather than
raising, because each one is a step in a state machine that will be retried.
Nothing is *deleted*: the worst case is a duplicate folder, never a lost class.

``COPY_WAIT_SECONDS`` is how long one run waits for a server-side copy to land,
polling every ``COPY_POLL_SECONDS``. Teams names a recording
``<subject>-YYYYMMDD_HHMMSS-Meeting Recording.mp4``, with the subject's
punctuation dropped and the time in the organiser's zone; the stamp may end in
``UTC`` (then it is UTC), and a second part of the same start is saved as
``Meeting Recording 1.mp4`` (``_RECORDING_NAME``). ``RECORDING_MATCH_WINDOW``
is how far the file name's time may be from Graph's recording timestamp.
"""

import datetime as dt
import logging
import re

from django.conf import settings

from . import graph

logger = logging.getLogger('apps')


def owner_upn_for(meeting):
    """Whose OneDrive holds this session's folder.

    The configured archive drive, else the organiser's own drive (where Teams
    put the recording, so no cross-drive copy is needed).
    """
    from apps.livesessions.models import LiveSessionSettings
    configured = (LiveSessionSettings.load().onedrive_owner_upn or '').strip()
    if configured:
        return configured
    return (meeting.organizer_upn or archive_owner_upn())


def archive_owner_upn():
    """The drive that holds the institution → programme → module tree — the one
    ``provision_onedrive`` builds in. Summaries are always filed here, whoever
    hosted the class, so the whole archive stays in one place."""
    from apps.livesessions.models import LiveSessionSettings
    return ((LiveSessionSettings.load().onedrive_owner_upn or '').strip()
            or (getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip())


def ensure_path(parts, *, owner_upn=None):
    """Create (or find) ``<root>/<parts…>`` in the archive drive; return a folder dict or None."""
    from apps.livesessions.models import LiveSessionSettings
    if not graph.is_configured():
        return None
    owner = owner_upn or archive_owner_upn()
    drive_id = owner and graph.get_drive_id(owner)
    if not drive_id:
        return None
    root = graph.get_item_by_path(drive_id, '')
    parent_id, node = (root or {}).get('id'), None
    if not parent_id:
        return None
    walked = []
    for part in [LiveSessionSettings.load().onedrive_root.strip() or 'Metanoia Sessions'] + list(parts):
        node = graph.create_folder(drive_id, parent_id, part)
        if not node or not node.get('id'):
            logger.error('onedrive: could not create folder %r under %s', part, '/'.join(walked) or 'root')
            return None
        parent_id = node['id']
        walked.append(part)
    return {'drive_id': drive_id, 'folder_id': parent_id, 'path': '/'.join(walked),
            'web_url': (node or {}).get('webUrl', ''), 'owner_upn': owner}


def ensure_session_folder(meeting):
    """Create (or find) this session's folder and return its details.

    Returns ``{'drive_id', 'folder_id', 'path', 'web_url', 'owner_upn'}``, or
    None if Graph is unavailable or the tree could not be built.
    """
    from apps.livesessions.models import LiveSessionSettings

    if not graph.is_configured():
        return None
    owner = owner_upn_for(meeting)
    if not owner:
        logger.warning('onedrive: no drive owner for %s — cannot file artifacts', meeting)
        return None

    drive_id = graph.get_drive_id(owner)
    if not drive_id:
        logger.error('onedrive: could not resolve the drive for %s', owner)
        return None

    conf = LiveSessionSettings.load()
    parts = [conf.onedrive_root.strip() or 'Metanoia Sessions'] + list(meeting.storage_path_parts)

    root = graph.get_item_by_path(drive_id, '')
    parent_id = (root or {}).get('id')
    if not parent_id:
        return None

    walked = []
    node = None
    for part in parts:
        node = graph.create_folder(drive_id, parent_id, part)
        if not node or not node.get('id'):
            logger.error('onedrive: could not create folder %r under %s', part, '/'.join(walked) or 'root')
            return None
        parent_id = node['id']
        walked.append(part)

    return {
        'drive_id': drive_id,
        'folder_id': parent_id,
        'path': '/'.join(walked),
        'web_url': (node or {}).get('webUrl', ''),
        'owner_upn': owner,
    }


COPY_WAIT_SECONDS = 120
COPY_POLL_SECONDS = 10


def file_recording(meeting, folder, recording, *, wait=COPY_WAIT_SECONDS):
    """Put the Teams recording into the session folder; return the drive item.

    ``recording`` is one entry from ``graph.list_recordings``. With
    ``keep_teams_copy`` on (the default) the file is **copied** server-side, so
    Teams keeps playing its own copy and the platform has one in the session
    folder. With it off, a recording on the archive drive is *moved* instead —
    a metadata-only change, but Teams then loses track of it.

    Copies are asynchronous: this waits up to ``wait`` seconds for the copy to
    appear and returns None if it has not yet, so the caller tries again on its
    next run (a copy already in the folder is found by name, never repeated).
    A copy on the same tenant usually lands within a minute.

    Graph's ``callRecording`` usually carries no drive reference at all; the
    file is then looked up in the organiser's OneDrive ``Recordings`` folder
    (:func:`find_teams_recording`).
    """
    import time

    from apps.livesessions.models import LiveSessionSettings

    item_id, source_drive = _recording_item_ref(recording)
    if not item_id:
        found = find_teams_recording(meeting, recording)
        if found is None:
            return None
        item_id, source_drive = found['id'], found['drive_id']

    target_name = f'{artifact_stem(meeting)}.mp4'
    existing = find_child(folder['drive_id'], folder['folder_id'], target_name)
    if existing:
        return existing

    same_drive = (not source_drive) or source_drive == folder['drive_id']
    keep_original = LiveSessionSettings.load().keep_teams_copy

    if same_drive and not keep_original:
        moved = graph.move_item(folder['drive_id'], item_id,
                                parent_item_id=folder['folder_id'], new_name=target_name)
        if moved:
            return moved
        logger.warning('onedrive: move failed for %s — falling back to a copy', meeting)

    accepted = graph.copy_item(source_drive or folder['drive_id'], item_id,
                               parent_item_id=folder['folder_id'], new_name=target_name)
    if not accepted:
        return None
    deadline = time.monotonic() + wait
    while True:
        found = find_child(folder['drive_id'], folder['folder_id'], target_name)
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(COPY_POLL_SECONDS)


_RECORDING_NAME = re.compile(
    r'^(?P<title>.*)-(?P<stamp>\d{8}_\d{6})(?P<utc>UTC)?-Meeting Recording(?: \d+)?\.mp4$', re.I)
RECORDING_MATCH_WINDOW = dt.timedelta(minutes=15)


def _squash(text):
    """``text`` lower-cased with everything but letters and digits removed, for title matching."""
    return re.sub(r'[^0-9a-z]', '', (text or '').lower())


def find_teams_recording(meeting, recording):
    """``{'id', 'drive_id', 'name'}`` of this recording's file in the organiser's
    OneDrive ``Recordings`` folder, or None.

    Matched on the title (ignoring punctuation and spacing) and on the
    filename's timestamp lying within :data:`RECORDING_MATCH_WINDOW` of the time
    Graph gives the recording — so a recurring class's weekly recordings, which
    share a title, are told apart by when they were made. Teams names the file
    after the *series* ("MP PGDA P26F | MACF") even when one occurrence was
    renamed ("… MACF Debrief session"), so a file title that is the start of the
    meeting's title also matches. Failing any title match, the one recording in
    the time window is taken — the organiser records one meeting at a time.

    The stamp is read in the organiser's mailbox zone (``MS_TEAMS_RECORDING_TZ``,
    SAST by default); UTC is tried too so a mailbox set to UTC still matches.
    """
    from zoneinfo import ZoneInfo

    owner = meeting.organizer_upn
    drive_id = graph.get_drive_id(owner) if owner else None
    if not drive_id:
        return None
    folder = graph.get_item_by_path(drive_id, 'Recordings')
    if not folder or not folder.get('id'):
        return None

    raw = recording.get('createdDateTime') or recording.get('endDateTime') or ''
    try:
        made = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        made = meeting.scheduled_start
    if made is None:
        return None
    zones = (ZoneInfo(getattr(settings, 'MS_TEAMS_RECORDING_TZ', '') or 'Africa/Johannesburg'),
             dt.timezone.utc)
    title = _squash(meeting.title)

    best, best_gap, in_window = None, None, []
    for child in graph.list_children(drive_id, folder['id']):
        m = _RECORDING_NAME.match(child.get('name') or '')
        file_title = _squash(m.group('title')) if m else ''
        if not file_title:
            continue
        titled = title == file_title or title.startswith(file_title)
        try:
            naive = dt.datetime.strptime(m.group('stamp'), '%Y%m%d_%H%M%S')
        except ValueError:
            continue
        candidates = (dt.timezone.utc,) if m.group('utc') else zones
        gap = min(abs(naive.replace(tzinfo=zone) - made) for zone in candidates)
        if gap > RECORDING_MATCH_WINDOW:
            continue
        in_window.append(child)
        if titled and (best_gap is None or gap < best_gap):
            best, best_gap = child, gap
    if best is None and len(in_window) == 1:
        best = in_window[0]
    if best is None:
        logger.info('onedrive: no file in Recordings matches %s (%s)', meeting, raw)
        return None
    return {'id': best['id'], 'drive_id': drive_id, 'name': best.get('name', '')}


def ensure_subfolder(folder, name):
    """Ensure a named subfolder under a resolved session folder; return a folder dict.

    Used to drop a session's artifacts into the week's ``Recordings`` /
    ``Session summaries`` buckets (the tree ``provision_onedrive`` builds). Returns
    None on failure so the caller can fall back to the base folder.
    """
    if not folder:
        return None
    node = graph.create_folder(folder['drive_id'], folder['folder_id'], name)
    if not node or not node.get('id'):
        return None
    return {
        'drive_id': folder['drive_id'],
        'folder_id': node['id'],
        'path': f"{folder['path']}/{name}",
        'web_url': node.get('webUrl', ''),
        'owner_upn': folder['owner_upn'],
    }


def find_child(drive_id, folder_id, name):
    """The child of ``folder_id`` called ``name``, or None."""
    for child in graph.list_children(drive_id, folder_id):
        if child.get('name') == name:
            return child
    return None


def put_text(folder, name, text, content_type='text/plain'):
    """Write a small text file into the session folder. Returns the item or None."""
    if not text:
        return None
    return graph.upload_small_file(folder['drive_id'], folder['folder_id'], name, text, content_type)


def put_bytes(folder, name, blob, content_type='application/octet-stream'):
    """Write a small binary file into the session folder. Returns the item or None."""
    if not blob:
        return None
    return graph.upload_small_file(folder['drive_id'], folder['folder_id'], name, blob, content_type)


def anonymous_link(folder, item_id):
    """A view link a student with no Microsoft account can open, or ''."""
    if not item_id:
        return ''
    return graph.create_share_link(folder['drive_id'], item_id, link_type='view', scope='anonymous')


def _recording_item_ref(recording):
    """``(item_id, drive_id)`` for a Graph ``callRecording``.

    Graph reports the recording's location inconsistently between the beta and
    v1.0 shapes, so this checks each known spelling rather than assuming one.
    """
    if not recording:
        return None, None
    ref = recording.get('driveItem') or recording.get('recordingDriveItem') or {}
    item_id = ref.get('id') or recording.get('driveItemId') or ''
    drive_id = (ref.get('parentReference') or {}).get('driveId') or recording.get('driveId') or ''
    return (item_id or None), (drive_id or None)


def artifact_stem(meeting):
    """The base filename every artifact of this session shares.

    ``2026-03-04 Deferred tax workshop`` — every artifact adds a suffix to it.
    Public because the livesessions pipeline names its own uploads with it.
    """
    from apps.communication.models import _safe_folder_name
    from django.utils import timezone as tz
    when = meeting.scheduled_start or meeting.created_at or tz.now()
    return _safe_folder_name(f'{tz.localtime(when):%Y-%m-%d} {meeting.title}', limit=100)
