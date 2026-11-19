"""HTML views for assessments: list, take, submit (auto-marks via ``marking``)
and result. Login-protected; normal session auth + CSRF apply."""


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.roles import role_flags
from core.sanitize import script_json
from core.student_hidden import hidden_from_students
from core.scoping import children_of, is_scoped, taught_module_qs, viewing_child

from . import models, proctoring
from .marking import _assessment_questions, grade_attempt


def _is_staff_like(request):
    """True when the requester is admin/staff or an educator."""
    flags = role_flags(request)
    return flags['is_admin_staff'] or flags['is_educator']


def _oversees(request, assessment):
    """Admin/staff for any paper; an educator only for a module they teach.

    ``_is_staff_like`` alone is a *role* check — it would let any educator read
    any learner's result by its URL, including on modules they do not teach.
    """
    flags = role_flags(request)
    if flags['is_admin_staff']:
        return True
    if not flags['is_educator'] or not assessment.module_id:
        return False
    return taught_module_qs(request.user).filter(pk=assessment.module_id).exists()


def _can_access(user, assessment):
    """Whether ``user`` is on the module this paper belongs to.

    A paper with no module is refused rather than raising: an assessment can be
    half-built (imported against a topic, or created before an offering was
    picked), and a candidate meeting one should be turned away, not shown a 500.
    """
    person = getattr(user, 'profile', None)
    offering = assessment.module
    if person is None or offering is None:
        return False
    return (offering.students.filter(pk=person.pk).exists()
            or offering.educators.filter(pk=person.pk).exists())


def _within_window(assessment):
    """True when now falls inside the assessment's ``available_from`` / ``available_to``
    window.
    """
    now = timezone.now()
    if assessment.available_from and now < assessment.available_from:
        return False
    if assessment.available_to and now > assessment.available_to:
        return False
    return True


@login_required
@hidden_from_students
def index(request):
    """The quizzes, tests and mock exams this user is concerned with.

    Learners see the open ones in their own modules (including schedule papers
    built by :mod:`apps.assessments.papers`); educators the ones they set; a
    parent sees their child's list read-only (no Take button).

    A paper built from the schedule is listed only when its paper is on the
    learner's own schedule (published, their intake), and opens only when the
    module gate would open the PDF itself — the paper does not become a way
    round paying for the module.

    An educator sees only the papers of the modules they teach — they set and mark work
    for their own modules only. For a learner the page shows, per paper, their best
    finished attempt and any still in progress, skipping papers that are not on their
    schedule. A released memo still waits for the learner's own submission, and a
    submitted paper whose memo is not out yet says so. Kinds are grouped as the learner
    thinks of them, in the order they come in the year.
    """
    is_staff = _is_staff_like(request)
    child = viewing_child(request)
    learner = child or request.user
    qs = models.Assessment.objects.select_related('module__module', 'module__programme').order_by(
        'module_id', 'kind', 'title')

    if child is not None or not is_staff:
        person = getattr(learner, 'profile', None)
        module_ids = list(person.selected_modules.values_list('id', flat=True)) if person else []
        qs = qs.filter(status='open', module_id__in=module_ids)
    elif is_scoped(request.user):
        qs = qs.filter(module__in=taught_module_qs(request.user))
    assessments = list(qs[:400])

    best, in_progress = {}, set()
    if not is_staff or child is not None:
        for att in (models.AssessmentAttempt.objects
                    .filter(student=learner, assessment__in=[a.pk for a in assessments])
                    .order_by('assessment_id', '-score')):
            if att.status == models.AssessmentAttempt.STATUS_IN_PROGRESS:
                in_progress.add(att.assessment_id)
            else:
                best.setdefault(att.assessment_id, att)

    from .papers import PaperAccess, SOURCE_PREFIX
    access = PaperAccess(learner)
    rows = []
    for a in assessments:
        row = {'a': a, 'is_paper': a.source_ref.startswith(SOURCE_PREFIX),
               'attempt': best.get(a.pk), 'in_progress': a.pk in in_progress,
               'open': True, 'lock_label': '', 'sitting': None, 'memo': None,
               'due': a.available_to}
        if row['is_paper']:
            paper = access.paper(a)
            if paper is None and not is_staff:
                continue
            if paper is not None:
                row['open'] = paper.open
                row['lock_label'] = '' if paper.open else paper.reason_label
                event = paper.material.phase.calendar_event
                row['sitting'] = event.start if event else None
            memo = access.memo(a)
            row['memo'] = 'open' if (memo and memo.open) else ('later' if memo else None)
        elif not is_staff and not _within_window(a):
            row['open'] = False
            row['lock_label'] = 'Not open yet' if (a.available_from and a.available_from > timezone.now()) else 'Closed'

        attempt = row['attempt']
        if row['in_progress']:
            row['state'], row['state_label'] = 'progress', 'In progress'
        elif attempt and attempt.status == models.AssessmentAttempt.STATUS_MARKED:
            row['state'], row['state_label'] = 'marked', 'Marked'
        elif attempt:
            row['state'], row['state_label'] = 'submitted', 'Submitted'
        else:
            row['state'], row['state_label'] = 'new', 'Not started'
        if row['memo'] and not is_staff:
            if not attempt:
                row['memo'] = 'later'
            elif row['memo'] == 'later':
                row['memo'] = 'pending'
        rows.append(row)

    kind_order = [models.Assessment.KIND_QUIZ, models.Assessment.KIND_TEST,
                  models.Assessment.KIND_MOCK_EXAM, models.Assessment.KIND_EXAM_SECTION,
                  models.Assessment.KIND_ASSIGNMENT]
    kind_labels = {models.Assessment.KIND_QUIZ: 'Quizzes', models.Assessment.KIND_TEST: 'Tests',
                   models.Assessment.KIND_MOCK_EXAM: 'Mock exams',
                   models.Assessment.KIND_EXAM_SECTION: 'Exam sections',
                   models.Assessment.KIND_ASSIGNMENT: 'Assignments'}
    present = {row['a'].kind for row in rows}
    kinds = [(k, kind_labels[k], sum(1 for r in rows if r['a'].kind == k))
             for k in kind_order if k in present]

    groups = []
    for row in rows:
        module = row['a'].module
        if not groups or groups[-1]['module'] != module:
            groups.append({'module': module, 'rows': []})
        groups[-1]['rows'].append(row)
    for group in groups:
        group['rows'].sort(key=lambda r: (kind_order.index(r['a'].kind) if r['a'].kind in kind_order else 9,
                                          r['a'].title))

    name = (child.get_full_name() or child.get_username()) if child else ''
    return render(request, 'assessments/index.html', {
        'page_title': f'{name}’s Assessments' if child else 'Assessments',
        'groups': groups, 'kinds': kinds, 'total': len(rows),
        'is_staff': is_staff and child is None,
        'child': child, 'read_only': child is not None,
        'children': children_of(request.user) if child else None,
    })


@login_required
def take(request, assessment_id):
    """Start or resume an attempt and render the take page (login required).

    Access depends on what the paper is:

    - Only someone who oversees the module (admin/staff, or an educator who teaches it)
      previews past the status, window and schedule gates. Being an educator somewhere
      else is not enough — that would open every closed or unreleased paper on the
      platform to any coach. Educators previewing their own assessment are not
      proctored.
    - An assessment set as material on a schedule week opens only while that week is
      open to the learner (:mod:`apps.learning.week_access`): paid month, previous /
      current / next week, or made available. Other non-paper assessments require access
      to the offering.
    - A paper built from the schedule is asked of its materials instead: it opens
      exactly when its PDF would — the module gate (paid / trial / bought, which knows
      enrolments) and the learner's own schedule.

    An attempt the focus guard ended cannot be reopened: it is finalised with whatever
    was answered, so reloading the page is not a way back in. For resuming, the page
    receives ``{question_id: [selected choice ids]}`` and ``{question_id: "text"}``
    maps.
    """
    assessment = get_object_or_404(models.Assessment.objects.select_related('module'), pk=assessment_id)
    from .papers import SOURCE_PREFIX
    is_paper = assessment.source_ref.startswith(SOURCE_PREFIX)
    if not is_paper and not _can_access(request.user, assessment):
        return HttpResponseForbidden('You are not enrolled for this assessment.')

    is_staff = _oversees(request, assessment)
    if not is_staff and (assessment.status != 'open' or not _within_window(assessment)):
        from core.errors import note
        note('ASMT-4003', request, assessment=assessment.pk, status=assessment.status)
        messages.error(request, 'This assessment is not currently open.')
        return redirect('assessments:index')

    if not is_staff and not is_paper:
        from django.urls import reverse
        from apps.learning.access import schedule_refusal
        refused = schedule_refusal(request.user, assessment=assessment)
        if refused is not None:
            messages.info(request, f'{assessment.title} is in a week that is locked for you: pay to unlock it.')
            anchor = f'#week-{refused.week_id}' if refused.week_id else f'#mat-{refused.pk}'
            return redirect(reverse('learning:module-feed', args=[refused.phase.programme_module_id]) + anchor)

    paper_url = ''
    if is_paper:
        from .papers import PaperAccess
        paper = PaperAccess(request.user).paper(assessment)
        if not is_staff and (paper is None or not paper.open):
            messages.error(request, paper.reason_label if paper else 'This paper is not on your schedule.')
            return redirect('assessments:index')
        paper_url = paper.url if paper else ''

    attempt = _get_or_create_attempt(request.user, assessment)
    if attempt is None:
        messages.warning(request, 'You have used all your attempts for this assessment.')
        return redirect('assessments:index')

    if attempt.terminated_at and attempt.status == models.AssessmentAttempt.STATUS_IN_PROGRESS:
        grade_attempt(attempt)
        messages.error(request, 'This attempt was ended because you left the assessment window '
                                'too many times. It has been submitted as it stood.')
        return redirect('assessments:result', public_id=attempt.public_id)

    sections = assessment.sections.prefetch_related('questions__choices').order_by('order')
    existing = {a.question_id: a for a in attempt.answers.prefetch_related('selected_choices')}
    selected_map = {str(qid): list(a.selected_choices.values_list('id', flat=True))
                    for qid, a in existing.items()}
    text_map = {str(qid): (a.response or {}).get('text', '') for qid, a in existing.items()}

    guard = proctoring.client_config(assessment, attempt)
    if is_staff:
        guard['enabled'] = False

    return render(request, 'assessments/take.html', {
        'page_title': assessment.title, 'assessment': assessment,
        'attempt': attempt, 'sections': sections,
        'selected_map': script_json(selected_map), 'text_map': script_json(text_map),
        'time_limit': assessment.time_limit_minutes,
        'guard_json': script_json(guard), 'guard': guard,
        'is_paper': is_paper, 'paper_url': paper_url,
    })


@login_required
@require_POST
def proctor_event(request, public_id):
    """Report one integrity event for an in-progress attempt (focus guard).

    The browser says only *what happened*; the tally, the limit and any
    decision to end the attempt are computed in
    :mod:`apps.assessments.proctoring` so a tampered client cannot lower its
    own warning count.
    """
    attempt = get_object_or_404(
        models.AssessmentAttempt.objects.select_related('assessment'),
        public_id=public_id, student=request.user)
    if attempt.status != models.AssessmentAttempt.STATUS_IN_PROGRESS:
        return JsonResponse({'ok': False, 'error': 'not-in-progress'}, status=409)
    if not proctoring.is_active(attempt.assessment):
        return JsonResponse({'ok': True, 'action': '', 'warnings': 0})

    detail = {'ua': request.META.get('HTTP_USER_AGENT', '')[:200]}
    if request.POST.get('detail'):
        detail['note'] = request.POST['detail'][:200]
    state = proctoring.record(
        attempt, request.POST.get('kind') or '',
        seconds_away=request.POST.get('seconds') or 0, detail=detail)
    state['ok'] = True
    return JsonResponse(state)


def _get_or_create_attempt(user, assessment):
    """Resume an in-progress attempt, or start a new one if attempts remain.

    ``attempts_allowed`` of 0 means unlimited.
    """
    attempts = models.AssessmentAttempt.objects.filter(assessment=assessment, student=user)
    in_progress = attempts.filter(status=models.AssessmentAttempt.STATUS_IN_PROGRESS).order_by('-attempt_no').first()
    if in_progress:
        return in_progress
    finished = attempts.exclude(status=models.AssessmentAttempt.STATUS_IN_PROGRESS)
    allowed = assessment.attempts_allowed
    if allowed and finished.count() >= allowed:
        return None
    last = attempts.order_by('-attempt_no').first()
    next_no = (last.attempt_no + 1) if last else 1
    return models.AssessmentAttempt.objects.create(
        assessment=assessment, student=user, attempt_no=next_no,
        status=models.AssessmentAttempt.STATUS_IN_PROGRESS, started_at=timezone.now())


@login_required
@require_POST
def submit(request, public_id):
    """Save the student's answers for an in-progress attempt, grade it and show the result.

    POST only, by the attempt's own student. A schedule computation arrives as one input
    per scoring line and is kept keyed by line index rather than flattened to text,
    because the marker scores each line on its own and needs to know which is which.
    Uploaded files are validated explicitly, because assigning the upload skips the
    field's validators. Other free-text types (fill / short / long / essay / coding …)
    are stored as ``text``. :func:`grade_attempt` then sets score, passed and status,
    and fires the grade signal.
    """
    attempt = get_object_or_404(
        models.AssessmentAttempt.objects.select_related('assessment'),
        public_id=public_id, student=request.user)
    if attempt.status != models.AssessmentAttempt.STATUS_IN_PROGRESS:
        return redirect('assessments:result', public_id=attempt.public_id)

    for question in _assessment_questions(attempt.assessment):
        field = f'q_{question.id}'
        answer, _ = models.Answer.objects.get_or_create(attempt=attempt, question=question)

        if question.type in ('mcq', 'tf'):
            answer.save()
            choice_id = request.POST.get(field)
            answer.selected_choices.set([choice_id] if choice_id else [])
        elif question.type == 'multi':
            answer.save()
            answer.selected_choices.set(request.POST.getlist(field))
        elif question.type in ('matching', 'ordering'):
            answer.response = {'value': request.POST.get(field, '')}
            answer.save()
        elif question.type == 'schedule':
            lines = {}
            for index in range(len(((question.config or {}).get('lines') or []))):
                value = request.POST.get(f'{field}_line_{index}', '')
                if value != '':
                    lines[str(index)] = value
            answer.response = {'lines': lines}
            answer.save()
        elif question.type in ('file_upload', 'project', 'audio_resp', 'video_resp'):
            if field in request.FILES:
                from core import validators as v
                upload = request.FILES[field]
                errors = v.upload_errors(upload, v.validate_attachment)
                if errors:
                    messages.error(request, f'{upload.name} was not accepted: {" ".join(errors)}')
                else:
                    answer.file = upload
            answer.save()
        else:
            answer.response = {'text': request.POST.get(field, '')}
            answer.save()

    if attempt.started_at:
        attempt.time_spent_seconds = int((timezone.now() - attempt.started_at).total_seconds())
    grade_attempt(attempt)
    messages.success(request, 'Your responses have been submitted.')
    return redirect('assessments:result', public_id=attempt.public_id)


def _solution_for(question):
    """The model answer for one question, shaped for the result page.

    Reads the structure the content-pack importer wrote into ``config``: a
    computation carries ``lines`` (label · authority · expected figure · marks),
    a discussion carries ``rubric`` (the point that earns the mark · authority ·
    marks). Falls back to the free-text ``guidance`` an educator typed by hand.
    """
    config = question.config or {}
    kind = config.get('kind')
    if kind == 'schedule' and config.get('lines'):
        return {'kind': 'schedule', 'lines': config['lines'], 'text': ''}
    if kind == 'rubric' and config.get('rubric'):
        return {'kind': 'rubric', 'rubric': config['rubric'], 'text': ''}
    if question.guidance:
        return {'kind': 'text', 'text': question.guidance}
    return None


@login_required
def result(request, public_id):
    """The result page for one attempt.

    Visible to the learner who sat it, the staff/educators who set it, and the learner's
    parent — results are exactly what a guardian is here to see. The memo opens only
    once this candidate has committed an answer (see
    :meth:`Assessment.solution_released_for`); this is resolved here, once, rather than
    being a condition the template could get wrong, and a question's ``solution`` is
    only ever populated when the memo is released, so a solution cannot reach the page
    and be hidden with CSS. "How you could have scored more" is likewise shown only once
    the memo is released, because it names the right answers; on a practice paper built
    from the schedule it also waits until a coach has marked it, since it reads marks.
    For such a paper the answer is shown beside the memo and the memo's own PDF is
    linked once it is out (through the gated door). The full integrity event log is the
    educator's; the student sees their tally only.
    """
    attempt = get_object_or_404(
        models.AssessmentAttempt.objects.select_related('assessment', 'student'), public_id=public_id)
    oversees = _oversees(request, attempt.assessment)
    allowed = (attempt.student_id == request.user.id
               or oversees
               or children_of(request.user).filter(pk=attempt.student_id).exists())
    if not allowed:
        return HttpResponseForbidden()

    assessment = attempt.assessment
    show_solution = assessment.solution_released_for(request.user)

    questions = _assessment_questions(assessment)
    answers = {a.question_id: a for a in attempt.answers.prefetch_related('selected_choices')}
    rows = []
    for question in questions:
        answer = answers.get(question.id)
        rows.append({
            'question': question, 'answer': answer,
            'choices': list(question.choices.all()),
            'selected': set(answer.selected_choices.values_list('id', flat=True)) if answer else set(),
            'solution': _solution_for(question) if show_solution else None,
        })

    total = float(assessment.total_marks or 0)
    pct = round(float(attempt.score) / total * 100, 1) if total else 0
    from .analytics import score_best
    best = score_best(attempt) if show_solution else None

    from .papers import SOURCE_PREFIX
    is_paper = assessment.source_ref.startswith(SOURCE_PREFIX)
    memo_state = None
    if is_paper:
        from .papers import PaperAccess
        memo_state = PaperAccess(request.user).memo(assessment)
        if attempt.status != models.AssessmentAttempt.STATUS_MARKED:
            best = None
    return render(request, 'assessments/result.html', {
        'best': best,
        'analytics_url': (reverse('assessments:analytics', args=[assessment.pk]) if oversees else ''),
        'page_title': f'Result — {assessment.title}',
        'attempt': attempt, 'rows': rows, 'pct': pct,
        'show_solution': show_solution,
        'is_paper': is_paper,
        'memo_waiting': bool(memo_state) and not show_solution,
        'memo_url': memo_state.url if (memo_state and show_solution) else '',
        'solution_notes': (assessment.solution_notes or {}) if show_solution else {},
        'integrity': proctoring.summary(attempt) if oversees else None,
    })


@login_required
def analytics(request, assessment_id):
    """How the class answered this paper — the coach's item analysis."""
    assessment = get_object_or_404(models.Assessment.objects.select_related('module'), pk=assessment_id)
    if not _oversees(request, assessment):
        return HttpResponseForbidden()
    from .analytics import item_analysis
    data = item_analysis(assessment, best_only=request.GET.get('all') != '1')
    return render(request, 'assessments/analytics.html', {
        'page_title': f'Analytics — {assessment.title}', 'assessment': assessment,
        'all_attempts': request.GET.get('all') == '1', **data})
