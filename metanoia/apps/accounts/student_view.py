"""Super student — the navbar's "Student view" toggle.

Three small views over :mod:`core.student_view`, which holds the rules: who may
switch, over which institutions/programmes, and how the switch works. These
URLs live under ``/community/student-view/``, which the middleware never swaps,
so here ``request.user`` is always the real admin or educator.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core import student_view as sv
from core.errors import report


def _forbidden():
    return HttpResponseForbidden('Student view is for admins, staff and educators.')


@login_required
def picker(request):
    """Choose what to see as a student or a parent: everything, an institution,
    a programme or one of its cohorts. ``?as=parent`` switches the picker to
    parent view (the choice rides along in every option's form)."""
    if not sv.can_use(request.user):
        return _forbidden()
    scope = getattr(request, 'student_view', None)
    as_ = request.GET.get('as') or (scope.as_ if scope is not None else sv.AS_STUDENT)
    as_ = sv.AS_PARENT if as_ == sv.AS_PARENT else sv.AS_STUDENT
    return render(request, 'accounts/student-view.html', {
        'page_title': 'Student and parent view',
        'as_': as_,
        'groups': sv.picker_choices(request.user),
        'scope': getattr(request, 'student_view', None),
        'module_count': sv.allowed_modules(request.user).count(),
    })


@login_required
@require_POST
def start(request):
    """Switch into (or re-scope) student view, then land on the student dashboard."""
    if not sv.can_use(request.user):
        return _forbidden()
    scope = sv.resolve_scope(request.user, request.POST.get('institution'),
                             request.POST.get('programme'), request.POST.get('cohort'),
                             request.POST.get('as') or sv.AS_STUDENT)
    if scope is None:
        report('USER-2005', request=request, context={
            'institution': request.POST.get('institution'), 'programme': request.POST.get('programme'),
            'cohort': request.POST.get('cohort')})
        messages.error(request, 'That isn’t something you can view as a student.')
        return redirect('accounts:student-view')
    try:
        sv.start(request, scope)
    except Exception as exc:
        report('USER-6001', exc=exc, request=request)
        messages.error(request, 'Student view could not be switched on. Please try again.')
        return redirect('accounts:student-view')
    messages.info(request, f'You’re now seeing the platform as a {scope.audience} — {scope.label()}.')
    return redirect('myhub:index')


@login_required
@require_POST
def stop(request):
    """Back to your own account."""
    sv.stop(request)
    messages.info(request, 'Preview is off — you’re back in your own account.')
    return redirect('myhub:index')
