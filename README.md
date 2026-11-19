<div align="center">

<img src="metanoia/static/images/brand/metanoia-mark.png" alt="Metanoia Group" height="96">

# Metanoia Group — the Metanoia Learning Hub

### Empowering through transformation

The learning platform behind Metanoia Group: live classes, study material, assessments, marks and
progress, messaging, payments and the shop — in one branded hub that learners, educators and staff
sign into.

☎️ +27 11 027 8720 · ✉️ [info@metanoiagroup.co.za](mailto:info@metanoiagroup.co.za) · Metanoia House, 86 Fleet Street, Ferndale, Randburg 2194

<sub>Django 6.0 · Python 3.12 · PostgreSQL · Channels (WebSockets) · HTMX · Bootstrap 5 · Microsoft Teams / OneDrive (Graph) · PayFast · Claude (Anthropic)</sub>

</div>

---

## What it is

Metanoia Group is a South African B-BBEE verification, transformation and skills-development
group. The hub is where its programmes are delivered and run:

- **Students (learners)** register for a programme at their institution, pick the modules they're taking,
  and get a week free. Each module has its own page: a week-by-week schedule of study guides,
  blueprints, question packs and solutions, live classes and recordings, mock exams, the module's
  group chat, and a feed of what's new.
- **Coaches (educators)** run live classes on Microsoft Teams, publish material into the schedule,
  mark work, and see who's falling behind.
- **Staff and admins** run the practice from the **staff desk**: students, grades, certificates,
  invoices and payments, the shop, WhatsApp, e-mail, backups and the job scheduler.

### Programmes on the platform

Institutions, programmes and modules are data, not code: staff set them up in the staff desk or
seed them with `manage.py seed_sample_structure` (see [Getting started](#getting-started-development)).
The academic spine is `Institution → Programme → Module offering → Topic`. Each offering runs per
**intake** (cohort, e.g. *P26S*, *B26F*), and billing is **per module, per month**.

---

## What you can do

<details open>
<summary><b>🎓 Learning</b></summary>

- **Module pages** — tabs for Schedule, Messages, Notifications, Documents, Live sessions, Mock
  exams, Blueprints and Assessments. A side rail shows the module's **Activity** (when you joined,
  your free week, invoices and payments, new material, recordings) and every **file** it holds.
- **The schedule** — blocks and weeks per intake, with topics under each week. Materials open in a
  set order (read the guide before the mock; submit your attempt to see the solution).
- **Study guides and blueprints** read as Metanoia articles, with reading progress and bookmarks.
- **Assessments** — tests, mocks and exams with auto-marking and rubric marking; weighted grades,
  report cards and the Metanoia alumni certificate.
- **Revision** — questions you got wrong come back on a spaced-repetition schedule until they stick.
- **Content import** — staff upload content packs (study guides, question packs, solution
  workbooks) and the system files them into modules and weeks.
</details>

<details>
<summary><b>🎥 Live classes</b></summary>

- Classes are **Microsoft Teams** meetings created from the hub, scheduled to an intake, a module,
  a programme or invited people only.
- **After class**, the recording and transcript are copied from Teams to OneDrive, and an
  **AI session pack** (summary and action items) is written for that class only. Students watch the
  recording in the hub; a nightly job also backs it up to **YouTube**.
- **The Metanoia calendar** — each student sees only their own programme, modules and intake: classes,
  academic dates (tests, exams, coaching nights), assessment deadlines and new lessons. Items open a
  details popup; subscribe from Google or Outlook, or connect your own calendar.
- **Online sessions** — Upcoming and Past (with recordings) behind one sidebar entry.
</details>

<details>
<summary><b>💬 Communication</b></summary>

- **Messages** — one group chat per module, direct messages (student-to-student only after a
  connection is accepted), files, @mentions, replies, read receipts and quick calls.
- **Notifications** — the bell updates live with a chime; each notification opens as a letter with
  sender, body and signature. Desktop push alerts are available.
- **Metanoia Mail** — an Outlook-style mailbox for staff and coaches on the Metanoia domain
  (info@, accounts@, learninghub@ and personal boxes), with branded signatures.
- **Branded e-mail** — invoices, receipts and account e-mails from the right mailbox, with an
  unsubscribe link on everything automated. Reminders stay in-app unless a student opts in.
- **WhatsApp** — a staff inbox on the Meta Cloud API with number verification at registration
  (built, switched off until Meta setup is finished).
- **Announcements** to an institution, programme, intake, module or chosen people.
</details>

<details>
<summary><b>💳 Shop & finance</b></summary>

- **Registration** — 3 steps: your details → institution, programme and modules (with a live
  total) → finish with **Pay now** (PayFast), the **7-day free week**, or **pay by EFT**. Every
  sign-up gets the free week, so a failed payment never locks someone out early.
- **Shop** — study materials (downloads), merchandise (sizes and colours, with courier or Pudo
  locker delivery) and **1-on-1 coaching** booked into the Metanoia calendar.
  Coupons and automatic deals.
- **Finance** — invoices (INV-YYYY-NNNN), estimates, statements, expenses, payment reminders, VAT,
  and **My finances** for students and parents. Reports for staff.
</details>

<details>
<summary><b>🛠️ Running the practice</b></summary>

- **Staff desk** (`/staff/`) — students (Student 360), grades and weightings, certificates,
  at-risk scans, content imports, AI reports, moderation, audit log, invitations, backups and
  restore, and the job scheduler.
- **Student view** — admins and coaches switch into a student's view of any institution or
  programme, then exit.
- **Error log** — every fault recorded once with a count; a code dictionary explains each code.
- **Claude assistant** for admins and staff, grounded in the platform's data (optional).
</details>

### Roles

| Role | Sees / can do |
|---|---|
| **Student** | Their programme and modules (locked until paid or on the free week), schedule, classes, recordings, assessments, revision, progress, messages, the shop and their own invoices. Can invite parents. |
| **Parent / guardian** | Their child's progress and invoices; can pay. |
| **Educator (coach)** | The modules they teach: schedule, materials, classes, marking, the teaching dashboard, Metanoia Mail. |
| **Staff** | Running the practice: students, finance, shop, the staff desk, WhatsApp and Metanoia Mail. |
| **Admin** | Everything staff can do, plus settings, backups and the Django admin. |

People are always shown by **picture and first name + surname**; clicking a name opens a person card.

---

## Getting started (development)

```bash
cd metanoia
python3.12 -m venv .venv && source .venv/bin/activate   # the venv is .venv — never .env
python .install_requirements.py                          # or: pip install -r requirements.txt

cp .env.example .env      # set SECRET_KEY, POSTGRES_*, and anything optional you want on
createdb metanoia_db

python manage.py migrate
python manage.py seed_sample_structure      # institutions, programmes, modules
python manage.py createsuperuser
python manage.py collectstatic --noinput
python run_server.py                     # serves your LAN and prints the address
```

**Fresh demo environment** (⚠️ destructive — drops and rebuilds every table):

```bash
python .admin_wipe_and_create.py   # wipe → migrate → base accounts (admin/educator/student/parent @metanoia.co.za)
python .demo_seed.py               # optional demo students and coursework; --wipe removes them
```

**Tests** — run against a private database name so they never collide with another run:

```bash
SECURE_SSL_REDIRECT=False POSTGRES_DB=metanoia_test_mine python manage.py test apps.learning --noinput
```

`requirements.txt` lists every package with what it is for, and ends with a setup guide for each
external service.

---

## Connecting services (`metanoia/.env`)

Everything optional is **dormant until its keys are set** — nothing breaks without it.

| Service | Needed? | What for | Keys |
|---|---|---|---|
| PostgreSQL | ✅ Required | Primary database | `POSTGRES_*` |
| Redis | Production | Cache + the Channels layer (live chat, bell, presence) | `USE_REDIS`, `REDIS_URL` |
| Own-domain mail | ✅ For real e-mail | Outbound mail and Metanoia Mail (IMAP/SMTP) | `EMAIL_{ACCOUNTS,INFO,LEARNINGHUB}_*` |
| Microsoft Graph | Live classes | Teams meetings, OneDrive recordings and transcripts | `MS_GRAPH_*` |
| YouTube Data API | Optional | Nightly backup of class recordings | `YOUTUBE_*` |
| PayFast | Optional | Card / instant-EFT payments | `PAYFAST_*`, `PAYMENTS_ENABLED` |
| Google / Apple sign-in | Optional | Social sign-in | `GOOGLE_OAUTH_*`, `APPLE_*` |
| WhatsApp Cloud API | Optional | Staff inbox, verification codes | `WHATSAPP_*` |
| Web Push | Optional | Desktop notifications | `VAPID_*` |
| Anthropic | Optional | Admin assistant, AI session packs, AI authoring | `ANTHROPIC_API_KEY`, `ADMIN_AI_ENABLED` |
| Sentry | Optional | Error reporting | `SENTRY_DSN` |

---

## Production

- **Server:** a Linux VM (`<server-ip>`). **Caddy** terminates HTTPS
  (Let's Encrypt) for `learn.metanoiagroup.co.za` and proxies to **gunicorn + uvicorn workers**
  (ASGI, `config.asgi`) on `127.0.0.1:8000`. Units and the Caddyfile are in `deploy/server/`.
- The bare domain and `www` are the public website (hosted separately).
- **Scheduled jobs** run from systemd timers in `deploy/scheduler/production/`:

  | Timer | When | What |
  |---|---|---|
  | `scheduler` | hourly, 06:05–22:05 SAST | general jobs |
  | `scheduler-quick` | every 15 min | class reminders, broadcasts, bookings, auto-notices |
  | `scheduler-sessions` | 22:00 and 06:00 SAST | after-class recordings, transcripts, session packs |
  | `scheduler-youtube` | 00:00 SAST | YouTube backup (up to 6 recordings) |
  | `scheduler-backup` | every 3 h, 00:30–21:30 SAST | database backup + OneDrive copy ("metanoia DB BCKP"); 3-day retention |
  | `mail` | every 2 min | Metanoia Mail inbox sync and new-mail alerts |

- **Deploy:** back up the DB (`manage.py backup_database`) → pull → `migrate` →
  `collectstatic --noinput` → `sudo systemctl restart metanoia`.

---

## Architecture

- Django project `config`; every domain is an app under `apps/`, with business logic in modules
  (`services.py`, `access.py`, …) rather than views.
- All templates live in `templates/<app>/`. Signed-in pages extend
  `templates/myhub/elements/layouts/admin.html`; public and auth pages extend `templates/base.html`
  / `templates/soft_ui/auth_base.html`.
- The Metanoia look: `static/css/brand.css` + `metanoia-ui.css` (Metanoia Brown `#3E2415`,
  Metanoia Gold `#F2A900`, Metanoia Yellow `#FFC20E`, Blue `#0B5FC6`, Cream `#FAF7F2`,
  Montserrat). Palette, logo files and how to swap in the official logo:
  [`metanoia/docs/brand/METANOIA_BRAND.md`](metanoia/docs/brand/METANOIA_BRAND.md).
- **Branding and wording live in one file**, `metanoia/.strings.json` (read by
  `core/branding.py`). Don't hard-code user-facing text.
- HTMX for in-page tab swaps; Channels for WebSockets, with polling as the fallback.

### Apps

```
apps/accounts       people, roles, registration + free week, parents/invites, settings, POPIA, student view
apps/learning       academic spine, module pages + schedule, materials, lessons/articles, content import, access gates
apps/assessments    tests / mocks / exams, take-engine, auto + rubric marking, weighting
apps/revision       spaced-repetition review cards from missed questions
apps/reports        grades, report cards, alumni certificates, progress
apps/analytics      academic intelligence and risk
apps/livesessions   the Metanoia calendar, live classes, after-class pipeline, recordings, YouTube backup
apps/msteams        Microsoft Graph: Teams meetings, OneDrive
apps/communication  chat, notifications (+ push), announcements, e-mail library, WhatsApp, attendance
apps/mailbox        Metanoia Mail (IMAP/SMTP mailboxes, signatures, Gmail import)
apps/shop           store, cart, coupons, deliveries, 1-on-1 bookings
apps/finance        invoices, estimates, statements, expenses, PayFast, reports
apps/staffdesk      the staff desk, Student 360, teaching dashboard, WhatsApp inbox, backups
apps/scheduler      the job scheduler behind the systemd timers
apps/diagnostics    the error log and error-code dictionary
apps/myhub          dashboard / programme feed, search, auth pages, legal pages
apps/tasks          assignable tasks with progress
apps/ai_assistant   the admin Claude assistant
```

More detail: `metanoia/docs/` (live sessions, Teams, Metanoia Mail, WebSockets, HTMX,
error codes, WhatsApp setup, legal).

---

## Legal

Terms & Conditions, Terms of Service, the refund and shipping policies and the POPIA privacy
policy are in the hub (`/social/privacy-and-terms/`), with PDFs in `static/legal/`. Created by
R&G Tech (Gerson Antonio) for Metanoia Group. Proprietary — all rights reserved.

<div align="center">
<sub>© 2026 Metanoia Group · <a href="https://metanoiagroup.co.za">metanoiagroup.co.za</a> · Metanoia House, 86 Fleet Street, Ferndale, Randburg 2194</sub>
</div>
