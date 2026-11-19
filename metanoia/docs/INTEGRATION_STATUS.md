# Integration status — what is live, what is blocked

Measured against the running installation on 2026-09-10, by probing the real
credentials rather than reading the config.

**Re-run everything with one command** — it authenticates against each service
and reports what the credentials can actually *do*, not whether a variable is
non-empty:

```bash
./.environment/bin/python deploy/check_integrations.py          # read-only
./.environment/bin/python deploy/check_integrations.py --write  # also creates + deletes a test Teams meeting
```

| Integration | Works | Blocked on |
|---|---|---|
| PostgreSQL | ✅ yes | — |
| Redis cache + channel layer | ✅ yes | — |
| Gmail SMTP (outbound e-mail) | ✅ yes | — |
| Google sign-in / sign-up | ✅ yes | one console check, below |
| Admin AI (Anthropic) | ✅ yes | — |
| Microsoft Teams | ❌ no | one permission: **`User.Read.All`** |
| YouTube | ❌ partly | no owner OAuth (uploads + unlisted) |
| PayFast | ⚠️ inert | no merchant credentials; enrolment is manual |

---

## 1. Gmail SMTP — working

Authenticates as `info@metanoiagroup.co.za` against `smtp.gmail.com:587` with
STARTTLS, using a 16-character Google **app password** (not the account
password — an app password is the only thing Google accepts here once 2FA is
on). A branded test message was sent end to end and arrived in the inbox.

```bash
.environment/bin/python -c "
import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.core.mail import get_connection
c = get_connection(); c.open(); print('SMTP OK'); c.close()"
```

If this ever starts failing with `535`, the app password has been revoked —
mint a new one at <https://myaccount.google.com/apppasswords> and update
`EMAIL_HOST_PASSWORD`. Nothing in the code needs to change.

---

## 2. Google sign-in — working

The OAuth client is configured in the environment (`GOOGLE_OAUTH_CLIENT_ID` /
`GOOGLE_OAUTH_SECRET`) and `/accounts/google/login/` redirects to Google with
the right client id, `scope=email profile` and:

```
redirect_uri = http://<server-ip>/accounts/google/login/callback/
```

**The one thing to confirm in the console:** that exact URI must appear under
*Authorised redirect URIs* on the OAuth client at
<https://console.cloud.google.com/apis/credentials>. It has to match
character for character, including the scheme and the trailing slash — a
mismatch gives Google's `redirect_uri_mismatch` screen and never reaches the
site, so it cannot be detected from here.

When the domain changes, that URI changes with it. Update `SITE_URL`, the
Django `Site` row **and** the console entry together.

### Two traps worth knowing

**Never configure the provider twice.** allauth resolves a provider by finding
exactly one app for it. With `GOOGLE_OAUTH_CLIENT_ID` set in the environment
*and* a Google `SocialApp` row added in the Django admin, it raises
`MultipleObjectsReturned` and **every** Google sign-in 500s. Pick one — the
environment variables are the configured route here, so do not add the admin
row.

**Signing in with Google onto an existing password account** used to fail: the
person landed on "confirmation e-mail sent", signed out, with nothing linked.
Fixed by `SOCIALACCOUNT_EMAIL_AUTHENTICATION` /
`..._AUTO_CONNECT` in `config/settings.py`, and pinned by
`apps/accounts/test_google_auth.py`. This matters because nearly everybody here
registers with a password first and clicks the Google button months later.

---

## 3. Microsoft Teams — one permission short

**This section changed on 2026-09-10.** Consent has since been granted for six of
the seven permissions, and the earlier conclusion — "the application has been
granted nothing" — is no longer true. What is left is much narrower.

Verified working against the live tenant:

- a client-credentials token is issued;
- **creating a Teams meeting succeeds** (a real meeting was created and deleted),
  which means the *application access policy* over the organiser is in place —
  that is the step permissions alone do not cover, so it is worth knowing it is done;
- `/users/{organizer}/calendar` → 200, so staff Outlook invites can be written;
- `/users/{organizer}/drive` → 200, so recordings can be filed into OneDrive.

The single gap is **`User.Read.All`**, which is not in the token's `roles` claim:

```
GET /v1.0/users/MetanoiaGroup@MetanoiaGroup.onmicrosoft.com
403 Authorization_RequestDenied
```

That one call is `apps.msteams.graph.resolve_user_id`, and it is on the critical
path: `/onlineMeetings` is the one Graph family that refuses a UPN and demands an
object GUID, so every meeting create/update/delete and every transcript,
recording and attendance fetch begins by turning the organiser's UPN into a
GUID. Without the permission that returns `None` and the whole path gives up —
even though the meeting API itself would accept the call.

### Two ways to fix it

**A. Grant the permission (clean, needs a tenant admin).** Add `User.Read.All`
as an *Application* permission and press **Grant admin consent**. Nothing else
changes.

**B. Skip the lookup (works immediately, no admin).** `resolve_user_id` passes a
GUID straight through without calling the directory, and `/events` and `/drive`
accept a GUID just as happily as a UPN. So putting the organiser's **object id**
in `MS_GRAPH_DEFAULT_ORGANIZER` (and in each educator's `profile.ms_upn`) removes
the only call that needs the permission:

```
MS_GRAPH_DEFAULT_ORGANIZER=888cb6b7-f380-43a7-9924-97d2c7bb871f
```

The trade-off is legibility: a GUID says nothing about whose account it is, and
if the organiser account is ever replaced the failure is silent and confusing.
Prefer A when an admin is available; B is the unblock when one is not.

### What to do

In the Entra admin centre → **App registrations** → this app → **API
permissions**, as **Application** permissions (not Delegated), then press
**Grant admin consent for the tenant** — adding them without that final click
changes nothing. Only the bolded row is still outstanding:

| Permission | Needed for | Granted |
|---|---|---|
| `OnlineMeetings.ReadWrite.All` | creating the class meeting | ✅ |
| `Calendars.ReadWrite` | the staff Outlook invite | ✅ |
| **`User.Read.All`** | **resolving an educator to an organiser GUID** | ❌ **missing** |
| `OnlineMeetingTranscript.Read.All` | the AI recap | ✅ |
| `OnlineMeetingRecording.Read.All` | the recording pipeline | ✅ |
| `OnlineMeetingArtifact.Read.All` | attendance reports | ✅ |
| `Files.ReadWrite.All` | filing recordings into OneDrive | ✅ |

`OnlineMeetings.ReadWrite.All` additionally needs an **application access
policy** granting the app rights over the organiser's account — permission alone
is not enough for meeting creation. **This is already in place**, proven by
creating a real meeting; see `docs/TEAMS_INTEGRATION.md` if it ever needs
rebuilding.

### The other half: each educator needs a Microsoft identity

`services.organizer_upn_for` prefers `profile.ms_upn`, then falls back to the
account e-mail, and only then to `MS_GRAPH_DEFAULT_ORGANIZER`. Both staff
accounts here sign in with addresses that do not exist in the tenant
(`info@metanoiagroup.co.za`, `admin@metanoia.co.za`), so **the fallback is never
reached** — the e-mail is used and the lookup fails. `ms_upn` has been set on
both staff profiles to the licensed organiser account. Any new educator needs
the same, under Admin → user → Microsoft Teams UPN.

Then set `MS_TEAMS_ENABLED=true` and re-probe:

```bash
.environment/bin/python -c "
import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from django.conf import settings
import msal, urllib.request, json
app = msal.ConfidentialClientApplication(
    settings.MS_GRAPH_CLIENT_ID,
    authority='https://login.microsoftonline.com/' + settings.MS_GRAPH_TENANT_ID,
    client_credential=settings.MS_GRAPH_CLIENT_SECRET)
tok = app.acquire_token_for_client(scopes=['https://graph.microsoft.com/.default'])['access_token']
req = urllib.request.Request(
    'https://graph.microsoft.com/v1.0/users/' + settings.MS_GRAPH_DEFAULT_ORGANIZER,
    headers={'Authorization': 'Bearer ' + tok})
print(json.load(urllib.request.urlopen(req))['displayName'])"
```

A name printed means consent has landed. Until then sessions fall back to the
in-app room, which works — students join by link and attendance is still
recorded — they simply are not Teams meetings.

### What this currently costs

With Teams off, `services.ensure_calendar_event` never runs, so **no Outlook
invite goes out for any session**. (Students were never on that invite anyway —
only licensed staff are.) The per-session **Add to my calendar** `.ics`
download on the session page covers everybody in the meantime, and the
subscribable feed under Calendar → Settings covers anyone who wants their whole
diary kept in step.

---

## 4. YouTube — read-only, uploads off

`YOUTUBE_API_KEY` is set **and valid** — a probe against a known public channel
returns data, so the key and its quota are fine. Everything else is empty:

| Setting | State |
|---|---|
| `YOUTUBE_API_KEY` | ✅ set and valid |
| `YOUTUBE_CHANNEL_ID` | ✅ `UChc5D8szBuSvX8qOyUxoUjA` (@metanoiagroup) — set 2026-09-10 |
| `YOUTUBE_CLIENT_ID` | ❌ empty |
| `YOUTUBE_CLIENT_SECRET` | ❌ empty |
| `YOUTUBE_REFRESH_TOKEN` | ❌ empty |

With the channel id in place the scanner now sees the channel's **4 public
playlists** (22 videos) instead of nothing. The channel's own video count is 0
public, which is the point below about unlisted content: everything already
uploaded there is invisible to an API key.

So `youtube.is_configured()` is still `False` (it requires the OAuth trio) and
`LiveSessionSettings.youtube_enabled` is off. Consequences, in order of how much
they matter:

1. **No uploads.** Recordings are never published to YouTube. Uploading is
   inherently an owner action — an API key cannot do it, only OAuth can.
2. ~~**Scanning finds nothing.**~~ Fixed on 2026-09-10 by setting
   `YOUTUBE_CHANNEL_ID`. `/calendar/recordings/manage/` now lists the four
   playlists — but only their *public* videos.
3. **Unlisted videos would be invisible even with a channel id.** An API key
   only ever sees public content. Past sessions are uploaded *unlisted* by
   default (`youtube_privacy`), so the owner OAuth credentials are what make the
   library work at all.

### What to do

1. ~~**Set the channel id.**~~ Done — `UChc5D8szBuSvX8qOyUxoUjA`. To resolve a
   different channel later:
   `https://www.googleapis.com/youtube/v3/channels?part=id&forHandle=@yourhandle&key=<API_KEY>`.
2. **Mint owner OAuth credentials** for uploads and for unlisted videos. In the
   same Google Cloud project create an **OAuth client (Desktop app)**, put its id
   and secret in `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`, then run the
   helper that already ships with the project:

   ```bash
   python3 deploy/mint_youtube_token.py
   ```

   It walks the consent flow with the `youtube.upload` and `youtube.readonly`
   scopes and offers to write `YOUTUBE_REFRESH_TOKEN` and `YOUTUBE_CHANNEL_ID`
   back into `.env` — which also covers step 1. Sign in as the account that
   **owns the channel**; consenting as anyone else mints a token that cannot
   upload.
3. **Switch it on** — Calendar → Settings → *Publish recordings to YouTube*.
   Mind `youtube_daily_quota`: an upload costs 1600 units against a default
   10,000/day, so roughly six a day before the quota stops it.

Verify:

```bash
.environment/bin/python -c "
import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); django.setup()
from apps.livesessions import youtube as yt
print('configured:', yt.is_configured(), '| enabled:', yt.enabled())
print('channel   :', yt.resolve_channel_id())
print('playlists :', len(yt.list_playlists() or []))"
```

Nothing else in the platform depends on this. Recordings still land in OneDrive
and are still linked from the session page; YouTube is the public library on top.
