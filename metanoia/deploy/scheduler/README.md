# Running the hub's background jobs unattended

Everything the hub needs to do on a schedule — closing class registers, pulling
Teams attendance, recomputing class positions, sending deadline reminders,
purging closed accounts — is driven by one command:

```bash
python manage.py run_scheduled_jobs
```

Each job's cadence lives in `apps/scheduler/jobs.py`, **not** in your crontab. The
command ticks, asks each job whether its interval has elapsed, and runs the ones
that are due. That means:

* one scheduler entry to install, whatever jobs exist now or later;
* changing a cadence is a code change, reviewed like any other;
* a tick missed during an outage runs on the next tick instead of being skipped.

Concurrent runs are safe — each job takes a database lock for its duration, so a
slow job never gets started twice.

## Pick one installation method

| Method | Best for | File |
|---|---|---|
| **cron** | Linux servers | `crontab.txt` |
| **systemd timer** | Linux servers with systemd (better logging) | `metanoia-scheduler.service`, `metanoia-scheduler.timer` |
| **launchd** | macOS (dev machines, small deployments) | `com.metanoiagroup.scheduler.plist` |
| **loop mode** | Containers, or anywhere without a system scheduler | `--loop` (see below) |

Do **not** install two of them against the same database — the lock keeps it
correct, but you would be doing double the work for nothing.

### cron

```bash
crontab -e     # then paste the line from crontab.txt, with your real paths
```

### systemd

```bash
sudo cp metanoia-scheduler.service metanoia-scheduler.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metanoia-scheduler.timer
systemctl list-timers metanoia-scheduler.timer      # confirm it is scheduled
journalctl -u metanoia-scheduler -f                 # watch it run
```

### This deployment

The production host runs the project from `/home/ubuntu/projects/Metanoia/metanoia`
with the in-tree `.environment/` virtualenv, as the `ubuntu` user, and has no
`cron` installed — so it uses the systemd timer. `production/` holds the two
files already adapted to those paths (the copies at the top level are generic
templates with `/srv` placeholders; do not install those as-is):

```bash
sudo cp deploy/scheduler/production/metanoia-scheduler.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metanoia-scheduler.timer
systemctl list-timers metanoia-scheduler.timer
```

The other units in `production/` are installed the same way — each runs only
the jobs marked with its `tick=` in `apps/scheduler/jobs.py`. The database
backup has its own (it must run overnight too, when the main tick sleeps):

```bash
sudo cp deploy/scheduler/production/metanoia-scheduler-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metanoia-scheduler-backup.timer   # 00:30, 03:30 … 21:30 SAST
```

Note that the first tick runs **every** job, because each one is due until it has
run once. Check what that means before enabling it on a live database:

```bash
python manage.py run_scheduled_jobs --dry-run          # what is due right now
python manage.py purge_closed_accounts --dry-run       # the only destructive job
```

### launchd (macOS)

```bash
# Edit the paths in the plist first, then:
cp com.metanoiagroup.scheduler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.metanoiagroup.scheduler.plist
launchctl list | grep metanoia                      # confirm it is loaded
tail -f /tmp/metanoia-scheduler.log
```

### Loop mode (containers)

No system scheduler needed — run it as a long-lived process and let your
supervisor restart it:

```bash
python manage.py run_scheduled_jobs --loop --interval 60
```

It handles `SIGTERM`/`SIGINT`, finishing the current tick before exiting, so
`docker stop` is clean.

## Checking on it

```bash
python manage.py run_scheduled_jobs --list       # schedule + last result per job
python manage.py run_scheduled_jobs --dry-run    # what is due right now
```

Django admin → **Background jobs → Scheduled jobs** shows the same thing with
failure counts and the captured output of the last run. A job that has failed
repeatedly shows its consecutive-failure count in red.

## Running one job by hand

```bash
python manage.py run_scheduled_jobs --job rank-classes --force
```

The underlying commands still work on their own, which is what you want when
debugging:

```bash
python manage.py finalise_attendance --dry-run
python manage.py sync_teams_meetings --limit 5
python manage.py rank_classes --subject 12
```

## Adding a job

Add one `Job(...)` to `JOBS` in `apps/scheduler/jobs.py`. It takes either a
management command:

```python
Job(name='my-job', every=30 * MINUTE, command='my_command', options={'verbosity': 1})
```

or any importable callable, which may return a string to be recorded as output:

```python
Job(name='my-job', every=HOUR, dotted='apps.myapp.tasks:do_the_thing')
```

Nothing else to wire — the runner picks it up, and the first tick creates its
tracking row.
