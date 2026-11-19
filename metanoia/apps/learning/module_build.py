"""Building a module's schedule — the educator's side of the module feed.

Everything here is guarded by :attr:`apps.learning.access.Gate.can_author` —
staff, admins, and the educators attached to the offering. The student feed
reads the same rows; drafts are just ``is_published=False``.

``DEFAULT_WEEKS`` is how many weeks a freshly scaffolded block gets: a test
block is a month's work (4); an exam block is the run-up to the paper and is
longer (6). ``SHOP_CATEGORY`` is the shop category every module material is
published into.
"""

import logging

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from . import models
from .access import Gate

logger = logging.getLogger('apps')

DEFAULT_WEEKS = {models.ModulePhase.KIND_TEST: 4, models.ModulePhase.KIND_EXAM: 6}

SHOP_CATEGORY = 'Study material'


def _gate_or_403(request, offering):
    """The request's :class:`Gate` on ``offering`` when the user may author it, else
    ``None``.
    """
    gate = Gate(request.user, offering)
    return gate if gate.can_author else None


def _offering(pk):
    """The active offering ``pk`` (with programme, institution and module), or 404."""
    return get_object_or_404(
        models.ProgrammeModule.objects.select_related('programme__institution', 'module'),
        pk=pk, is_active=True)


@login_required
def module_build(request, pk):
    """The whole schedule of one offering, editable in place.

    ``?cohort=`` picks which intake's schedule is being built (per-cohort content).
    No cohort selected means the shared template blocks every intake sees, and the
    author sees all cohorts'.
    """
    offering = _offering(pk)
    gate = _gate_or_403(request, offering)
    if gate is None:
        return HttpResponseForbidden()

    from .module_feed import _load_phases, _schedule

    cid = request.GET.get('cohort')
    build_cohort = (offering.programme.cohorts.filter(pk=cid).first()
                    if cid and cid.isdigit() else None)
    phases = _load_phases(offering, gate, cohort=build_cohort)
    return render(request, 'learning/manage/module_build.html', {
        'page_title': f'Build: {offering.display_name}',
        'offering': offering,
        'gate': gate,
        'schedule': _schedule(phases, gate),
        'build_cohort': build_cohort,
        'cohorts': list(offering.programme.cohorts.filter(is_active=True)),
        'phase_kinds': models.ModulePhase.KIND_CHOICES,
        'material_kinds': models.ModuleMaterial.KIND_CHOICES,
        'topics': offering.topics.filter(is_active=True).order_by('order', 'code'),
        'lessons': models.Lesson.objects.order_by('title')[:200],
        'assessments': _assessments(),
        'meetings': _meetings(),
    })


def _assessments():
    """The 200 newest assessments, for the builder's pickers (empty if the app is
    unavailable).
    """
    try:
        from apps.assessments.models import Assessment
        return Assessment.objects.order_by('-created_at')[:200]
    except Exception:  # pragma: no cover
        return []


def _meetings():
    """The 200 latest live sessions, for the builder's pickers (empty if the app is
    unavailable).
    """
    try:
        from apps.communication.models import MeetingRoom
        return MeetingRoom.objects.order_by('-scheduled_start')[:200]
    except Exception:  # pragma: no cover
        return []


@login_required
@require_POST
def module_scaffold(request, pk):
    """Create the standard year — Test 1–4 then Exam 1–2 — with empty weeks.

    Idempotent: existing blocks are left as they are.
    """
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()

    cid = request.POST.get('cohort') or request.GET.get('cohort')
    cohort = (offering.programme.cohorts.filter(pk=cid).first()
              if cid and cid.isdigit() else None)
    created = scaffold_schedule(offering, cohort=cohort)
    if created:
        flash.success(request, f'Added {created} preparation block{"" if created == 1 else "s"}.')
    else:
        flash.info(request, 'The schedule is already scaffolded.')
    return redirect('learning:module-build', pk=offering.pk)


def scaffold_schedule(offering, weeks_per_phase=None, cohort=None):
    """Build the default six-block plan for ``offering``; returns blocks created.

    ``cohort=None`` builds the shared template. Also used by the
    ``scaffold_module_schedule`` command.
    """
    weeks_per_phase = weeks_per_phase or DEFAULT_WEEKS
    created = 0
    for index, (kind, sequence) in enumerate(models.ModulePhase.DEFAULT_PLAN):
        phase, made = models.ModulePhase.objects.get_or_create(
            programme_module=offering, kind=kind, sequence=sequence, cohort=cohort,
            defaults={'order': index, 'title': models.ModulePhase.build_title(kind, sequence)},
        )
        if not made:
            continue
        created += 1
        for number in range(1, weeks_per_phase.get(kind, 4) + 1):
            models.ModuleWeek.objects.get_or_create(
                phase=phase, number=number, defaults={'order': number - 1})
    return created


@login_required
@require_POST
def phase_save(request, pk, phase_id=None):
    """Create or update one preparation block.

    The posted ``cohort`` says which intake the block belongs to (blank = shared by
    every cohort). A new block can be created with ``with_weeks`` empty weeks.
    """
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()

    if phase_id:
        phase = get_object_or_404(models.ModulePhase, pk=phase_id, programme_module=offering)
    else:
        phase = models.ModulePhase(programme_module=offering,
                                   order=offering.phases.count())
    _cid = request.POST.get('cohort') or ''
    phase.cohort_id = int(_cid) if _cid.isdigit() else None
    phase.kind = request.POST.get('kind') or phase.kind
    phase.sequence = int(request.POST.get('sequence') or phase.sequence or 1)
    phase.title = request.POST.get('title', '').strip()
    phase.summary = request.POST.get('summary', '').strip()
    phase.starts_on = parse_date(request.POST.get('starts_on', '') or '') or None
    phase.ends_on = parse_date(request.POST.get('ends_on', '') or '') or None
    phase.is_published = request.POST.get('is_published') == 'on'
    event_id = request.POST.get('calendar_event') or None
    phase.calendar_event_id = int(event_id) if event_id else None
    phase.save()

    if not phase_id and request.POST.get('with_weeks'):
        count = int(request.POST.get('with_weeks') or 0)
        for number in range(1, count + 1):
            models.ModuleWeek.objects.get_or_create(
                phase=phase, number=number, defaults={'order': number - 1})

    flash.success(request, f'{phase.display_title} saved.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def phase_delete(request, pk, phase_id):
    """Delete one preparation block (with its weeks and materials). Authors only."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    phase = get_object_or_404(models.ModulePhase, pk=phase_id, programme_module=offering)
    title = phase.display_title
    phase.delete()
    flash.success(request, f'{title} removed.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def week_save(request, pk, week_id=None):
    """Create or update one week of a block, and its series of topics.

    "Make available" opens the week to every student on the module, paid or not
    (:mod:`apps.learning.week_access`). It is only touched when the form carries it,
    so a form without the tick never silently withdraws access. The topics are a
    multi-select of topic ids, kept in submitted order. Authors only.
    """
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()

    if week_id:
        week = get_object_or_404(models.ModuleWeek, pk=week_id,
                                 phase__programme_module=offering)
    else:
        phase = get_object_or_404(models.ModulePhase, pk=request.POST.get('phase'),
                                  programme_module=offering)
        next_number = (phase.weeks.order_by('-number').values_list('number', flat=True).first() or 0) + 1
        week = models.ModuleWeek(phase=phase, number=next_number, order=next_number - 1)

    if request.POST.get('number'):
        week.number = int(request.POST['number'])
    week.title = request.POST.get('title', '').strip()
    week.summary = request.POST.get('summary', '').strip()
    week.starts_on = parse_date(request.POST.get('starts_on', '') or '') or None
    week.ends_on = parse_date(request.POST.get('ends_on', '') or '') or None
    week.is_published = request.POST.get('is_published') == 'on'
    if not week_id or 'make_available' in request.POST:
        week.make_available = request.POST.get('make_available') in ('on', '1', 'true')
    week.save()
    _set_week_topics(week, offering, request.POST.getlist('topics'))
    flash.success(request, f'{week.display_title} saved.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def week_availability(request, pk, week_id):
    """Tick / untick "make available" on one week (opens it to every student,
    paid or not). Admin, staff and the module's own educators only."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    week = get_object_or_404(models.ModuleWeek, pk=week_id, phase__programme_module=offering)
    week.make_available = request.POST.get('make_available') in ('1', 'on', 'true')
    week.save(update_fields=['make_available', 'updated_at'])
    logger.info('week %s make_available=%s by user %s', week.pk, week.make_available, request.user.pk)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'id': week.pk, 'make_available': week.make_available})
    flash.success(request, f'{week.display_title}: '
                           + ('made available to every student.' if week.make_available
                              else 'back under the payment rule.'))
    nxt = request.POST.get('next') or ''
    from django.utils.http import url_has_allowed_host_and_scheme
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return redirect(nxt)
    return redirect('learning:module-build', pk=offering.pk)


def _set_week_topics(week, offering, raw_ids):
    """Sync the week's WeekTopic rows to `raw_ids`, in that order.

    Only topics belonging to this offering are accepted.
    """
    valid = set(offering.topics.values_list('id', flat=True))
    ids = [int(t) for t in raw_ids if t.isdigit() and int(t) in valid]
    week.week_topics.exclude(topic_id__in=ids).delete()
    existing = {wt.topic_id: wt for wt in week.week_topics.all()}
    for order, tid in enumerate(ids):
        wt = existing.get(tid)
        if wt is None:
            models.WeekTopic.objects.create(week=week, topic_id=tid, order=order)
        elif wt.order != order:
            wt.order = order
            wt.save(update_fields=['order'])


@login_required
@require_POST
def week_delete(request, pk, week_id):
    """Delete one week (with its materials). Authors only."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    week = get_object_or_404(models.ModuleWeek, pk=week_id, phase__programme_module=offering)
    title = week.display_title
    week.delete()
    flash.success(request, f'{title} removed.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def material_save(request, pk, material_id=None):
    """Add or edit one studiable item; a blank ``week`` attaches it to the block.

    An uploaded file is validated here, because assigning the upload skips the
    field's validators. "Also sell this on its own" (``sell``) publishes it to the
    shop in one tick; unticking it deactivates the existing product.
    """
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()

    if material_id:
        material = get_object_or_404(models.ModuleMaterial, pk=material_id,
                                     phase__programme_module=offering)
    else:
        phase = get_object_or_404(models.ModulePhase, pk=request.POST.get('phase'),
                                  programme_module=offering)
        material = models.ModuleMaterial(phase=phase, created_by=request.user,
                                         order=phase.materials.count())

    week_id = request.POST.get('week') or None
    if week_id:
        material.week = get_object_or_404(models.ModuleWeek, pk=week_id, phase=material.phase)
    else:
        material.week = None

    material.kind = request.POST.get('kind') or material.kind
    material.title = request.POST.get('title', '').strip() or material.get_kind_display()
    material.description = request.POST.get('description', '').strip()
    material.url = request.POST.get('url', '').strip()
    material.lesson_id = int(request.POST['lesson']) if request.POST.get('lesson') else None
    material.assessment_id = int(request.POST['assessment']) if request.POST.get('assessment') else None
    material.meeting_id = int(request.POST['meeting']) if request.POST.get('meeting') else None
    material.is_published = request.POST.get('is_published') == 'on'
    material.is_preview = request.POST.get('is_preview') == 'on'
    released = request.POST.get('available_from', '')
    material.available_from = parse_datetime(released) if released else None
    if request.FILES.get('file'):
        from core import validators as v
        errors = v.upload_errors(request.FILES['file'], v.validate_attachment)
        if errors:
            flash.error(request, f'{request.FILES["file"].name}: {" ".join(errors)}')
            return redirect('learning:module-build', pk=offering.pk)
        material.file = request.FILES['file']
    material.save()

    if request.POST.get('sell') == 'on':
        publish_to_shop(material, price=request.POST.get('price'), user=request.user)
    elif material.product_id and request.POST.get('sell') != 'on':
        material.product.status = 'inactive'
        material.product.save(update_fields=['status', 'updated_at'])

    flash.success(request, f'{material.title} saved.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def material_delete(request, pk, material_id):
    """Delete one material. Authors only."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    material = get_object_or_404(models.ModuleMaterial, pk=material_id,
                                 phase__programme_module=offering)
    title = material.title
    material.delete()
    flash.success(request, f'{title} removed.')
    return redirect('learning:module-build', pk=offering.pk)


@login_required
@require_POST
def material_reorder(request, pk):
    """Drag-and-drop ordering inside a week (or a block)."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    ids = request.POST.getlist('order[]') or request.POST.getlist('order')
    for index, material_id in enumerate(ids):
        (models.ModuleMaterial.objects
         .filter(pk=material_id, phase__programme_module=offering)
         .update(order=index))
    return JsonResponse({'ok': True})


def publish_to_shop(material, price=None, user=None):
    """List a material in the shop so it can be bought without the module.

    Creates the product on first publish and keeps it in step afterwards. The
    link (:attr:`ModuleMaterial.product`) is what
    :class:`apps.learning.access.Gate` checks for non-payers.
    """
    try:
        from apps.shop.models import Product, ProductCategory
    except Exception:  # pragma: no cover — the shop is optional
        logger.exception('module build: shop unavailable, cannot publish %s', material)
        return None

    offering = material.phase.programme_module
    category, _ = ProductCategory.objects.get_or_create(name=SHOP_CATEGORY)
    try:
        amount = float(price) if price not in (None, '') else float(material.product.price)
    except (TypeError, ValueError, AttributeError):
        amount = 0

    product = material.product
    if product is None:
        product = Product(kind=Product.KIND_SERVICE, created_by=user)
    product.category = category
    product.name = f'{offering.label} · {material.title}'
    product.summary = f'{material.get_kind_display()} — {offering.display_name}'
    product.description = material.description or product.description
    product.price = amount
    product.status = 'active'
    product.save()

    if material.product_id != product.pk:
        material.product = product
        material.save(update_fields=['product', 'updated_at'])
    return product


@login_required
@require_POST
def material_publish(request, pk, material_id):
    """Publish (or re-price) one material in the shop."""
    offering = _offering(pk)
    if _gate_or_403(request, offering) is None:
        return HttpResponseForbidden()
    material = get_object_or_404(models.ModuleMaterial, pk=material_id,
                                 phase__programme_module=offering)
    product = publish_to_shop(material, price=request.POST.get('price'), user=request.user)
    if product is None:
        flash.error(request, 'The shop is not available, so nothing was listed.')
    else:
        flash.success(request, f'"{material.title}" is now on sale at R{product.price:.2f}. '
                               f'Students who pay for {offering.code} still get it free.')
    return redirect('learning:module-build', pk=offering.pk)


def build_url(offering):
    """Convenience for templates/emails that link straight into the builder."""
    return reverse('learning:module-build', args=[offering.pk])
