"""Django app configuration for :mod:`apps.finance` (invoices, estimates, payments and the books).
"""

from django.apps import AppConfig


class FinanceConfig(AppConfig):
    """App config for the finance app.

    ``ready()`` imports :mod:`apps.finance.signals` so invoice totals/status
    stay in sync and CRUD events are audited, and :mod:`apps.finance.checks` so
    a production build cannot boot pointed at PayFast's sandbox.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.finance'
    verbose_name = 'Finance'

    def ready(self):
        """Import the signal receivers and system checks, which register themselves on import."""
        from . import checks, signals  # noqa: F401  (imported for side effects)
