"""Callable jobs for the scheduler (see :mod:`apps.scheduler.jobs`).

Each returns a short string, which the runner records as that run's output so
``manage.py run_scheduled_jobs --list`` shows what actually happened.
"""


def run_pipeline():
    """Advance every finished session one step through the after-class chain."""
    from . import pipeline
    return pipeline.run()


def run_pipeline_recent():
    """The quick after-class check: publish a recording minutes after Teams has it."""
    from . import pipeline
    return pipeline.run(limit=10, recent_only=True)


def run_reminders():
    """Send any session reminders that have come due."""
    from . import reminders
    return reminders.run()


def run_ai_followup():
    """Write the Claude follow-up pack for sessions that do not have one."""
    from . import ai_followup
    return ai_followup.run()


def run_calendar_subscriptions():
    """Re-import every connected Google / Outlook / Apple calendar."""
    from . import subscriptions
    return subscriptions.run()


def run_session_summaries():
    """Import ended Teams classes, then publish each one's summary when it exists."""
    from . import summaries
    return summaries.run()


def run_session_summaries_backfill():
    """Once a day: sweep back for missed classes and late summaries."""
    from . import summaries
    return summaries.run_backfill()
