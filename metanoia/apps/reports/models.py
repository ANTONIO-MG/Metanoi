"""Reporting & certificates (My Learning Hub plan, sections 8–9).

* :class:`ModuleWeighting` — how a module's 100% is split across assignments,
  quizzes, tests, exams and study tasks (validated to total 100).
* :class:`Grade` — a computed report-card row per student per module.
* :class:`Certificate` — auto-issued when a student passes a module/module/etc.,
  with a unique number + verification UUID (PDF rendered in a later phase).
"""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core import validators as v


DEFAULT_GRADE_SCALE = [
    {'min': 80, 'letter': 'A'},
    {'min': 70, 'letter': 'B'},
    {'min': 60, 'letter': 'C'},
    {'min': 50, 'letter': 'D'},
    {'min': 0, 'letter': 'F'},
]
"""A sensible default A–F scale (descending ``min`` percentages). Educators can
override it per module via :attr:`ModuleWeighting.grade_scale`.
"""


def letter_for(pct, scale=None):
    """Map a percentage to a letter grade using ``scale`` (or the default)."""
    scale = scale or DEFAULT_GRADE_SCALE
    try:
        ordered = sorted(scale, key=lambda band: -float(band.get('min', 0)))
    except (TypeError, ValueError):
        ordered = DEFAULT_GRADE_SCALE
    for band in ordered:
        if float(pct) >= float(band.get('min', 0)):
            return band.get('letter', '')
    return ordered[-1].get('letter', '') if ordered else ''


class ModuleWeighting(models.Model):
    """How a module's 100% is split across the five assessed components.

    ``assignments_pct`` … ``tasks_pct`` must total 100 (enforced by
    :meth:`clean`); ``pass_mark_pct`` is the final mark needed to pass.

    Flexible grading: ``extra_credit_pct`` caps the bonus % extra-credit work may
    add on top of the weighted score, and ``grade_scale`` is an optional custom
    letter-grade scale (a list of ``{min, letter}``) that replaces
    :data:`DEFAULT_GRADE_SCALE` for this module.
    """

    module = models.OneToOneField('learning.ProgrammeModule', on_delete=models.CASCADE, related_name='weighting')
    assignments_pct = models.PositiveIntegerField(default=20)
    quizzes_pct = models.PositiveIntegerField(default=10)
    tests_pct = models.PositiveIntegerField(default=20)
    exams_pct = models.PositiveIntegerField(default=40)
    tasks_pct = models.PositiveIntegerField(default=10)
    pass_mark_pct = models.PositiveIntegerField(default=50)

    extra_credit_pct = models.PositiveIntegerField(
        default=0, help_text='Maximum bonus % that extra-credit work can add on top of the final mark.')
    grade_scale = models.JSONField(
        default=list, blank=True,
        help_text='Letter-grade bands, e.g. [{"min":80,"letter":"A"}, …]. Blank = default A–F scale.')

    def total(self):
        """Sum of the five component weightings (should be 100)."""
        return (self.assignments_pct + self.quizzes_pct + self.tests_pct
                + self.exams_pct + self.tasks_pct)

    def clean(self):
        """Reject weightings that do not add up to 100%."""
        if self.total() != 100:
            raise ValidationError('Weightings must add up to 100%.')

    def scale(self):
        """The module's letter-grade scale, falling back to :data:`DEFAULT_GRADE_SCALE`."""
        return self.grade_scale or DEFAULT_GRADE_SCALE

    def __str__(self):
        """Label shown in the admin."""
        return f'Weighting · {self.module}'


class Grade(models.Model):
    """A computed report-card row per student per module.

    ``components`` holds the per-component breakdown
    (``{assignments, quizzes, tests, exams, tasks}`` as percentages).

    **Cohort standing** — ``class_position``, ``cohort_size``,
    ``cohort_average`` and ``ranked_at`` are written together by
    :func:`apps.reports.services.rank_module`. Positions use competition ranking
    (1, 2, 2, 4): equal marks share a position and the next distinct mark skips
    the tie. ``cohort_size`` is how many graded students the position is out of
    and ``cohort_average`` their mean mark — both stored so a report or
    dashboard can show "3rd of 24, class average 61%" from this row alone
    instead of re-reading the whole module.

    **Staff override** (set from the staff desk, ``/staff/grades/``) —
    ``computed_pct`` is always what :func:`apps.reports.services.compute_grade`
    worked out from the marks. When ``override_pct`` is set it *becomes*
    ``final_pct`` (and the letter and pass/fail follow it), so everything
    downstream — ranking, the report card, certificates — honours the override
    without knowing about it. Recomputing keeps the override until a person
    clears it.
    """

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='grades')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE,
                               null=True, blank=True, related_name='grades')
    components = models.JSONField(default=dict, blank=True)
    final_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    letter = models.CharField(max_length=4, blank=True)
    extra_credit_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    passed = models.BooleanField(default=False)

    class_position = models.PositiveIntegerField(null=True, blank=True)
    cohort_size = models.PositiveIntegerField(default=0)
    cohort_average = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    ranked_at = models.DateTimeField(null=True, blank=True)

    attendance_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    study_hours = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    computed_at = models.DateTimeField(auto_now=True)

    computed_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    override_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    override_reason = models.TextField(blank=True)
    overridden_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='+')
    overridden_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('student', 'module')
        ordering = ['module', '-final_pct']

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.student} · {self.module} — {self.final_pct}%'

    @property
    def percentile(self):
        """Share of the cohort this student is at or above (0–100), or ``None``
        when the module has not been ranked or the cohort is a single student.

        Position 1 of 20 gives the 100th percentile; position 20 of 20 the 5th.
        """
        if not self.class_position or self.cohort_size < 2:
            return None
        return round((self.cohort_size - self.class_position + 1) / self.cohort_size * 100)

    @property
    def cohort_delta(self):
        """Points above (+) or below (−) the class average, or ``None``."""
        if self.cohort_size < 2:
            return None
        return round(float(self.final_pct) - float(self.cohort_average))

    @property
    def is_overridden(self):
        """Whether a staff override currently replaces the computed mark."""
        return self.override_pct is not None


class Certificate(models.Model):
    """A certificate issued to a student for a module, programme or class.

    ``number`` is generated on first save (``CERT-`` plus ten hex characters) and
    ``verification_uuid`` backs the public verification link.

    Revocation: a revoked certificate is kept (``revoked_at`` / ``revoked_reason``
    set) so its number and verification link stay resolvable and the public
    verify page can say it was withdrawn rather than pretend it never existed.
    """

    KIND_CHOICES = [
        ('module', 'Module'), ('programme', 'Programme'), ('class', 'Class'),
    ]

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='certificates')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='certificates')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='module')
    title = models.CharField(max_length=200)
    final_mark = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    number = models.CharField(max_length=40, unique=True, blank=True)
    verification_uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    signature = models.CharField(max_length=200, blank=True)
    pdf = models.FileField(upload_to='certificates/', blank=True, null=True,
                           validators=v.validate_document)
    issued_at = models.DateTimeField(default=timezone.now)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-issued_at']

    @property
    def is_revoked(self):
        """Whether the certificate has been withdrawn."""
        return self.revoked_at is not None

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.number} — {self.student}'

    def save(self, *args, **kwargs):
        """Assign a unique ``CERT-…`` number on first save, then save."""
        if not self.number:
            self.number = f'CERT-{uuid.uuid4().hex[:10].upper()}'
        super().save(*args, **kwargs)


class CompletionRecord(models.Model):
    """One thing a student has finished — the rows of "What you have completed".

    Written by :func:`apps.reports.completions.record_completions`, which reads
    the work the platform already holds (attempts, submissions, class
    joins, finished lessons) and the schedule a module is taught to, and records
    what is done. It is **idempotent**: every row carries a stable ``key`` (e.g.
    ``assessment:12``, ``phase:40``, ``module:7``) unique per student, so running
    it twice — on every page load, from the post-save signal and from the
    backfill command — never writes a second copy.

    A record is history, not state: once a block of work has been completed it
    stays completed, even if an educator later adds more material to it. The one
    field that is refreshed is the score of an assessment row, because a paper is
    completed when it is submitted and only scored when it is marked.
    """

    KIND_ASSESSMENT = 'assessment'
    KIND_ASSIGNMENT = 'assignment'
    KIND_PHASE = 'phase'
    KIND_MODULE = 'module'
    KIND_CLASS = 'class'
    KIND_LESSON = 'lesson'
    KIND_CHOICES = [
        (KIND_ASSESSMENT, 'Assessment'),
        (KIND_ASSIGNMENT, 'Assignment'),
        (KIND_PHASE, 'Preparation block'),
        (KIND_MODULE, 'Module'),
        (KIND_CLASS, 'Live class'),
        (KIND_LESSON, 'Lesson'),
    ]
    KIND_ICONS = {
        KIND_ASSESSMENT: 'bi-ui-checks',
        KIND_ASSIGNMENT: 'bi-file-earmark-check',
        KIND_PHASE: 'bi-flag',
        KIND_MODULE: 'bi-mortarboard',
        KIND_CLASS: 'bi-camera-video',
        KIND_LESSON: 'bi-journal-check',
    }

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='completion_records')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, db_index=True)
    key = models.CharField(max_length=60, help_text='Stable id of the thing completed, e.g. "assessment:12".')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='completion_records')
    title = models.CharField(max_length=255)
    detail = models.CharField(max_length=255, blank=True)
    score_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)
    url = models.CharField(max_length=500, blank=True)
    completed_at = models.DateTimeField(db_index=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-completed_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['student', 'key'], name='uniq_completion_per_student_key'),
        ]
        indexes = [models.Index(fields=['student', '-completed_at'], name='reports_compl_student_when')]

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.student} · {self.title}'

    @property
    def icon(self):
        """Bootstrap Icons class for this record's kind."""
        return self.KIND_ICONS.get(self.kind, 'bi-check2-circle')

    @property
    def is_milestone(self):
        """Blocks and modules are the big finishes; the timeline draws them larger."""
        return self.kind in (self.KIND_PHASE, self.KIND_MODULE)
