"""Turn a validated content pack into database rows.

Rules that make it safe to run twice (and to hand to a language model later):

* **Nothing is published.** Guides import as draft lessons, papers as draft
  assessments; publishing stays a deliberate human act.
* **Re-importing updates, it does not duplicate.** Every row carries a
  ``source_ref`` derived from the pack, looked up first.
* **Nothing lands unless everything validates.** One transaction per import.

Each item is also placed as a ``ModuleMaterial`` in the week that teaches the
topic — without that a candidate never meets it, and the teaching-sequence gate
in :mod:`apps.learning.sequence` reasons over a week's materials.

Mappings:

:data:`KIND_BY_TYPE`
    Pack item type → the Assessment kind it becomes.
:data:`MATERIAL_KIND_BY_TYPE`
    Pack item type → the ModuleMaterial kind it is scheduled as. This is what
    decides where it sits in the week's teaching order
    (:mod:`apps.learning.sequence`).
:data:`ITEM_ORDER`
    Where each item type sits in the week: guide, blueprint, quiz, solution,
    mock.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.utils.text import slugify

from .schema import DEFAULT_TOLERANCE, READING_TYPES, packs_of, validate_or_raise

logger = logging.getLogger(__name__)

KIND_BY_TYPE = {
    'quiz': 'quiz',
    'mock': 'test',
    'challenge': 'assignment',
    'exam': 'mock_exam',
}

MATERIAL_KIND_BY_TYPE = {
    'study_guide': 'study_guide',
    'blueprint': 'blueprint',
    'quiz': 'questions',
    'mock': 'questions',
    'challenge': 'assessment',
    'exam': 'mock_exam',
}


class ImportResult:
    """What an import did, in terms a human can check against the source.

    Besides the counts it keeps the IDs of the rows this import touched
    (``lesson_ids``, ``assessment_ids``, ``material_ids``, ``week_ids``,
    ``phase_ids``), so a review queue can publish exactly these and nothing else.
    """

    def __init__(self):
        self.topic = None
        self.week = None
        self.guides_created = self.guides_updated = 0
        self.papers_created = self.papers_updated = 0
        self.questions = 0
        self.scheduled = 0
        self.marks = Decimal('0')
        self.warnings = []
        self.lesson_ids, self.assessment_ids, self.material_ids = [], [], []
        self.week_ids, self.phase_ids = [], []

    def as_dict(self):
        """The touched row IDs, summary and warnings, for storing on a review item."""
        return {'lessons': self.lesson_ids, 'assessments': self.assessment_ids,
                'materials': self.material_ids, 'weeks': self.week_ids,
                'phases': self.phase_ids, 'summary': str(self), 'warnings': self.warnings}

    def merge(self, other):
        """Add ``other``'s counts, warnings and row IDs into this result."""
        for name in ('guides_created', 'guides_updated', 'papers_created', 'papers_updated',
                     'questions', 'scheduled'):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.marks += other.marks
        self.warnings += other.warnings
        for name in ('lesson_ids', 'assessment_ids', 'material_ids', 'week_ids', 'phase_ids'):
            getattr(self, name).extend(i for i in getattr(other, name) if i not in getattr(self, name))
        self.topic = self.topic or other.topic
        self.week = self.week or other.week

    @property
    def created(self):
        """Guides plus papers created."""
        return self.guides_created + self.papers_created

    @property
    def updated(self):
        """Guides plus papers updated."""
        return self.guides_updated + self.papers_updated

    def __str__(self):
        scheduled = f' · scheduled into {self.week}' if self.week else ''
        return (f'{self.guides_created + self.guides_updated} study guide(s), '
                f'{self.papers_created + self.papers_updated} paper(s), '
                f'{self.questions} question part(s), {self.marks} mark(s) — '
                f'{self.created} created, {self.updated} updated{scheduled}')


class PackImportError(Exception):
    """The pack is valid JSON and valid against the schema, but does not fit the
    database — an unknown module code, a programme that is not installed."""


def _resolve_topic(pack):
    """Find (or create) the syllabus topic this pack belongs to.

    The pack names its module by the institution's own code (``TAX``) and its
    programme by full code (``MILPARK-PGDA``); both must already exist — the
    academic spine is installed by ``.academic_seed.py`` and a content
    pack is never allowed to invent one. The *topic* may be created, because a
    new topic is content, not structure.
    """
    from apps.learning.models import ProgrammeModule, Topic

    programme_code = pack['programme']
    module_code = pack['module']
    institution_code, _, prog = programme_code.partition('-')

    offering = (ProgrammeModule.objects
                .filter(programme__institution__code__iexact=institution_code,
                        programme__code__iexact=prog or programme_code,
                        code__iexact=module_code)
                .select_related('programme__institution', 'module')
                .first())
    if offering is None:
        raise PackImportError(
            f'No module "{module_code}" on programme "{programme_code}". '
            f'The academic spine must already have it — content packs do not '
            f'create institutions, programmes or modules.')

    spec = pack['topic']
    topic, created = Topic.objects.get_or_create(
        programme_module=offering, code=spec['code'],
        defaults={'title': spec['title'],
                  'slug': slugify(f"{offering.code}-{spec['code']}-{spec['title']}")[:270],
                  'description': spec.get('description', '')},
    )
    if not created and spec.get('title') and topic.title != spec['title']:
        topic.title = spec['title']
        topic.save(update_fields=['title', 'updated_at'])
    return topic, offering


def _source_ref(pack, item, index):
    """A stable identity for a pack item, so a re-import finds its own row."""
    explicit = item.get('source_ref')
    if explicit:
        return str(explicit)[:120]
    bits = [pack['module'], pack['topic']['code'], item['type']]
    if item.get('week'):
        bits.append(f"w{item['week']}")
    if not item.get('week') and index:
        bits.append(str(index))
    return '/'.join(bits)[:120]


def _import_study_guide(pack, item, topic, actor, ref, result):
    """The guide becomes a draft Lesson on the topic, one section per heading.

    Sections are rebuilt rather than merged: the pack is the source of truth, and
    a half-merged guide carrying orphaned old sections is worse than a rebuild. A
    section is a container; a ``video_url`` (the live-session recording that
    teaches it) becomes a video block above its notes, and its prose a text block.
    """
    from apps.learning.models import Lesson, LessonBlock, LessonSection

    lesson = Lesson.objects.filter(topic=topic, title=item['title']).first()
    if lesson is None:
        lesson = Lesson(topic=topic, title=item['title'])
        result.guides_created += 1
    else:
        result.guides_updated += 1

    lesson.status = Lesson.STATUS_DRAFT
    lesson.module = topic.programme_module
    lesson.subtitle = (item.get('subtitle') or '')[:200]
    lesson.estimated_minutes = _reading_minutes(item)
    if not lesson.slug:
        lesson.slug = slugify(f"{topic.code}-{item['title']}")[:220]
    if item.get('author_name'):
        lesson.author_name = item['author_name'][:120]
    lesson.save()

    lesson.sections.all().delete()
    lesson.blocks.all().delete()
    for order, section in enumerate(item.get('sections') or []):
        row = LessonSection.objects.create(
            lesson=lesson, title=section['heading'][:200], order=order,
            summary=(section.get('summary') or '')[:300],
            section_type=(LessonSection.TYPE_VIDEO if section.get('video_url')
                          else LessonSection.TYPE_READING),
            duration_minutes=_words_minutes(section.get('body_html', '')),
            open_by_default=(order == 0))
        block_order = 0
        if section.get('video_url'):
            LessonBlock.objects.create(
                lesson=lesson, section=row, order=block_order,
                block_type=LessonBlock.TYPE_VIDEO,
                data={'url': section['video_url'], 'provider': _video_provider(section['video_url']),
                      'caption': section.get('video_caption', '')})
            block_order += 1
        LessonBlock.objects.create(
            lesson=lesson, section=row, order=block_order,
            block_type=LessonBlock.TYPE_TEXT,
            data={'html': section['body_html']})
    result.lesson_ids.append(lesson.pk)
    return lesson


def _words_minutes(html):
    """Reading time in minutes for ``html`` at 200 words a minute (at least 1)."""
    import re
    words = len(re.sub(r'<[^>]+>', ' ', html or '').split())
    return max(1, round(words / 200))


def _reading_minutes(item):
    """A guide's reading time: its own ``estimated_minutes``, else the sections' sum (default 5)."""
    if item.get('estimated_minutes'):
        return int(item['estimated_minutes'])
    return sum(_words_minutes(s.get('body_html', '')) for s in item.get('sections') or []) or 5


def _video_provider(url):
    """``'youtube'``, ``'vimeo'`` or ``'link'`` for a video URL."""
    url = (url or '').lower()
    if 'youtu' in url:
        return 'youtube'
    if 'vimeo' in url:
        return 'vimeo'
    return 'link'


def _question_payload(part):
    """Map one pack part onto the fields of a :class:`Question`.

    A part is either a computation (``lines``) or a discussion (``rubric``); the
    schema has already refused anything that is both or neither. The structure
    goes into ``config`` where the marker reads it, and the model answer into
    ``guidance`` where a human marker sees it.

    For a rubric part the intended mode (e.g. ``ai_assisted``) is recorded as
    ``marking_intent`` so the marking queue knows a suggestion can be asked for.
    Until that lands the part simply marks by hand, which is the safe default
    rather than a silent zero.
    """
    lines = part.get('lines') or []
    rubric = part.get('rubric') or []
    choices = part.get('choices') or []
    mode = part.get('marking') or 'manual'

    if choices:
        correct = [c for c in choices if c.get('correct')]
        qtype = 'multi' if len(correct) > 1 else 'mcq'
        config = {'kind': 'choice', 'explanation': part.get('explanation', '')}
        guidance = 'Correct: ' + '; '.join(c['text'] for c in correct)
        if part.get('explanation'):
            guidance += '\n' + part['explanation']
        return qtype, config, guidance, 'auto'

    if lines:
        config = {
            'kind': 'schedule',
            'lines': [
                {
                    'label': line['label'],
                    'authority': line.get('authority', ''),
                    'amount': str(Decimal(str(line['amount']))),
                    'marks': str(Decimal(str(line['marks']))),
                    'tolerance': str(Decimal(str(line.get('tolerance', DEFAULT_TOLERANCE)))),
                }
                for line in lines
            ],
        }
        guidance = '\n'.join(
            f"{line['label']}"
            + (f" [{line['authority']}]" if line.get('authority') else '')
            + f" = {line['amount']}  ({line['marks']} mark(s))"
            for line in lines)
        return 'schedule', config, guidance, 'auto' if mode == 'auto' else 'manual'

    config = {
        'kind': 'rubric',
        'marking_intent': mode,
        'rubric': [
            {
                'point': point['point'],
                'authority': point.get('authority', ''),
                'marks': str(Decimal(str(point['marks']))),
                'professional_skill': bool(point.get('professional_skill')),
            }
            for point in rubric
        ],
    }
    guidance = '\n'.join(
        f"({point['marks']}) {point['point']}"
        + (f"  — {point['authority']}" if point.get('authority') else '')
        for point in rubric)
    return 'long', config, guidance, 'manual'


def _import_paper(pack, item, topic, actor, ref, result):
    """A mock, challenge or exam becomes a draft Assessment with one question
    per REQUIRED part — which is exactly how the source papers are written.

    The paper's ``module`` is set as well as its ``topic``: a topic belongs to
    exactly one offering, and every access check in the assessments app is
    written against ``module``. Setting only ``topic`` left imported papers
    unreachable — ``assessment.module`` was None and the take page raised rather
    than refusing.

    An import never publishes (status is always Draft); a quiz's
    ``attempts_allowed`` defaults to 0 (practice: unlimited). The single
    "Required" section is rebuilt from the pack. A challenge is one evolving
    scenario released a week at a time, so its week and scenario key travel with
    the paper in ``solution_notes``.
    """
    from apps.assessments.models import Assessment, Question, Section

    kind = KIND_BY_TYPE[item['type']]
    paper = Assessment.objects.filter(source_ref=ref).first()
    if paper is None:
        paper = Assessment(source_ref=ref)
        result.papers_created += 1
    else:
        result.papers_updated += 1

    paper.topic = topic
    paper.module = topic.programme_module
    paper.title = item['title']
    paper.kind = kind
    paper.description = item.get('scenario_html', '')
    paper.total_marks = Decimal(str(item['total_marks']))
    paper.time_limit_minutes = int(item.get('minutes') or 0)
    paper.release_solution = item.get('release_solution') or Assessment.RELEASE_ON_SUBMIT
    paper.solution_notes = item.get('solution_notes') or {}
    paper.status = Assessment.STATUS_DRAFT
    if item['type'] == 'quiz':
        paper.attempts_allowed = int(item.get('attempts_allowed') or 0)
    paper.save()
    result.assessment_ids.append(paper.pk)

    paper.sections.all().delete()
    section = Section.objects.create(
        assessment=paper, title='Required',
        instructions=item.get('instructions', ''), order=0)

    from apps.assessments.models import Choice
    for order, part in enumerate(item.get('parts') or []):
        qtype, config, guidance, marking_mode = _question_payload(part)
        question = Question.objects.create(
            section=section, type=qtype, order=order,
            text=f"({part['ref']})  {part['required']}",
            marks=Decimal(str(part['marks'])),
            config=config, guidance=guidance, marking_mode=marking_mode,
        )
        for c_order, choice in enumerate(part.get('choices') or []):
            Choice.objects.create(question=question, text=choice['text'][:400],
                                  is_correct=bool(choice.get('correct')), order=c_order)
        result.questions += 1
        result.marks += Decimal(str(part['marks']))

    if item['type'] == 'challenge':
        paper.solution_notes = dict(paper.solution_notes or {},
                                    scenario_key=item.get('scenario_key', ''),
                                    week=item.get('week'))
        paper.save(update_fields=['solution_notes', 'updated_at'])
    return paper



def _resolve_cohort(pack, offering):
    """The intake this pack targets, from ``pack['cohort']`` (a cohort code) on
    the offering's programme — or None (a shared template) when blank/unknown."""
    code = (pack.get('cohort') or '').strip()
    if not code:
        return None
    from apps.learning.models import Cohort
    return Cohort.objects.filter(programme=offering.programme, code__iexact=code).first()


def _resolve_week(pack, topic, offering):
    """The :class:`ModuleWeek` this pack's material belongs in.

    In order: a week already pointing at this topic (the normal case); the week
    ``pack["week"]`` names; else a new week appended to the first phase. A module
    with no year planned gets an unpublished, undated block, so content is never
    stranded off-schedule but the test date stays the coach's call.

    Weeks are scoped by intake (per-cohort content): a blank or unknown cohort
    means the shared template (``cohort=None``), visible to every intake. The
    cohort lives on :class:`ModulePhase`, so weeks are scoped via ``phase__``,
    and this intake's phase is preferred over the shared one. A week already
    working through this topic is reused — a re-import fills it in and creates
    nothing new. With no year planned for this module and intake, the block the
    pack names (or the default first test block) is created for this intake,
    unpublished and undated, because when the test actually sits is the coach's
    call, not a pack's.
    """
    from django.db.models import Q

    from apps.learning.models import ModulePhase, ModuleWeek

    cohort = _resolve_cohort(pack, offering)
    week_scope = Q(phase__cohort=cohort) | Q(phase__cohort__isnull=True)
    phase_scope = Q(cohort=cohort) | Q(cohort__isnull=True)

    existing = (ModuleWeek.objects
                .filter(phase__programme_module=offering, topics=topic)
                .filter(week_scope).order_by('phase__cohort').first())
    if existing is not None:
        _link_week_topic(existing, topic)
        return existing, False

    spec = pack.get('phase') or {}
    kind = spec.get('kind') or ModulePhase.KIND_TEST
    sequence = spec.get('sequence') or 1

    phase = None
    if spec:
        phase = (ModulePhase.objects
                 .filter(programme_module=offering, sequence=sequence, kind=kind)
                 .filter(phase_scope).order_by('cohort').first())
    if phase is None:
        phase = (ModulePhase.objects.filter(programme_module=offering)
                 .filter(phase_scope).order_by('cohort', 'order', 'id').first())
    if phase is None:
        phase = ModulePhase.objects.create(
            programme_module=offering, kind=kind, sequence=sequence,
            title=spec.get('title', ''), order=sequence, is_published=False,
            cohort=cohort)

    number = pack.get('week')
    if not number:
        last = ModuleWeek.objects.filter(phase=phase).order_by('-number').first()
        number = (last.number + 1) if last else 1

    week, created = ModuleWeek.objects.get_or_create(
        phase=phase, number=number,
        defaults={'order': number, 'is_published': False,
                  'title': (pack.get('week_title') or '')[:200]},
    )
    _link_week_topic(week, topic)
    return week, created


def _link_week_topic(week, topic):
    """Attach ``topic`` to ``week`` (idempotent), appended after existing topics.

    A week works through a *series* of topics; each pack contributes its one topic
    to that series, in import order. Re-importing the same pack is a no-op.
    """
    from apps.learning.models import WeekTopic
    if WeekTopic.objects.filter(week=week, topic=topic).exists():
        return
    last = WeekTopic.objects.filter(week=week).order_by('-order').first()
    order = (last.order + 1) if last else 0
    WeekTopic.objects.create(week=week, topic=topic, order=order)


def _schedule_material(week, item, ref, lesson=None, paper=None, actor=None, order=0, topic=None,
                       pack=None):
    """Put one imported item on the week's plan, once (keyed on ``source_ref``).

    Lands **unpublished**: it becomes visible only when the import is published.
    """
    from apps.learning.models import ModuleMaterial

    kind = MATERIAL_KIND_BY_TYPE.get(item['type'], ModuleMaterial.KIND_DOCUMENT)
    material = ModuleMaterial.objects.filter(source_ref=ref).first()
    created = material is None
    if created:
        material = ModuleMaterial(source_ref=ref)

    material.phase = week.phase
    material.week = week
    material.topic = topic
    material.kind = kind
    material.title = item['title'][:200]
    material.lesson = lesson
    material.assessment = paper
    material.order = order
    material.is_published = False
    material.audience = (item.get('audience') or (pack or {}).get('audience') or 'students')
    if lesson is not None:
        material.estimated_minutes = lesson.estimated_minutes
    elif paper is not None and paper.time_limit_minutes:
        material.estimated_minutes = paper.time_limit_minutes
    material.created_by = material.created_by or actor
    material.save()
    return material, created


@transaction.atomic
def import_pack(pack, *, actor=None, validate=True):
    """Import ``pack``. Returns an :class:`ImportResult`.

    ``actor`` is the admin/staff user doing the import — recorded as the author
    where the model has somewhere to put it. Permission is the caller's job:
    this function is the mechanism, and the view or command in front of it is
    where "admin and staff only" is enforced.
    """
    if validate:
        validate_or_raise(pack)

    result = ImportResult()
    topic, offering = _resolve_topic(pack)
    result.topic = topic

    week, week_created = _resolve_week(pack, topic, offering)
    result.week = week
    if week is not None and week_created:
        result.warnings.append(
            f'Created week {week.number} on "{week.phase}" to hold this topic. '
            f'It is unpublished and undated — set its dates on the module '
            f'schedule before candidates see it.')

    for index, item in enumerate(pack['items']):
        ref = _source_ref(pack, item, index)
        if item['type'] in READING_TYPES:
            row = _import_study_guide(pack, item, topic, actor, ref, result)
            lesson, paper = row, None
        else:
            row = _import_paper(pack, item, topic, actor, ref, result)
            lesson, paper = None, row

        if week is not None:
            material, _ = _schedule_material(week, item, f'{ref}/material', lesson=lesson,
                                             paper=paper, actor=actor, order=_order(item, index),
                                             topic=topic, pack=pack)
            result.material_ids.append(material.pk)
            result.scheduled += 1
    if week is not None:
        result.week_ids.append(week.pk)
        result.phase_ids.append(week.phase_id)

    logger.info('content pack imported: %s · %s — %s',
                offering.label, topic.code, result)
    return result


ITEM_ORDER = {'study_guide': 10, 'blueprint': 20, 'quiz': 30, 'challenge': 32, 'mock': 34, 'exam': 50}


def _order(item, index):
    """Sort key for an item's material in the week (:data:`ITEM_ORDER` plus its index)."""
    return ITEM_ORDER.get(item['type'], 40) + index


@transaction.atomic
def import_bundle(bundle, *, actor=None):
    """Import a single pack or a ``metanoia-bundle/1`` of them, all or nothing."""
    total = ImportResult()
    for pack in packs_of(bundle):
        total.merge(import_pack(pack, actor=actor))
    return total


def publish_imported(result, *, actor=None):
    """Make exactly the rows an import created visible to students.

    The second human click. Lessons go live, papers move Draft → Review → Open
    through the assessment lifecycle (which still refuses a paper with no
    questions), and the materials, their weeks and their blocks are published —
    except material marked staff-only, which ``ModuleMaterial.save`` keeps hidden.
    A paper the lifecycle refuses (e.g. an empty one) is reported, not forced.
    Returns a list of problems; an empty list means everything went live.
    """
    from django.core.exceptions import ValidationError
    from django.utils import timezone

    from apps.assessments.models import Assessment
    from apps.learning.models import Lesson, ModuleMaterial, ModulePhase, ModuleWeek

    problems = []
    for lesson in Lesson.objects.filter(pk__in=result.get('lessons') or []):
        lesson.status = Lesson.STATUS_PUBLISHED
        lesson.publish_at = lesson.publish_at or timezone.now()
        lesson.save(update_fields=['status', 'publish_at', 'updated_at'])
    for paper in Assessment.objects.filter(pk__in=result.get('assessments') or []):
        try:
            if paper.status == Assessment.STATUS_DRAFT:
                paper.transition_to(Assessment.STATUS_REVIEW, by=actor)
            if paper.status in (Assessment.STATUS_REVIEW, Assessment.STATUS_CLOSED):
                paper.transition_to(Assessment.STATUS_OPEN, by=actor)
        except (ValidationError, Exception) as exc:
            problems.append(f'"{paper.title}" was not opened: {exc}')
    for material in ModuleMaterial.objects.filter(pk__in=result.get('materials') or []):
        material.is_published = True
        material.save(update_fields=['is_published', 'updated_at'])
    ModuleWeek.objects.filter(pk__in=result.get('weeks') or []).update(is_published=True)
    ModulePhase.objects.filter(pk__in=result.get('phases') or []).update(is_published=True)
    return problems
