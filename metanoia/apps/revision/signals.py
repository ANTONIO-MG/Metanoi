"""Turn a freshly marked attempt into review cards.

Fires on the transition to ``marked`` only — the stored status is read in
``pre_save`` — and runs after the marking transaction commits. Anything that
goes wrong here is logged and swallowed: revision must never break marking.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

MARKED = 'marked'


@receiver(pre_save, sender='assessments.AssessmentAttempt')
def remember_previous_status(sender, instance, update_fields=None, **kwargs):
    """Note on the instance whether the attempt was already marked before this save.

    Draft autosaves and timer ticks never touch status, so they cost no query.
    """
    instance._revision_was_marked = None
    if instance.status != MARKED or (update_fields is not None and 'status' not in update_fields):
        return
    try:
        if instance.pk:
            previous = sender.objects.filter(pk=instance.pk).values_list('status', flat=True).first()
            instance._revision_was_marked = previous == MARKED
        else:
            instance._revision_was_marked = False
    except Exception:  # pragma: no cover - never block the save
        logger.exception('revision: could not read previous attempt status')


@receiver(post_save, sender='assessments.AssessmentAttempt')
def build_cards_on_marked(sender, instance, **kwargs):
    """On the first transition to ``marked``, build review cards once the transaction commits."""
    if instance.status != MARKED or getattr(instance, '_revision_was_marked', None) is not False:
        return
    attempt_id = instance.pk

    def _build():
        """Build the cards; failures are logged and reported as ``REV-9002``."""
        try:
            from apps.assessments.models import AssessmentAttempt
            from .services import cards_from_attempt
            attempt = AssessmentAttempt.objects.filter(pk=attempt_id).first()
            if attempt is not None:
                cards_from_attempt(attempt)
        except Exception as exc:
            logger.exception('revision: building review cards failed for attempt %s', attempt_id)
            from core.errors import report
            report('REV-9002', exc, context={'attempt': attempt_id})

    try:
        transaction.on_commit(_build)
    except Exception:  # pragma: no cover
        logger.exception('revision: could not schedule card build for attempt %s', attempt_id)
