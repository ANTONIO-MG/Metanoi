# Microsoft Graph setup — Teams meetings, calendars and OneDrive backups

This is the checklist for switching on everything the LMS does through Microsoft
Graph: scheduling Teams meetings from the LMS, putting them on Outlook/Teams
calendars, importing classes scheduled directly in Teams, filing recordings and
summaries in OneDrive, and the off-site copy of the daily database backup.

**Status on 2026-11-07:** none of it has been tested against Microsoft yet. The
tenant ID, client ID and default organiser (`admin@metanoia.co.za`) are set, but
`MS_GRAPH_CLIENT_SECRET` holds the secret's **ID**, not its **value**, so no
token can be issued. All the code is covered by tests against a mocked Graph.
Once the steps below are done, `deploy/check_integrations.py --write` proves each
piece for real (see the last section).

The app uses **application** permissions (client credentials): the LMS acts
without anybody signing in, so meetings can be created straight from the LMS.
Every application permission needs **admin consent**.

---

## 1. Permissions the app registration needs

All of these are *Microsoft Graph → Application permissions*.

| Permission | What the LMS uses it for | Used by the code today? |
|---|---|---|
| `User.Read.All` | Turn an organiser's e-mail (UPN) into their object id — the `/onlineMeetings` API only accepts a GUID. | Yes |
| `OnlineMeetings.ReadWrite.All` | Create the Teams meeting when a session is scheduled, update its title/times when it is edited, delete it when it is cancelled; find the meeting behind a Teams-scheduled class. | Yes |
| `Calendars.ReadWrite` | Put the session on the organiser's Outlook/Teams calendar and invite the module's educators and named staff (Exchange puts it in *their* calendars); update and cancel that event; read the organiser's calendar to import classes scheduled directly in Teams. | Yes |
| `Files.ReadWrite.All` | Build the session folders in OneDrive and file recordings, transcripts and summaries; create sharing links; **and the new OneDrive database backup** (create the backup folder, upload dumps, delete old ones). | Yes (backups are a new use of the same permission) |
| `OnlineMeetingArtifact.Read.All` | Attendance reports (who was in the class, for how long). | Yes |
| `OnlineMeetingTranscript.Read.All` | Transcripts, for the session summary. | Yes |
| `OnlineMeetingRecording.Read.All` | Locate the recording after the class. | Yes |
| `OnlineMeetingAiInsight.Read.All` | *Optional.* Teams' Copilot recap of a meeting. Needs a Microsoft 365 Copilot licence on the organiser; without it the LMS writes its own summary. | Yes, if granted |

**Newly needed by this change: nothing beyond the list above.** The Teams flow
fixes and the OneDrive backup use the same permissions the platform already
expected (`deploy/check_integrations.py` checks exactly this list). What is new
is that `Files.ReadWrite.All` is now also used for backups, and that consent has
to actually be granted — it never has been, because no token has ever been
issued.

### Do you need permission to update the calendar with the Teams calendar?

Yes — `Calendars.ReadWrite` (application, admin consent). It is the one
permission that lets the LMS write sessions into Outlook/Teams calendars and
read the organiser's calendar for the Teams → LMS import. If you only wanted the
import (read) and not the LMS writing events, `Calendars.Read` would do, but the
LMS → Teams direction needs `Calendars.ReadWrite`. Note that the Teams calendar
*is* the Outlook (Exchange) calendar: there is no separate "Teams calendar"
permission.

### `Files.ReadWrite.All` or `Sites.Selected`?

`Files.ReadWrite.All` gives the app every user's OneDrive. `Sites.Selected` would
limit it to sites you name, but the recording pipeline has to reach into each
organiser's own OneDrive (that is where Teams saves recordings), and the code
addresses drives as `/users/{upn}/drive`, which `Sites.Selected` does not cover.
So `Files.ReadWrite.All` is required as the code stands. If you want to fence it
in later, keep all organisers' recordings in one account and talk to the
developer about moving to `Sites.Selected` on that account's OneDrive site.

### Optional hardening: limit calendar access to some mailboxes

`Calendars.ReadWrite` (application) reaches every mailbox in the tenant. To
restrict it to the people who host classes, create a mail-enabled security
group (say `lms-organisers@metanoia.co.za`) holding the organisers, then in
Exchange Online PowerShell:

```powershell
Connect-ExchangeOnline -UserPrincipalName admin@metanoia.co.za
New-ApplicationAccessPolicy -AppId <CLIENT-ID> `
  -PolicyScopeGroupId lms-organisers@metanoia.co.za `
  -AccessRight RestrictAccess `
  -Description "Metanoia LMS: calendars of class organisers only"
Test-ApplicationAccessPolicy -Identity admin@metanoia.co.za -AppId <CLIENT-ID>
```

Anyone outside the group then cannot have LMS sessions put on their calendar,
so add every educator who hosts. Skip this unless you need it.

---

## 2. The Teams *application access policy* (required for meetings)

Permissions alone are **not** enough to create Teams meetings with app-only
auth. A Teams administrator must also allow the app to act for the organisers,
with Teams PowerShell. Without it, meeting creation answers **403** even with
`OnlineMeetings.ReadWrite.All` consented — and the LMS falls back to the
built-in video room. This policy also covers reading transcripts, recordings
and attendance reports. It is **not** needed for calendar events or OneDrive.

```powershell
Install-Module -Name MicrosoftTeams -Scope CurrentUser   # once
Connect-MicrosoftTeams                                    # sign in as a Teams admin

New-CsApplicationAccessPolicy -Identity "Metanoia-LMS-Meetings" `
  -AppIds "<CLIENT-ID>" `
  -Description "Metanoia LMS creates and reads Teams meetings for its organisers"

# Either: every user in the tenant can be an organiser (simplest)
Grant-CsApplicationAccessPolicy -PolicyName "Metanoia-LMS-Meetings" -Global

# Or: only named organisers (repeat per educator who hosts classes)
Grant-CsApplicationAccessPolicy -PolicyName "Metanoia-LMS-Meetings" -Identity "admin@metanoia.co.za"

# Check
Get-CsApplicationAccessPolicy -Identity "Metanoia-LMS-Meetings"
```

`<CLIENT-ID>` is the app registration's *Application (client) ID* — the same
value as `MS_GRAPH_CLIENT_ID`. The policy can take up to 30 minutes to apply.

For transcripts and the AI summary, also make sure Graph transcript access is on
(Teams admin centre → Meetings → Meeting policies / settings, or
`Set-CsTeamsMeetingConfiguration -Identity Global -EnableGraphTranscriptAccess $true`
where your tenant offers it), and that transcription is allowed in the meeting
policy the organisers have.

Each organiser (by default `admin@metanoia.co.za`, or an educator's own
account) needs a **Teams licence and an Exchange Online mailbox**, and a
OneDrive for the recordings. The OneDrive backup owner needs a OneDrive.

---

## 3. Step by step for the owner

1. **Entra admin centre** (<https://entra.microsoft.com>) → *Identity* →
   *Applications* → **App registrations** → open the Metanoia app (its
   *Application (client) ID* must equal `MS_GRAPH_CLIENT_ID`; *Directory (tenant)
   ID* must equal `MS_GRAPH_TENANT_ID`).
2. **API permissions** → *Add a permission* → *Microsoft Graph* → **Application
   permissions** → tick every permission in the table in section 1 → *Add
   permissions*. (Remove any *Delegated* duplicates you do not use.)
3. Click **Grant admin consent for <your organisation>** and confirm. Every row
   must show a green tick under *Status*.
4. **Certificates & secrets** → *Client secrets* → **New client secret** → give
   it a description (e.g. `metanoia-lms-2026`) and an expiry (24 months maximum; put
   the expiry date in your diary) → *Add*.
5. **Copy the `Value` column immediately** — it is shown only once. Do **not**
   copy the `Secret ID` column: that is what was supplied before, and Microsoft
   rejects it (`AADSTS7000215: Invalid client secret provided`).
6. **AWS console** → region *Africa (Cape Town) af-south-1* → **Secrets
   Manager** → `metanoia/production` → *Retrieve secret value* → *Edit*
   → set the key **`MS_GRAPH_CLIENT_SECRET`** to the value from step 5 → *Save*.
7. Run the Teams PowerShell in section 2 (application access policy).
8. On the server, reload the secrets (this restarts the app):
   `sudo systemctl restart metanoia-secrets`
9. Run the health check in read-only mode (section 4). Fix anything marked FAIL.
10. Run it with `--write`. When the Teams meeting, calendar event and OneDrive
    probes all pass, switch the features on in the environment (`.env` on the
    server, or Secrets Manager — wherever the other non-secret settings live):

    ```
    MS_TEAMS_ENABLED=true
    MEETING_PROVIDER=teams
    MS_GRAPH_DEFAULT_ORGANIZER=admin@metanoia.co.za
    # optional (the OneDrive backup is on by default once Graph is configured):
    ONEDRIVE_BACKUP_ENABLED=true
    MS_GRAPH_BACKUP_DRIVE_USER=admin@metanoia.co.za
    MS_GRAPH_BACKUP_FOLDER=metanoia DB BCKP
    BACKUP_RETENTION_DAYS=3
    ```

    then `sudo systemctl restart metanoia-secrets` (or restart the app) again.
11. Schedule a test session from the LMS (Calendar → *Schedule a session*) as
    the admin, with yourself as an invitee, and check: it is in Teams and
    Outlook, the join link opens the same Teams meeting, students on the module
    got a notification, and moving or cancelling it updates Teams.

`MS_TEAMS_ENABLED` and `ONEDRIVE_BACKUP_ENABLED` are independent: backups can go
to OneDrive with Teams still off, and the reverse.

---

## 4. Verifying it: `deploy/check_integrations.py`

Run on the server with the production secrets loaded (on the AWS server use the
wrapper that loads `/run/metanoia/secrets.env`, `deploy/server/aws/manage.sh`,
or source that file first):

```
cd /home/ubuntu/projects/Metanoia/metanoia
./.environment/bin/python deploy/check_integrations.py          # read-only
./.environment/bin/python deploy/check_integrations.py --write  # also creates and deletes probes
```

What it checks under **Microsoft Graph** and **OneDrive database backups**:

| Check | Read-only | `--write` |
|---|---|---|
| Client-credentials token (says plainly if the secret ID was pasted instead of the value) | ✔ | ✔ |
| Every permission in section 1 is in the token (admin consent granted) | ✔ | ✔ |
| Organiser's OneDrive and calendar can be read; calendarView for the import | ✔ | ✔ |
| Organiser resolves to an object id (`User.Read.All`) | ✔ | ✔ |
| Creates and deletes a real Teams meeting (proves the access policy) | | ✔ |
| Creates, updates and deletes a calendar event with **no attendees** (proves `Calendars.ReadWrite`; nobody is invited) | | ✔ |
| Backup owner's OneDrive reachable, free space, backup folder present | ✔ | ✔ |
| Creates the backup folder if missing, uploads and deletes a small probe file, opens an upload session (used for dumps over 4 MB) | | ✔ |

Secrets are masked in its output, so it can be pasted into a support message.

---

## 5. What the LMS does once it is on

**Scheduling** (Calendar → *Schedule a session*, admin/staff/educator):

1. A Teams meeting is created in the organiser's name (the host's
   `ms_upn`, else their e-mail, else `MS_GRAPH_DEFAULT_ORGANIZER`), set to
   record automatically and to let everyone past the lobby.
2. An Outlook/Teams calendar event is created in the organiser's calendar, with
   the module's educators and any named staff invitees as attendees — Exchange
   puts it in their calendars too. Its location and body carry the join link of
   **the same** Teams meeting (it is not a second meeting). Times are sent in UTC.
3. The session appears on the LMS calendar, the module schedule and the session
   page, which has the *Join* button (it records who joined, then hands over to
   Teams). Students join anonymously from there; they are not on the Microsoft
   invite.
4. Everyone in the audience (students on the module/cohort/programme, its
   educators, and the admin/staff team) gets an in-app notification, a browser
   push, WhatsApp if they opted in, and the branded invite e-mail with the join
   link if they have *Reminders by e-mail* on.
5. Reminders go 24 hours and 30 minutes before (configurable in Live-session
   settings), once per person per lead, in each person's own time zone.

**Editing:** title and times are pushed to the Teams meeting; the calendar event
is updated (Exchange sends attendees the update); the audience is told in-app
and by e-mail ("Session moved" / "Session updated"); reminders are re-armed for
the new time.

**Cancelling:** the calendar event is cancelled with the reason (attendees get
Exchange's cancellation), the Teams meeting is deleted, the session comes off
the module schedule, reminders stop, and the audience is told in-app and by
e-mail.

**If Graph refuses** a meeting (missing access policy, expired secret), the
session is still created on the built-in video room, the scheduler sees a
warning saying so, and the Graph error is in the error log.

**OneDrive backups:** after each automatic backup (every 3 hours, by
`metanoia-scheduler-backup.timer`), the dump and its `.json` are uploaded
to `<owner's OneDrive>/metanoia DB BCKP/` (created if missing; dumps over 4 MB go
through a resumable upload session in 10 MiB pieces). Dumps older than
`BACKUP_RETENTION_DAYS` (3) are deleted there, as on the server — never the
newest one; files the job did not write are never touched. The folder is
**never shared**: the job creates no sharing link, and checks the folder's
permissions on every upload — a link or a grant beyond the owner (added by
hand, or inherited) is logged as **BKP-5004**. The dumps contain personal data
and password hashes. The outcome is written into the
backup's `.json` under `"onedrive"` (`uploaded` with the link, or `failed` with
the reason), and a failure is logged as **BKP-5003** in the error log. A failed
upload never affects the backup on the server.

---

## 6. Known limits

- A *recurring* session is one Teams meeting at the first occurrence's time; the
  LMS does not create a Teams recurrence series.
- Students are not added to the Outlook invite (they have no Microsoft
  accounts); they are reached by the LMS's own notifications and e-mail.
- The new-session e-mail and reminder e-mails follow each person's *Reminders by
  e-mail* preference (off by default, by design); the in-app notification
  always goes. Changes and cancellations made by a person are e-mailed to
  anyone with e-mail notifications on.
- Client secrets expire (24 months at most). When it does, tokens fail with
  `AADSTS7000222`; create a new secret and repeat steps 4–8.
