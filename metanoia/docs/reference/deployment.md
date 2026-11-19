# Deployment reference

What each deployment file does, when it runs, and the operational warnings that
used to live in its comments. Environment variables are described in
[configuration.md](configuration.md); Python packages in
[dependencies.md](dependencies.md). `deploy/scheduler/README.md` is the
step-by-step guide to installing the background-job runner.

## Contents

1. [Production host layout (AWS)](#1-production-host-layout-aws)
2. [Web service: `metanoia.service`](#2-web-service-metanoiaservice)
3. [Caddy: `Caddyfile` and `Caddyfile.ip`](#3-caddy-caddyfile-and-caddyfileip)
4. [Moving to the domain: `switch-to-domain.sh`](#4-moving-to-the-domain-switch-to-domainsh)
5. [Secrets: `deploy/server/aws/`](#5-secrets-deployserveraws)
6. [Background jobs: systemd units and timers](#6-background-jobs-systemd-units-and-timers)
7. [Generic scheduler templates and cron](#7-generic-scheduler-templates-and-cron)
8. [Render: `render.yaml` and `render-build.sh`](#8-render-renderyaml-and-render-buildsh)
9. [Maintenance scripts](#9-maintenance-scripts)
10. [Repository attributes: `.gitattributes`](#10-repository-attributes-gitattributes)

---

## 1. Production host layout (AWS)

* Project: `/home/ubuntu/projects/Metanoia/metanoia`, run as
  the `ubuntu` user with the in-tree virtualenv `.environment/`.
* Services: PostgreSQL 16 (`postgresql@16-main`), Redis (`redis-server`),
  Caddy on ports 80/443, the app on `127.0.0.1:8000`.
* The host has no `cron`; every schedule is a systemd timer.
* Secrets are fetched from AWS Secrets Manager into `/run/metanoia/secrets.env`
  at boot; `.env` holds only non-secret configuration.

## 2. Web service: `metanoia.service`

`deploy/server/metanoia.service`, installed as
`/etc/systemd/system/metanoia.service`. Runs Django under gunicorn with
the uvicorn worker (`config.asgi:application` — ASGI, never WSGI, see
[dependencies.md](dependencies.md#production-web-server)).

* `Type=notify`, `Restart=always`, `RestartSec=5`, `NoNewPrivileges`,
  `PrivateTmp`, `PYTHONUNBUFFERED=1`.
* **4 workers on 2 CPUs**: requests mostly wait on the database, so four serve
  four at once. `--max-requests 2000 --max-requests-jitter 200` recycles a
  worker now and then so slow memory growth never builds up.
* Binds `127.0.0.1:8000` only; Caddy owns ports 80/443 and proxies to it.
* `Wants=postgresql.service redis-server.service`, **not** `Requires=`: a
  `Requires=` would propagate `systemctl stop redis-server` into stopping the
  site, and the site would not come back when Redis returned. Ordering is still
  guaranteed by `After=`, and `Restart=always` covers a Redis blip.
* Secrets come from the `secrets.conf` drop-in (section 5).

## 3. Caddy: `Caddyfile` and `Caddyfile.ip`

Both are installed as `/etc/caddy/Caddyfile` (one at a time). The ACME contact
e-mail is `info@metanoiagroup.co.za`.

**`Caddyfile`** — HTTPS for `learn.metanoiagroup.co.za` with a Let's Encrypt
certificate Caddy renews automatically, and one canonical address: old links
(e-mails, bookmarks, calendar feeds) that used the server's bare IP over HTTP are
redirected permanently to the domain. The bare domain and `www` are the public
website, hosted elsewhere, and are not served here; only the `learn` subdomain's A record
points at this server.

**`Caddyfile.ip`** — the layout used *before* the domain points at the server:
plain HTTP on `:80` (the raw IP), `auto_https off` because there is no domain to
fetch a certificate for. `switch-to-domain.sh` replaces it with `Caddyfile`, and
its `--rollback` puts it back.

Common to both:

* `encode zstd gzip`.
* `reverse_proxy 127.0.0.1:8000` with `flush_interval -1`, so live sessions and
  chat, which stream over WebSockets, stay open.
* Because the app listens on loopback only, the `X-Forwarded-For` /
  `X-Forwarded-Proto` headers Caddy adds cannot be forged from outside; uvicorn
  trusts them from 127.0.0.1, and Django sees the real visitor IP and `https`.

> **Warning — `/media/` must go to Django.** Paid course files (materials,
> memos, solutions, lesson and question media) are handed over only after
> `core.media_access` checks the viewer's enrolment or payment. Never add a
> `file_server`, `root` or `handle /media/*` block that reads the media folder
> from disk: it would serve every paid file to anyone who knows or guesses its
> URL. A one-line reminder of this stays in both files.

## 4. Moving to the domain: `switch-to-domain.sh`

Moves the LMS from plain HTTP on the server IP to
`https://learn.metanoiagroup.co.za`, leaving the public website alone.

```bash
sudo bash deploy/server/switch-to-domain.sh            # do it
sudo bash deploy/server/switch-to-domain.sh --rollback # undo it
```

It does nothing until the domain resolves to this server, so it is safe to run
early, and backs up every file it replaces in `~ubuntu/metanoia-backups/domain-switch/<timestamp>/`.
Must run as root.

Steps:

1. **DNS check** against public DNS (`dig @8.8.8.8`, else `getent`) — what Let's
   Encrypt and visitors see — not the VCN resolver, which caches "no such name"
   for up to an hour after the record is added. Exits until the A record points
   at the server (update DNS at the domain's DNS host, wait, re-run).
2. **Backup** of the systemd unit, `.env` (mode 600) and any existing Caddyfile.
3. **Install Caddy** if missing; stop it first so it cannot grab `:80` before the
   app moves off it; install and validate `Caddyfile`.
4. **Update `.env`**: drop the plain-HTTP and security overrides, add the domain
   to `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`, set `SITE_URL`, and append
   `USE_X_FORWARDED_PROTO=true`, `SECURE_HSTS_SECONDS=86400`,
   `SECURE_HSTS_INCLUDE_SUBDOMAINS=false`, `SECURE_HSTS_PRELOAD=false`. HSTS starts
   at one day without subdomains or preload (`www` and `mail.*` are served by other hosts);
   raise `SECURE_HSTS_SECONDS` to 31536000 once everything is confirmed.
5. **Point the Django `Site`** (pk 1) at the domain, run `check --deploy`,
   `collectstatic`.
6. **Move the app** to `127.0.0.1:8000` with 4 workers (installs
   `metanoia.service`) and start Caddy.
7. **Check** both services and the HTTPS and IP responses. If HTTPS fails, port
   443 may be closed in the cloud security list, or Let's Encrypt is still
   issuing (`journalctl -u caddy -n 50`).

`--rollback` restores the newest backup: unit file, `.env`, the `Site` domain
back to the IP, and either the previous Caddyfile (if Caddy already served the
IP) or Caddy disabled (if the app itself listened on `:80`).

Note: the script's `IP` and the IP redirect in `Caddyfile` are both hard-coded
(`<server-ip>`). Set both to the new host's public address before running the
script on a new host.

## 5. Secrets: `deploy/server/aws/`

The secrets live in AWS Secrets Manager as one JSON secret, by default
`metanoia/production` in region `af-south-1` (override with
`METANOIA_SECRET_ID` / `METANOIA_SECRET_REGION`). The EC2 instance role grants access, so
no AWS keys are stored on the box.

| File | What it does |
|---|---|
| `secret-keys.txt` | The keys that live in Secrets Manager, not in `.env`, one per line (lines starting `#` are ignored by the scripts). Everything else in `.env` is non-secret configuration. |
| `fetch-secrets.sh` | Reads the secret and writes `/run/metanoia/secrets.env` (tmpfs, root-only, `umask 077`, written to a temp file then moved). Rejects key names that are not `[A-Z_][A-Z0-9_]*` and multi-line values; leaves out empty values (unused integrations) so they do not shadow anything; double-quotes each value with `\ " ` $` escaped, so systemd and bash read it identically. Falls back to `/snap/bin/aws`. |
| `metanoia-secrets.service` | Oneshot unit (`RemainAfterExit=yes`, `Restart=on-failure`, 10 s) that runs `fetch-secrets.sh` after the network is up and before `metanoia.service`. |
| `secrets.conf` | systemd drop-in installed as `/etc/systemd/system/<unit>.service.d/secrets.conf` for the web service and every scheduler/mail service: `Requires=` + `After=metanoia-secrets.service` and `EnvironmentFile=/run/metanoia/secrets.env`. `settings.py`'s `load_dotenv` never overrides those values. |
| `manage.sh` | Runs `manage.py` by hand with the secrets loaded (they are not in `.env`): `deploy/server/aws/manage.sh shell`. Uses `sudo` to read the file. |
| `check-secrets.sh` | Proves the instance can read the secret and compares every listed key with `.env`. Prints key names and status only (`ok`, `MISSING from secret`, `empty (placeholder)`, `DIFFERS from .env`), never values; exits 1 on problems. |
| `sync-secret-from-env.sh` | One-time (or re-sync) copy of the listed keys from `.env` into the secret (creates it if missing). Values go straight to AWS through a root-only temp file that is shredded afterwards; nothing is printed. Needs the instance role to allow `CreateSecret` / `PutSecretValue` — remove that permission afterwards. |

Install and refresh:

```bash
sudo cp deploy/server/aws/metanoia-secrets.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now metanoia-secrets
# after changing a secret in AWS:
sudo systemctl restart metanoia-secrets metanoia
```

## 6. Background jobs: systemd units and timers

Everything scheduled runs through `python manage.py run_scheduled_jobs`. Each
job's cadence lives in `apps/scheduler/jobs.py`; a timer only decides when the
runner ticks, and `--tick <name>` / `--job <names>` select which jobs a unit may
run. Concurrent runs are safe: each unit is `Type=oneshot` (a run still going is
not started twice) and each job takes a database lock.

The production units in `deploy/scheduler/production/` are host-ready copies for
this deployment (paths under `/home/ubuntu`, `.environment` virtualenv, user
`ubuntu`), adapted from the generic `../metanoia-scheduler.service`.
All of them start after the network, PostgreSQL 16 and Redis.

Install any pair the same way:

```bash
sudo cp deploy/scheduler/production/<unit>.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now <unit>.timer
systemctl list-timers 'metanoia-scheduler*'
journalctl -u <unit> -f                    # watch it
sudo systemctl start <unit>                # run it now
```

Before enabling on a live database, note that the first tick runs **every** job
(each is due until it has run once): check with `run_scheduled_jobs --dry-run`
and `purge_closed_accounts --dry-run`.

| Unit | Schedule (SAST, `Africa/Johannesburg`) | Runs | Timeout | Notes |
|---|---|---|---|---|
| `metanoia-scheduler` | hourly 06:05–22:05, ±60 s random delay; missed ticks run at boot | every due job | 1800 s | The main tick. Hourly keeps background load low; each job's own interval still decides whether it runs. Nothing runs overnight; the first morning tick catches up. |
| `metanoia-scheduler-quick` | every 15 min 06:00–22:45; persistent | `--job session-reminders,send-broadcasts,shop-bookings,auto-notices,mail-sync` | 600 s | Session reminders, scheduled notifications, automatic "new material" notices, releasing unpaid 1-on-1 slots, full mail sync. An hour is too coarse here — a 10:50 session would miss its 30-minute reminder. Each job is a few cheap queries. |
| `metanoia-scheduler-sessions` | 06:00 and 22:00; persistent | `--tick sessions` | 7200 s | The after-class run: imports the day's classes from Teams, copies recordings and transcripts into OneDrive, publishes them, and has the Admin AI write and send each class its session pack. The hourly tick never runs these. Two hours is generous yet stops a wedged run reaching the next. |
| `metanoia-scheduler-recordings` | every 5 min at :30 s, day and night; not persistent | `--tick recordings` | 1500 s, `MemoryMax=600M` | Asks Teams about classes that ended in the last 36 hours and, as soon as a recording or transcript exists, files it to OneDrive, publishes it to the class and tells them. Classes with nothing new cost two Graph reads each. |
| `metanoia-scheduler-youtube` | 00:00 daily; persistent | `--tick youtube` | 10800 s | Copies up to six recordings from OneDrive to YouTube (the daily quota allows six), filing each into its institution + programme playlist. Downloads and uploads run one after another; three hours is generous. |
| `metanoia-scheduler-backup` | every 3 h at :30 (00:30, 03:30 … 21:30), ±60 s; persistent | `--tick backup` | 5400 s | Makes the automatic restore point (`backups/db/`), copies it to the OneDrive folder "metanoia DB BCKP", and deletes automatic backups older than 3 days in both places. Has its own timer because the main tick sleeps 22:05–06:05, which would leave a nine-hour gap. `pg_dump` plus upload must finish well inside the 3-hour gap. |
| `metanoia-mail` | every 2 min, day and night; not persistent | `--job mail-inbox` | 110 s | Checks every Metanoia Mail inbox so new mail raises the bell, its sound and the desktop alert (Web Push) within ~2 minutes even when nobody has a page open. The full mail sync stays on the quick tick. |

"Persistent" (`Persistent=true`) means a run missed during downtime happens at
boot instead of being skipped.

Shared settings and why:

* `Nice=15`, `IOSchedulingClass=idle`, `CPUWeight=20` (mail: `Nice=10`,
  `CPUWeight=30`): background work must never slow the site down.
* `TimeoutStartSec`: a tick should be quick; if a job wedges it must not block
  the next run forever.
* Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`.
  `ProtectHome=false` is deliberate: unlike the `/srv` layout the generic
  template assumes, this deployment lives under `/home/ubuntu`, and the runner
  writes `media/` and `backups/` inside the project directory.

## 7. Generic scheduler templates and cron

The files at the top of `deploy/scheduler/` are templates with `/srv`
placeholders; do not install them as-is. Use exactly **one** method per
database (cron, systemd, launchd or `--loop`) — the job lock keeps a second one
correct, but it doubles the work for nothing.

**`metanoia-scheduler.service` / `.timer`** — one tick every minute
(`OnBootSec=2min`, `OnUnitActiveSec=1min`, `AccuracySec=10s`, persistent).
`Type=oneshot` keeps each run's output in the journal as a discrete entry. Edit
`User`/`Group` and the three paths. `TimeoutStartSec=900`. Hardened with
`ProtectHome=read-only` and write access only to `media/` and `backups/` — the
runner only needs to read the project and talk to the database.

**`crontab.txt`** — the single cron line for Linux hosts with cron:

```cron
* * * * * cd /srv/metanoia/metanoia && /srv/metanoia/.venv/bin/python manage.py run_scheduled_jobs >> /var/log/metanoia-jobs.log 2>&1
```

* One entry is all that is needed; do not add a line per job, which would put
  the schedule in two places.
* Install with `crontab -e`, replacing `/srv/metanoia/metanoia`
  with the directory holding `manage.py` and `/srv/metanoia/.venv/bin/python`
  with the project's interpreter.
* cron runs with a minimal environment: use absolute paths and `cd` first. The
  project loads `.env` itself; anything else must be exported on the line.
* Rotate the log with `/etc/logrotate.d/metanoia-jobs`:

```text
/var/log/metanoia-jobs.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

## 8. Render: `render.yaml` and `render-build.sh`

**`render.yaml`** (repository root) is a Render Blueprint creating the web
service and its PostgreSQL database in one go: Render dashboard → New →
Blueprint → pick the repository. The Django project lives one directory down,
hence `rootDir: metanoia`.

Free-tier consequences:

* The web service sleeps after 15 minutes without traffic; the next request
  waits ~50 s for a cold start, and open WebSockets (live chat, notification
  push) drop when it sleeps.
* The free database **expires 30 days after creation**. Render e-mails first;
  when it goes, so does everything in it. Dump before then
  (`pg_dump "$DATABASE_URL" > backup.sql`) or upgrade.
* The container filesystem is ephemeral: uploads (profile pictures, course
  covers, chat attachments) are destroyed on every deploy and restart. Set
  `FILE_SERVER_BACKEND=s3` with the `AWS_*` credentials to keep them.

Service settings:

* `region: frankfurt` — the closest free region to South Africa. The database
  must be in the **same** region; cross-region means no private network and the
  internal `DATABASE_URL` would not resolve.
* `branch: main`, `buildCommand: ./render-build.sh`.
* Start command: gunicorn on `config.asgi:application` with the uvicorn worker,
  `--bind 0.0.0.0:$PORT --workers 1 --timeout 120`. ASGI, not WSGI: WSGI would
  answer ordinary HTTP while silently dropping every WebSocket handshake.
* **One worker**, deliberately: the free instance has 512 MB and a second worker
  risks the OOM killer (reported as a mysterious restart); and with
  `USE_REDIS=false` the Channels layer is in-process, so two workers mean two
  disconnected chat buses. The uvicorn worker still overlaps requests by running
  sync views in a thread pool. Raise it, with `USE_REDIS=true` and a Redis
  instance, when leaving the free tier.

Environment set by the blueprint:

| Key | Value | Why |
|---|---|---|
| `PYTHON_VERSION` | `3.14.3` | Django 6.0 supports 3.12–3.14; pinned to the version a real Render build resolved every wheel on (the series the project is developed against), rather than Render's moving default. |
| `SECRET_KEY` | generated | Fresh 64-character value, never in git. The committed `.env`'s key is public and must not sign production sessions, reset tokens or JWTs. |
| `DEBUG` | `false` | Settings already default it off on a platform, but the committed `.env` says `True` and this is not a setting to leave to inference. |
| `DATABASE_URL` | from `metanoia-db` | The **internal** connection string (private network, not reachable from a laptop). Locally, copy the External Database URL into `DATABASE_PUBLIC_URL`. |
| `PAYMENTS_ENABLED` | `false` | PayFast is unconfigured and `apps/finance/checks.py` refuses a production build on the sandbox or without merchant credentials. Set `true` once `PAYFAST_MERCHANT_ID` / `_KEY` / `_PASSPHRASE` are real and `PAYFAST_SANDBOX` is false. |
| `RUN_DB_WIPE` | `false` | **One-shot** — see below. |

Database: `metanoia-db`, free plan, Frankfurt, database
`metanoia`, user `metanoia`.

**`render-build.sh`** runs on every deploy before the new instance takes traffic,
with the service's environment already set (so `DATABASE_URL` points at the
attached database and settings resolve as at run time). `errexit`, `nounset`,
`pipefail`: any failing step fails the deploy instead of shipping a half-built
image.

1. **Dependencies**: upgrade pip, install `requirements.txt`.
2. **Static files**: `collectstatic` into `staticfiles/`, compressed by
   WhiteNoise, which serves them from the app process (no web server in front on
   the free tier). Must happen before the instance starts or the site loads
   without CSS.
3. **Database**: `migrate --noinput`, unless `RUN_DB_WIPE=true`, in which case
   `python .academic_seed.py --fresh --yes` **drops every table**, re-runs the
   migrations (so no separate migrate), installs the academic structure and
   creates the team accounts. `--yes` skips the "Type 'FRESH' to continue"
   prompt nothing can answer in a build container. `SEED_ACCOUNT_PASSWORD` (a
   secret env var) is the accounts' first password; without it the seed prints a
   random one in the build log.
4. **Refuse an unsafe configuration**: `check --deploy --fail-level ERROR`, which
   adds Django's production checks (TLS redirect, secure cookies, HSTS, host
   allow-list) to the project's guards in `apps/diagnostics/deploy_checks.py` and
   `apps/finance/checks.py`. Failing here costs a deploy; failing in production
   costs rather more.

> **Warning — `RUN_DB_WIPE` is read on every deploy.** Leave it `true` and every
> push to the branch destroys all production data: students, marks, uploads,
> invoices. Set it to `true` for the first deploy only (the database starts empty
> and needs the schema, the academic spine and the base accounts), then set it
> back to `false` in the Render dashboard **before** pushing again.

## 9. Maintenance scripts

* `deploy/check_integrations.py` — one-shot, read-only health check of every
  external integration (database, Redis, SMTP, Google sign-in, Graph/Teams,
  OneDrive backups, YouTube, Admin AI, PayFast). `--write` also proves Teams
  meeting creation, calendar writes and OneDrive uploads with throwaway objects.
  Secrets are masked; see its module docstring.
* `deploy/mint_youtube_token.py` — one-time helper to mint
  `YOUTUBE_REFRESH_TOKEN` and discover `YOUTUBE_CHANNEL_ID`; see its module
  docstring.

## 10. Repository attributes: `.gitattributes`

Font and image binaries (`*.woff`, `*.woff2`, `*.ttf`, `*.otf`, `*.eot`, `*.png`,
`*.jpg`, `*.jpeg`, `*.gif`, `*.ico`, `*.webp`, `*.pdf`) are marked `binary`, so
git never normalises line endings or re-encodes them. Every bundled icon font in
the first commit had been mojibake-corrupted (read as text and re-saved as
UTF-8), which inflated each file about 1.8× and made every icon render as an
empty box. `apps/myhub/checks.py:check_icon_fonts_intact` guards the working
tree; these rules keep git out of the binaries entirely.
