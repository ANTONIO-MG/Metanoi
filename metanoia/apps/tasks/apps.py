from django.apps import AppConfig


class TasksConfig(AppConfig):
    """Retired tasks app — migrations only (see :mod:`apps.tasks.models`).

    The label stays ``orgtasks`` so the existing migration history (and the
    ``accounts`` / ``myhub`` migrations that depend on it) still resolves.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tasks'
    label = 'orgtasks'
    verbose_name = 'Tasks (retired)'
