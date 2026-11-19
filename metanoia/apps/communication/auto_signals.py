"""Signal hooks that queue automatic notifications (see :mod:`apps.communication.auto`).

Only the *moment something becomes news* queues a notice: a row created already
published, or one changing into published / open. Editing last year's blueprint
must not announce it again, so each hook compares with the stored row first.
Queueing is one cheap insert; sending happens later in the scheduler job, never
inside the request that saved the row.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.assessments.models import Assessment
from apps.learning.models import Lesson, ModuleMaterial
from apps.livesessions.models import YouTubeVideo

from . import auto

logger = logging.getLogger('apps')


def _quiet(instance):
    """A row saved with ``_quiet = True`` is announced by whoever saved it.

    The live-session pipeline sets this on the summary lesson and the recording
    and summary materials: the class is sent one session message instead of a
    notice per row.
    """
    return bool(getattr(instance, '_quiet', False))


def _later(fn, *args):
    """Queue after the transaction commits, and never let a hook break a save."""
    def _run():
        """Run the hook, logging and reporting (``NOTF-9004``) any failure instead of
        raising.
        """
        try:
            fn(*args)
        except Exception as exc:   # pragma: no cover
            logger.exception('auto-notice: %s failed', getattr(fn, '__name__', fn))
            from core.errors import report
            report('NOTF-9004', exc, context={'hook': getattr(fn, '__name__', str(fn)),
                                              'object': repr(args[0])[:120] if args else ''})
    transaction.on_commit(_run)


def _stash(sender, instance, fields):
    """Remember the stored values of ``fields`` on the instance before it saves."""
    if not instance.pk:
        instance._auto_before = None
        return
    instance._auto_before = sender.objects.filter(pk=instance.pk).values(*fields).first()


@receiver(pre_save, sender=ModuleMaterial)
def _material_before(sender, instance, **kwargs):
    """Stash a material's stored ``is_published`` before it saves."""
    _stash(sender, instance, ['is_published'])


@receiver(post_save, sender=ModuleMaterial)
def _material_saved(sender, instance, created, raw=False, **kwargs):
    """Queue a material notice when it is created published or becomes published."""
    if raw or not instance.is_published or _quiet(instance):
        return
    before = getattr(instance, '_auto_before', None)
    if created or (before is not None and not before['is_published']):
        _later(auto.queue_material, instance)


@receiver(pre_save, sender=Lesson)
def _lesson_before(sender, instance, **kwargs):
    """Stash a lesson's stored ``status`` before it saves."""
    _stash(sender, instance, ['status'])


@receiver(post_save, sender=Lesson)
def _lesson_saved(sender, instance, created, raw=False, **kwargs):
    """Queue a lesson notice when it is created, or changes into, published or scheduled.
    """
    if raw or instance.status not in (Lesson.STATUS_PUBLISHED, Lesson.STATUS_SCHEDULED) or _quiet(instance):
        return
    before = getattr(instance, '_auto_before', None)
    if created or (before is not None and before['status'] != instance.status):
        _later(auto.queue_lesson, instance)


@receiver(pre_save, sender=Assessment)
def _assessment_before(sender, instance, **kwargs):
    """Stash an assessment's stored ``status`` before it saves."""
    _stash(sender, instance, ['status'])


@receiver(post_save, sender=Assessment)
def _assessment_saved(sender, instance, created, raw=False, **kwargs):
    """On a status change, queue an "opening" notice (open/scheduled) or a solutions notice
    (closed).
    """
    if raw:
        return
    before = getattr(instance, '_auto_before', None)
    changed = created or (before is not None and before['status'] != instance.status)
    if not changed:
        return
    if instance.status in (Assessment.STATUS_OPEN, Assessment.STATUS_SCHEDULED):
        _later(auto.queue_assessment_open, instance)
    elif instance.status == Assessment.STATUS_CLOSED:
        _later(auto.queue_solutions, instance)


@receiver(post_save, sender=YouTubeVideo)
def _video_found(sender, instance, created, raw=False, **kwargs):
    """Queue a notice for a newly found YouTube video."""
    if created and not raw:
        _later(auto.queue_video, instance)
