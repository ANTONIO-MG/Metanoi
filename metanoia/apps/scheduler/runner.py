"""Executes registered jobs, records how they went, and keeps them from overlapping.

The scheduling decision is deliberately in the database rather than in crontab:
one cron entry ticks the runner, and each job's own interval decides whether it
is due. That keeps cadence next to the job definition and survives a missed tick
— a job that should have run during an outage runs on the next tick instead of
being skipped until its next slot.
"""

import io
import logging
import sys
import traceback
from contextlib import redirect_stdout, redirect_stderr
from importlib import import_module

from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from .jobs import JOBS_BY_NAME, LOCK_STALE_AFTER
from .models import JobRun

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 4000
"""Output longer than this is truncated before being stored."""


def _resolve_dotted(path):
    """Import ``'pkg.module:callable'`` and return the callable."""
    module_path, _, attr = path.partition(':')
    if not attr:
        raise ValueError(f"Expected 'module:callable', got {path!r}")
    return getattr(import_module(module_path), attr)


GRACE_SECONDS = 5 * 60
"""The most a job may run early to absorb timer jitter (see :func:`is_due`)."""


def is_due(job, state, *, now=None):
    """Whether ``job`` should run on this tick.

    A job that has never run is due now. Ticks come from a timer that is itself a
    few seconds late or early, so a job is due slightly before its full interval
    (up to :data:`GRACE_SECONDS`, at most 10% of the interval): without this, an
    hourly job that last started at 06:05:02 would be "not yet due" at 07:05:00
    and slip to every two hours.
    """
    if not job.enabled:
        return False
    if state.last_started_at is None:
        return True
    now = now or timezone.now()
    grace = min(GRACE_SECONDS, job.every * 0.1)
    return (now - state.last_started_at).total_seconds() >= job.every - grace


def _acquire(state, *, now=None):
    """Take the run lock, unless another runner holds a fresh one.

    ``select_for_update`` plus the timestamp check makes two concurrent runners
    safe: the loser sees the lock and skips. A lock older than
    :data:`~apps.scheduler.jobs.LOCK_STALE_AFTER` is assumed to belong to a
    process that died mid-run and is taken over.
    """
    now = now or timezone.now()
    with transaction.atomic():
        row = JobRun.objects.select_for_update().get(pk=state.pk)
        if row.locked_at and (now - row.locked_at).total_seconds() < LOCK_STALE_AFTER:
            return False
        row.locked_at = now
        row.last_started_at = now
        row.last_status = JobRun.STATUS_RUNNING
        row.save(update_fields=['locked_at', 'last_started_at', 'last_status'])
    state.refresh_from_db()
    return True


def run_job(job, *, force=False, now=None):
    """Run one job if it is due (or ``force``). Returns ``(ran, ok, output)``.

    Whatever the job prints is captured so it lands in the run record instead of
    only in a cron mail nobody reads.
    """
    now = now or timezone.now()
    state, _ = JobRun.objects.get_or_create(name=job.name)

    if not force and not is_due(job, state, now=now):
        return False, True, ''
    if not _acquire(state, now=now):
        logger.info('scheduler: %s is already running elsewhere — skipping', job.name)
        return False, True, 'locked'

    started = timezone.now()
    buffer = io.StringIO()
    ok, output = True, ''
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            if job.command:
                call_command(job.command, *job.args, **job.options)
            else:
                result = _resolve_dotted(job.dotted)()
                if result:
                    print(result)
        output = buffer.getvalue().strip()
    except Exception:
        ok = False
        output = (buffer.getvalue() + '\n' + traceback.format_exc()).strip()
        logger.exception('scheduler: job %s failed', job.name)
        from core.errors import report
        report('SCHD-9001', sys.exc_info()[1], context={'job': job.name})

    finished = timezone.now()
    state.refresh_from_db()
    state.locked_at = None
    state.last_finished_at = finished
    state.last_duration_ms = int((finished - started).total_seconds() * 1000)
    state.last_status = JobRun.STATUS_OK if ok else JobRun.STATUS_FAILED
    state.last_output = (output or '')[:MAX_OUTPUT_CHARS]
    state.run_count += 1
    if ok:
        state.consecutive_failures = 0
    else:
        state.failure_count += 1
        state.consecutive_failures += 1
    state.save(update_fields=[
        'locked_at', 'last_finished_at', 'last_duration_ms', 'last_status',
        'last_output', 'run_count', 'failure_count', 'consecutive_failures'])
    return True, ok, output


def pending_migrations():
    """Names of migrations the code on disk has but the database does not.

    Code is deployed before ``migrate`` runs; a tick that lands in between runs
    new code against the old schema and fails on every missing column (it did,
    twice). The runner checks this first and sits the tick out instead.
    """
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:           # pragma: no cover - never block the runner on the check itself
        logger.exception('scheduler: could not check for pending migrations')
        return []
    return [f'{migration.app_label}.{migration.name}' for migration, _backwards in plan]


def tick(*, only=None, force=False, now=None, timer='main'):
    """Run every due job once. Returns a list of ``(name, ran, ok, output)``.

    Without ``only``, a tick runs the jobs that belong to ``timer`` (see
    :attr:`Job.tick`) — so the hourly tick never runs the twice-daily
    after-class jobs, and the after-class run runs nothing else.

    One job's failure never stops the others — each is isolated inside
    :func:`run_job`.
    """
    now = now or timezone.now()
    pending = pending_migrations()
    if pending:
        logger.warning('scheduler: %s migration(s) not applied yet (%s) — skipping this tick; '
                       'run manage.py migrate', len(pending), ', '.join(pending[:5]))
        return [('(all)', False, True, f'skipped: {len(pending)} migration(s) pending')]
    if isinstance(only, str):
        only = [n.strip() for n in only.split(',') if n.strip()]
    names = list(only) if only else [n for n, j in JOBS_BY_NAME.items() if j.tick == timer]
    results = []
    for name in names:
        job = JOBS_BY_NAME.get(name)
        if job is None:
            results.append((name, False, False, f'unknown job {name!r}'))
            continue
        ran, ok, output = run_job(job, force=force, now=now)
        results.append((name, ran, ok, output))
    return results
