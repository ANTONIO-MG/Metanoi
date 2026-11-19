"""Thin Microsoft Graph client for the Teams live classroom.

App-only (client-credentials) auth: the LMS acts on behalf of licensed educators
without them signing in — so meetings are created straight from the LMS. Every
call is best-effort and returns ``None``/``[]`` rather than raising into a view;
the whole thing is **dormant** until ``MS_GRAPH_*`` is configured.

Requires an Entra app registration with *application* Graph permissions and a
Teams *application access policy* (see docs/TEAMS_INTEGRATION.md). ``msal`` is an
optional dependency — absent, the client simply reports "not configured".

``_refusals_logged`` remembers the standing refusals (and unresolvable users)
already logged by this process, so a configuration fault produces one log line
per process rather than one per call (see :func:`list_transcripts`).
``SIMPLE_UPLOAD_LIMIT`` is Graph's limit for a single-request upload (4 MB);
anything larger needs an upload session, whose fragments must be a multiple of
320 KiB — ``UPLOAD_CHUNK`` is 10 MiB, i.e. 32 × 320 KiB.
"""

import contextlib
import logging
import re
import threading
import time

from django.conf import settings

logger = logging.getLogger('apps')

_GRAPH_SCOPE = ['https://graph.microsoft.com/.default']
_token_cache = {'value': None, 'expires_at': 0}


def is_configured():
    """True only when Teams is switched on AND all Graph credentials are present."""
    return bool(
        getattr(settings, 'MS_TEAMS_ENABLED', False)
        and settings.MS_GRAPH_TENANT_ID
        and settings.MS_GRAPH_CLIENT_ID
        and settings.MS_GRAPH_CLIENT_SECRET
    )


def credentials_present():
    """True when the app registration's tenant, client id and secret are all set.

    Independent of ``MS_TEAMS_ENABLED``: the OneDrive database backup uses the
    same app registration but must not need Teams switched on.
    """
    return bool(settings.MS_GRAPH_TENANT_ID and settings.MS_GRAPH_CLIENT_ID
                and settings.MS_GRAPH_CLIENT_SECRET)


_without_teams = threading.local()


@contextlib.contextmanager
def without_teams_switch():
    """Let Graph calls inside this block run while ``MS_TEAMS_ENABLED`` is off.

    Only credentials are required inside it. Used by features that share the app
    registration but are not Teams — the off-site database backup — so that
    turning the live classroom on or off never switches backups on or off too.
    """
    previous = getattr(_without_teams, 'on', False)
    _without_teams.on = True
    try:
        yield
    finally:
        _without_teams.on = previous


def _token_allowed():
    """Whether a token may be fetched: full Teams configuration, or just credentials inside :func:`without_teams_switch`."""
    if getattr(_without_teams, 'on', False):
        return credentials_present()
    return is_configured()


def _msal_app():
    """The MSAL confidential client for the app registration, or ``None`` when ``msal`` is not installed."""
    try:
        import msal
    except Exception:  # pragma: no cover - optional dependency
        logger.warning('msteams: msal is not installed; Teams integration is off')
        return None
    authority = f"{settings.MS_GRAPH_AUTHORITY.rstrip('/')}/{settings.MS_GRAPH_TENANT_ID}"
    return msal.ConfidentialClientApplication(
        client_id=settings.MS_GRAPH_CLIENT_ID,
        client_credential=settings.MS_GRAPH_CLIENT_SECRET,
        authority=authority,
    )


def _get_token():
    """A cached app-only bearer token (client credentials), or None."""
    if not _token_allowed():
        return None
    now = time.time()
    if _token_cache['value'] and _token_cache['expires_at'] - 60 > now:
        return _token_cache['value']
    app = _msal_app()
    if app is None:
        return None
    try:
        result = app.acquire_token_for_client(scopes=_GRAPH_SCOPE)
    except Exception:  # pragma: no cover
        logger.exception('msteams: token acquisition failed')
        return None
    token = result.get('access_token') if result else None
    if not token:
        logger.error('msteams: no access_token (%s)', (result or {}).get('error_description', 'unknown'))
        return None
    _token_cache['value'] = token
    _token_cache['expires_at'] = now + int((result or {}).get('expires_in', 3600))
    return token


def _request(method, path, *, json=None, params=None, expected=()):
    """Call Graph. ``path`` is relative to MS_GRAPH_BASE (or absolute). Returns the
    parsed JSON dict, or None on any failure (logged, never raised)."""
    return _request_ex(method, path, json=json, params=params, expected=expected)[1]


def _request_ex(method, path, *, json=None, params=None, expected=()):
    """As :func:`_request`, but returns ``(status_code, data)``.

    ``expected`` lists error statuses that are a normal answer for this call
    (409 from an idempotent create, 404 "no report yet"); they log at INFO.

    ``status_code`` is 0 when the call never reached Graph. Callers use it to
    tell "Graph refused" from "nothing here" — a refusal read as emptiness is
    one a pipeline would wait on forever.

    A 401 clears the cached token so the next call fetches a fresh one.
    """
    token = _get_token()
    if not token:
        return 0, None
    import requests
    url = path if path.startswith('http') else f"{settings.MS_GRAPH_BASE.rstrip('/')}/{path.lstrip('/')}"
    try:
        resp = requests.request(
            method, url, params=params, json=json, timeout=30,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        )
    except Exception:  # pragma: no cover
        logger.exception('msteams: request error %s %s', method, url)
        return 0, None
    if resp.status_code == 401:
        _token_cache['value'] = None
    if resp.status_code >= 400:
        log = logger.info if resp.status_code in expected else logger.error
        log('msteams: %s %s → %s %s', method, url, resp.status_code, resp.text[:300])
        return resp.status_code, None
    if resp.status_code == 204 or not resp.content:
        return resp.status_code, {}
    try:
        return resp.status_code, resp.json()
    except ValueError:  # pragma: no cover
        return resp.status_code, {}


_user_id_cache = {}
_refusals_logged = {}

_OBJECT_ID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def resolve_user_id(upn_or_id):
    """Return the Entra **object id** for a UPN (or pass a GUID straight through).

    The ``/onlineMeetings`` family is the one place Graph refuses a UPN — it
    answers 400 "The userId in request URL is not a valid GUID". ``/events`` and
    ``/drive`` accept either, so this lookup is applied only where it's required
    rather than to every call.

    Returns None when the UPN doesn't resolve. The mapping is cached for the life
    of the process: a user's object id never changes. An unresolvable UPN is a
    data problem (an account with no Microsoft identity, e.g. a demo
    educator), so it is warned about once per process, not once per call.
    """
    if not upn_or_id:
        return None
    key = str(upn_or_id).strip()
    if _OBJECT_ID_RE.match(key):
        return key
    cached = _user_id_cache.get(key.lower())
    if cached:
        return cached
    data = _request('GET', f'/users/{key}', params={'$select': 'id'}, expected=(404,))
    user_id = (data or {}).get('id')
    if not user_id:
        if not _refusals_logged.get(('user', key.lower())):
            _refusals_logged[('user', key.lower())] = True
            logger.warning('msteams: cannot resolve %r to a Microsoft object id — set the '
                           "person's Microsoft account (ms_upn) or give them a licence", key)
        return None
    _user_id_cache[key.lower()] = user_id
    return user_id


def create_online_meeting(organizer_upn, *, subject, start_iso, end_iso, allow_recording=True):
    """Create a Teams meeting owned by ``organizer_upn`` (their UPN / work e-mail).

    Returns ``{'id', 'join_url'}`` or None. Anonymous attendees are allowed
    (students join by link); the organizer is the sole presenter by default.

    ``recordAutomatically`` makes the class record itself when the host joins,
    which the recording pipeline relies on.

    ``subject`` is the title Teams shows in the call, in the organiser's
    calendar, and on the recording it files in OneDrive — which is what the
    after-class pipeline matches on.
    """
    if not is_configured():
        return None
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return None
    body = {
        'startDateTime': start_iso,
        'endDateTime': end_iso,
        'subject': subject,
        'allowedPresenters': 'organizer',
        'allowAttendeeToEnableCamera': True,
        'allowAttendeeToEnableMic': True,
        'isEntryExitAnnounced': False,
        'lobbyBypassSettings': {'scope': 'everyone', 'isDialInBypassEnabled': True},
        'recordAutomatically': bool(allow_recording),
    }
    data = _request('POST', f'/users/{organizer_id}/onlineMeetings', json=body)
    if not data or not data.get('id'):
        return None
    return {'id': data['id'], 'join_url': data.get('joinWebUrl', '')}


def update_online_meeting(organizer_upn, meeting_id, **fields):
    """PATCH an existing meeting (times, subject, recording flag). None on failure."""
    if not is_configured() or not fields:
        return None
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return None
    return _request('PATCH', f'/users/{organizer_id}/onlineMeetings/{meeting_id}', json=fields)


def delete_online_meeting(organizer_upn, meeting_id):
    """Delete a Teams meeting. ``None`` on failure."""
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return None
    return _request('DELETE', f'/users/{organizer_id}/onlineMeetings/{meeting_id}')


def graph_datetime(moment):
    """A ``dateTimeTimeZone`` dict for Graph *events*, in UTC.

    ``/events`` reads ``dateTime`` as a wall-clock time in ``timeZone`` and does
    not honour an offset written into the string, so ``14:00+02:00`` sent with
    ``timeZone: UTC`` lands at 14:00 UTC — two hours late in South Africa. The
    instant is therefore converted to UTC first and written without an offset.
    Accepts an aware/naive datetime or an ISO string.
    """
    import datetime as _dt
    from django.utils import timezone as _tz
    if isinstance(moment, str):
        moment = _dt.datetime.fromisoformat(moment.replace('Z', '+00:00'))
    if _tz.is_naive(moment):
        moment = _tz.make_aware(moment, _tz.get_current_timezone())
    utc = moment.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return {'dateTime': utc.strftime('%Y-%m-%dT%H:%M:%S'), 'timeZone': 'UTC'}


def event_attendees(attendees):
    """``[(upn, name), …]`` → Graph ``attendees`` (blank addresses dropped, de-duplicated)."""
    seen, out = set(), []
    for upn, name in attendees or ():
        key = (upn or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({'emailAddress': {'address': upn.strip(), 'name': name or upn.strip()},
                    'type': 'required'})
    return out


def teams_location(join_url):
    """The event's location: the Teams link, so Outlook/Teams show it as the place."""
    return {'displayName': 'Microsoft Teams meeting', 'locationType': 'default',
            'locationUri': join_url or ''}


def create_calendar_event(organizer_upn, *, subject, start_iso, end_iso, body_html='',
                          attendees=(), join_url='', time_zone='UTC'):
    """Put the session on ``organizer_upn``'s Outlook/Teams calendar and invite staff.

    Exchange then places the event in every attendee's calendar too (the normal
    meeting invitation), so it shows in their Outlook and their Teams calendar.

    Students are deliberately **not** attendees: they have no Microsoft account,
    so invitations would go to addresses the tenant cannot resolve.

    The event points at the meeting from ``create_online_meeting`` (join link in
    location and body) and is **not** flagged ``isOnlineMeeting``: with that
    flag Graph mints a second, unrelated Teams meeting (``onlineMeeting`` is
    read-only on an event), so staff and students would join different calls
    and only one would record.

    ``start_iso``/``end_iso`` may carry any offset; they are converted to UTC.
    Returns the created event dict (``id`` is what to store), or None.
    """
    if not is_configured():
        return None
    event = {
        'subject': subject,
        'body': {'contentType': 'HTML', 'content': body_html or ''},
        'start': graph_datetime(start_iso),
        'end': graph_datetime(end_iso),
        'attendees': event_attendees(attendees),
        'isOnlineMeeting': False,
        'allowNewTimeProposals': False,
        'responseRequested': True,
        'isReminderOn': True,
        'reminderMinutesBeforeStart': 15,
    }
    if join_url:
        event['location'] = teams_location(join_url)
    return _request('POST', f'/users/{organizer_upn}/events', json=event)


def update_calendar_event(organizer_upn, event_id, **fields):
    """PATCH an event. Exchange sends the attendees an updated invitation."""
    if not is_configured() or not event_id or not fields:
        return None
    return _request('PATCH', f'/users/{organizer_upn}/events/{event_id}', json=fields)


def cancel_calendar_event(organizer_upn, event_id, comment=''):
    """Cancel an event as its organiser, telling every attendee (with ``comment``).

    ``POST /events/{id}/cancel`` sends the cancellation notice and removes the
    event from the organiser's calendar. If Graph refuses it (an event with no
    attendees answers 400 on some tenants) the event is deleted instead, which
    also clears it from the calendar. Returns True when the event is gone.
    """
    if not is_configured() or not event_id:
        return False
    status, _data = _request_ex('POST', f'/users/{organizer_upn}/events/{event_id}/cancel',
                                json={'comment': (comment or '')[:1000]}, expected=(400, 404))
    if 200 <= status < 300 or status == 404:
        return True
    status, _data = _request_ex('DELETE', f'/users/{organizer_upn}/events/{event_id}',
                                expected=(404,))
    return 200 <= status < 300 or status == 404


def get_drive_id(owner_upn):
    """The id of ``owner_upn``'s OneDrive, or None."""
    data = _request('GET', f'/users/{owner_upn}/drive')
    return (data or {}).get('id')


def download_url(drive_id, item_id):
    """A pre-authenticated, short-lived (about an hour) URL for the file's bytes.

    It needs no Microsoft sign-in, honours Range requests (so a video can be
    seeked), and expires on its own — which is what lets the platform play a
    recording to a student without ever making the file public.
    """
    item = _request('GET', f'/drives/{drive_id}/items/{item_id}', expected=(404,))
    return (item or {}).get('@microsoft.graph.downloadUrl') or ''


def get_item_by_path(drive_id, path):
    """Look a drive item up by its path relative to the drive root. None if absent."""
    if not path:
        return _request('GET', f'/drives/{drive_id}/root')
    return _request('GET', f'/drives/{drive_id}/root:/{_quote_path(path)}', expected=(404,))


def list_children(drive_id, parent_item_id):
    """The immediate children of a folder, every page of them. Always a list."""
    out, path = [], f'/drives/{drive_id}/items/{parent_item_id}/children'
    params = {'$select': 'id,name,size,webUrl,file,folder,createdDateTime', '$top': '200'}
    for _page in range(50):
        data = _request('GET', path, params=params)
        if not data:
            break
        out.extend(data.get('value', []) or [])
        path, params = data.get('@odata.nextLink'), None
        if not path:
            break
    return out


def create_folder(drive_id, parent_item_id, name):
    """Create ``name`` under ``parent_item_id``; return the existing one if it is there.

    ``conflictBehavior: replace`` would delete a folder that already holds last
    week's recording, so this fails on conflict and reads the existing child
    back — idempotent across pipeline retries.
    """
    body = {'name': name, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'}
    created = _request('POST', f'/drives/{drive_id}/items/{parent_item_id}/children', json=body,
                       expected=(409,))
    if created and created.get('id'):
        return created
    listing = _request('GET', f'/drives/{drive_id}/items/{parent_item_id}/children',
                       params={'$filter': f"name eq '{str(name).replace(chr(39), chr(39) * 2)}'",
                               '$select': 'id,name,folder,webUrl'})
    for child in (listing or {}).get('value', []):
        if child.get('name') == name and 'folder' in child:
            return child
    return None


def move_item(drive_id, item_id, *, parent_item_id, new_name=None):
    """Move (and optionally rename) a drive item. Returns the updated item or None."""
    body = {'parentReference': {'id': parent_item_id},
            '@microsoft.graph.conflictBehavior': 'rename'}
    if new_name:
        body['name'] = new_name
    return _request('PATCH', f'/drives/{drive_id}/items/{item_id}', json=body)


def copy_item(drive_id, item_id, *, parent_item_id, new_name=None):
    """Server-side copy. Graph does this asynchronously and returns 202, so this
    reports only whether the copy was *accepted*, not that it has finished."""
    body = {'parentReference': {'driveId': drive_id, 'id': parent_item_id}}
    if new_name:
        body['name'] = new_name
    return _request('POST', f'/drives/{drive_id}/items/{item_id}/copy', json=body) is not None


def upload_small_file(drive_id, parent_item_id, name, content, content_type='text/plain'):
    """Upload up to ~4 MB in one PUT. Returns the drive item, or None.

    Used for the transcript, the summary and the thumbnail — all small. The
    recording is never uploaded: it is already in OneDrive, and gets *moved*.
    """
    token = _get_token()
    if not token:
        return None
    import requests
    if isinstance(content, str):
        content = content.encode('utf-8')
    url = (f"{settings.MS_GRAPH_BASE.rstrip('/')}/drives/{drive_id}/items/"
           f"{parent_item_id}:/{_quote_path(name)}:/content")
    try:
        resp = requests.put(url, data=content, timeout=120,
                            headers={'Authorization': f'Bearer {token}',
                                     'Content-Type': content_type})
    except Exception:  # pragma: no cover
        logger.exception('msteams: upload failed for %s', name)
        return None
    if resp.status_code >= 400:
        logger.error('msteams: upload %s → %s %s', name, resp.status_code, resp.text[:300])
        return None
    try:
        return resp.json()
    except ValueError:  # pragma: no cover
        return None


SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
UPLOAD_CHUNK = 32 * 320 * 1024


def create_upload_session(drive_id, parent_item_id, name, *, conflict='replace'):
    """Open a resumable upload session for ``name`` in a folder; its ``uploadUrl`` or ''."""
    body = {'item': {'@microsoft.graph.conflictBehavior': conflict, 'name': name}}
    data = _request('POST', f'/drives/{drive_id}/items/{parent_item_id}:/{_quote_path(name)}:'
                            f'/createUploadSession', json=body)
    return (data or {}).get('uploadUrl', '')


def upload_large_file(drive_id, parent_item_id, name, path, *, chunk_size=UPLOAD_CHUNK):
    """Upload a file from disk through a resumable upload session. Drive item, or None.

    Used for database dumps, which are far past the 4 MB single-PUT limit. The
    file is read and sent one fragment at a time — never held in memory whole.
    The ``uploadUrl`` is pre-authenticated, so the fragments carry no bearer
    token (Graph rejects one there). A fragment that fails with a 5xx or a
    network error is retried twice; a session that still fails is cancelled so
    it does not linger in the drive.
    """
    import os

    import requests

    size = os.path.getsize(path)
    if size == 0:
        return None
    upload_url = create_upload_session(drive_id, parent_item_id, name)
    if not upload_url:
        logger.error('msteams: could not open an upload session for %s', name)
        return None
    item = None
    try:
        with open(path, 'rb') as fh:
            offset = 0
            while offset < size:
                blob = fh.read(chunk_size)
                if not blob:
                    break
                end = offset + len(blob) - 1
                headers = {'Content-Length': str(len(blob)),
                           'Content-Range': f'bytes {offset}-{end}/{size}'}
                resp = None
                for _attempt in range(3):
                    try:
                        resp = requests.put(upload_url, data=blob, headers=headers, timeout=300)
                    except Exception:
                        resp = None
                        continue
                    if resp.status_code < 500:
                        break
                if resp is None or resp.status_code >= 400:
                    logger.error('msteams: upload of %s failed at byte %s → %s %s', name, offset,
                                 getattr(resp, 'status_code', 'no answer'),
                                 (getattr(resp, 'text', '') or '')[:300])
                    _cancel_upload(upload_url)
                    return None
                if resp.status_code in (200, 201):
                    try:
                        item = resp.json()
                    except ValueError:  # pragma: no cover
                        item = {}
                offset += len(blob)
    except OSError:
        logger.exception('msteams: could not read %s for upload', path)
        _cancel_upload(upload_url)
        return None
    if item is None:
        logger.error('msteams: upload of %s ended without Graph confirming the file', name)
    return item


def _cancel_upload(upload_url):
    """Cancel an upload session so it does not linger in the drive; errors are ignored."""
    try:
        import requests
        requests.delete(upload_url, timeout=30)
    except Exception:   # pragma: no cover
        pass


def upload_file(drive_id, parent_item_id, name, path, content_type='application/octet-stream'):
    """Upload a file from disk: one PUT up to 4 MB, an upload session above that."""
    import os
    if os.path.getsize(path) <= SIMPLE_UPLOAD_LIMIT:
        with open(path, 'rb') as fh:
            return upload_small_file(drive_id, parent_item_id, name, fh.read(), content_type)
    return upload_large_file(drive_id, parent_item_id, name, path)


def delete_item(drive_id, item_id):
    """Delete a drive item (into the drive's recycle bin). True when it is gone."""
    status, _data = _request_ex('DELETE', f'/drives/{drive_id}/items/{item_id}', expected=(404,))
    return 200 <= status < 300 or status == 404


def ensure_folder_path(drive_id, path):
    """Create (or find) every folder along ``path`` from the drive root; the last one, or None."""
    node = get_item_by_path(drive_id, '')
    for part in [p.strip() for p in str(path or '').split('/') if p.strip()]:
        if not node or not node.get('id'):
            return None
        node = create_folder(drive_id, node['id'], part)
    return node if node and node.get('id') else None


def create_share_link(drive_id, item_id, *, link_type='view', scope='anonymous'):
    """Mint a sharing link for a drive item and return its URL, or ''.

    ``scope='anonymous'`` is what lets a student who has no Microsoft account
    watch the recording. Tenants can forbid anonymous links; when they do, Graph
    returns 403 and the caller falls back to the organisation-scoped link.
    """
    body = {'type': link_type, 'scope': scope}
    data = _request('POST', f'/drives/{drive_id}/items/{item_id}/createLink', json=body)
    if not data and scope == 'anonymous':
        data = _request('POST', f'/drives/{drive_id}/items/{item_id}/createLink',
                        json={'type': link_type, 'scope': 'organization'})
    return ((data or {}).get('link') or {}).get('webUrl', '')


def list_permissions(drive_id, item_id):
    """The sharing permissions on a drive item (owner, links, grants), or [] on failure.

    Read only. Used to prove a private folder — the database backups — has no
    sharing link and nobody but its owner on it.
    """
    data = _request('GET', f'/drives/{drive_id}/items/{item_id}/permissions')
    return (data or {}).get('value') or []


def download_item(drive_id, item_id):
    """Fetch a drive item's bytes. Returns ``bytes`` or None.

    Only used when something has to leave Microsoft — the YouTube upload. Files
    can be gigabytes, so this streams into memory in chunks rather than letting
    ``requests`` buffer the whole body twice.
    """
    token = _get_token()
    if not token:
        return None
    import requests
    url = f"{settings.MS_GRAPH_BASE.rstrip('/')}/drives/{drive_id}/items/{item_id}/content"
    try:
        with requests.get(url, stream=True, timeout=(30, 600),
                          headers={'Authorization': f'Bearer {token}'}) as resp:
            if resp.status_code >= 400:
                logger.error('msteams: download %s → %s', item_id, resp.status_code)
                return None
            chunks = bytearray()
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    chunks.extend(chunk)
            return bytes(chunks)
    except Exception:  # pragma: no cover
        logger.exception('msteams: download error for %s', item_id)
        return None


def _quote_path(path):
    """Percent-encode a drive path, keeping the ``/`` separators intact."""
    from urllib.parse import quote
    return quote(str(path).strip('/'), safe='/')


def list_transcripts(organizer_upn, meeting_id):
    """Transcripts Teams has published for a meeting; ``[]`` when there are none.

    A tenant without Graph transcript access answers 403 for *every* meeting,
    permanently. That is logged as a configuration failure, since the pipelines
    read an empty list as "not landed yet" and would otherwise wait forever.
    """
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return []
    status, data = _request_ex(
        'GET', f'/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts', expected=(403,))
    if data is None:
        if status == 403:
            if _refusals_logged.get('transcripts'):
                return []
            _refusals_logged['transcripts'] = True
            logger.error(
                'msteams: Graph transcript access is disabled for this tenant — no session '
                'will produce a transcript or an AI recap until an administrator enables it '
                '(Teams admin centre → Meetings → Meeting settings → Transcript API access, '
                'or Set-CsTeamsMeetingConfiguration -Identity Global '
                '-EnableGraphTranscriptAccess $true). Meeting %s.', meeting_id)
        else:
            logger.error('msteams: could not list transcripts for meeting %s (HTTP %s)',
                         meeting_id, status)
        return []
    return data.get('value', []) or []


def get_transcript_content(organizer_upn, meeting_id, transcript_id, fmt='text/vtt'):
    """Raw transcript content (VTT). Returns text or None."""
    token = _get_token()
    if not token:
        return None
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return None
    import requests
    url = (f"{settings.MS_GRAPH_BASE.rstrip('/')}"
           f"/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content")
    try:
        resp = requests.get(url, params={'$format': fmt}, timeout=60,
                            headers={'Authorization': f'Bearer {token}'})
    except Exception:  # pragma: no cover
        logger.exception('msteams: transcript content error')
        return None
    if resp.status_code >= 400:
        logger.error('msteams: transcript content → %s', resp.status_code)
        return None
    return resp.text


def list_recordings(organizer_upn, meeting_id):
    """Recordings Teams has published for a meeting; ``[]`` when there are none."""
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return []
    data = _request('GET', f'/users/{organizer_id}/onlineMeetings/{meeting_id}/recordings')
    return (data or {}).get('value', []) or []


def list_attendance_reports(organizer_upn, meeting_id):
    """Attendance reports for a meeting; ``[]`` (a 404 is normal) when nobody joined or none is published yet."""
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return []
    data = _request('GET', f'/users/{organizer_id}/onlineMeetings/{meeting_id}/attendanceReports',
                    expected=(404,))
    return (data or {}).get('value', []) or []


def get_attendance_records(organizer_upn, meeting_id, report_id):
    """The attendance records (who joined, for how long) in one attendance report."""
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return []
    data = _request(
        'GET',
        f'/users/{organizer_id}/onlineMeetings/{meeting_id}/attendanceReports/{report_id}/attendanceRecords',
    )
    return (data or {}).get('value', []) or []


def calendar_view(organizer_upn, start_iso, end_iso, *, select='id,subject,start,end,isOnlineMeeting,onlineMeeting,isCancelled,type,seriesMasterId'):
    """Every calendar occurrence between two instants (recurring series expanded).

    Follows ``@odata.nextLink`` so a busy month is read in full. ``[]`` on failure.
    """
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return []
    out, path = [], f'/users/{organizer_id}/calendarView'
    params = {'startDateTime': start_iso, 'endDateTime': end_iso, '$top': '100', '$select': select}
    for _page in range(20):
        status, data = _request_ex('GET', path, params=params)
        if data is None:
            logger.error('msteams: calendarView failed (HTTP %s)', status)
            break
        out.extend(data.get('value', []) or [])
        path, params = data.get('@odata.nextLink'), None
        if not path:
            break
    return out


def find_online_meeting(organizer_upn, join_url):
    """The ``onlineMeeting`` behind a calendar event's Teams link, or None.

    Needs the organiser's *object id*: the same filter against their UPN is
    refused with a 400.
    """
    organizer_id = resolve_user_id(organizer_upn)
    if not (organizer_id and join_url):
        return None
    safe = join_url.replace("'", "''")
    status, data = _request_ex('GET', f'/users/{organizer_id}/onlineMeetings',
                               params={'$filter': f"JoinWebUrl eq '{safe}'"})
    rows = (data or {}).get('value') or []
    return rows[0] if rows else None


def transcripts_with_status(organizer_upn, meeting_id):
    """``(status, transcripts)`` — so a caller can tell "none yet" from "refused".

    A 403 is expected here; the caller records why (``summary_note``).
    """
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return 0, []
    status, data = _request_ex('GET', f'/users/{organizer_id}/onlineMeetings/{meeting_id}/transcripts',
                               expected=(403,))
    return status, ((data or {}).get('value') or []) if data is not None else []


def ai_insights_with_status(organizer_upn, meeting_id):
    """``(status, insights)`` — Teams' own AI recap of a meeting.

    Microsoft 365 Copilot's meeting insights: meeting notes with sub-points,
    action items with owners, and per-person mentions. Needs the application
    permission ``OnlineMeetingAiInsight.Read.All`` and a Copilot licence on the
    organiser; without them this answers 403, a known and standing answer.
    """
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return 0, []
    status, data = _request_ex(
        'GET', f'/copilot/users/{organizer_id}/onlineMeetings/{meeting_id}/aiInsights',
        expected=(403,))
    return status, ((data or {}).get('value') or []) if data is not None else []


def get_ai_insight(organizer_upn, meeting_id, insight_id):
    """One Copilot AI insight (meeting notes, action items, mentions), or ``None``."""
    organizer_id = resolve_user_id(organizer_upn)
    if not organizer_id:
        return None
    return _request('GET', f'/copilot/users/{organizer_id}/onlineMeetings/{meeting_id}/aiInsights/{insight_id}')
