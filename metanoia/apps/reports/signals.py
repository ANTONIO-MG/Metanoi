"""When a student's assessment is submitted/marked, recompute their module
grade, re-rank the module's cohort, auto-issue a certificate if they have
now passed the module, and record what they have completed (the Reports page's
"What you have completed" — see apps.reports.completions).

One student's new mark can move everyone else's class position, so the whole
module is re-ranked — not just the row that changed."""

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='assessments.AssessmentAttempt')
def recompute_grade_on_attempt(sender, instance, **kwargs):
    """Refresh grade, ranking, certificate and completions when an attempt is submitted or marked."""
    if instance.status not in ('submitted', 'marked'):
        return
    _refresh(instance.student, instance.assessment.module)


@receiver(post_save, sender='assessments.AssignmentSubmission')
def recompute_grade_on_assignment(sender, instance, **kwargs):
    """Refresh grade, ranking, certificate and completions when an assignment is marked or completed."""
    if instance.status not in ('marked', 'completed'):
        return
    _refresh(instance.student, instance.assessment.module)


def _refresh(student, module):
    """Recompute ``student``'s grade for ``module``, re-rank and issue a certificate on a pass.

    A new mark reorders the cohort, so positions are rewritten module-wide. The
    ranking is guarded separately: a ranking failure must not block the
    certificate. Completions are recorded afterwards, so a pass that completes
    the module is recorded with it.
    """
    from . import services
    try:
        grade = services.compute_grade(student, module)
        try:
            services.rank_module(module)
        except Exception:  # pragma: no cover
            logger.exception('Class ranking failed for %s', module)
        if grade.passed:
            services.issue_certificate(
                student, module=module, kind='module',
                title=f'ProgrammeModule Completion — {module}', final_mark=grade.final_pct,
            )
    except Exception:  # pragma: no cover - never let grading break a save
        logger.exception('Grade/certificate refresh failed for %s / %s', student, module)
    _record(student)


def _record(student):
    """Record what the student has now completed. Idempotent, and guarded on
    its own so it can never stand in the way of grading or a submission."""
    try:
        from .completions import record_completions
        record_completions(student)
    except Exception:  # pragma: no cover
        logger.exception('Completion recording failed for %s', student)
