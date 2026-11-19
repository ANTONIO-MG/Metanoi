"""The course report for one module: who read what, for how long, and how they scored.

Built from what the platform already records — :class:`StudySession` (time and
completion per lesson) and assessment attempts — for the materials on the
module's schedule, optionally narrowed to one intake. Staff and the module's
educators only.
"""

from collections import defaultdict
from statistics import mean

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render

from . import models
from .access import Gate


@login_required
def course_analytics(request, pk):
    offering = get_object_or_404(models.ProgrammeModule.objects.select_related('programme__institution'), pk=pk)
    gate = Gate(request.user, offering, person=getattr(request.user, 'profile', None))
    if not gate.can_author:
        return HttpResponseForbidden()

    from apps.assessments.models import AssessmentAttempt

    cohorts = list(offering.programme.cohorts.filter(is_active=True))
    cohort = next((c for c in cohorts if str(c.pk) == request.GET.get('cohort')), None)

    materials = (models.ModuleMaterial.objects
                 .filter(phase__programme_module=offering, is_published=True)
                 .select_related('phase', 'week', 'lesson', 'assessment')
                 .order_by('phase__order', 'week__order', 'order'))
    if cohort is not None:
        materials = materials.filter(Q(phase__cohort=cohort) | Q(phase__cohort__isnull=True))

    students = offering.students
    if cohort is not None:
        students = students.filter(programme_enrolments__cohort=cohort)
    student_users = {p.user_id: p for p in students.select_related('user')}

    lesson_ids = {m.lesson_id for m in materials if m.lesson_id}
    paper_ids = {m.assessment_id for m in materials if m.assessment_id}
    sessions = (models.StudySession.objects
                .filter(lesson_id__in=lesson_ids, student_id__in=student_users)
                .values('lesson_id', 'student_id', 'total_seconds', 'completion_pct'))
    attempts = (AssessmentAttempt.objects
                .filter(assessment_id__in=paper_ids, student_id__in=student_users,
                        status__in=[AssessmentAttempt.STATUS_SUBMITTED, AssessmentAttempt.STATUS_MARKED])
                .select_related('assessment'))

    by_lesson = defaultdict(list)
    per_student = defaultdict(lambda: {'seconds': 0, 'lessons': set(), 'scores': []})
    for s in sessions:
        by_lesson[s['lesson_id']].append(s)
        per_student[s['student_id']]['seconds'] += s['total_seconds'] or 0
        per_student[s['student_id']]['lessons'].add(s['lesson_id'])
    by_paper = defaultdict(dict)
    for a in attempts:
        total = float(a.assessment.total_marks or 0)
        pct = round(float(a.score or 0) / total * 100, 1) if total else 0
        best = by_paper[a.assessment_id].get(a.student_id)
        if best is None or pct > best:
            by_paper[a.assessment_id][a.student_id] = pct
    for paper_id, scores in by_paper.items():
        for student_id, pct in scores.items():
            per_student[student_id]['scores'].append(pct)

    n = len(student_users) or 1
    rows = []
    for m in materials:
        row = {'material': m, 'kind': m.get_kind_display(), 'reach': None, 'minutes': None,
               'completion': None, 'mean': None, 'attempts': None}
        if m.lesson_id:
            got = by_lesson.get(m.lesson_id, [])
            row.update(reach=round(len({s['student_id'] for s in got}) / n * 100),
                       minutes=round(mean([s['total_seconds'] / 60 for s in got]), 1) if got else None,
                       completion=round(mean([s['completion_pct'] for s in got])) if got else None,
                       expected=m.lesson.estimated_minutes)
        elif m.assessment_id:
            scores = list(by_paper.get(m.assessment_id, {}).values())
            row.update(reach=round(len(scores) / n * 100), attempts=len(scores),
                       mean=round(mean(scores), 1) if scores else None)
        else:
            continue
        rows.append(row)

    people = []
    for user_id, person in student_users.items():
        st = per_student.get(user_id, {'seconds': 0, 'lessons': set(), 'scores': []})
        people.append({'person': person, 'minutes': round(st['seconds'] / 60),
                       'lessons': len(st['lessons']), 'lessons_total': len(lesson_ids),
                       'quizzes': len(st['scores']), 'quizzes_total': len(paper_ids),
                       'mean': round(mean(st['scores']), 1) if st['scores'] else None})
    people.sort(key=lambda p: (-(p['mean'] or -1), -p['minutes']))

    return render(request, 'learning/course_analytics.html', {
        'page_title': f'Course report — {offering}',
        'offering': offering, 'cohorts': cohorts, 'cohort': cohort,
        'rows': rows, 'people': people, 'student_count': len(student_users),
    })
