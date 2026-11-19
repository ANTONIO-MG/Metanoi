"""App configuration for :mod:`apps.revision` — wires up its signal handlers on start-up."""
from django.apps import AppConfig


class RevisionConfig(AppConfig):
    """Spaced-repetition revision built from the student's own mistakes."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.revision'
    label = 'revision'
    verbose_name = 'Revision'

    def ready(self):
        """Import :mod:`apps.revision.signals` so its receivers are registered."""
        from . import signals  # noqa: F401
