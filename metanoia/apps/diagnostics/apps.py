"""App configuration for the diagnostics app (error log, error catalogue and deploy checks)."""

from django.apps import AppConfig


class DiagnosticsConfig(AppConfig):
    """Diagnostics app.

    ``ready()`` imports :mod:`apps.diagnostics.deploy_checks` so a build that is
    configured to serve publicly with development security refuses to start.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.diagnostics'
    verbose_name = 'Diagnostics & error log'

    def ready(self):
        """Import :mod:`apps.diagnostics.deploy_checks` for its side effects: it registers the deploy safety checks."""
        from . import deploy_checks  # noqa: F401  (imported for side effects)
