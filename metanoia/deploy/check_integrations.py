#!/usr/bin/env python
"""One-shot health check for every external integration in `.env`.

Read-only by default: it authenticates against each service and reports what the
credentials can actually *do*, rather than whether a variable is non-empty —
which is the distinction that matters, because Teams and YouTube both look fully
configured from the key names alone.

    ./.environment/bin/python deploy/check_integrations.py
    ./.environment/bin/python deploy/check_integrations.py --write

`--write` additionally creates and immediately deletes a throwaway Teams meeting
(the only way to prove the Teams *application access policy* is in place —
permissions alone are not enough), creates/updates/deletes a calendar event with
no attendees, and creates the OneDrive backup folder (if missing) and uploads,
then deletes, a small probe file. It sends no invitations.

Secrets are masked in the output, so it is safe to paste into a bug report.
"""
import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402
django.setup()
from django.conf import settings  # noqa: E402

OK, BAD, WARN = 'OK  ', 'FAIL', 'WARN'
results = []


def report(area, status, detail):
    """Record one check result in :data:`results` and print it as ``[STATUS] detail``."""
    results.append((area, status, detail))
    print(f'  [{status}] {detail}')


def mask(v):
    """Return a printable stand-in for secret *v*: its first six and last four characters plus its length.

    Short values are shown only as ``(set, short)`` so nothing guessable leaks into a bug report.
    """
    v = str(v or '')
    if not v:
        return '(empty)'
    return f'{v[:6]}…{v[-4:]} ({len(v)} chars)' if len(v) > 14 else '(set, short)'


def head(title):
    """Print a section heading for one integration."""
    print(f'\n=== {title} ===')


def check_database():
    """Open a connection to the ``default`` PostgreSQL database and report its server version."""
    head('PostgreSQL')
    try:
        from django.db import connection
        with connection.cursor() as c:
            c.execute('select version()')
            ver = c.fetchone()[0].split(',')[0]
        report('database', OK, ver)
    except Exception as exc:
        report('database', BAD, f'{type(exc).__name__}: {exc}')


def check_redis():
    """Round-trip a value through the cache and a message through the Channels layer.

    ``USE_REDIS`` is consumed by ``config/settings.py`` at import time to pick the
    backends and is never assigned as a setting, so this reads the environment and,
    more to the point, reports the backends that were actually selected.
    """
    head('Redis cache + channel layer')
    if os.getenv('USE_REDIS', '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        return report('redis', WARN, 'USE_REDIS is not true — in-memory fallback in use')
    print(f"  cache backend   {settings.CACHES['default']['BACKEND']}")
    print(f"  channel backend {settings.CHANNEL_LAYERS['default']['BACKEND']}")
    try:
        from django.core.cache import cache
        cache.set('_probe', 'v', 10)
        report('cache', OK if cache.get('_probe') == 'v' else BAD, 'cache round-trip')
    except Exception as exc:
        report('cache', BAD, f'{type(exc).__name__}: {str(exc)[:120]}')
    try:
        import asyncio
        from channels.layers import get_channel_layer
        layer = get_channel_layer()

        async def roundtrip():
            """Send one message to a probe channel and read it back."""
            await layer.send('_probe', {'type': 'x'})
            return await layer.receive('_probe')

        asyncio.run(asyncio.wait_for(roundtrip(), 8))
        report('channels', OK, f'channel layer round-trip ({type(layer).__name__})')
    except Exception as exc:
        report('channels', BAD, f'{type(exc).__name__}: {str(exc)[:120]}')


def check_smtp():
    """Authenticate against the default SMTP connection; flags ``EMAIL_USE_TLS`` and ``EMAIL_USE_SSL`` both on."""
    head('Gmail SMTP')
    print(f'  host {settings.EMAIL_HOST}:{settings.EMAIL_PORT} '
          f'TLS={settings.EMAIL_USE_TLS} SSL={settings.EMAIL_USE_SSL} '
          f'pass={mask(settings.EMAIL_HOST_PASSWORD)}')
    if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
        return report('smtp', BAD, 'EMAIL_USE_TLS and EMAIL_USE_SSL both true — pick one')
    try:
        from django.core.mail import get_connection
        conn = get_connection(timeout=20)
        conn.open()
        conn.close()
        report('smtp', OK, f'authenticated as {settings.EMAIL_HOST_USER}')
    except Exception as exc:
        report('smtp', BAD, f'{type(exc).__name__}: {str(exc)[:160]}')


def check_google():
    """Check Google sign-in is configured through exactly one route.

    A settings-based app *and* a ``SocialApp`` admin row for ``google`` make
    allauth raise ``MultipleObjectsReturned`` on every sign-in. The redirect URI
    registered in the Cloud Console cannot be tested from here, so it is printed
    as a warning to confirm by hand.
    """
    head('Google sign-in')
    try:
        from allauth.socialaccount.models import SocialApp
        rows = SocialApp.objects.filter(provider='google').count()
    except Exception:
        rows = 0
    if rows and os.getenv('GOOGLE_OAUTH_CLIENT_ID'):
        report('google', BAD,
               'configured BOTH in env and as a SocialApp admin row — '
               'allauth raises MultipleObjectsReturned and every sign-in 500s')
    else:
        report('google', OK, f'single configuration route (admin rows: {rows})')
    redirect = settings.SITE_URL.rstrip('/') + '/accounts/google/login/callback/'
    report('google', WARN, f'confirm in the console, cannot be tested from here: {redirect}')


NEEDED_GRAPH_ROLES = {
    'User.Read.All': 'resolve organisers to object ids (onlineMeetings needs a GUID)',
    'OnlineMeetings.ReadWrite.All': 'create / update / delete the Teams meeting',
    'Calendars.ReadWrite': 'put sessions on Outlook/Teams calendars; import Teams-scheduled classes',
    'Files.ReadWrite.All': 'recording/summary folders and the OneDrive database backup',
    'OnlineMeetingArtifact.Read.All': 'attendance reports',
    'OnlineMeetingTranscript.Read.All': 'transcripts for session summaries',
    'OnlineMeetingRecording.Read.All': 'locating the recording',
}
OPTIONAL_GRAPH_ROLES = {
    'OnlineMeetingAiInsight.Read.All': 'Copilot meeting recap (needs a Copilot licence)',
}

_token = {'value': None, 'tried': False}


def graph_token():
    """An app-only token (client credentials), acquired once per run; None on failure."""
    if _token['tried']:
        return _token['value']
    _token['tried'] = True
    if not all([settings.MS_GRAPH_TENANT_ID, settings.MS_GRAPH_CLIENT_ID,
                settings.MS_GRAPH_CLIENT_SECRET]):
        report('graph', BAD, 'tenant/client/secret incomplete — MS_GRAPH_CLIENT_SECRET must be '
                             'the secret VALUE (not its ID); see docs/microsoft-graph-setup.md')
        return None
    try:
        import msal
    except ImportError:
        report('graph', BAD, 'msal is not installed in this environment')
        return None
    app = msal.ConfidentialClientApplication(
        settings.MS_GRAPH_CLIENT_ID,
        authority=f'{settings.MS_GRAPH_AUTHORITY}/{settings.MS_GRAPH_TENANT_ID}',
        client_credential=settings.MS_GRAPH_CLIENT_SECRET)
    res = app.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])
    if 'access_token' not in res:
        hint = ''
        if 'AADSTS7000215' in str(res.get('error_description', '')):
            hint = ' — the secret is wrong: paste the secret VALUE, not the secret ID'
        report('graph', BAD,
               f"no token: {res.get('error')} {str(res.get('error_description'))[:120]}{hint}")
        return None
    _token['value'] = res['access_token']
    report('graph', OK, 'client-credentials token issued')
    return _token['value']


def granted_roles(token):
    """Return the application roles in the ``roles`` claim of Graph access *token* (empty set if unreadable)."""
    try:
        payload = token.split('.')[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except Exception:
        return set()
    return set(claims.get('roles', []))


def graph_call(token, path, method='GET', body=None):
    """``(status, json-or-error-code)`` for one Graph call; never raises."""
    data = json.dumps(body).encode() if body is not None else None
    url = path if path.startswith('http') else settings.MS_GRAPH_BASE.rstrip('/') + path
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()[:400]
        try:
            return exc.code, json.loads(raw)['error'].get('code', '?')
        except Exception:
            return exc.code, raw
    except Exception as exc:
        return 0, f'{type(exc).__name__}: {exc}'


def _q(path):
    """URL-quote a drive path, keeping its slashes."""
    return urllib.parse.quote(str(path), safe='/')


def check_teams(write=False):
    """Check the Microsoft Graph token, its permissions, the organiser's OneDrive and calendar.

    :data:`NEEDED_GRAPH_ROLES` lists the application permissions (admin consent)
    the code uses and what for; :data:`OPTIONAL_GRAPH_ROLES` those used if present,
    without which the feature degrades quietly. Keep both in step with
    ``docs/microsoft-graph-setup.md``.

    The ``/onlineMeetings`` family is the one place Graph insists on a GUID, so the
    default organiser is resolved to an object id. With *write* it also creates and
    deletes a throwaway Teams meeting (the only proof the application access policy
    is in place) and a calendar event with no attendees, which proves
    ``Calendars.ReadWrite`` without sending anybody an invitation.
    """
    head('Microsoft Graph — token, permissions, Teams meetings, calendar')
    print(f'  MS_TEAMS_ENABLED={settings.MS_TEAMS_ENABLED} '
          f'MEETING_PROVIDER={getattr(settings, "MEETING_PROVIDER", "")} '
          f'secret={mask(settings.MS_GRAPH_CLIENT_SECRET)}')
    token = graph_token()
    if not token:
        return

    granted = granted_roles(token)
    missing = [r for r in NEEDED_GRAPH_ROLES if r not in granted]
    if missing:
        report('graph', BAD, 'missing admin consent for: ' + ', '.join(missing))
    else:
        report('graph', OK, 'all required application permissions granted')
    for role, purpose in OPTIONAL_GRAPH_ROLES.items():
        if role not in granted:
            report('graph', WARN, f'optional {role} not granted ({purpose})')

    organizer = settings.MS_GRAPH_DEFAULT_ORGANIZER
    if not organizer:
        return report('teams', BAD, 'MS_GRAPH_DEFAULT_ORGANIZER is empty')

    code, drive = graph_call(token, f'/users/{organizer}/drive')
    report('teams', OK if code == 200 else BAD, f'OneDrive of {organizer} (recording storage): {code}')
    code, cal = graph_call(token, f'/users/{organizer}/calendar')
    report('calendar', OK if code == 200 else BAD,
           f'calendar read: {code}'
           + (f' (canEdit={cal.get("canEdit")})' if isinstance(cal, dict) else ''))
    now = datetime.datetime.now(datetime.timezone.utc)
    window = urllib.parse.urlencode({
        'startDateTime': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'endDateTime': (now + datetime.timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        '$top': '5', '$select': 'id,subject'})
    code, view = graph_call(token, f'/users/{organizer}/calendarView?{window}')
    report('calendar', OK if code == 200 else BAD,
           f'calendarView (Teams calendar import): {code}'
           + (f', {len(view.get("value", []))} event(s) in the next week'
              if isinstance(view, dict) else ''))

    from apps.msteams import graph as graph_mod
    with graph_mod.without_teams_switch():
        guid = graph_mod.resolve_user_id(organizer)
    if guid:
        report('teams', OK, f'organiser resolves to object id {guid}')
    else:
        owner = (drive.get('owner', {}).get('user', {}) or {}).get('id') \
            if isinstance(drive, dict) else None
        report('teams', BAD,
               'cannot resolve the organiser to an object id (needs User.Read.All). '
               + (f'Its object id is {owner} — putting that GUID in '
                  'MS_GRAPH_DEFAULT_ORGANIZER works without the permission.'
                  if owner else ''))
        guid = owner

    if not write:
        return report('teams', WARN, 're-run with --write to prove meeting and event creation work')

    start = now + datetime.timedelta(days=1)
    if guid:
        code, out = graph_call(token, f'/users/{guid}/onlineMeetings', 'POST', {
            'subject': 'Metanoia — automated config probe (safe to ignore)',
            'startDateTime': start.isoformat(),
            'endDateTime': (start + datetime.timedelta(hours=1)).isoformat()})
        if code in (200, 201) and isinstance(out, dict):
            report('teams', OK, 'created a real Teams meeting — access policy is in place')
            dcode, _ = graph_call(token, f'/users/{guid}/onlineMeetings/{out.get("id")}', 'DELETE')
            report('teams', OK if dcode in (200, 204) else BAD,
                   f'deleted the probe meeting: {dcode}')
        else:
            report('teams', BAD,
                   f'meeting creation refused ({code} {out}) — a 403 here is usually the missing '
                   'application access policy (New-/Grant-CsApplicationAccessPolicy), '
                   'not the permission')

    code, ev = graph_call(token, f'/users/{organizer}/events', 'POST', {
        'subject': 'Metanoia — calendar probe (safe to ignore)',
        'start': {'dateTime': start.strftime('%Y-%m-%dT%H:%M:%S'), 'timeZone': 'UTC'},
        'end': {'dateTime': (start + datetime.timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%S'),
                'timeZone': 'UTC'},
        'isReminderOn': False, 'showAs': 'free', 'attendees': []})
    if code in (200, 201) and isinstance(ev, dict):
        report('calendar', OK, 'calendar write: created a probe event')
        pcode, _ = graph_call(token, f'/users/{organizer}/events/{ev["id"]}', 'PATCH',
                              {'subject': 'Metanoia — calendar probe (updated)'})
        report('calendar', OK if pcode == 200 else BAD, f'calendar update: {pcode}')
        dcode, _ = graph_call(token, f'/users/{organizer}/events/{ev["id"]}', 'DELETE')
        report('calendar', OK if dcode in (200, 204) else BAD, f'deleted the probe event: {dcode}')
    else:
        report('calendar', BAD, f'calendar write refused ({code} {ev}) — needs Calendars.ReadWrite')


def check_onedrive_backup(write=False):
    """Check the OneDrive database-backup target: owner drive, folder, and that it is not shared.

    With *write* it creates or finds the folder, uploads and deletes a probe file and
    opens a resumable upload session, all through the same code the 3-hourly backup
    job uses.
    """
    head('OneDrive database backups')
    from apps.msteams import graph as graph_mod
    from apps.staffdesk import onedrive_backup as odb

    owner, folder = odb.drive_user(), odb.folder_path()
    print(f'  ONEDRIVE_BACKUP_ENABLED={odb.is_enabled()} owner={owner or "(empty)"} '
          f'folder="{folder}" keep={odb.keep_days()} days')
    if not odb.is_enabled():
        report('onedrive-backup', WARN,
               'OneDrive backup is off (ONEDRIVE_BACKUP_ENABLED=false, or Graph not configured) '
               '— backups stay on this server only')
    token = graph_token()
    if not token:
        return
    if 'Files.ReadWrite.All' not in granted_roles(token):
        report('onedrive-backup', BAD, 'Files.ReadWrite.All is not consented — uploads will fail')
    if not owner:
        return report('onedrive-backup', BAD, 'no drive owner: set MS_GRAPH_BACKUP_DRIVE_USER')

    code, drive = graph_call(token, f'/users/{owner}/drive')
    if code != 200 or not isinstance(drive, dict):
        return report('onedrive-backup', BAD, f'cannot open the OneDrive of {owner}: {code} {drive}')
    free = (drive.get('quota') or {}).get('remaining')
    report('onedrive-backup', OK, f'OneDrive of {owner} reachable'
           + (f', {free // (1024 ** 3)} GB free' if isinstance(free, int) else ''))

    code, item = graph_call(token, f'/drives/{drive["id"]}/root:/{_q(folder)}')
    if code == 200 and isinstance(item, dict):
        _c, kids = graph_call(token, f'/drives/{drive["id"]}/items/{item["id"]}/children?$top=200')
        dumps = [k for k in (kids.get('value', []) if isinstance(kids, dict) else [])
                 if k.get('name', '').endswith('.dump')]
        report('onedrive-backup', OK, f'folder "{folder}" exists ({len(dumps)} dump(s) in it)')
        with graph_mod.without_teams_switch():
            shared = odb.sharing_problems(drive['id'], item['id'])
        if shared:
            report('onedrive-backup', BAD, f'folder "{folder}" is SHARED: {"; ".join(shared)} '
                                           '— dumps hold personal data; remove the sharing')
        else:
            report('onedrive-backup', OK, f'folder "{folder}" is not shared')
    elif code == 404:
        report('onedrive-backup', WARN,
               f'folder "{folder}" does not exist yet — it is created on the first upload')
    else:
        report('onedrive-backup', BAD, f'folder lookup failed: {code} {item}')

    if not write:
        return report('onedrive-backup', WARN,
                      're-run with --write to prove folder creation and upload work')
    with graph_mod.without_teams_switch():
        try:
            drive_id, node = odb.resolve_folder()
        except Exception as exc:
            return report('onedrive-backup', BAD, f'folder create/find failed: {exc}')
        report('onedrive-backup', OK, f'folder "{folder}" created/found ({node.get("webUrl", "")})')
        name = f'connectivity-probe-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt'
        up = graph_mod.upload_small_file(drive_id, node['id'], name,
                                         b'Metanoia backup connectivity probe; safe to delete.\n')
        if not up or not up.get('id'):
            return report('onedrive-backup', BAD, 'probe upload failed (see the log above)')
        report('onedrive-backup', OK, 'uploaded a probe file')
        session = graph_mod.create_upload_session(drive_id, node['id'], name + '.session')
        report('onedrive-backup', OK if session else BAD,
               'resumable upload session (used for dumps over 4 MB): '
               + ('opened' if session else 'refused'))
        if session:
            graph_mod._cancel_upload(session)
        gone = graph_mod.delete_item(drive_id, up['id'])
        report('onedrive-backup', OK if gone else WARN,
               'deleted the probe file' if gone else f'could not delete {name}; remove it by hand')


def check_youtube():
    """Check the YouTube API key, channel id, owner OAuth and the admin publish toggle."""
    head('YouTube')
    from apps.livesessions import youtube as yt
    for name in ('YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REFRESH_TOKEN',
                 'YOUTUBE_CHANNEL_ID', 'YOUTUBE_API_KEY'):
        print(f'  {name:22} {mask(getattr(settings, name, ""))}')

    if settings.YOUTUBE_API_KEY:
        url = ('https://www.googleapis.com/youtube/v3/channels?part=id&forHandle=@YouTube'
               f'&key={urllib.parse.quote(settings.YOUTUBE_API_KEY)}')
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                json.load(r)
            report('youtube', OK, 'API key valid (public reads)')
        except urllib.error.HTTPError as exc:
            report('youtube', BAD, f'API key rejected: {exc.code}')
        except Exception as exc:
            report('youtube', BAD, f'{type(exc).__name__}: {exc}')
    else:
        report('youtube', WARN, 'no API key')

    channel = yt.resolve_channel_id()
    if channel:
        report('youtube', OK, f'channel resolves: {channel} '
                              f'({len(yt.list_playlists() or [])} public playlists)')
    else:
        report('youtube', BAD, 'no channel id — scanning finds nothing')

    if yt.is_configured():
        report('youtube', OK, 'owner OAuth present — uploads and unlisted scans possible')
    else:
        report('youtube', BAD,
               'no owner OAuth (client id/secret/refresh token) — uploads impossible '
               'and unlisted videos invisible. Run deploy/mint_youtube_token.py')
    report('youtube', OK if yt.enabled() else WARN,
           f'admin toggle "Publish recordings to YouTube": {yt.enabled()}')


def check_admin_ai():
    """Send a five-token request to the configured Admin AI model when it is enabled."""
    head('Admin AI (Anthropic)')
    if not settings.ADMIN_AI_ENABLED:
        return report('admin-ai', WARN, 'ADMIN_AI_ENABLED=false')
    if not settings.ANTHROPIC_API_KEY:
        return report('admin-ai', BAD, 'ANTHROPIC_API_KEY empty')
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=settings.ADMIN_AI_MODEL, max_tokens=5,
            messages=[{'role': 'user', 'content': 'Reply with the word OK only.'}])
        report('admin-ai', OK, f'{msg.model} responded')
    except Exception as exc:
        report('admin-ai', BAD, f'{type(exc).__name__}: {str(exc)[:160]}')


def check_payfast():
    """Report whether PayFast merchant credentials are configured (no request is made)."""
    head('PayFast')
    if not settings.PAYFAST_MERCHANT_ID or not settings.PAYFAST_MERCHANT_KEY:
        return report('payfast', WARN,
                      f'no merchant credentials (sandbox={settings.PAYFAST_SANDBOX}) — '
                      'card checkout cannot run; enrolment is manual')
    report('payfast', OK, f'merchant configured (sandbox={settings.PAYFAST_SANDBOX})')


def main():
    """Run every check, print the summary and return 1 if any check failed."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--write', action='store_true',
                    help='also create + delete a throwaway Teams meeting and calendar '
                         'event, and create the OneDrive backup folder + upload/delete a probe')
    args = ap.parse_args()

    check_database()
    check_redis()
    check_smtp()
    check_google()
    check_teams(write=args.write)
    check_onedrive_backup(write=args.write)
    check_youtube()
    check_admin_ai()
    check_payfast()

    bad = [r for r in results if r[1] == BAD]
    warn = [r for r in results if r[1] == WARN]
    print(f'\n=== summary: {len(results) - len(bad) - len(warn)} ok, '
          f'{len(warn)} warn, {len(bad)} failing ===')
    for area, _, detail in bad:
        print(f'  FAIL  {area}: {detail}')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
