"""The **General** module: one per programme, for everything that is not one module's.

Onboarding, blueprints and programme-wide briefings, recordings and sessions
live once here rather than copied onto every module.

Identity: a :class:`~apps.learning.models.ProgrammeModule` whose offering code
**and** canonical module code are both ``GEN`` (``is_general``;
``.general()`` / ``.without_general()``).

* **Free and open.** ``price_per_month`` is forced to 0 on save.
* **Access rule, not enrolment.** Nobody gets a ``ModuleEnrolment`` for
  General: enrolment rows are billing records (trials, invoices, grants,
  expired-trial lock, stalled-student lists) and a free row would distort them.
  Anyone on the programme sees it (:func:`general_offerings_for`).
* **Not counted** in fees, bundles, invoices, the registration picker,
  per-module calendar dates or seeded totals. It *is* counted in "what is on
  this programme" (the admin's module count).
* **Shown first** (``order`` 0).

To put a live session on General, set ``meeting.module`` to it with a cohort or
programme audience (a module audience would be invisible — nobody is enrolled
on General), or place a ``ModuleMaterial(kind='live')`` on
:func:`general_phase_for`.
"""

import re

from django.db import transaction
from django.db.models import F, Q

from .models import (GENERAL_CODE, GENERAL_NAME, Module, ModulePhase, ModuleWeek, Programme,
                     ProgrammeModule)

GENERAL_DESCRIPTION = ('Everything that applies to the whole programme: start here, the candidate guide, '
                       'the timetable, blueprints, programme-wide briefings, recordings and sessions. '
                       'Free and open to everyone on the programme.')
PHASE_TITLE = 'General information'
PHASE_SUMMARY = 'Onboarding and everything that applies to every module of the programme.'

__all__ = ['GENERAL_CODE', 'GENERAL_NAME', 'general_catalogue_module', 'general_module_for',
           'general_phase_for', 'general_week_for', 'general_offerings_for', 'is_general',
           'is_general_content_path']


def is_general(offering):
    """True when ``offering`` is a programme's General module (``None`` is not)."""
    return bool(offering is not None and getattr(offering, 'is_general', False))


def general_catalogue_module(create=True):
    """The canonical ``GEN`` :class:`Module` every General offering points at.

    ``Module.name`` is unique, so when another module already holds the General
    name the catalogue module is created as "General information" instead.
    """
    module = Module.objects.filter(code=GENERAL_CODE).first()
    if module is not None or not create:
        return module
    name = GENERAL_NAME
    if Module.objects.filter(name=name).exists():
        name = 'General information'
    return Module.objects.create(code=GENERAL_CODE, name=name, order=0,
                                 description=GENERAL_DESCRIPTION)


def general_module_for(programme, create=True):
    """The programme's General :class:`ProgrammeModule`, created when missing.

    ``programme`` is a :class:`Programme` or its pk. With ``create=False`` this
    only looks (returns ``None`` when there is none). Idempotent and safe to
    call concurrently: the programme row is locked while creating. An offering
    already coded ``GEN`` is taken to be the programme's General one: it is
    pointed at the canonical General module rather than sitting beside a second.
    """
    if not isinstance(programme, Programme):
        programme = Programme.objects.get(pk=programme)
    found = ProgrammeModule.objects.general().filter(programme=programme).select_related('module').first()
    if found is not None or not create:
        return found
    with transaction.atomic():
        Programme.objects.select_for_update().filter(pk=programme.pk).first()
        found = ProgrammeModule.objects.general().filter(programme=programme).first()
        if found is not None:
            return found
        canonical = general_catalogue_module()
        offering = ProgrammeModule.objects.filter(programme=programme, code=GENERAL_CODE).first()
        if offering is None:
            offering = ProgrammeModule(programme=programme, code=GENERAL_CODE)
        offering.module = canonical
        offering.name = offering.name or GENERAL_NAME
        offering.description = offering.description or GENERAL_DESCRIPTION
        offering.price_per_month = 0
        offering.order = 0
        offering.is_active = True
        offering.save()
        make_first(offering)
        return offering


def make_first(offering):
    """Put General ahead of the programme's other modules (``order`` 0, others ≥ 1)."""
    others = ProgrammeModule.objects.filter(programme_id=offering.programme_id).exclude(pk=offering.pk)
    if offering.order != 0:
        offering.order = 0
        offering.save(update_fields=['order', 'updated_at'])
    if others.filter(order=0).exists():
        others.update(order=F('order') + 1)


def general_phase_for(programme_or_offering, cohort=None, create=True):
    """The block on General that holds the material for ``cohort`` (``None`` =
    shared by every intake) — where onboarding and programme-wide items go."""
    offering = programme_or_offering
    if not isinstance(offering, ProgrammeModule):
        offering = general_module_for(programme_or_offering, create=create)
    elif not offering.is_general:
        offering = general_module_for(offering.programme, create=create)
    if offering is None:
        return None
    lookup = dict(programme_module=offering, cohort=cohort, kind=ModulePhase.KIND_OTHER, sequence=1)
    phase = ModulePhase.objects.filter(**lookup).first()
    if phase is None and create:
        phase = ModulePhase.objects.create(**lookup, title=PHASE_TITLE, summary=PHASE_SUMMARY, order=0)
    return phase


def general_week_for(phase, like):
    """The week on a General ``phase`` matching ``like`` (a week of another
    module: same number, title and dates) — so a dated programme-wide item, a
    mock's briefing say, keeps its place in the calendar."""
    week, created = ModuleWeek.objects.get_or_create(
        phase=phase, number=like.number,
        defaults={'title': like.title.split(' · Unit')[0][:200], 'starts_on': like.starts_on,
                  'ends_on': like.ends_on, 'order': like.order, 'is_published': like.is_published,
                  'summary': like.summary if not like.week_topics.exists() else ''})
    return week


def general_offerings_for(person):
    """The General modules ``person`` sees: one for each programme they are on
    (an active programme registration, or any module enrolment under it).

    A queryset, so it can be OR-ed into other filters (``module__in=...``).
    """
    if person is None:
        return ProgrammeModule.objects.none()
    programmes = Q(programme__enrolments__person=person, programme__enrolments__is_active=True) | Q(
        programme__modules__enrolments__person=person)
    return (ProgrammeModule.objects.general().filter(is_active=True, programme__is_active=True)
            .filter(programmes).distinct()
            .select_related('programme__institution', 'module'))


_MATERIAL_PATH = re.compile(r'^/learning/materials/(\d+)/open/?$')
_LESSON_PATH = re.compile(r'^/learning/lessons/(\d+)/view/?$')


def is_general_content_path(path):
    """True when ``path`` opens a General item (a material on a General module,
    or a lesson placed there) — which no payment state may close.

    A lesson counts only when it is published on General *and nowhere else*, so a
    lesson shared with a paid module is not opened by the back door.
    """
    from .models import ModuleMaterial

    match = _MATERIAL_PATH.match(path or '')
    if match:
        return ModuleMaterial.objects.filter(
            pk=match.group(1), phase__programme_module__code=GENERAL_CODE,
            phase__programme_module__module__code=GENERAL_CODE).exists()
    match = _LESSON_PATH.match(path or '')
    if match:
        on_general = Q(phase__programme_module__code=GENERAL_CODE,
                       phase__programme_module__module__code=GENERAL_CODE)
        if not ModuleMaterial.objects.filter(on_general, lesson_id=match.group(1), is_published=True).exists():
            return False
        return not ModuleMaterial.objects.filter(lesson_id=match.group(1)).exclude(on_general).exists()
    return False
