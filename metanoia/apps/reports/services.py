"""Grade computation + certificate issuing.

``compute_grade`` rolls a student's quiz/test/exam/assignment/task performance in
a module into a single weighted percentage using the module's
:class:`ModuleWeighting` (defaults if none). Components with no data are skipped
and the remaining weights are **renormalised**, so a half-finished module still
produces a fair mark. ``issue_certificate`` creates a certificate (and renders a
PDF when WeasyPrint is installed — optional/local).
"""

import logging
from decimal import Decimal

from django.utils import timezone

from . import models

logger = logging.getLogger(__name__)


def _weighted_mean(pairs):
    """Mean of ``(value, weight)`` pairs, or None when empty / zero total weight."""
    total_w = sum(w for _, w in pairs) or 0
    if not pairs or total_w <= 0:
        return None
    return sum(v * w for v, w in pairs) / total_w


def _assessment_best_pct(student, assessment):
    """Best achieved percentage for one assessment, or ``None`` if not attempted.

    Assignments are scored from their :class:`AssignmentSubmission`; every other
    kind (quiz/test/exam-section) is scored from the best submitted/marked :class:`AssessmentAttempt`.
    """
    from apps.assessments.models import Assessment, AssessmentAttempt, AssignmentSubmission
    if not assessment.total_marks:
        return None
    if assessment.kind == Assessment.KIND_ASSIGNMENT:
        sub = AssignmentSubmission.objects.filter(assessment=assessment, student=student).first()
        if sub and sub.grade is not None:
            return float(sub.grade) / float(assessment.total_marks) * 100
        return None
    best = (AssessmentAttempt.objects
            .filter(assessment=assessment, student=student, status__in=['submitted', 'marked'])
            .order_by('-score').first())
    if best:
        return float(best.score) / float(assessment.total_marks) * 100
    return None


def _component_pct(student, module, component):
    """Weighted mean of best percentages across every non-extra-credit
    assessment in ``module`` whose :attr:`grade_component` is ``component``.

    Grouping by the assessment's *component* (rather than its raw ``kind``) is
    what lets any assessment an educator re-buckets count towards the right report-card slice. Per-assessment ``weight`` is
    honoured; extra-credit assessments contribute bonus instead (see below).

    Practice papers (built from the schedule's past papers, ``source_ref``
    starting ``paper/``) are preparation, not assessment, so they are left out —
    see :mod:`apps.assessments.papers`.
    """
    from apps.assessments.models import Assessment
    pairs = []
    for a in Assessment.objects.filter(module=module, is_extra_credit=False).exclude(
            source_ref__startswith='paper/'):
        if a.grade_component != component:
            continue
        pct = _assessment_best_pct(student, a)
        if pct is not None:
            pairs.append((pct, float(a.weight or 1)))
    return _weighted_mean(pairs)


def _extra_credit_bonus(student, module, cap):
    """Bonus marks (0..cap) from extra-credit assessments: how well the student
    did on bonus work, scaled to the module's ``extra_credit_pct`` cap."""
    if not cap:
        return 0.0
    from apps.assessments.models import Assessment, AssessmentAttempt, AssignmentSubmission
    extra_credit_percentages = []
    for assessment in Assessment.objects.filter(module=module, is_extra_credit=True).exclude(
            source_ref__startswith='paper/'):
        if not assessment.total_marks:
            continue
        if assessment.kind == Assessment.KIND_ASSIGNMENT:
            submission = AssignmentSubmission.objects.filter(assessment=assessment, student=student).first()
            if submission and submission.grade is not None:
                extra_credit_percentages.append(float(submission.grade) / float(assessment.total_marks) * 100)
        else:
            best = (AssessmentAttempt.objects
                    .filter(assessment=assessment, student=student, status__in=['submitted', 'marked'])
                    .order_by('-score').first())
            if best:
                extra_credit_percentages.append(float(best.score) / float(assessment.total_marks) * 100)
    if not extra_credit_percentages:
        return 0.0
    achieved = sum(extra_credit_percentages) / len(extra_credit_percentages) / 100.0
    return round(min(cap, achieved * cap), 2)


def _task_stats(student, module):
    """(completion_pct, study_hours) from the student's study sessions in the module."""
    from apps.learning.models import StudySession
    sessions = StudySession.objects.filter(student=student, lesson__module=module)
    if not sessions.exists():
        return None, 0.0
    completion_percentages = [session.completion_pct for session in sessions]
    hours = sum(session.total_seconds for session in sessions) / 3600.0
    average_completion = (sum(completion_percentages) / len(completion_percentages)) if completion_percentages else None
    return average_completion, round(hours, 2)


def compute_grade(student, module):
    """Compute (or refresh) the student's :class:`Grade` for ``module``.

    Weights are renormalised across the components that actually have data.
    Extra credit is added on top of the weighted score, then capped at 100%. A
    staff override stands until someone clears it: the computed mark is still
    refreshed (so the desk can show both), but the final one is theirs.
    """
    weighting = getattr(module, 'weighting', None)
    weights = {
        'assignments': getattr(weighting, 'assignments_pct', 20),
        'quizzes': getattr(weighting, 'quizzes_pct', 10),
        'tests': getattr(weighting, 'tests_pct', 20),
        'exams': getattr(weighting, 'exams_pct', 40),
        'tasks': getattr(weighting, 'tasks_pct', 10),
    }
    pass_mark = getattr(weighting, 'pass_mark_pct', 50)
    extra_cap = getattr(weighting, 'extra_credit_pct', 0)
    scale = weighting.scale() if weighting else None

    task_pct, study_hours = _task_stats(student, module)
    components = {
        'assignments': _component_pct(student, module, 'assignments'),
        'quizzes': _component_pct(student, module, 'quizzes'),
        'tests': _component_pct(student, module, 'tests'),
        'exams': _component_pct(student, module, 'exams'),
        'tasks': task_pct,
    }

    available = {k: v for k, v in components.items() if v is not None}
    total_weight = sum(weights[k] for k in available) or 0
    if total_weight > 0:
        base = sum(components[k] * weights[k] for k in available) / total_weight
    else:
        base = 0.0

    bonus = _extra_credit_bonus(student, module, extra_cap)
    computed = min(100.0, base + bonus)

    override = (models.Grade.objects.filter(student=student, module=module)
                .values_list('override_pct', flat=True).first())
    final = float(override) if override is not None else computed

    grade, _ = models.Grade.objects.update_or_create(
        student=student, module=module,
        defaults={
            'components': {k: round(v, 2) for k, v in components.items() if v is not None},
            'computed_pct': Decimal(str(round(computed, 2))),
            'final_pct': Decimal(str(round(final, 2))),
            'letter': models.letter_for(final, scale),
            'extra_credit_pct': Decimal(str(bonus)),
            'passed': final >= pass_mark,
            'study_hours': Decimal(str(study_hours)),
        },
    )
    return grade


def override_grade(grade, *, value, reason, by=None):
    """Set a staff override on ``grade`` and re-rank its module.

    ``reason`` is required — an override is a change to someone's record and
    has to say why. Returns the refreshed grade.
    """
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('An override needs a reason.')
    value = Decimal(str(value))
    if value < 0 or value > 100:
        raise ValueError('An override must be between 0 and 100.')
    grade.override_pct = value
    grade.override_reason = reason
    grade.overridden_by = by
    grade.overridden_at = timezone.now()
    grade.save(update_fields=['override_pct', 'override_reason', 'overridden_by', 'overridden_at'])
    return _refresh_after_override(grade)


def clear_override(grade):
    """Drop the override so the computed mark is final again."""
    grade.override_pct = None
    grade.override_reason = ''
    grade.overridden_by = None
    grade.overridden_at = None
    grade.save(update_fields=['override_pct', 'override_reason', 'overridden_by', 'overridden_at'])
    return _refresh_after_override(grade)


def _refresh_after_override(grade):
    """Recompute the grade and re-rank its module after an override changes, then reload it."""
    if grade.module_id is None:
        return grade
    grade = compute_grade(grade.student, grade.module)
    rank_module(grade.module)
    grade.refresh_from_db()
    return grade


def revoke_certificate(cert, *, reason, by=None):
    """Withdraw ``cert``. It stays on record so verification can say so."""
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('Revoking a certificate needs a reason.')
    cert.revoked_at = timezone.now()
    cert.revoked_reason = reason
    cert.revoked_by = by
    cert.save(update_fields=['revoked_at', 'revoked_reason', 'revoked_by'])
    return cert


def reinstate_certificate(cert):
    """Undo a revocation — the same number and verification link become valid again."""
    cert.revoked_at = None
    cert.revoked_reason = ''
    cert.revoked_by = None
    cert.save(update_fields=['revoked_at', 'revoked_reason', 'revoked_by'])
    return cert


def rank_module(module):
    """Recompute and persist class position for every graded student in ``module``.

    Uses **competition ranking** (1, 2, 2, 4): students on the same mark share a
    position and the next distinct mark skips the tie, which is how a class
    position is conventionally read. Alongside the position it stores the cohort
    size and the class average on each row, so a report card or dashboard can
    render "3rd of 24, class average 61%" from one row without re-reading the
    module.

    The cohort is every :class:`Grade` row in the module — the same set the
    analytics elsewhere count — so a student who has been unenrolled but still
    holds a grade is still ranked against. Returns the number of rows updated.

    Cheap enough to run inline on every grade save: one SELECT of the module's
    grades plus one bulk UPDATE, no per-student queries.

    Archived (deleted, awaiting purge) accounts drop out of the cohort.
    """
    rows = list(models.Grade.objects.filter(module=module)
                .exclude(student__account_settings__is_closing=True).only('id', 'final_pct'))
    if not rows:
        return 0

    size = len(rows)
    average = Decimal(str(round(sum(float(g.final_pct) for g in rows) / size, 2)))
    now = timezone.now()

    rows.sort(key=lambda g: float(g.final_pct), reverse=True)
    position = 0
    previous_mark = None
    for index, grade in enumerate(rows, start=1):
        mark = float(grade.final_pct)
        if mark != previous_mark:
            position = index
            previous_mark = mark
        grade.class_position = position
        grade.cohort_size = size
        grade.cohort_average = average
        grade.ranked_at = now

    models.Grade.objects.bulk_update(
        rows, ['class_position', 'cohort_size', 'cohort_average', 'ranked_at'], batch_size=500)
    return size


def rank_all_subjects():
    """Re-rank every module that has at least one grade. Returns (modules, rows)."""
    from apps.learning.models import ProgrammeModule
    module_ids = models.Grade.objects.values_list('module_id', flat=True).distinct()
    modules = rows = 0
    for module in ProgrammeModule.objects.filter(id__in=list(module_ids)):
        updated = rank_module(module)
        if updated:
            modules += 1
            rows += updated
    return modules, rows


def student_progress(student, modules=None):
    """Aggregate one student's progress across grades, assessments, study
    and attendance into a single dict the progress dashboard renders.

    ``modules`` (a ProgrammeModule queryset) limits the module-bound results —
    grades, certificates, assessment attempts, study sessions — to those
    offerings. An educator looking at a student passes their taught modules, so
    they never see the student's marks in a module somebody else teaches.

    Every section is defensively guarded so a missing app/table never breaks the
    page (mirrors :mod:`apps.analytics.services`).

    Each section — grades, assessments, study, attendance — is guarded on its own.
    """
    data = {
        'modules': [], 'assessments': {}, 'study': {}, 'attendance': {},
        'certificates': 0,
    }

    try:
        grades = models.Grade.objects.filter(student=student)
        certificates = models.Certificate.objects.filter(student=student)
        if modules is not None:
            grades = grades.filter(module__in=modules)
            certificates = certificates.filter(module__in=modules)
        grades = grades.select_related('module').order_by('module__name')
        data['modules'] = [{
            'name': str(g.module), 'final_pct': float(g.final_pct), 'letter': g.letter,
            'passed': g.passed, 'study_hours': float(g.study_hours),
            'components': g.components or {},
        } for g in grades]
        data['certificates'] = certificates.count()
    except Exception:  # pragma: no cover
        logger.exception('student_progress: grades failed')

    try:
        from apps.assessments.models import AssessmentAttempt
        assessment_attempts = AssessmentAttempt.objects.filter(student=student, status__in=['submitted', 'marked'])
        if modules is not None:
            assessment_attempts = assessment_attempts.filter(assessment__module__in=modules)
        scored = [attempt for attempt in assessment_attempts.select_related('assessment') if attempt.assessment.total_marks]
        score_percentages = [float(attempt.score) / float(attempt.assessment.total_marks) * 100 for attempt in scored]
        data['assessments'] = {
            'attempts': assessment_attempts.count(),
            'passed': assessment_attempts.filter(passed=True).count(),
            'avg_pct': round(sum(score_percentages) / len(score_percentages), 1) if score_percentages else 0,
        }
    except Exception:  # pragma: no cover
        logger.exception('student_progress: assessments failed')

    try:
        from apps.learning.models import StudySession
        sessions = StudySession.objects.filter(student=student)
        if modules is not None:
            sessions = sessions.filter(lesson__module__in=modules)
        total_seconds = sum(s.total_seconds for s in sessions)
        data['study'] = {
            'sessions': sessions.count(),
            'hours': round(total_seconds / 3600.0, 1),
            'completed': sessions.filter(status=StudySession.STATUS_COMPLETED).count(),
        }
    except Exception:  # pragma: no cover
        logger.exception('student_progress: study failed')

    try:
        from apps.communication.services import attendance_rate
        present, total, pct = attendance_rate(student)
        data['attendance'] = {'present': present, 'total': total, 'pct': pct}
    except Exception:  # pragma: no cover
        logger.exception('student_progress: attendance failed')

    return data


def issue_certificate(student, *, module=None, kind='module', title='', final_mark=0):
    """Create a certificate (idempotent per student+module+kind) and try to render a PDF."""
    existing = models.Certificate.objects.filter(
        student=student, module=module, kind=kind).first()
    if existing:
        return existing
    if not title:
        title = f'{kind.title()} of Completion — {module}'
    cert = models.Certificate.objects.create(
        student=student, module=module, kind=kind,
        title=title, final_mark=Decimal(str(round(float(final_mark), 2))),
        issued_at=timezone.now(),
    )
    _render_pdf(cert)
    return cert


def _render_pdf(cert):
    """Render a simple certificate PDF with WeasyPrint, if installed. No-op otherwise."""
    try:
        from weasyprint import HTML
    except Exception:
        return
    try:
        from django.core.files.base import ContentFile
        html = f"""
        <html><body style='font-family:Inter,sans-serif;text-align:center;padding:60px;border:8px solid #0d6efd'>
          <h1 style='color:#0d6efd'>Certificate of Achievement</h1>
          <p>This certifies that</p>
          <h2>{cert.student.get_full_name() or cert.student.get_username()}</h2>
          <p>has successfully completed</p>
          <h3>{cert.title}</h3>
          <p>Final mark: <strong>{cert.final_mark}%</strong></p>
          <p>Certificate No: {cert.number}</p>
          <p style='font-size:11px;color:#888'>Verify: {cert.verification_uuid}</p>
        </body></html>"""
        pdf = HTML(string=html).write_pdf()
        cert.pdf.save(f'{cert.number}.pdf', ContentFile(pdf), save=True)
    except Exception:  # pragma: no cover
        logger.exception('Certificate PDF render failed for %s', cert.number)
