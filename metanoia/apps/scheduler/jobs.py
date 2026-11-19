"""The job registry — what runs unattended, and how often.

Adding a job means adding one :class:`Job` to :data:`JOBS`. The runner
(``manage.py run_scheduled_jobs``) does the rest: interval checking, locking,
error capture and health bookkeeping.

A job is either a management command (``command='name'``, plus optional
``args``) or any importable callable (``dotted='pkg.mod:function'``). Callables
may return a string, which is recorded as the run's output.

Intervals are advisory: a job runs on the first tick *after* its interval has
elapsed, so the real cadence is bounded below by how often the runner ticks. One
cron entry every minute gives every job minute-level resolution.
"""

from dataclasses import dataclass, field

LOCK_STALE_AFTER = 60 * 60
"""A held lock older than this (one hour) is assumed to belong to a killed process."""


@dataclass(frozen=True)
class Job:
    """One unattended job and how often it runs.

    :param name: unique job name (also the :class:`~apps.scheduler.models.JobRun` key).
    :param every: seconds between runs.
    :param command: management command name — or
    :param dotted: ``'pkg.module:callable'`` alternative; exactly one of the two is required.
    :param args: positional args for the command.
    :param options: keyword options for the command.
    :param tick: which timer runs the job: ``main`` (the hourly tick),
        ``sessions`` (the twice-daily after-class run, 06:00 and 22:00 SAST),
        ``recordings`` (the five-minutely recording check), ``youtube`` (the
        nightly backup, 00:00 SAST) or ``backup`` (the database backup, every 3
        hours round the clock). A tick only ever runs its own jobs; ``--job`` can
        still name any job directly.
    """

    name: str
    every: int
    description: str = ''
    command: str = ''
    args: tuple = ()
    options: dict = field(default_factory=dict)
    dotted: str = ''
    enabled: bool = True
    tick: str = 'main'

    def __post_init__(self):
        """Reject a job that names both, or neither, of ``command`` and ``dotted``."""
        if bool(self.command) == bool(self.dotted):
            raise ValueError(f'Job {self.name!r} needs exactly one of command= or dotted=')


MINUTE = 60
HOUR = 60 * MINUTE


JOBS = [
    Job(
        name='shop-bookings',
        every=5 * MINUTE,
        description='Release 1-on-1 slots held by orders that were never paid, mark '
                    'sessions that have ended as completed, re-send unpaid one-on-one '
                    'invoices and charge one-on-one overtime.',
        dotted='apps.shop.booking:run_housekeeping',
    ),
    Job(
        name='shop-tracking',
        every=2 * HOUR,
        description='Refresh Courier Guy tracking for parcels on their way and tell buyers '
                    'when they are delivered. Idle until COURIER_GUY_API_KEY is set.',
        dotted='apps.shop.fulfilment:refresh_tracking',
    ),
    Job(
        name='mail-sync',
        every=2 * MINUTE,
        description='Fetch new mail into every connected Metanoia mailbox and raise a notification '
                    'for each new inbox message. Pages open in a browser also check every minute.',
        dotted='apps.mailbox.sync:sync_all',
    ),
    Job(
        name='mail-inbox',
        every=MINUTE,
        description='Check every Metanoia mailbox inbox for new mail (bell, sound and desktop alert). '
                    'Run every 2 minutes, day and night, by metanoia-mail.timer.',
        dotted='apps.mailbox.sync:sync_inboxes',
    ),
    Job(
        name='finance-daily',
        every=6 * HOUR,
        description='Invoice reminders (3 days before, on the day, 7 days after), expire '
                    'estimates past their date, and book recurring expenses that fell due.',
        dotted='apps.finance.documents:run_daily',
    ),
    Job(
        name='finalise-attendance',
        every=15 * MINUTE,
        description='Close the register for ended class sessions: anyone below the '
                    'attendance threshold becomes absent and no-shows get a row.',
        command='finalise_attendance',
        options={'verbosity': 1},
    ),

    Job(
        name='session-reminders',
        every=5 * MINUTE,
        description='Send the 24-hour and 30-minute reminders for upcoming sessions, each '
                    "rendered in the recipient's own time zone.",
        dotted='apps.livesessions.tasks:run_reminders',
    ),

    Job(
        name='session-summaries',
        every=1 * HOUR,
        tick='sessions',
        description='Import ended classes (and one-on-ones with a registered student) from the '
                    'Teams calendar, then publish a summary for any class whose Teams Copilot '
                    'summary is in (or, 48 hours after the class without one, from its transcript).',
        dotted='apps.livesessions.tasks:run_session_summaries',
    ),
    Job(
        name='session-summaries-backfill',
        every=20 * HOUR,
        tick='sessions',
        description='Once a day, run backwards over the last 120 days of the Teams calendar: add '
                    'any class the platform missed and publish any summary that has appeared '
                    'since (quietly, for classes older than a week).',
        dotted='apps.livesessions.tasks:run_session_summaries_backfill',
    ),
    Job(
        name='sync-teams-meetings',
        every=1 * HOUR,
        tick='sessions',
        description='Pull the Graph attendance report for finished Teams meetings — it is '
                    'authoritative for time in call.',
        command='sync_teams_meetings',
        options={'verbosity': 1},
    ),
    Job(
        name='session-pipeline',
        every=1 * HOUR,
        tick='sessions',
        description='For each finished session: find its recording and transcript in Teams, copy '
                    'them into the OneDrive tree, publish them to the class (or the one-on-one '
                    'student) and tell them. (The YouTube backup runs separately, nightly.)',
        dotted='apps.livesessions.tasks:run_pipeline',
    ),
    Job(
        name='session-pipeline-recent',
        every=4 * MINUTE,
        tick='recordings',
        description='For classes that ended in the last 36 hours: as soon as Teams has published '
                    'the recording or transcript, file it to OneDrive, publish it to the class and '
                    'tell them; then wait for the Teams Copilot summary (asked every 30 minutes, up '
                    'to 48 hours) and build the class summary from it. Backs off after a failure; '
                    'the twice-daily run sweeps the rest.',
        dotted='apps.livesessions.tasks:run_pipeline_recent',
    ),
    Job(
        name='system-sample',
        every=4 * MINUTE,
        tick='recordings',
        description='Record the server\'s CPU, memory, disk and network and the last few minutes of '
                    'site traffic for the System monitor (/diagnostics/system/); keeps 90 days.',
        dotted='apps.diagnostics.monitor:record_sample',
    ),
    Job(
        name='session-ai-followup',
        every=1 * HOUR,
        tick='sessions',
        description='Have the admin Claude layer write the full session summary, the next '
                    "session's agenda and the action items; file them to OneDrive and the module, "
                    'assign the action items as tasks, and send the class (or the one-on-one '
                    'student) one message with the summary, agenda, actions and the recording '
                    'and transcript links.',
        dotted='apps.livesessions.tasks:run_ai_followup',
    ),

    Job(
        name='calendar-subscriptions',
        every=30 * MINUTE,
        description='Re-import the Google / Outlook / Apple calendars users have connected, '
                    'so the Metanoia calendar and the free-slot finder see their real commitments.',
        dotted='apps.livesessions.tasks:run_calendar_subscriptions',
    ),

    Job(
        name='youtube-backup',
        every=20 * HOUR,
        tick='youtube',
        description='Copy up to six class recordings from OneDrive to YouTube (oldest first) '
                    'and file each into the playlist for its institution and programme. A '
                    'backup only: students keep watching from OneDrive.',
        dotted='apps.livesessions.youtube_backup:run',
    ),
    Job(
        name='scan-youtube',
        every=20 * HOUR,
        tick='youtube',
        description='Re-import the YouTube channel so new recordings appear in the '
                    'past-sessions library, if quota is left after the backups. Playlist '
                    'mappings set by staff are never overwritten; new playlists arrive '
                    'unmapped for a human to place.',
        dotted='apps.livesessions.youtube_backup:run_scan',
    ),

    Job(
        name='rank-classes',
        every=6 * HOUR,
        description='Recompute persisted class positions and cohort averages.',
        command='rank_classes',
        options={'verbosity': 1},
    ),

    Job(
        name='send-broadcasts',
        every=MINUTE,
        description='Send the notifications staff scheduled in the notifications centre once '
                    'their time comes.',
        dotted='apps.communication.broadcast:send_due',
    ),
    Job(
        name='whatsapp-media',
        every=15 * MINUTE,
        description='Save photos, voice notes and files people sent on WhatsApp before Meta '
                    'deletes its copy (about 30 days). Does nothing until WhatsApp is set up.',
        dotted='apps.communication.whatsapp_inbox:fetch_pending_media',
    ),
    Job(
        name='auto-notices',
        every=10 * MINUTE,
        description='Tell students and educators about new material, lessons, videos, tests '
                    'opening, results, solutions and tasks — grouped per module, once each.',
        dotted='apps.communication.auto:run',
    ),
    Job(
        name='weekly-summary',
        every=HOUR,
        description='Monday from 06:00: each student\'s week ahead (deadlines, tests, classes, '
                    'new material, exam countdown) and each educator\'s teaching week.',
        dotted='apps.communication.weekly:run',
    ),
    Job(
        name='re-engagement',
        every=24 * HOUR,
        description='Nudge students who have not signed in for 7 days but still have work on '
                    '(at most once a fortnight each).',
        dotted='apps.communication.weekly:run_reengage',
    ),
    Job(
        name='risk-scan',
        every=24 * HOUR,
        description='Score every student for risk (inactivity, low marks, overdue work) and '
                    'open or update a flag for anyone at 30+, for the At-risk students page.',
        dotted='apps.staffdesk.views_academic:scan_risk',
    ),
    Job(
        name='database-backup',
        every=3 * HOUR,
        tick='backup',
        description='Make the automatic restore point of the database every 3 hours '
                    '(backups/db/; automatic backups older than 3 days are deleted, never the '
                    'newest) and copy it to the "metanoia DB BCKP" folder in OneDrive, which '
                    'keeps the same 3 days — see Staff desk → Backups.',
        dotted='apps.staffdesk.backups:scheduled_backup',
    ),
    Job(
        name='deadline-reminders',
        every=30 * MINUTE,
        description='Notify students about tasks, assessments and live sessions coming due.',
        dotted='apps.scheduler.tasks:run_deadline_reminders',
    ),
    Job(
        name='trial-ending-reminders',
        every=HOUR,
        description="E-mail students whose free week ends within 24 hours and whose "
                    "registration invoice is still unpaid — invoice attached, pay link "
                    "included. Sends once per invoice; skips anyone already settled or "
                    "marked paid by admin/staff.",
        dotted='apps.accounts.trial_reminders:send_trial_reminders',
    ),

    Job(
        name='purge-closed-accounts',
        every=HOUR,
        description='Permanently delete archived accounts whose grace period is over: accounts '
                    'deleted by an administrator after 5 days, accounts closed by their owner '
                    'after 30. Each is parked in the archive park first (see Closed accounts).',
        command='purge_closed_accounts',
        options={'verbosity': 1},
    ),
]
"""Every registered job, grouped by the timer that runs it.

* **main** (the hourly tick) — shop bookings and tracking, mail, finance,
  attendance, reminders and the other housekeeping jobs.
* **sessions** — after class, twice a day at 22:00 and 06:00 SAST
  (``deploy/scheduler/production/metanoia-scheduler-sessions.timer``).
  The jobs run in order in the same run: bring in the day's classes, pull
  their recordings and transcripts, then let the AI write and send each class
  its pack.
* **recordings** — every 5 minutes, day and night
  (``metanoia-scheduler-recordings.timer``). The twice-daily run meant
  a Saturday-morning class was not watchable until 22:00; this asks Teams
  every few minutes about classes that ended in the last 36 hours and files,
  publishes and announces each recording as soon as Teams has it.
* **youtube** — once a night at 00:00 SAST
  (``metanoia-scheduler-youtube.timer``). YouTube is a *backup* of the
  class recordings — students watch from OneDrive — so it never needs to be
  quick: one run a night uses the whole daily quota on up to six recordings,
  then scans the channel only if quota is left over.
* **backup** — the database backup every 3 hours round the clock
  (``metanoia-scheduler-backup.timer``, 00:30, 03:30 … 21:30 SAST),
  because the hourly main tick sleeps 22:05–06:05, which would leave a
  9-hour gap.
"""

JOBS_BY_NAME = {job.name: job for job in JOBS}
