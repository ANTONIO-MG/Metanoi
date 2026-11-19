"""On-demand academic-intelligence summaries (admin/staff/educator only).

Each function is guarded so a missing table never breaks a dashboard. Heavy
exports use openpyxl when available. Natural-language insight can reuse the
existing local AI (apps.ai_assistant) — kept optional.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()


def student_summary(modules=None):
    """Active/inactive student counts + recent login activity.

    ``modules`` (a ProgrammeModule queryset) narrows it to the students enrolled
    on those offerings — an educator's own audience. ``None`` = everyone.
    """
    try:
        from apps.accounts.models import Person
        now = timezone.now()
        students = Person.objects.filter(user_type='student', preview_of__isnull=True)
        users = (User.objects.filter(profile__user_type='student', profile__preview_of__isnull=True)
                 .exclude(account_settings__is_closing=True))
        if modules is not None:
            students = students.filter(module_enrolments__programme_module__in=modules).distinct()
            users = users.filter(profile__in=students).distinct()
        total = students.count()
        active = users.filter(is_active=True).count()
        logged_today = users.filter(last_login__date=now.date()).count()
        logged_week = users.filter(last_login__gte=now - timedelta(days=7)).count()
        return {
            'total': total, 'active': active, 'inactive': max(0, total - active),
            'logged_today': logged_today, 'logged_week': logged_week,
        }
    except Exception:
        logger.exception('student_summary failed')
        return {}


def module_popularity(limit=10, modules=None):
    """Most-enrolled module offerings (only ``modules`` when given)."""
    try:
        from apps.learning.models import ProgrammeModule
        qs = ProgrammeModule.objects.all() if modules is None else modules
        return list(qs.annotate(enrolled=Count('enrolments', filter=Q(
                        enrolments__person__user__is_active=True)))
                    .order_by('-enrolled')[:limit]
                    .values('name', 'enrolled'))
    except Exception:
        logger.exception('module_popularity failed')
        return []


def module_performance(limit=20, modules=None):
    """Average mark / pass rate per module from computed grades.

    ``modules`` limits it to those offerings — an educator must only ever see
    results for the modules they teach. ``None`` = every module (admin/staff).
    """
    try:
        from apps.reports.models import Grade
        grades = Grade.objects.all()
        if modules is not None:
            grades = grades.filter(module__in=modules)
        rows = (grades.values('module__name')
                .annotate(avg_mark=Avg('final_pct'), students=Count('id'),
                          passes=Count('id', filter=Q(passed=True)))
                .order_by('-avg_mark')[:limit])
        out = []
        for r in rows:
            students = r['students'] or 0
            out.append({
                'module': r['module__name'],
                'avg_mark': round(float(r['avg_mark'] or 0), 1),
                'students': students,
                'pass_rate': round((r['passes'] / students * 100) if students else 0, 1),
            })
        return out
    except Exception:
        logger.exception('module_performance failed')
        return []


def study_summary(modules=None):
    """Aggregate study hours + completed lesson sessions."""
    try:
        from apps.learning.models import StudySession
        from django.db.models import Sum
        sessions = StudySession.objects.all()
        if modules is not None:
            sessions = sessions.filter(lesson__module__in=modules)
        aggregated = sessions.aggregate(
            total_seconds=Sum('total_seconds'), session_count=Count('id'))
        completed = sessions.filter(status='completed').count()
        data = {
            'total_hours': round((aggregated['total_seconds'] or 0) / 3600.0, 1),
            'sessions': aggregated['session_count'] or 0, 'completed': completed,
        }
        return data
    except Exception:
        logger.exception('study_summary failed')
        return {}


def institution_dashboard(modules=None):
    """One-call executive summary for the analytics landing page.

    ``modules`` scopes every figure to those offerings (an educator's taught
    modules); ``None`` is the whole institution (admin/staff).
    """
    data = {
        'students': student_summary(modules),
        'popular': module_popularity(modules=modules),
        'modules': module_performance(modules=modules),
        'study': study_summary(modules),
    }
    try:
        from apps.reports.models import Certificate
        from apps.learning.models import ProgrammeModule
        certs = Certificate.objects.all()
        offerings = ProgrammeModule.objects.all()
        if modules is not None:
            certs = certs.filter(module__in=modules)
            offerings = modules
        data['certificates_issued'] = certs.count()
        data['total_modules'] = offerings.count()
    except Exception:
        pass
    try:
        from apps.learning.models import Lesson
        lessons = Lesson.objects.all()
        if modules is not None:
            lessons = lessons.filter(module__in=modules)
        data['total_lessons'] = lessons.count()
    except Exception:
        pass
    return data


def compute_risk(student):
    """Heuristic predictive-risk score (0–100) for a student, with reasons."""
    reasons = []
    score = 0
    try:
        from apps.reports.models import Grade
        from apps.learning.models import StudySession
        from apps.assessments.models import AssessmentAttempt

        failing = Grade.objects.filter(student=student, passed=False).count()
        if failing:
            score += min(40, failing * 20)
            reasons.append(f'{failing} failing module(s)')

        recent_study = StudySession.objects.filter(
            student=student, created_at__gte=timezone.now() - timedelta(days=14)).count()
        if recent_study == 0:
            score += 30
            reasons.append('No study sessions in 14 days')

        recent_fail = AssessmentAttempt.objects.filter(
            student=student, passed=False,
            submitted_at__gte=timezone.now() - timedelta(days=30)).count()
        if recent_fail >= 3:
            score += 30
            reasons.append(f'Failed {recent_fail} assessments recently')
    except Exception:
        logger.exception('compute_risk failed')
    return min(100, score), reasons


def export_rows_xlsx(rows, headers, filename='report.xlsx'):
    """Return (filename, bytes) for an Excel export, or (None, None) if openpyxl is absent."""
    try:
        import io
        from openpyxl import Workbook
    except Exception:
        return None, None
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, '') if isinstance(row, dict) else row for h in headers])
    buf = io.BytesIO()
    wb.save(buf)
    return filename, buf.getvalue()
