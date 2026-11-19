"""App configuration for the reports app (grades and certificates)."""

from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Reporting engine: per-module weightings, computed report-card grades and
    auto-generated certificates."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.reports'
    label = 'reports'
    verbose_name = 'Reports & Certificates'

    def ready(self):
        """Connect the grade and certificate signal handlers."""
        from . import signals  # noqa: F401
