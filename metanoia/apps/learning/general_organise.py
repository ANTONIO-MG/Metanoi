"""Move programme-wide material onto each programme's General module, once.

Used by ``manage.py organise_general_modules`` (dry run unless ``--apply``) and
the programme-pack builder. :func:`plan` only reads; :func:`apply` writes what
the plan says, and nothing else.

For each programme:

1. Ensure a General module (keep, rename an "overall" one, or create).
2. Find programme-wide items: the *same thing* (lesson, stored file, live
   session, assessment, link — or a same-name-and-size file / same-content
   lesson) on two or more modules for the same intake and audience, or anything
   in an onboarding-titled block. Items that merely share a title are reported
   and left alone — never moved or deleted on a guess.
3. Keep one copy (already on General, else most student activity, else first
   module's), move it to General and re-key its ``source_ref`` to
   ``<key>/<cohort>/GEN/…`` so the seeder finds it next run.
4. Re-point anything referencing the other copies to the kept one, then delete
   them.

Progress lives on lessons/assessments, never on the schedule row, so nothing is
lost when copies share a lesson. Distinct-but-identical lessons are taken off
the schedule (not deleted) and reported with study-session counts for manual
merging. ``MeetingRoom`` rows are never touched.

Patterns:

``OVERALL_NAME_RX``
    A whole name that says "this module (or block) is the programme's overall one".
``REF_RX``
    ``<key>/<cohort>/<CODE>/<rest>`` — a content-pack key that names its module.
``_STORED_PREFIX`` / ``_STORED_SUFFIX``
    The random / hash bits storage adds to a file name: ``1a2b3c4d-`` in front,
    ``_AbC1234`` before the extension.
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count

from .general import (GENERAL_CODE, GENERAL_NAME, general_catalogue_module, general_module_for,
                      general_phase_for, general_week_for, make_first)
from .models import Lesson, ModuleMaterial, Programme, ProgrammeModule

OVERALL_NAME_RX = re.compile(
    r'^\s*(the\s+)?(general|overall|onboarding|orientation|induction|start[\s_-]*here|getting[\s_-]*started|'
    r'welcome|programme[\s_-]*(information|info|overview))'
    r'(\s*(module|information|info|and\s+onboarding|&\s+onboarding|resources))?\s*$', re.I)

REF_RX = re.compile(r'^(?P<key>[^/]+)/(?P<cohort>[^/]+)/(?P<code>[^/]+)/(?P<rest>.+)$')

_STORED_PREFIX = re.compile(r'^[0-9a-f]{8}-')
_STORED_SUFFIX = re.compile(r'_[A-Za-z0-9]{7}$')


@dataclass
class Group:
    """One programme-wide item: the copy to keep and the duplicates to retire.

    ``why`` says how it was found; ``new_ref`` is the kept row's re-keyed
    ``source_ref``; ``lesson_retarget`` is set when the kept lesson must be opened
    to the whole programme; ``orphaned_lessons`` lists ``(lesson, sessions)`` pairs
    for distinct lessons that come off the schedule.
    """
    why: str
    keep: ModuleMaterial
    drop: list = field(default_factory=list)
    new_ref: str = ''
    lesson_retarget: bool = False
    orphaned_lessons: list = field(default_factory=list)


@dataclass
class ProgrammePlan:
    """Everything :func:`apply` would change for one programme.

    ``general`` is the existing General module, if any; ``reuse`` an "overall"
    module to turn into General; ``create`` says a new one is needed. ``groups``
    are the :class:`Group` items to move, ``lookalikes`` the ``(title, [material,
    ...])``
    sets left alone, and ``emptied_phases`` the onboarding blocks the move leaves
    empty.
    """
    programme: Programme
    general: ProgrammeModule = None
    reuse: ProgrammeModule = None
    create: bool = False
    groups: list = field(default_factory=list)
    lookalikes: list = field(default_factory=list)
    emptied_phases: list = field(default_factory=list)

    @property
    def moves(self):
        """The groups whose kept copy is not on General yet."""
        return [g for g in self.groups if not _on_general(g.keep)]

    @property
    def deletes(self):
        """Every duplicate material the plan deletes."""
        return [m for g in self.groups for m in g.drop]

    @property
    def changes(self):
        """True when applying the plan would change anything."""
        return bool(self.reuse or self.create or self.groups or self.emptied_phases)


def _on_general(material):
    """True when ``material`` sits on a General module."""
    offering = material.phase.programme_module
    return offering.code == GENERAL_CODE and offering.module.code == GENERAL_CODE


def _identity(material):
    """What a row *is*, independent of where it sits — or ``None`` if it points
    at nothing (then it can only ever be a lookalike)."""
    if material.lesson_id:
        return ('lesson', material.lesson_id)
    if material.meeting_id:
        return ('meeting', material.meeting_id)
    if material.assessment_id:
        return ('assessment', material.assessment_id)
    if material.file:
        return ('file', material.file.name)
    if material.url:
        return ('url', material.url.strip())
    return None


def _file_stem(name):
    """A stored file's original name, lower-cased, with storage's random prefix and
    suffix removed.
    """
    base, ext = os.path.splitext(os.path.basename(name or ''))
    base = _STORED_SUFFIX.sub('', _STORED_PREFIX.sub('', base))
    return (base.lower() + ext.lower())


def _file_size(material):
    """The file's size, or ``None`` when the file is missing or storage is unreachable
    (never a match).
    """
    try:
        return material.file.size
    except Exception:
        return None


def _lesson_fingerprint(lesson):
    """A hash of a lesson's title, body and blocks — equal for same-content lessons."""
    rows = [(b.block_type, json.dumps(b.data, sort_keys=True, default=str))
            for b in lesson.blocks.order_by('order', 'id')]
    raw = json.dumps([lesson.title.strip().lower(), lesson.body.strip(), rows])
    return hashlib.sha1(raw.encode()).hexdigest()


def _lesson_sessions(lesson_ids):
    """``{lesson pk: study-session count}`` for the given lessons."""
    if not lesson_ids:
        return {}
    return dict(Lesson.objects.filter(pk__in=lesson_ids).annotate(n=Count('study_sessions'))
                .values_list('pk', 'n'))


def _find_overall(programme):
    """The programme's first non-General module whose name says it is the overall one,
    else ``None``.
    """
    for offering in programme.modules.select_related('module').order_by('order', 'pk'):
        if offering.is_general:
            continue
        names = [offering.name, offering.module.name, offering.code]
        if any(OVERALL_NAME_RX.match(n or '') for n in names):
            return offering
    return None


def _new_ref(material):
    """``material``'s ``source_ref`` re-keyed onto General (``…/GEN/…``).

    Left unchanged when the key does not name the material's module, or when the
    new key would be longer than the 140-character field.
    """
    match = REF_RX.match(material.source_ref or '')
    if not match or match['code'] != material.phase.programme_module.code:
        return material.source_ref
    ref = f'{match["key"]}/{match["cohort"]}/{GENERAL_CODE}/{match["rest"]}'
    return ref if len(ref) <= 140 else material.source_ref


def plan(programme):
    """What :func:`apply` would do for ``programme``. Reads only.

    1. **Exact:** the same thing (by :func:`_identity`) on two or more modules, per
       intake and audience.
    2. **Lookalikes:** the same file (original name *and* size) or the same lesson
       content under different rows. The same name alone is only a candidate;
       different names under one title are different files. Anything else that only
       shares a title is reported, not moved.
    3. **Onboarding blocks** on a taught module: everything in them is
    programme-wide.

    For each cluster the copy kept is the one already on General, else the one with
    most study sessions, else the first module's. Onboarding blocks the move leaves
    with nothing in them are listed for deactivation.
    """
    result = ProgrammePlan(programme=programme)
    result.general = general_module_for(programme, create=False)
    if result.general is None:
        result.reuse = _find_overall(programme)
        result.create = result.reuse is None

    general_ids = {result.general.pk} if result.general else set()
    if result.reuse is not None:
        general_ids.add(result.reuse.pk)

    materials = list(ModuleMaterial.objects
                     .filter(phase__programme_module__programme=programme, phase__is_active=True)
                     .select_related('phase__programme_module__module', 'phase__cohort', 'week',
                                     'lesson', 'meeting')
                     .order_by('phase__programme_module__order', 'phase__programme_module_id',
                               'phase__order', 'week__order', 'order', 'pk'))

    def offering_of(m):
        """The module offering a material sits on."""
        return m.phase.programme_module

    def placed_on_general(m):
        """True when the material is on General (or the module about to become it)."""
        return offering_of(m).pk in general_ids

    exact = defaultdict(list)
    for m in materials:
        ident = _identity(m)
        if ident is not None:
            exact[(m.phase.cohort_id, m.audience, ident)].append(m)
    clusters = []
    seen = set()
    for key, rows in exact.items():
        modules = {offering_of(m).pk for m in rows}
        if len(modules) >= 2 or (any(placed_on_general(m) for m in rows) and len(rows) >= 2):
            clusters.append(('same item on several modules', rows))
            seen.update(m.pk for m in rows)

    alike = defaultdict(list)
    for m in materials:
        if m.pk in seen:
            continue
        alike[(m.phase.cohort_id, m.audience, m.kind, m.title.strip().lower())].append(m)
    def spread(rows):
        """How many different modules the rows sit on."""
        return len({offering_of(m).pk for m in rows})

    for key, rows in alike.items():
        if spread(rows) < 2:
            continue
        by_stem = defaultdict(list)
        for m in rows:
            if m.file and not (m.lesson_id or m.meeting_id or m.assessment_id):
                by_stem[_file_stem(m.file.name)].append(m)
        for stem, named in by_stem.items():
            if spread(named) < 2:
                continue
            by_size = defaultdict(list)
            for m in named:
                by_size[_file_size(m)].append(m)
            unsure = []
            for size, same in by_size.items():
                if size is not None and spread(same) >= 2:
                    clusters.append(('same file (name and size) on several modules', same))
                    seen.update(m.pk for m in same)
                else:
                    unsure.extend(same)
            if spread(unsure) >= 2:
                result.lookalikes.append((rows[0].title, unsure))
        by_title = defaultdict(list)
        for m in rows:
            if m.lesson_id:
                by_title[m.lesson.title.strip().lower()].append(m)
        for title, titled in by_title.items():
            if spread(titled) < 2:
                continue
            by_content = defaultdict(list)
            for m in titled:
                by_content[_lesson_fingerprint(m.lesson)].append(m)
            unsure = []
            for same in by_content.values():
                if spread(same) >= 2:
                    clusters.append(('same lesson content on several modules', same))
                    seen.update(m.pk for m in same)
                else:
                    unsure.extend(same)
            if spread(unsure) >= 2:
                result.lookalikes.append((rows[0].title, unsure))

    onboarding_phases = set()
    for m in materials:
        phase = m.phase
        if m.pk not in seen and not placed_on_general(m) and OVERALL_NAME_RX.match(phase.title or ''):
            clusters.append(('in an onboarding block', [m]))
            seen.add(m.pk)
            onboarding_phases.add(phase.pk)

    lesson_ids = {m.lesson_id for _, rows in clusters for m in rows if m.lesson_id}
    sessions = _lesson_sessions(lesson_ids)
    for why, rows in clusters:
        rows = sorted(rows, key=lambda m: (not placed_on_general(m), -sessions.get(m.lesson_id, 0),
                                           offering_of(m).order, m.pk))
        keep, drop = rows[0], rows[1:]
        group = Group(why=why, keep=keep, drop=drop)
        group.new_ref = keep.source_ref if placed_on_general(keep) else _new_ref(keep)
        lesson = keep.lesson
        group.lesson_retarget = bool(lesson and (
            lesson.visibility != Lesson.VIS_PROGRAMME
            or not lesson.target_programmes.filter(pk=programme.pk).exists()
            or (lesson.module_id and lesson.module_id not in general_ids)))
        group.orphaned_lessons = [(m.lesson, sessions.get(m.lesson_id, 0)) for m in drop
                                  if m.lesson_id and m.lesson_id != keep.lesson_id]
        result.groups.append(group)
    result.groups.sort(key=lambda g: (g.keep.phase.cohort_id or 0, g.keep.week.number if g.keep.week_id else 0,
                                      g.keep.audience != ModuleMaterial.AUDIENCE_STUDENTS, g.keep.title))

    moving = {g.keep.pk for g in result.groups} | {m.pk for g in result.groups for m in g.drop}
    for phase_id in onboarding_phases:
        left = [m for m in materials if m.phase_id == phase_id and m.pk not in moving]
        if not left:
            result.emptied_phases.append(next(m.phase for m in materials if m.phase_id == phase_id))
    return result


def retire(duplicate, keep):
    """Re-point whatever refers to ``duplicate`` at ``keep``, then delete it."""
    for source in duplicate.paper_sources.all():
        source.source_materials.add(keep)
    for source in duplicate.memo_sources.all():
        source.solution_materials.add(keep)
    duplicate.delete()


def _reuse_as_general(offering):
    """Turn an "overall" module into the programme's free General module, shown first.
    """
    offering.module = general_catalogue_module()
    offering.code = GENERAL_CODE
    offering.name = GENERAL_NAME
    offering.price_per_month = 0
    offering.is_active = True
    offering.save()
    make_first(offering)
    return offering


@transaction.atomic
def apply(result):
    """Carry out ``result`` (a :class:`ProgrammePlan`). Returns the General module."""
    programme = result.programme
    if result.reuse is not None:
        general = _reuse_as_general(result.reuse)
    else:
        general = general_module_for(programme)
        make_first(general)
    for group in result.groups:
        keep = group.keep
        if keep.phase.programme_module_id != general.pk:
            source_week = keep.week
            phase = general_phase_for(general, cohort=keep.phase.cohort)
            if keep.phase.is_published and not phase.is_published:
                phase.is_published = True
                phase.save(update_fields=['is_published', 'updated_at'])
            keep.phase = phase
            keep.week = general_week_for(phase, source_week) if source_week else None
            keep.topic = None
        keep.source_ref = group.new_ref
        keep._quiet = True
        keep.save()
        if group.lesson_retarget and keep.lesson_id:
            lesson = keep.lesson
            lesson.module = general
            lesson.visibility = Lesson.VIS_PROGRAMME
            lesson._quiet = True
            lesson.save(update_fields=['module', 'visibility', 'updated_at'])
            lesson.target_programmes.add(programme)
        for duplicate in group.drop:
            retire(duplicate, keep)
    for phase in result.emptied_phases:
        phase.is_active = False
        phase.save(update_fields=['is_active', 'updated_at'])
    return general


def _where(material):
    """Where a material sits, for the report: module code, intake and week."""
    offering = material.phase.programme_module
    parts = [offering.code]
    if material.phase.cohort_id:
        parts.append(material.phase.cohort.code)
    parts.append(f'week {material.week.number}' if material.week_id else 'block')
    return ' · '.join(parts)


def describe(result):
    """The plan as lines of text — exactly what :func:`apply` changes."""
    p = result.programme
    lines = [f'{p.full_code} — {p.display_name}']
    if result.general is not None:
        lines.append(f'  General: exists ({result.general.code}, #{result.general.pk})')
    elif result.reuse is not None:
        enrolled = result.reuse.enrolments.count()
        lines.append(f'  General: REUSE "{result.reuse.display_name}" ({result.reuse.code}, #{result.reuse.pk}) — '
                     f'renamed General, code {GENERAL_CODE}, free, shown first'
                     + (f'; its {enrolled} enrolment row(s) are kept' if enrolled else ''))
    else:
        lines.append(f'  General: CREATE "{GENERAL_NAME}" ({GENERAL_CODE}) — free, shown first')
    if not result.groups:
        lines.append('  Programme-wide items: none found on the taught modules')
    for g in result.groups:
        keep = g.keep
        target = 'stays on General' if _on_general(keep) else (
            'MOVE to General' + (f' · week {keep.week.number}' if keep.week_id else ' · block'))
        lines.append(f'  [{keep.kind}] {keep.title} ({keep.get_audience_display().lower()}) — {g.why}')
        lines.append(f'      keep #{keep.pk} ({_where(keep)}): {target}')
        if g.new_ref != keep.source_ref:
            lines.append(f'      key {keep.source_ref} -> {g.new_ref}')
        if g.lesson_retarget:
            lines.append(f'      lesson #{keep.lesson_id} opened to the whole programme')
        if g.drop:
            lines.append('      DELETE duplicate(s): ' + ', '.join(f'#{m.pk} ({_where(m)})' for m in g.drop))
        for lesson, n in g.orphaned_lessons:
            lines.append(f'      NOTE lesson #{lesson.pk} "{lesson.title}" comes off the schedule; '
                         f'its {n} study session(s) stay on it (not merged)')
    for phase in result.emptied_phases:
        lines.append(f'  DEACTIVATE empty onboarding block #{phase.pk} "{phase.display_title}" '
                     f'on {phase.programme_module.code}')
    for title, rows in result.lookalikes:
        lines.append(f'  LEFT ALONE (same title, different content): "{title}" on '
                     + ', '.join(f'#{m.pk} ({_where(m)})' for m in rows))
    moved = len(result.moves)
    lines.append(f'  Summary: {moved} item(s) moved to General, {len(result.deletes)} duplicate(s) deleted, '
                 f'{len(result.lookalikes)} lookalike group(s) left alone')
    return lines
