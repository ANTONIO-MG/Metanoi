"""HTML views for the learning app: lessons list and view, the module paywall,
the lesson-player API and the module profile."""


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.errors import capture, note
from core.sanitize import script_json
from core.scoping import is_scoped, taught_module_qs
from . import models


def _is_staff_like(user):
    """Can this user *teach* — i.e. see the authoring side rather than the
    learner side? Says nothing about **which** content; use :func:`_teachable`
    for that."""
    person = getattr(user, 'profile', None)
    return bool(user.is_staff or (person and person.user_type in ('admin', 'staff', 'educator')))


def _teachable(user, queryset, *, module_field='module', author_field='created_by'):
    """Narrow an authoring queryset to what this user may actually reach.

    Admin/staff get everything; an educator gets the modules they teach plus
    anything they authored themselves (wherever it was filed).
    """
    if not is_scoped(user):
        return queryset
    from django.db.models import Q
    return queryset.filter(
        Q(**{f'{module_field}__in': taught_module_qs(user)}) | Q(**{author_field: user})
    ).distinct()


@login_required
def lessons(request):
    """List the lessons available to the current user.

    Filters: ``?programme=``, ``?module=`` and ``?when=past|today|upcoming``
    (by ``publish_at``, falling back to ``created_at`` — a lesson with no
    ``publish_at`` is already live, so it counts as today-or-earlier).

    Learners see published lessons on their selected modules; educators see only
    the slice of the institution they teach (:func:`_teachable`). Nobody is shown a
    lesson written for another role (a learner never sees the educator's copy, and
    vice versa). The programme and module filter options are the ones these
    lessons actually span.
    """
    qs = models.Lesson.objects.select_related('module', 'module__programme').order_by('-created_at')
    person = getattr(request.user, 'profile', None)
    is_staff = _is_staff_like(request.user)
    if not is_staff:
        module_ids = list(person.selected_modules.values_list('id', flat=True)) if person else []
        qs = qs.filter(status=models.Lesson.STATUS_PUBLISHED, module_id__in=module_ids)
    else:
        qs = _teachable(request.user, qs)
    qs = qs.for_role(request.user)

    from apps.learning.models import ProgrammeModule
    visible_module_ids = set(qs.values_list('module_id', flat=True))
    modules = (ProgrammeModule.objects.filter(pk__in=visible_module_ids)
                .select_related('programme').order_by('order', 'name'))
    from apps.learning.models import Programme
    programmes = (Programme.objects.filter(modules__in=visible_module_ids)
                  .distinct().order_by('name'))

    programme_id = (request.GET.get('programme') or '').strip()
    module_id = (request.GET.get('module') or '').strip()
    when = (request.GET.get('when') or '').strip()

    if programme_id.isdigit():
        qs = qs.filter(module__programme_id=int(programme_id))
        modules = modules.filter(programme_id=int(programme_id))
    if module_id.isdigit():
        qs = qs.filter(module_id=int(module_id))

    if when in ('past', 'today', 'upcoming'):
        from datetime import timedelta

        from django.db.models import F, Q
        from django.db.models.functions import Coalesce
        from django.utils import timezone
        now = timezone.localtime()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        qs = qs.annotate(live_at=Coalesce(F('publish_at'), F('created_at')))
        if when == 'past':
            qs = qs.filter(live_at__lt=day_start)
        elif when == 'today':
            qs = qs.filter(live_at__gte=day_start, live_at__lt=day_end)
        else:
            qs = qs.filter(Q(live_at__gte=day_end))

    return render(request, 'learning/lessons.html', {
        'page_title': 'Lessons', 'lessons': qs[:100], 'can_teach': is_staff,
        'programmes': programmes, 'modules': modules,
        'active_programme': int(programme_id) if programme_id.isdigit() else None,
        'active_module': int(module_id) if module_id.isdigit() else None,
        'active_when': when if when in ('past', 'today', 'upcoming') else '',
    })


def _can_view_lesson(user, lesson):
    """Author/staff always; learners only if the lesson is live and targeted at them.

    Targeting: individual students, a programme or a module. An educator gets
    the author's view only for lessons in a module they teach or wrote;
    otherwise they fall through to the learner rules.

    Learner rules, in order: a lesson written for a specific role is only for that
    role; individual targeting takes precedence when set; a programme-targeted
    lesson is for anyone registered for the programme or holding any module under
    it; otherwise the learner must hold one of the lesson's modules. Everyone on a
    programme "holds" its General module without an enrolment row.
    """
    if _is_staff_like(user):
        if not is_scoped(user):
            return True
        if lesson.created_by_id == user.id:
            return True
        if taught_module_qs(user).filter(pk=lesson.module_id).exists():
            return True
    if not lesson.is_live:
        return False
    person = getattr(user, 'profile', None)
    if person is None:
        return False
    roles = lesson.target_roles or []
    if roles and person.user_type not in roles:
        return False
    if lesson.visibility == models.Lesson.VIS_INDIVIDUAL:
        return lesson.target_users.filter(pk=user.pk).exists()
    enrolled_ids = set(person.selected_modules.values_list('id', flat=True))
    from .general import general_offerings_for
    enrolled_ids |= set(general_offerings_for(person).values_list('id', flat=True))
    if lesson.visibility == models.Lesson.VIS_PROGRAMME and lesson.target_programmes.exists():
        programme_ids = set(lesson.target_programmes.values_list('id', flat=True))
        if person.programme_enrolments.filter(programme_id__in=programme_ids).exists():
            return True
        return person.selected_modules.filter(programme_id__in=programme_ids).exists()
    if lesson.target_modules.exists():
        return bool(enrolled_ids & set(lesson.target_modules.values_list('id', flat=True)))
    return lesson.module_id in enrolled_ids


def _locked_module_for_lesson(user, lesson):
    """The :class:`ProgrammeModule` a lesson belongs to when it is LOCKED for this
    user, else ``None``. Staff/authors are never blocked; a free module is never
    locked. A lesson reaches its module via ``lesson.topic.programme_module``."""
    if _is_staff_like(user):
        return None
    topic = getattr(lesson, 'topic', None)
    module = topic.programme_module if (topic and topic.programme_module_id) else None
    if module is None:
        return None
    person = getattr(user, 'profile', None)
    if person is None or module.is_unlocked_for(person):
        return None
    return module


@login_required
def my_modules(request):
    """The student's modules, grouped by programme, showing which are unlocked
    (paid or on trial) and which are locked awaiting payment.

    The programme's General module comes first: free, open to everyone on the
    programme, and never an enrolment row (see :mod:`apps.learning.general`). Each
    enrolment carries ``rec_count``, how many imported recordings its module has
    flagged for My Programme, so the card can show a "▶ N recordings" link.
    """
    person = getattr(request.user, 'profile', None)
    if person is None:
        return redirect('myhub:index')
    enrolments = (person.module_enrolments
                  .select_related('programme_module__programme__institution',
                                  'programme_module__module')
                  .order_by('programme_module__programme__institution__name',
                            'programme_module__order'))
    groups = {}
    from .general import general_offerings_for
    general = {g.programme_id: g for g in general_offerings_for(person)}
    for programme_id, offering in sorted(general.items(), key=lambda kv: kv[1].programme.institution.name):
        groups.setdefault(offering.programme, [])
    for enrolment in enrolments:
        groups.setdefault(enrolment.programme_module.programme, []).append(enrolment)

    rec_counts = {}
    try:
        from django.db.models import Count

        from apps.livesessions.models import YouTubeVideo
        rows = (YouTubeVideo.objects
                .filter(is_hidden=False, playlist__is_active=True, playlist__show_my_programme=True,
                        playlist__programme_module__in=[e.programme_module_id for e in enrolments])
                .values('playlist__programme_module').annotate(n=Count('id')))
        rec_counts = {r['playlist__programme_module']: r['n'] for r in rows}
    except Exception:
        rec_counts = {}
    for enrolment in enrolments:
        enrolment.rec_count = rec_counts.get(enrolment.programme_module_id, 0)

    return render(request, 'learning/modules.html', {
        'page_title': 'My Modules',
        'groups': [{'programme': prog, 'enrolments': rows, 'general': general.get(prog.pk)}
                   for prog, rows in groups.items()],
    })


@login_required
def module_unlock(request, module_id):
    """Click a locked module → raise (or reuse) its invoice, then pay now via
    PayFast or have the EFT invoice e-mailed. Payment unlocks it instantly (via
    the finance ``invoice_paid`` signal).

    ``?renew=1``: a paying student whose week falls outside their paid month
    (:mod:`apps.learning.week_access`) pays the next month ahead; ``activate()`` then
    extends their paid run from the current ``paid_until``. A free module is
    unlocked immediately. E-mailing the EFT invoice is best-effort; a failure is
    reported as ``MAIL-5001`` and the invoice still stands.
    """
    from . import enrolment as enrol

    person = getattr(request.user, 'profile', None)
    if person is None:
        return redirect('myhub:index')
    module = models.ProgrammeModule.objects.filter(pk=module_id, is_active=True).first()
    if module is None:
        note('LRN-3002', request, module=module_id)
        messages.error(request, 'That module is no longer available.')
        return redirect('learning:my-modules')
    renew = request.GET.get('renew') == '1'
    if module.is_unlocked_for(person) and not (renew and not module.is_free):
        messages.info(request, f'{module.display_name} is already unlocked.')
        return redirect('learning:my-modules')

    if request.method == 'POST':
        action = request.POST.get('action')
        with capture('FIN-8002', request):
            enrolment, invoice = enrol.unlock_single_module(person, module)
        if invoice is None:
            enrolment.activate(months=1)
            messages.success(request, f'{module.display_name} unlocked.')
            return redirect('learning:my-modules')
        trial = (f' Your free week has started — {module.display_name} is open until '
                 f'{timezone.localtime(enrolment.trial_ends_at):%A %-d %B}.'
                 if enrolment.is_trial_valid and enrolment.trial_ends_at else '')
        if trial:
            messages.success(request, trial.strip())
        if action == 'eft':
            try:
                from apps.finance.emails import send_invoice_email
                send_invoice_email(invoice)
            except Exception as exc:
                from core.errors import report
                report('MAIL-5001', exc=exc, request=request, context={'invoice': str(invoice.public_id)})
            messages.info(request, 'Invoice e-mailed. Pay by EFT and the module unlocks the moment it clears.')
            return redirect('learning:my-modules')
        return redirect('finance:pay', public_id=invoice.public_id)

    return render(request, 'learning/module_unlock.html', {
        'page_title': f'Unlock {module.display_name}',
        'module': module, 'price': module.price_per_month, 'currency': 'ZAR',
    })


def _lesson_videos(lesson):
    """YouTube videos a staff member attached to this lesson. Best-effort."""
    try:
        from apps.livesessions.imported import videos_for_lesson
        return list(videos_for_lesson(lesson))
    except Exception:
        return []


@login_required
def lesson_view(request, lesson_id):
    """Render a lesson as the tear-drop player: ordered, labelled, timed sections
    with a personal rail (notes · bookmarks · scores · messages) alongside.

    Three gates run first:

    * **Targeting** — :func:`_can_view_lesson`.
    * **Module paywall** — content under a priced module is locked until paid or
      trialling; the learner is sent to the unlock page.
    * **Week gate** — a lesson that sits on a module's schedule opens only while its
      week is open to this student (paid month / previous-current-next week / made
      available), the same rule the schedule shows. Typing the URL of a locked
      week's study guide is refused here, not just hidden there.

    Opening the lesson heals its body flow (an anchorless section gets its place)
    and, for learners, starts or resumes the study session. ``rows`` is the body in
    author order; ``sections`` is the subset the contents rail and progress bar work
    from. Pure reading (blueprints, study guides, session summaries) renders as a
    Metanoia blog post rather than a stack of accordions — see
    :mod:`apps.learning.article`; ``?view=sections`` forces the player.
    """
    from . import player

    lesson = get_object_or_404(
        models.Lesson.objects.select_related('module', 'created_by', 'topic__programme_module'),
        pk=lesson_id)
    if not _can_view_lesson(request.user, lesson):
        note('LRN-2001', request, lesson=lesson_id)
        messages.error(request, 'That lesson is not available to you.')
        return redirect('learning:lessons')
    locked_module = _locked_module_for_lesson(request.user, lesson)
    if locked_module is not None:
        note('LRN-2003', request, module=locked_module.pk, lesson=lesson.pk)
        messages.info(request, f'"{locked_module.display_name}" is locked — unlock it to continue.')
        return redirect('learning:module-unlock', module_id=locked_module.pk)
    if not _is_staff_like(request.user):
        from .access import schedule_refusal
        refused = schedule_refusal(request.user, lesson=lesson)
        if refused is not None:
            note('LRN-2003', request, module=refused.phase.programme_module_id, lesson=lesson.pk)
            messages.info(request, f'"{lesson.title}" is in a week that is locked for you: '
                                   'pay to unlock it.')
            anchor = f'#week-{refused.week_id}' if refused.week_id else f'#mat-{refused.pk}'
            return redirect(reverse('learning:module-feed', args=[refused.phase.programme_module_id])
                            + anchor)

    is_author = _is_staff_like(request.user)
    from .authoring import ensure_body_flow
    ensure_body_flow(lesson)

    rows = player.body_rows(lesson, request.user, preview=is_author)
    sections = [r for r in rows if r['kind'] == 'section']

    session = None
    if not is_author:
        session, _ = models.StudySession.objects.get_or_create(
            student=request.user, lesson=lesson,
            defaults={'status': models.StudySession.STATUS_NOT_STARTED})
        if session.status in (models.StudySession.STATUS_NOT_STARTED, models.StudySession.STATUS_PAUSED):
            session.start()

    done = False
    if session is not None:
        done = session.status in (models.StudySession.STATUS_SUBMITTED, models.StudySession.STATUS_COMPLETED)

    notes = {str(r['section'].id): r['note'] for r in sections}
    notes[''] = player.lesson_note(lesson, request.user)

    ctx = {
        'page_title': lesson.title, 'lesson': lesson,
        'rows': rows, 'sections': sections,
        'notes_json': script_json(notes),
        'attachments': lesson.resources.all(),
        'completion': player.completion_pct(sections),
        'is_author': is_author,
        'already_completed': done,
        'lesson_videos': _lesson_videos(lesson),
    }
    ctx.update(player.lesson_rail(lesson, request.user, sections))
    from . import article
    if article.is_article(rows) and request.GET.get('view') != 'sections':
        ctx['article'] = True
        ctx['article_toc'] = article.prepare(rows, lesson)
        ctx['article_kind'] = _article_kind(lesson)
        ctx['article_crumbs'] = _article_crumbs(lesson)
    return render(request, 'learning/lesson_view.html', ctx)


def _article_kind(lesson):
    """'Blueprint', 'Study guide', 'Session summary'… — what this read is."""
    material = models.ModuleMaterial.objects.filter(lesson=lesson).first()
    if material is not None:
        return material.get_kind_display()
    return 'Session summary' if (lesson.slug or '').startswith('session-summary-') else 'Study notes'


def _article_crumbs(lesson):
    """Institution · programme · module codes for the hero's kicker line."""
    module = lesson.module
    if module is None:
        return []
    programme = getattr(module, 'programme', None)
    institution = getattr(programme, 'institution', None)
    return [x for x in (getattr(institution, 'code', ''), getattr(programme, 'code', ''),
                        getattr(module, 'code', '')) if x]


@login_required
@require_POST
def section_progress(request, section_id):
    """Mark a tear-drop complete (or accrue time on it) for the current learner."""
    from . import player

    section = get_object_or_404(
        models.LessonSection.objects.select_related('lesson'), pk=section_id)
    if not _can_view_lesson(request.user, section.lesson):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    completed = request.POST.get('completed', '1') not in ('0', 'false', '')
    try:
        seconds = int(request.POST.get('seconds') or 0)
    except (TypeError, ValueError):
        seconds = 0
    player.mark_section(section, request.user, completed=completed, seconds=seconds)

    rows = player.section_rows(section.lesson, request.user)
    return JsonResponse({
        'ok': True,
        'completion': player.completion_pct(rows),
        'unlocked': [r['section'].id for r in rows if not r['locked']],
    })


@login_required
@require_POST
def note_save(request, lesson_id):
    """Autosave the learner's private note for one section of a lesson."""
    lesson = get_object_or_404(models.Lesson, pk=lesson_id)
    if not _can_view_lesson(request.user, lesson):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)
    section = None
    section_id = request.POST.get('section')
    if section_id and str(section_id).isdigit():
        section = models.LessonSection.objects.filter(pk=section_id, lesson=lesson).first()
    note, _ = models.LessonNote.objects.get_or_create(
        lesson=lesson, section=section, student=request.user)
    note.body = (request.POST.get('body') or '')[:20000]
    note.save(update_fields=['body', 'updated_at'])
    return JsonResponse({'ok': True, 'saved_at': note.updated_at.isoformat()})


@login_required
@require_POST
def bookmark_add(request, lesson_id):
    """Save a spot in the lesson — a highlighted passage, a moment in a video or
    a question — so the learner can jump straight back to it."""
    lesson = get_object_or_404(models.Lesson, pk=lesson_id)
    if not _can_view_lesson(request.user, lesson):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

    kind = request.POST.get('kind')
    if kind not in dict(models.LessonBookmark.KIND_CHOICES):
        kind = models.LessonBookmark.KIND_TEXT
    section = models.LessonSection.objects.filter(
        pk=request.POST.get('section') or 0, lesson=lesson).first()
    block = models.LessonBlock.objects.filter(
        pk=request.POST.get('block') or 0, lesson=lesson).first()

    anchor = {}
    if request.POST.get('seconds'):
        try:
            anchor['seconds'] = round(float(request.POST['seconds']), 1)
        except (TypeError, ValueError):
            pass
    if request.POST.get('text'):
        anchor['text'] = request.POST['text'][:400]

    bookmark = models.LessonBookmark.objects.create(
        lesson=lesson, section=section, block=block, student=request.user,
        kind=kind, label=(request.POST.get('label') or '')[:300],
        note=(request.POST.get('note') or '')[:500], anchor=anchor)
    return JsonResponse({'ok': True, 'bookmark': {
        'id': bookmark.id, 'kind': bookmark.kind, 'icon': bookmark.icon,
        'label': bookmark.label, 'note': bookmark.note,
        'section_id': bookmark.section_id, 'block_id': bookmark.block_id,
        'section_title': bookmark.section.title if bookmark.section_id else '',
        'timestamp': bookmark.timestamp_label, 'anchor': bookmark.anchor,
    }})


@login_required
@require_POST
def bookmark_delete(request, bookmark_id):
    """Delete one of the learner's own bookmarks (POST; JSON reply)."""
    bookmark = get_object_or_404(models.LessonBookmark, pk=bookmark_id, student=request.user)
    bookmark.delete()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def lesson_block_track(request, block_id):
    """Heartbeat from a trackable embed/video block — accrues study time."""
    block = get_object_or_404(models.LessonBlock.objects.select_related('lesson'), pk=block_id)
    try:
        seconds = max(0, min(600, int(request.POST.get('seconds') or 0)))
    except (ValueError, TypeError):
        seconds = 0
    verb = request.POST.get('verb') or 'progressed'
    if verb not in ('progressed', 'experienced', 'completed'):
        verb = 'progressed'
    return JsonResponse({'ok': True})


@login_required
@require_POST
def lesson_complete(request, lesson_id):
    """Mark a lesson finished — closes the study session."""
    lesson = get_object_or_404(models.Lesson, pk=lesson_id)
    session = models.StudySession.objects.filter(student=request.user, lesson=lesson).first()
    if session:
        session.finish(100)
    return JsonResponse({'ok': True, 'minutes': session.minutes if session else 0})


@login_required
@require_POST
def session_action(request, lesson_id):
    """Start / pause / finish a study session for a lesson (timer API)."""
    lesson = get_object_or_404(models.Lesson, pk=lesson_id)
    action = request.POST.get('action', 'start')
    session, _ = models.StudySession.objects.get_or_create(
        student=request.user, lesson=lesson,
        defaults={'status': models.StudySession.STATUS_NOT_STARTED},
    )
    if action == 'start':
        session.start()
    elif action == 'pause':
        session.pause()
    elif action == 'finish':
        session.finish(int(request.POST.get('completion', 100) or 100))
    return JsonResponse({
        'status': session.status, 'minutes': session.minutes,
        'completion': session.completion_pct,
    })
@login_required
def module_profile(request, pk, tab='feed'):
    """The module profile page, tabbed (``feed`` by default).

    Visibility: admin/staff always; educators and learners only for modules they
    are actually attached to (teaching or enrolled). ``base_template`` comes from
    :func:`core.profiles.shell_base`, so an HTMX tab swap renders the fragment only.
    """
    from core.profiles import MODULE_TABS, module_context
    from apps.learning.models import ProgrammeModule

    module = get_object_or_404(ProgrammeModule, pk=pk)

    if is_scoped(request.user):
        person = getattr(request.user, 'profile', None)
        enrolled = bool(person and (module.students.filter(pk=person.pk).exists()
                                    or module.educators.filter(pk=person.pk).exists()))
        if not enrolled:
            note('LRN-2001', request, kind='module')
            messages.error(request, 'That module is not available to you.')
            return redirect('learning:courses')

    valid = {k for k, _l, _i in MODULE_TABS}
    if tab not in valid:
        tab = 'feed'

    from core.profiles import shell_base
    ctx = module_context(request, module, tab)
    ctx['page_title'] = module.name
    ctx['base_template'] = shell_base(request)
    return render(request, 'learning/module_profile.html', ctx)
