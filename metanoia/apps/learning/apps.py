"""Django app configuration for :mod:`apps.learning`."""

from django.apps import AppConfig


class LearningConfig(AppConfig):
    """Study content: modules, lessons (with resources & versioning) and the
    study-session timer that measures actual studying.

    ``ready()`` registers file clean-up so lesson media is removed when replaced or
    deleted, and imports the signals that keep each offering's chat group in step
    with who may open the module.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.learning'
    label = 'learning'
    verbose_name = 'Learning'

    def ready(self):
        """Register media clean-up for lesson and material files, and connect the signals."""
        from core.file_cleanup import register
        register(self.get_model('LessonResource'), ['file'])
        register(self.get_model('Lesson'), ['cover_image', 'author_avatar'])
        register(self.get_model('LessonBlock'), ['media'])
        register(self.get_model('ModuleMaterial'), ['file'])
        from . import signals  # noqa: F401
