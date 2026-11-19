"""Unlisted YouTube uploads — the only player a non-tenant student can rely on.

**The quota is the design constraint.** A YouTube Data API project gets 10,000
units a day by default and ``videos.insert`` costs **1,600** — six uploads, then
every attempt fails until midnight Pacific. So callers *ask* whether there is
room (:func:`quota_available`) and leave the session queued if not. Raising the
quota needs a Google audit, so the pacing is permanent.

Dormant unless ``YOUTUBE_CLIENT_ID`` / ``YOUTUBE_CLIENT_SECRET`` /
``YOUTUBE_REFRESH_TOKEN`` are set *and* the settings row has YouTube switched on.

Constants:

* ``UPLOAD_COST`` (1,600) -- what ``videos.insert`` costs against the daily quota.
* ``THUMBNAIL_COST`` (50) -- what ``thumbnails.set`` costs.
* ``PLAYLIST_ITEM_COST`` (50) -- what ``playlistItems.insert`` costs; every
  backup is filed into a playlist.
* ``PLAYLIST_INSERT_COST`` (50) -- what ``playlists.insert`` costs, once per
  new institution/programme playlist.
* ``BACKUP_COST`` -- quota for one backup: the upload plus filing it into its
  playlist. Six of these (9,900 units) fit the default 10,000 a day, which is
  why the nightly run stops at six.
* ``QUOTA_RESET_TZ`` -- YouTube quota resets at midnight US/Pacific, not UTC
  and not local time.
* ``SCOPES`` -- the one OAuth scope the refresh token is minted with
  (``deploy/mint_youtube_token.py``). ``youtube.force-ssl`` covers uploading,
  reading the channel's unlisted videos and creating / filling playlists.
  Every client asks for exactly this scope: asking a refresh for a scope the
  token was not granted fails with ``invalid_scope``.
* ``BACKUP_PLAYLIST_MARK`` -- the first line of every playlist description the
  backup creates. It marks the playlist as ours (for quota accounting, and for
  finding it again) and keeps the scanner's library from mistaking a backup
  playlist for teaching content.
"""

import datetime as dt
import io
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('apps')

UPLOAD_COST = 1600
THUMBNAIL_COST = 50
PLAYLIST_ITEM_COST = 50
PLAYLIST_INSERT_COST = 50
BACKUP_COST = UPLOAD_COST + PLAYLIST_ITEM_COST
QUOTA_RESET_TZ = 'America/Los_Angeles'
SCOPES = ['https://www.googleapis.com/auth/youtube.force-ssl']


def is_configured():
    """True when the OAuth credentials for a YouTube channel are all present."""
    return bool(
        getattr(settings, 'YOUTUBE_CLIENT_ID', '')
        and getattr(settings, 'YOUTUBE_CLIENT_SECRET', '')
        and getattr(settings, 'YOUTUBE_REFRESH_TOKEN', '')
    )


def enabled():
    """True when uploads should actually be attempted."""
    from .models import LiveSessionSettings
    return bool(LiveSessionSettings.load().youtube_enabled and is_configured())


def quota_window_start(now=None):
    """The start of the current quota day, in UTC.

    Google resets quota at midnight Pacific; counting from local midnight would
    misattribute a 22:00 SAST upload and cause a confusing ``quotaExceeded``.
    Without tzdata it falls back to UTC days.
    """
    now = now or timezone.now()
    try:
        from zoneinfo import ZoneInfo
        pacific = now.astimezone(ZoneInfo(QUOTA_RESET_TZ))
        midnight = pacific.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(dt.timezone.utc)
    except Exception:       # pragma: no cover - no tzdata → fall back to UTC days
        return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(now=None):
    """Quota units this platform has spent since the last reset: each backup
    (upload + playlist filing) and each backup playlist it had to create."""
    from .models import SessionArtifact, YouTubePlaylist
    since = quota_window_start(now)
    uploads = SessionArtifact.objects.filter(youtube_uploaded_at__gte=since).count()
    playlists = YouTubePlaylist.objects.filter(
        created_at__gte=since, description__startswith=BACKUP_PLAYLIST_MARK).count()
    return uploads * BACKUP_COST + playlists * PLAYLIST_INSERT_COST


def daily_cap():
    """The configured daily quota in units (10,000 when unset)."""
    from .models import LiveSessionSettings
    return LiveSessionSettings.load().youtube_daily_quota or 10000


def quota_left(now=None):
    """Units left before the next reset (never negative)."""
    return max(0, daily_cap() - spent_today(now))


def quota_available(now=None):
    """True when there is room for one more backup before the next reset."""
    return quota_left(now) >= BACKUP_COST


def uploads_remaining_today(now=None):
    """How many more sessions can be backed up before the quota resets."""
    return quota_left(now) // BACKUP_COST


def _client():
    """An authenticated YouTube Data API client, or None.

    ``google-api-python-client`` is an optional dependency: without it (or
    without credentials) the whole module reports "not configured" and the
    pipeline simply publishes the OneDrive link instead.
    """
    if not is_configured():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except Exception:
        logger.warning('youtube: google-api-python-client is not installed — uploads are off')
        return None
    try:
        creds = Credentials(
            None,
            refresh_token=settings.YOUTUBE_REFRESH_TOKEN,
            client_id=settings.YOUTUBE_CLIENT_ID,
            client_secret=settings.YOUTUBE_CLIENT_SECRET,
            token_uri='https://oauth2.googleapis.com/token',
            scopes=SCOPES,
        )
        return build('youtube', 'v3', credentials=creds, cache_discovery=False)
    except Exception:
        logger.exception('youtube: could not build the API client')
        return None


def can_scan():
    """True when the channel can be read — owner OAuth or a public API key."""
    return bool(is_configured() or getattr(settings, 'YOUTUBE_API_KEY', ''))


def _read_client():
    """A YouTube Data API client for READING playlists/videos, or None.

    Prefers the owner OAuth credentials (so UNLISTED past-session videos are
    visible — see :data:`SCOPES`), and
    falls back to a public ``YOUTUBE_API_KEY`` (public content only).
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except Exception:
        logger.warning('youtube: google-api-python-client is not installed — scanning is off')
        return None
    if is_configured():
        try:
            creds = Credentials(
                None,
                refresh_token=settings.YOUTUBE_REFRESH_TOKEN,
                client_id=settings.YOUTUBE_CLIENT_ID,
                client_secret=settings.YOUTUBE_CLIENT_SECRET,
                token_uri='https://oauth2.googleapis.com/token',
                scopes=SCOPES,
            )
            return build('youtube', 'v3', credentials=creds, cache_discovery=False)
        except Exception:
            logger.exception('youtube: read client (OAuth) could not be built')
    key = getattr(settings, 'YOUTUBE_API_KEY', '')
    if key:
        try:
            return build('youtube', 'v3', developerKey=key, cache_discovery=False)
        except Exception:
            logger.exception('youtube: read client (API key) could not be built')
    return None


def _thumb(snippet):
    """The best available thumbnail URL from a YouTube ``snippet``, or ''."""
    thumbs = (snippet or {}).get('thumbnails') or {}
    for size in ('medium', 'high', 'standard', 'default', 'maxres'):
        if size in thumbs:
            return thumbs[size].get('url', '')
    return ''


def resolve_channel_id(client=None):
    """The channel to scan: the configured ``YOUTUBE_CHANNEL_ID``, else the
    OAuth owner's own channel (channels.list mine=True)."""
    cid = getattr(settings, 'YOUTUBE_CHANNEL_ID', '')
    if cid:
        return cid
    client = client or _read_client()
    if client is None or not is_configured():
        return ''
    try:
        resp = client.channels().list(part='id', mine=True).execute()
        items = resp.get('items') or []
        return items[0]['id'] if items else ''
    except Exception:
        logger.exception('youtube: could not resolve the owner channel id')
        return ''


def list_playlists(client=None):
    """Every playlist on the channel: ``[{id, title, description, count, thumbnail}]``."""
    client = client or _read_client()
    if client is None:
        return []
    cid = resolve_channel_id(client)
    if not cid:
        return []
    out, token = [], None
    try:
        while True:
            resp = client.playlists().list(
                part='snippet,contentDetails', channelId=cid, maxResults=50, pageToken=token).execute()
            for it in resp.get('items', []):
                sn = it.get('snippet', {})
                out.append({
                    'id': it['id'],
                    'title': sn.get('title', ''),
                    'description': sn.get('description', ''),
                    'count': (it.get('contentDetails') or {}).get('itemCount', 0),
                    'thumbnail': _thumb(sn),
                })
            token = resp.get('nextPageToken')
            if not token:
                break
    except Exception:
        logger.exception('youtube: listing playlists failed')
    return out


def list_playlist_videos(playlist_id, client=None):
    """Videos in a playlist: ``[{video_id, title, description, published_at, thumbnail, position}]``."""
    client = client or _read_client()
    if client is None:
        return []
    out, token = [], None
    try:
        while True:
            resp = client.playlistItems().list(
                part='snippet,contentDetails', playlistId=playlist_id,
                maxResults=50, pageToken=token).execute()
            for it in resp.get('items', []):
                sn = it.get('snippet', {})
                cd = it.get('contentDetails', {})
                vid = cd.get('videoId') or (sn.get('resourceId') or {}).get('videoId', '')
                if not vid:
                    continue
                out.append({
                    'video_id': vid,
                    'title': sn.get('title', ''),
                    'description': sn.get('description', ''),
                    'published_at': cd.get('videoPublishedAt') or sn.get('publishedAt', ''),
                    'thumbnail': _thumb(sn),
                    'position': sn.get('position', 0),
                })
            token = resp.get('nextPageToken')
            if not token:
                break
    except Exception:
        logger.exception('youtube: listing playlist %s failed', playlist_id)
    return out


def scan_channel(client=None):
    """One structured read of the channel for the importer: every playlist with
    its videos. ``[{id, title, description, count, thumbnail, videos: [...]}]``.
    """
    client = client or _read_client()
    if client is None:
        return []
    playlists = list_playlists(client)
    for pl in playlists:
        pl['videos'] = list_playlist_videos(pl['id'], client)
    return playlists


def upload(*, video_bytes, title, description, privacy='unlisted', thumbnail_bytes=None,
           tags=(), recorded_at=None):
    """Upload one video. Returns ``(video_id, error)`` — exactly one is truthy.

    The upload is resumable, so a dropped connection part-way through a
    multi-gigabyte recording resumes rather than restarting. The title is cut
    to YouTube's hard limit of 100 characters, and the video is filed under
    category 27 (Education).
    """
    if not video_bytes:
        return '', 'no video content'
    client = _client()
    if client is None:
        return '', 'YouTube is not configured'

    try:
        from googleapiclient.http import MediaIoBaseUpload
    except Exception:
        return '', 'google-api-python-client is not installed'

    body = {
        'snippet': {
            'title': (title or 'Live session')[:100],
            'description': (description or '')[:5000],
            'tags': [str(tag)[:30] for tag in tags][:15],
            'categoryId': '27',
        },
        'status': {
            'privacyStatus': privacy,
            'selfDeclaredMadeForKids': False,
            'embeddable': True,
        },
    }
    if recorded_at:
        body['snippet']['publishedAt'] = recorded_at.isoformat()

    media = MediaIoBaseUpload(io.BytesIO(video_bytes), mimetype='video/mp4',
                              chunksize=8 * 1024 * 1024, resumable=True)
    try:
        request = client.videos().insert(part='snippet,status', body=body, media_body=media)
        response = None
        while response is None:
            _status, response = request.next_chunk()
        video_id = (response or {}).get('id', '')
    except Exception as exc:
        logger.exception('youtube: upload failed for %r', title)
        return '', _readable(exc)

    if not video_id:
        return '', 'YouTube accepted the upload but returned no video id'

    if thumbnail_bytes:
        _set_thumbnail(client, video_id, thumbnail_bytes)
    return video_id, ''


BACKUP_PLAYLIST_MARK = 'Metanoia recordings backup'


def ensure_playlist(*, title, institution=None, programme=None, privacy='private', client=None):
    """The YouTube id of the backup playlist called ``title``, creating it if needed.

    Looked up in the database first (free), then on the channel (1 unit — in case
    the row was lost), and only then created (50 units). Returns ``(id, error)``.

    The channel has well over 50 playlists, so the lookup pages through them
    (1 unit a page). The stored row is a backup, not teaching content, so it is
    never surfaced in the students' library.
    """
    from .models import YouTubePlaylist
    row = YouTubePlaylist.objects.filter(title=title,
                                         description__startswith=BACKUP_PLAYLIST_MARK).first()
    if row:
        return row.youtube_id, ''
    client = client or _client()
    if client is None:
        return '', 'YouTube is not configured'
    try:
        found, page = None, None
        while True:
            resp = client.playlists().list(part='snippet', mine=True, maxResults=50,
                                           pageToken=page).execute()
            found = next((p for p in resp.get('items', [])
                          if (p.get('snippet') or {}).get('title') == title), None)
            page = resp.get('nextPageToken')
            if found or not page:
                break
        if found:
            playlist_id = found['id']
        else:
            created = client.playlists().insert(part='snippet,status', body={
                'snippet': {'title': title[:150],
                            'description': f'{BACKUP_PLAYLIST_MARK}. Class recordings for {title}.'},
                'status': {'privacyStatus': privacy},
            }).execute()
            playlist_id = created['id']
    except Exception as exc:
        logger.exception('youtube: could not find or create playlist %r', title)
        return '', _readable(exc)
    YouTubePlaylist.objects.update_or_create(youtube_id=playlist_id, defaults={
        'title': title, 'description': f'{BACKUP_PLAYLIST_MARK}. Class recordings for {title}.',
        'institution': institution, 'programme': programme,
        'show_past_sessions': False, 'show_my_programme': False, 'show_schedule': False,
    })
    return playlist_id, ''


def add_to_playlist(playlist_id, video_id, client=None):
    """File ``video_id`` into ``playlist_id``. Returns an error string, or ''."""
    client = client or _client()
    if client is None:
        return 'YouTube is not configured'
    try:
        client.playlistItems().insert(part='snippet', body={'snippet': {
            'playlistId': playlist_id,
            'resourceId': {'kind': 'youtube#video', 'videoId': video_id},
        }}).execute()
    except Exception as exc:
        logger.exception('youtube: could not add %s to playlist %s', video_id, playlist_id)
        return _readable(exc)
    return ''


def _set_thumbnail(client, video_id, blob):
    """Attach the generated card. Best effort — a video with the default frame
    is still a working video, so a thumbnail failure never fails the upload."""
    try:
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(blob), mimetype='image/jpeg', resumable=False)
        client.thumbnails().set(videoId=video_id, media_body=media).execute()
    except Exception:
        logger.warning('youtube: could not set the thumbnail for %s', video_id, exc_info=True)


def _readable(exc):
    """Pull the useful sentence out of a googleapiclient HttpError."""
    detail = getattr(exc, 'content', None) or getattr(exc, 'reason', None) or str(exc)
    if isinstance(detail, bytes):
        detail = detail.decode('utf-8', 'replace')
    text = str(detail)
    if 'quotaExceeded' in text or 'uploadLimitExceeded' in text:
        return 'YouTube daily quota exceeded — the upload will be retried after the reset'
    return text[:300]
