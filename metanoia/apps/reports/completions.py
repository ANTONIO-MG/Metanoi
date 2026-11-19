"""What a student has completed — recorded, so it can be reported as history.

:func:`record_completions` reads one learner's work through
:func:`apps.reports.progress_board.load` (the same facts the progress page
draws) and writes a :class:`~apps.reports.models.CompletionRecord` for each
thing that is now finished. It is idempotent — every record has a stable key,
unique per student — so it is safe to run on every visit to the Reports page,
from the assessment post-save signal and from ``manage.py record_completions``.

The rules
---------
=============  ==============================================================
Kind           Completed when
=============  ==============================================================
assessment     an attempt at a quiz / test / mock exam is **submitted** (the
               date of the first submission). The score and pass mark follow
               the best submitted/marked attempt and are refreshed when marked.
assignment     the submission is handed in (``submitted_at`` set).
class          the student joined the live class from Metanoia, or the register
               marks them present / late.
lesson         a study session on the lesson is completed.
phase          a preparation block (Test 1–4, Exam 1–2) has at least one
               trackable item — a lesson or an assessment — released to the
               student, and **every** one of them is done. Dated the moment the
               last of them was done.
module         either the module's report-card :class:`Grade` is a **pass**
               (the formal rule certificates are issued on), or the module's
               teaching is over — every published block's date has passed —
               and every trackable item across all its blocks is done.
=============  ==============================================================

A record is never deleted by this function: completing something is an event,
and adding more material to a block later does not un-happen it.
"""

import logging
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from . import models

logger = logging.getLogger(__name__)

Record = models.CompletionRecord


def _url(name, *args):
    """Reverse URL ``name`` with positional ``args``, or ``''`` when it cannot be reversed."""
    try:
        return reverse(name, args=args)
    except Exception:
        return ''


def _dec(value):
    """Round ``value`` to two places as a :class:`~decimal.Decimal`; ``None`` stays ``None``."""
    return None if value is None else Decimal(str(round(float(value), 2)))


def _candidates(data):
    """Every completion the learner's work currently supports, as dicts.

    Assessments yield one record per paper, dated by its first submission;
    ``done_at`` maps each assessment id to that first submission so blocks can
    be dated by their last finished item. Lessons are emitted last because
    their titles need one extra query, made only for the finished ones.
    """
    out = []
    now = data.now

    by_assessment = {}
    for attempt in data.attempts:
        by_assessment.setdefault(attempt.assessment_id, []).append(attempt)
    done_at = {}
    for assessment_id, attempts in by_assessment.items():
        first = attempts[0]
        assessment = first.assessment
        scored = [(data.attempt_pct(a), a) for a in attempts]
        scored = [(p, a) for p, a in scored if p is not None]
        best_pct, best = max(scored, key=lambda pair: pair[0]) if scored else (None, first)
        marked = any(a.status == 'marked' for a in attempts)
        when = first.submitted_at or first.updated_at or now
        done_at[assessment_id] = when
        detail = assessment.get_kind_display()
        if len(attempts) > 1:
            detail += f' · {len(attempts)} attempts'
        if not marked:
            detail += ' · awaiting marking'
        out.append({
            'kind': Record.KIND_ASSESSMENT, 'key': f'assessment:{assessment_id}',
            'module_id': assessment.module_id, 'title': assessment.title, 'detail': detail,
            'score_pct': _dec(best_pct) if marked else None,
            'passed': bool(best.passed) if marked else None,
            'url': _url('assessments:result', best.public_id),
            'completed_at': when,
        })

    for submission in data.submissions:
        assessment = submission.assessment
        pct = None
        if submission.grade is not None and assessment.total_marks:
            pct = float(submission.grade) / float(assessment.total_marks) * 100
        done_at.setdefault(assessment.pk, submission.submitted_at)
        out.append({
            'kind': Record.KIND_ASSIGNMENT, 'key': f'assignment:{assessment.pk}',
            'module_id': assessment.module_id, 'title': assessment.title,
            'detail': submission.get_status_display(),
            'score_pct': _dec(pct),
            'passed': (pct >= (assessment.pass_mark_pct or 0)) if pct is not None else None,
            'url': '', 'completed_at': submission.submitted_at,
        })

    for module_id, rooms in data.classes.items():
        for room in rooms:
            when = data.attended.get(room.pk)
            if room.pk not in data.attended:
                continue
            out.append({
                'kind': Record.KIND_CLASS, 'key': f'class:{room.pk}',
                'module_id': module_id, 'title': room.title,
                'detail': 'Live class attended', 'score_pct': None, 'passed': None,
                'url': '', 'completed_at': when or room.scheduled_start,
            })

    lesson_done_at = {}
    for row in data.study_rows:
        if row['status'] == 'completed' and row['lesson_id']:
            previous = lesson_done_at.get(row['lesson_id'])
            if previous is None or row['updated_at'] < previous:
                lesson_done_at[row['lesson_id']] = row['updated_at']

    offerings = {o.pk: o for o in data.offerings}
    for module_id, phases in data.phases.items():
        offering = offerings.get(module_id)
        module_tracked = module_done = 0
        module_last = None
        all_dated_past = bool(phases)
        for phase in phases:
            tracked, done = data.phase_items(phase)
            module_tracked += tracked
            module_done += done
            date = phase.assessment_date or phase.ends_on
            if date is None or date >= timezone.localdate():
                all_dated_past = False
            if not tracked or done < tracked:
                continue
            stamps = []
            for material in phase.released_materials:
                if material.lesson_id:
                    stamps.append(lesson_done_at.get(material.lesson_id))
                elif material.assessment_id:
                    stamps.append(done_at.get(material.assessment_id))
            stamps = [s for s in stamps if s]
            when = max(stamps) if stamps else now
            module_last = max(module_last, when) if module_last else when
            out.append({
                'kind': Record.KIND_PHASE, 'key': f'phase:{phase.pk}',
                'module_id': module_id,
                'title': f'{phase.display_title} preparation — {offering.display_name if offering else ""}'
                         .rstrip(' —'),
                'detail': f'All {tracked} trackable item{"s" if tracked != 1 else ""} done',
                'score_pct': None, 'passed': None,
                'url': _url('learning:module-feed', module_id), 'completed_at': when,
            })

        grade = data.grades.get(module_id)
        by_schedule = all_dated_past and module_tracked and module_done >= module_tracked
        if (grade is not None and grade.passed) or by_schedule:
            if grade is not None and grade.passed:
                detail = f'Passed · {round(float(grade.final_pct))}%'
                when = grade.computed_at or now
            else:
                detail = 'All scheduled work done'
                when = module_last or now
            out.append({
                'kind': Record.KIND_MODULE, 'key': f'module:{module_id}',
                'module_id': module_id,
                'title': offering.display_name if offering else 'Module',
                'detail': detail,
                'score_pct': _dec(grade.final_pct) if grade is not None else None,
                'passed': grade.passed if grade is not None else None,
                'url': _url('learning:module-feed', module_id), 'completed_at': when,
            })

    if lesson_done_at:
        from apps.learning.models import Lesson
        for lesson in Lesson.objects.filter(pk__in=list(lesson_done_at)).only('id', 'title', 'module_id'):
            out.append({
                'kind': Record.KIND_LESSON, 'key': f'lesson:{lesson.pk}',
                'module_id': lesson.module_id, 'title': lesson.title, 'detail': 'Lesson completed',
                'score_pct': None, 'passed': None,
                'url': _url('learning:lesson-view', lesson.pk),
                'completed_at': lesson_done_at[lesson.pk],
            })
    return out


_REFRESHABLE = ('score_pct', 'passed', 'detail', 'url')
"""Fields an existing record may have refreshed — the result of a paper that
was submitted first and marked later. Nothing else about history changes.
"""


def record_completions(user, data=None):
    """Record everything ``user`` has completed that is not on file yet.

    Returns ``(created, updated)``. Never raises — a failure is logged and the
    Reports page still renders whatever is already on record.

    A missing completion table (code deployed ahead of its migration) is logged
    and treated as nothing to record. New rows are bulk-created with
    ``ignore_conflicts`` because a concurrent request may have recorded the same
    key a moment ago — the unique constraint is the real guard.
    """
    try:
        if data is None:
            from .progress_board import load
            data = load(user)
        candidates = _candidates(data)
    except Exception:
        logger.exception('record_completions: reading work failed for %s', getattr(user, 'pk', user))
        return 0, 0

    try:
        existing = {r.key: r for r in Record.objects.filter(student=user)}
    except Exception:
        logger.exception('record_completions: completion table unavailable')
        return 0, 0
    valid_modules = set()
    if candidates:
        from apps.learning.models import ProgrammeModule
        wanted = {c['module_id'] for c in candidates if c['module_id']}
        valid_modules = set(ProgrammeModule.objects.filter(pk__in=wanted).values_list('pk', flat=True))

    fresh, changed = [], []
    for row in candidates:
        if row['completed_at'] is None:
            continue
        row = dict(row, title=(row['title'] or '')[:255], detail=(row['detail'] or '')[:255],
                   url=(row['url'] or '')[:500])
        module_id = row.pop('module_id')
        record = existing.get(row['key'])
        if record is None:
            fresh.append(Record(student=user, module_id=module_id if module_id in valid_modules else None,
                                **row))
            existing[row['key']] = fresh[-1]
            continue
        dirty = False
        for field in _REFRESHABLE:
            if getattr(record, field) != row[field]:
                setattr(record, field, row[field])
                dirty = True
        if dirty and record.pk:
            changed.append(record)

    try:
        if fresh:
            Record.objects.bulk_create(fresh, ignore_conflicts=True)
        if changed:
            Record.objects.bulk_update(changed, list(_REFRESHABLE))
    except Exception:
        logger.exception('record_completions: write failed for %s', user.pk)
        return 0, 0
    return len(fresh), len(changed)
