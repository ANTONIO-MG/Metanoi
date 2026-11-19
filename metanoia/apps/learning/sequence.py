"""The weekly teaching sequence, and the gate that enforces it.

:mod:`apps.learning.access` answers the commercial question (*may they open
it at all*); this answers **have they earned it yet**:

    1. Study guide        always open
    2. Questions / mock   opens once the guide is opened
    3. Answers / solution released ONLY once the attempt is submitted
    4. Challenge          opens after the mock
    5. Mock exam          the full sitting, at the end of the phase

The solution gate is the one that matters, so it is enforced server-side, tied
to *submission*. Locked rows are still listed with their reason (it is a
teaching instruction, not missing content). Staff and educators are never
sequenced.

Why a row is shut by the *teaching* sequence, not by money:

``LOCK_SEQUENCE``
    An earlier step in the week is not done.
``LOCK_SOLUTION``
    The attempt has not been submitted yet.
``LOCK_ATTEMPT``
    The paper it answers has not been attempted.

``SEQUENCE_LOCK_LABELS`` gives the learner-facing wording of each.
"""

import logging

logger = logging.getLogger('apps')

LOCK_SEQUENCE = 'sequence'
LOCK_SOLUTION = 'solution'
LOCK_ATTEMPT = 'attempt'

SEQUENCE_LOCK_LABELS = {
    LOCK_SEQUENCE: 'Work through this week in order',
    LOCK_SOLUTION: 'Submit your attempt to see the solution',
    LOCK_ATTEMPT: 'Sit the paper first',
}


def _kinds():
    """The :class:`~apps.learning.models.ModuleMaterial` model, imported late to avoid
    an import cycle.
    """
    from .models import ModuleMaterial as M
    return M


def sequence_rank(kind):
    """Where a material kind sits in the week's order. Lower comes first.

    Reference material outside the taught sequence (blueprint, live session,
    recording, loose document) returns ``0`` — never sequenced.
    """
    M = _kinds()
    return {
        M.KIND_STUDY_GUIDE: 1,
        M.KIND_QUESTIONS: 2,
        M.KIND_ASSESSMENT: 2,
        M.KIND_ANSWERS: 3,
        M.KIND_MOCK_EXAM: 4,
    }.get(kind, 0)


def solution_kinds():
    """Kinds that are a *solution* — gated on the candidate's own submission."""
    M = _kinds()
    return {M.KIND_ANSWERS}


def attemptable_kinds():
    """Kinds that are *sat* — an attempt against them is what unlocks the solution."""
    M = _kinds()
    return {M.KIND_QUESTIONS, M.KIND_ASSESSMENT, M.KIND_MOCK_EXAM}


class WeekProgress:
    """One candidate's progress through one week, resolved in two queries.

    Built once per page by :class:`SequenceGate` for every week at once, to
    avoid a query per material.
    """

    __slots__ = ('opened_lessons', 'submitted_assessments', 'started_assessments')

    def __init__(self, opened_lessons, submitted_assessments, started_assessments):
        """Hold the opened lesson ids and the submitted and started assessment ids."""
        self.opened_lessons = opened_lessons
        self.submitted_assessments = submitted_assessments
        self.started_assessments = started_assessments

    def has_opened(self, lesson_id):
        """True when the candidate has opened (started a study session on) the lesson.
        """
        return lesson_id in self.opened_lessons

    def has_submitted(self, assessment_id):
        """True when the candidate has submitted (or been marked on) the assessment."""
        return assessment_id in self.submitted_assessments


def _load_progress(user, materials):
    """What this candidate has already done, across every material on the page."""
    lesson_ids = {m.lesson_id for m in materials if m.lesson_id}
    assessment_ids = {m.assessment_id for m in materials if m.assessment_id}

    opened, submitted, started = set(), set(), set()
    if not getattr(user, 'is_authenticated', False):
        return WeekProgress(opened, submitted, started)

    if lesson_ids:
        try:
            from .models import StudySession
            opened = set(
                StudySession.objects
                .filter(student=user, lesson_id__in=lesson_ids)
                .values_list('lesson_id', flat=True))
        except Exception:  # pragma: no cover
            logger.exception('sequence: study-session lookup failed')

    if assessment_ids:
        try:
            from apps.assessments.models import AssessmentAttempt as A
            rows = (A.objects
                    .filter(student=user, assessment_id__in=assessment_ids)
                    .values_list('assessment_id', 'status'))
            for assessment_id, status in rows:
                started.add(assessment_id)
                if status in (A.STATUS_SUBMITTED, A.STATUS_MARKED):
                    submitted.add(assessment_id)
        except Exception:  # pragma: no cover
            logger.exception('sequence: attempt lookup failed')

    return WeekProgress(opened, submitted, started)


class SequenceGate:
    """Does the candidate's own work entitle them to this row *yet*?

    Composed with :class:`apps.learning.access.Gate`, not folded into it: a row
    can be paid-for and still not yet earned.

        seq = SequenceGate(request.user, materials, bypass=gate.can_author)
        verdict = seq.check(material, siblings_in_week)
    """

    def __init__(self, user, materials, *, bypass=False):
        """Resolve the candidate's progress over ``materials`` once.

        ``bypass`` is set for staff and the offering's educators: they write the week
        and are never sequenced, so no progress is loaded for them.
        """
        self.user = user
        self.bypass = bool(bypass)
        self.materials = list(materials)
        self.progress = (WeekProgress(set(), set(), set()) if self.bypass
                         else _load_progress(user, self.materials))

    def check(self, material, week_materials):
        """``(is_open, reason)`` — ``reason`` is ``None`` when it is open.

        ``week_materials`` is every material in the same week. Reference material is
        never sequenced. A solution opens once any paper beside it has been
        submitted;
        a solution with no paper beside it is reference material — refusing it would
        lock a candidate out of something with no way to earn it. Everything else
        opens once every earlier step in the week is done.
        """
        if self.bypass:
            return True, None

        M = _kinds()
        rank = sequence_rank(material.kind)
        if rank == 0:
            return True, None

        if material.kind in solution_kinds():
            papers = [m for m in week_materials
                      if m.kind in attemptable_kinds() and m.assessment_id]
            if not papers:
                return True, None
            if any(self.progress.has_submitted(p.assessment_id) for p in papers):
                return True, None
            return False, LOCK_SOLUTION

        earlier = [m for m in week_materials
                   if 0 < sequence_rank(m.kind) < rank
                   and m.kind not in solution_kinds()]
        if not earlier:
            return True, None

        for step in earlier:
            if step.lesson_id and not self.progress.has_opened(step.lesson_id):
                return False, LOCK_SEQUENCE
            if step.assessment_id and not self.progress.has_submitted(step.assessment_id):
                return False, LOCK_SEQUENCE
        return True, None

    def next_step(self, week_materials):
        """The one row this candidate should open next in this week, or ``None``.

        Gets the "Start here" / "Continue" button on the module page.
        """
        if self.bypass:
            return None
        ordered = sorted((m for m in week_materials if sequence_rank(m.kind)),
                         key=lambda m: (sequence_rank(m.kind), m.order, m.pk))
        for material in ordered:
            is_open, _ = self.check(material, week_materials)
            if not is_open:
                continue
            if material.lesson_id and not self.progress.has_opened(material.lesson_id):
                return material
            if material.assessment_id and not self.progress.has_submitted(material.assessment_id):
                return material
        return None
