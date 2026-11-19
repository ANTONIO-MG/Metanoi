"""HTML views for reports & certificates.

Who sees what:

* **student**          — their own grades and their own certificates.
* **parent**           — the grades and certificates of the child(ren) they are
  linked to, and nobody else's (never the class around them).
* **educator**         — grades for the modules they teach (marking depends on
  them), and **no certificates at all**: every certificate route 404s for an
  educator (see :func:`_certificate_for`).
* **admin / staff**    — everything.
"""

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.roles import role_flags
from core.scoping import children_of, taught_module_qs, viewing_child

from . import certificates, completions, models, progress_board

logger = logging.getLogger('apps')


def _student_pool(request, flags):
    """The students this viewer may pick between on a report, or ``None`` for a
    student (who only ever sees themself).

    Staff see any, an educator the students they teach, a parent their own
    child(ren) and nobody else's. Selecting is always done against this pool,
    so an id from outside it is a 404 rather than someone else's data.
    """
    if not (flags['is_admin_staff'] or flags['is_educator'] or flags['is_parent']):
        return None
    User = get_user_model()
    if flags['is_parent']:
        students = children_of(request.user)
    else:
        students = (User.objects.filter(profile__user_type='student', profile__preview_of__isnull=True)
                    .exclude(account_settings__is_closing=True))
        if not flags['is_admin_staff']:
            students = students.filter(
                profile__module_enrolments__programme_module__in=taught_module_qs(request.user))
    return students.distinct().order_by('first_name', 'last_name', 'username')


@login_required
def my_reports(request):
    """Reports: the assessment report, the module report card and the timeline
    of what has been completed.

    Certificates are deliberately not shown here (they have their own pages).

    A student sees their own; a parent their child (picker when there are
    several); an educator and staff pick a student, or see the latest results
    across everyone they may see when none is picked. Completion records are
    refreshed for whoever is being looked at, so the timeline is never stale
    for the person reading it.

    Educators only ever see the modules they teach. Ahead of its migration the
    completion table may not exist yet; the rest of the report still stands.
    """
    flags = role_flags(request)
    students = _student_pool(request, flags)
    target = None
    if students is None:
        target = request.user
    else:
        sid = request.GET.get('student')
        if sid:
            target = get_object_or_404(students, pk=sid)
        elif flags['is_parent']:
            target = viewing_child(request)

    attempts = None
    try:
        from apps.assessments.models import AssessmentAttempt
        attempts = (AssessmentAttempt.objects
                    .filter(status__in=['submitted', 'marked'])
                    .select_related('assessment', 'assessment__module', 'student', 'student__profile')
                    .defer('draft'))
    except Exception:  # pragma: no cover — the assessments app is being rebuilt
        logger.exception('reports: assessment attempts unavailable')
    grades = models.Grade.objects.select_related('student', 'student__profile', 'module')
    records = models.CompletionRecord.objects.select_related('module', 'student', 'student__profile')

    taught = taught_module_qs(request.user) if (flags['is_educator'] and not flags['is_admin_staff']) else None
    if taught is not None:
        grades = grades.filter(module__in=taught)
        records = records.filter(module__in=taught)
        if attempts is not None:
            attempts = attempts.filter(assessment__module__in=taught)

    if target is not None:
        completions.record_completions(target)
        people = [target]
    elif students is not None:
        people = students
    else:
        people = [request.user]
    grades = grades.filter(student__in=people)
    records = records.filter(student__in=people)
    if attempts is not None:
        attempts = attempts.filter(student__in=people)

    attempt_rows = []
    for attempt in (attempts.order_by('-submitted_at', '-id')[:200] if attempts is not None else []):
        total = float(attempt.assessment.total_marks or 0)
        attempt_rows.append({
            'attempt': attempt,
            'pct': round(float(attempt.score or 0) / total * 100) if total else None,
            'is_marked': attempt.status == 'marked',
            'url': reverse('assessments:result', args=[attempt.public_id]),
        })
    marked = [r for r in attempt_rows if r['is_marked'] and r['pct'] is not None]
    try:
        record_list = list(records.order_by('-completed_at', '-id')[:150])
        completed = records.count()
        milestones = records.filter(kind__in=[models.CompletionRecord.KIND_PHASE,
                                              models.CompletionRecord.KIND_MODULE]).count()
    except Exception:
        logger.exception('reports: completion records unavailable')
        record_list, completed, milestones = [], 0, 0

    summary = {
        'sat': len(attempt_rows),
        'marked': len(marked),
        'passed': sum(1 for r in marked if r['attempt'].passed),
        'avg': round(sum(r['pct'] for r in marked) / len(marked)) if marked else None,
        'awaiting': sum(1 for r in attempt_rows if not r['is_marked']),
        'completed': completed,
        'milestones': milestones,
    }
    return render(request, 'reports/my-reports.html', {
        'page_title': 'Reports',
        'target': target,
        'students': students,
        'is_own': target == request.user,
        'show_student': target is None,
        'picker_label': 'Child' if flags['is_parent'] else 'Student',
        'attempt_rows': attempt_rows,
        'grades': grades.order_by('module', '-final_pct')[:200],
        'records': record_list,
        'summary': summary,
    })


def _certificate_for(request, pk):
    """The certificate, if this user is allowed to see it.

    A student may see their own, a parent their child's, and admin/staff any.
    **Educators may see none** — not their students' and not their own; the whole
    surface is closed to them. Everyone else gets a 404 rather than a 403, so a
    certificate someone can't see is never even confirmed to exist.
    """
    cert = get_object_or_404(
        models.Certificate.objects.select_related('student', 'module'), pk=pk)
    flags = role_flags(request)
    if flags['is_admin_staff']:
        allowed = True
    elif flags['is_educator']:
        allowed = False
    elif flags['is_parent']:
        allowed = children_of(request.user).filter(pk=cert.student_id).exists()
    else:
        allowed = cert.student_id == request.user.id
    if not allowed:
        from django.http import Http404
        raise Http404
    return cert


@login_required
def certificate_view(request, pk):
    """On-screen preview — the same artwork the PNG and PDF are cut from."""
    cert = _certificate_for(request, pk)
    try:
        ctx = certificates.certificate_context(cert, request)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('reports:my-reports')
    ctx['page_title'] = cert.title
    return render(request, 'reports/certificate.html', ctx)


def _certificate_download(request, pk, *, renderer, content_type, extension, disposition):
    """Shared plumbing for the image/PDF endpoints.

    ``disposition`` is ``attachment`` for a download and ``inline`` for the copy
    the preview page embeds, so the same rendering path serves both.
    """
    cert = _certificate_for(request, pk)
    try:
        data = renderer(cert, request)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return redirect('reports:my-reports')
    except Exception:
        logger.exception('reports: certificate %s failed to render', cert.pk)
        messages.error(request, 'That certificate could not be rendered just now.')
        return redirect('reports:my-reports')

    response = HttpResponse(data, content_type=content_type)
    response['Content-Disposition'] = f'{disposition}; filename="{cert.number}.{extension}"'
    response['Content-Length'] = str(len(data))
    return response


@login_required
def certificate_pdf(request, pk):
    """Download the certificate as a print-ready A4-landscape PDF."""
    return _certificate_download(
        request, pk, renderer=certificates.ensure_pdf,
        content_type='application/pdf', extension='pdf', disposition='attachment')


@login_required
def certificate_png(request, pk):
    """Download the certificate as a PNG image."""
    return _certificate_download(
        request, pk, renderer=lambda c, r: certificates.render_png(c, r, scale=1.5),
        content_type='image/png', extension='png', disposition='attachment')


@login_required
def certificate_image(request, pk):
    """The same PNG, served inline — this is what the preview page displays."""
    return _certificate_download(
        request, pk, renderer=lambda c, r: certificates.render_png(c, r, scale=1.0),
        content_type='image/png', extension='png', disposition='inline')


@login_required
def my_progress(request):
    """The progress dashboard: overall and per-module progress, results, activity
    and what to improve — see :mod:`apps.reports.progress_board`.

    This *is* the analytics page for a single learner, so each role reaches it
    through the same view with a different pool of students behind ``?student=``
    (:func:`_student_pool`): staff see any, an educator the students they teach,
    and a parent only their own child(ren).

    A parent has no progress of their own, so the page opens on their child and
    shows the child's programme General module as a card of its own ahead of the
    module cards (it is free, open and not counted). An educator viewing a
    student sees only the modules they teach. The picker reads as "whose
    progress am I looking at" for a parent ("Child") and as "pick a student" for
    staff ("Student").
    """
    flags = role_flags(request)
    students = _student_pool(request, flags)
    target = request.user
    if students is not None:
        sid = request.GET.get('student')
        if sid:
            target = get_object_or_404(students, pk=sid)
        elif flags['is_parent']:
            target = viewing_child(request) or request.user

    scope = None
    if flags['is_educator'] and not flags['is_admin_staff'] and target != request.user:
        scope = taught_module_qs(request.user)
    board = progress_board.build_dashboard(target, scope=scope)
    is_own = target == request.user
    general = progress_board.general_cards(target) if (flags['is_parent'] and not is_own) else []
    return render(request, 'reports/my-progress.html', {
        'page_title': 'My progress' if is_own else f'Progress · {target.get_full_name() or target.get_username()}',
        'board': board,
        'general_cards': general,
        'target': target,
        'students': students,
        'is_own': is_own,
        'picker_label': 'Child' if flags['is_parent'] else 'Student',
    })


def verify_certificate(request, verification_uuid):
    """Public certificate verification by UUID. A revoked certificate is still
    found — and reported as revoked, which is the answer a verifier needs."""
    cert = get_object_or_404(models.Certificate, verification_uuid=verification_uuid)
    return render(request, 'reports/verify.html', {
        'page_title': 'Certificate verification', 'cert': cert,
        'valid': not cert.is_revoked, 'revoked': cert.is_revoked,
    })
