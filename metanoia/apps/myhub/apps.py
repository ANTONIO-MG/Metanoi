"""App configuration for MyHub, the dashboard app."""

from django.apps import AppConfig


class MyHubConfig(AppConfig):
    """App config for the MyHub dashboard."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.myhub'
    verbose_name = 'MyHub Dashboard'

    def ready(self):
        """Register the template check and the navbar counter signals.

        Importing :mod:`apps.myhub.checks` registers the startup guard that
        compiles all first-party templates, so a broken tag fails
        ``manage.py check`` instead of a live request. The counter signals
        clear a user's cached navbar counters when the rows behind them change
        (new notification, chat message, cart item, task, enrolment …).
        """
        from . import checks  # noqa: F401

        from core.counters import connect_signals
        connect_signals()
