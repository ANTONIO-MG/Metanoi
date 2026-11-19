"""Seed the sample academic structure, with every offering's own topics.

Builds, in order:

1. **Institutions** — ``METANOIA`` (the programmes Metanoia Group runs itself)
   and the ``OTHER`` catch-all.
2. **Programmes**, their phases and their cohorts.
3. The **canonical module catalogue** — the three sample modules.
4. **Offerings** (``programme_module``) under each institution's own code.
5. Each offering's own copy of its **topics**, from the topic library.

Everything here is neutral sample data that keeps a new install working end to
end; replace it with the real programmes. It shares natural keys with
``core.academic_spine`` (which adds prices, logos and calendars), so either can
run first.

Idempotent: creates what is missing and leaves existing rows alone
(``--update`` refreshes their fields).

    python manage.py seed_sample_structure
    python manage.py seed_sample_structure --update

The seed data:

``INSTITUTIONS``
    The sample institution. The accent colour is a starting point; edit it in
    the admin.
``OTHER_INSTITUTION``
    The catch-all, kept in step with ``core.academic_spine.INSTITUTIONS``, so an
    unlisted provider can still get through the registration picker. Ordered 99
    so real institutions sort above it. Without it the registration picker has
    no row for a student whose provider is not on the list, and they cannot
    finish signing up.
``MODULES``
    The canonical module catalogue: the three sample modules.
``TOPICS`` / ``TOPIC_LIBRARY``
    The topic library — the template each offering's own topic copies start
    from — as ``(canonical module code, topic code, title, reference,
    description)`` rows, and indexed by topic code (``INTRO-01`` → ``(module
    code, title, reference, description)``).
``PROGRAMMES``
    Programmes and their offerings. An offering is ``(local code, canonical
    module code, topic codes, depth, name override, coverage prose)``.
    OTHER/CURATED mirrors ``core.academic_spine``: the same modules, and no
    cohorts — that support is scheduled with the student rather than run as an
    intake.
``TREND_ANALYSIS``
    Topic frequencies per ``(programme full code, offering code)``. Empty: a
    frequency is evidence from worked past papers, so it stays blank rather than
    being guessed. Add rows here once papers have been analysed.
``PINNED_FREQUENCIES``
    📌 Non-negotiable: a topic that appears in every or most sittings is never
    optional preparation.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.learning.models import (
    DEPTH_FOUNDATIONAL, Cohort, Institution, Module, Programme, ProgrammeModule, ProgrammePhase, Topic,
)

FND = DEPTH_FOUNDATIONAL


INSTITUTIONS = [
    {
        'code': 'METANOIA', 'name': 'Metanoia Group', 'order': 1,
        'accent_name': 'Brown', 'accent_colour': '#3E2415',
        'description':
            'The programmes Metanoia Group runs itself. This row ships with the sample data so a new '
            'install has a working structure; rename it, describe it or add other institutions in the '
            'admin.',
    },
]

OTHER_INSTITUTION = {
    'code': 'OTHER', 'name': 'Other institution', 'order': 99,
    'accent_name': 'Slate', 'accent_colour': '#6B7A8F',
    'description':
        'For students registered somewhere that is not listed above. Choose this and your '
        'programme is put together with you one on one rather than run as a standard intake.',
}

MODULES = [
    {'code': 'INTRO', 'name': 'Introduction', 'order': 1,
     'description': 'Sample module: welcome, orientation and planning your studies.'},
    {'code': 'CORE', 'name': 'Core concepts', 'order': 2,
     'description': 'Sample module: the key terms of the programme and how they fit together.'},
    {'code': 'PRACT', 'name': 'Practice and assessment', 'order': 3,
     'description': 'Sample module: a worked example, practice questions and a mini mock.'},
]

TOPICS = [
    ('INTRO', 'INTRO-01', 'Welcome and orientation', '',
     'Sample topic: how the programme runs, where the material is and who to ask.'),
    ('INTRO', 'INTRO-02', 'Planning your studies', '',
     'Sample topic: a weekly study plan and how to keep to it.'),
    ('CORE', 'CORE-01', 'Key terms', '',
     'Sample topic: the vocabulary the rest of the programme uses.'),
    ('CORE', 'CORE-02', 'Putting it together', '',
     'Sample topic: how the key terms connect, in a one-page summary.'),
    ('PRACT', 'PRACT-01', 'A worked example', '',
     'Sample topic: one example worked step by step.'),
    ('PRACT', 'PRACT-02', 'Practice questions', '',
     'Sample topic: short questions with a self-marking workbook.'),
]

TOPIC_LIBRARY = {code: (module_code, title, reference, description)
                 for module_code, code, title, reference, description in TOPICS}


def codes(prefix, *numbers):
    """``codes('CORE', 1, 2)`` → ``['CORE-01', 'CORE-02']``."""
    return [f'{prefix}-{n:02d}' for n in numbers]


def series(prefix, last):
    """``series('CORE', 2)`` → every code from ``CORE-01`` to ``CORE-02``."""
    return codes(prefix, *range(1, last + 1))


ALL_INTRO, ALL_CORE, ALL_PRACT = series('INTRO', 2), series('CORE', 2), series('PRACT', 2)

PROGRAMMES = [
    {
        'institution': 'METANOIA', 'code': 'SAMPLE', 'name': 'Sample Programme', 'order': 1,
        'full_name': 'Sample Programme',
        'level': Programme.LEVEL_CTA, 'depth_default': FND,
        'description':
            'A small sample programme that ships with the platform so a new install has a working '
            'structure: three modules, each with a test-and-examination cycle. Replace it with your '
            'own programmes.',
        'offerings': [
            ('INTRO', 'INTRO', ALL_INTRO, FND, '', ''),
            ('CORE', 'CORE', ALL_CORE, FND, '', ''),
            ('PRACT', 'PRACT', ALL_PRACT, FND, '', ''),
        ],
        'phases': [
            ('Foundation', 'First term', 'Orientation and the key terms.'),
            ('Practice', 'Second term', 'Worked examples and practice questions.'),
        ],
        'cohorts': [('SAMPLE1', 'Sample intake 1'), ('SAMPLE2', 'Sample intake 2'),
                    ('SAMPLE3', 'Sample intake 3 · dates to confirm')],
    },
    {
        'institution': 'OTHER', 'code': 'CURATED', 'name': 'Curated', 'order': 1,
        'full_name': 'Curated one-on-one support',
        'level': Programme.LEVEL_CTA, 'depth_default': FND,
        'description':
            'For students whose institution is not one we run a standard programme for. Pick the '
            'modules you are carrying and the plan is put together around them and reviewed with you '
            'one on one rather than on a fixed cohort timetable.',
        'offerings': [
            ('INTRO', 'INTRO', ALL_INTRO, FND, '', ''),
            ('CORE', 'CORE', ALL_CORE, FND, '', ''),
            ('PRACT', 'PRACT', ALL_PRACT, FND, '', ''),
        ],
        'cohorts': [],
    },
]

E, M, R, P = (Topic.FREQ_EVERY_SITTING, Topic.FREQ_MOST_SITTINGS,
              Topic.FREQ_RECURRING, Topic.FREQ_PERIODIC)
TREND_ANALYSIS = {}
PINNED_FREQUENCIES = {E, M}


class Command(BaseCommand):
    """``manage.py seed_sample_structure [--update]`` — see the module docstring."""
    help = ('Seed institutions, programmes, phases, cohorts, the canonical module catalogue, '
            "every offering, and each offering's own topic list.")

    def add_arguments(self, parser):
        """Add ``--update`` (refresh existing rows)."""
        parser.add_argument(
            '--update', action='store_true',
            help='Refresh the fields of rows that already exist (default: leave them alone).')

    @transaction.atomic
    def handle(self, *args, **options):
        """Seed institutions, modules and programmes in one transaction, then print the
        totals.
        """
        self.update = options['update']
        self.counts = {'created': 0, 'updated': 0, 'kept': 0}

        institution_specs = list(INSTITUTIONS) + [OTHER_INSTITUTION]
        programme_specs = list(PROGRAMMES)

        institutions = self._seed_institutions(institution_specs)
        modules = self._seed_modules()
        self._seed_programmes(programme_specs, institutions, modules)

        self.stdout.write(self.style.SUCCESS(
            f"Academic spine seeded — {self.counts['created']} created, "
            f"{self.counts['updated']} updated, {self.counts['kept']} left as they were."))
        self.stdout.write(
            f'  {Institution.objects.count()} institutions · {Programme.objects.count()} programmes · '
            f'{ProgrammePhase.objects.count()} phases · {Cohort.objects.count()} cohorts')
        self.stdout.write(
            f'  {Module.objects.count()} canonical modules · {ProgrammeModule.objects.count()} '
            f'offerings · {Topic.objects.count()} topics '
            f'({len(TOPICS)} in the library template)')
        analysed = Topic.objects.exclude(frequency='').count()
        self.stdout.write(
            f'  {analysed} topics carry trend analysis; the rest are blank until past '
            'papers have been worked.')

    def _sync(self, model, defaults, **lookup):
        """get_or_create, honouring ``--update`` for rows that already exist."""
        obj, created = model.objects.get_or_create(**lookup, defaults=defaults)
        if created:
            self.counts['created'] += 1
        elif self.update:
            for field, value in defaults.items():
                setattr(obj, field, value)
            obj.save()
            self.counts['updated'] += 1
        else:
            self.counts['kept'] += 1
        return obj

    def _seed_institutions(self, specs):
        """Sync each institution spec; returns ``{code: Institution}``."""
        out = {}
        for spec in specs:
            spec = dict(spec)
            code = spec.pop('code')
            out[code] = self._sync(Institution, spec, code=code)
        return out

    def _seed_modules(self):
        """Sync the canonical module catalogue; returns ``{code: Module}``."""
        out = {}
        for spec in MODULES:
            spec = dict(spec)
            code = spec.pop('code')
            out[code] = self._sync(Module, spec, code=code)
        return out

    def _seed_programmes(self, specs, institutions, modules):
        """Sync each programme with its phases, cohorts, offerings and their topics."""
        for spec in specs:
            spec = dict(spec)
            institution = institutions[spec.pop('institution')]
            code = spec.pop('code')
            offerings = spec.pop('offerings')
            cohorts = spec.pop('cohorts')
            phases = spec.pop('phases', [])

            programme = self._sync(Programme, spec, institution=institution, code=code)

            for order, (name, period, focus) in enumerate(phases, start=1):
                self._sync(ProgrammePhase, {'period': period, 'focus': focus, 'order': order},
                           programme=programme, name=name)

            for order, (cohort_code, cohort_name) in enumerate(cohorts, start=1):
                self._sync(Cohort, {'name': cohort_name}, programme=programme, code=cohort_code)

            for order, offering in enumerate(offerings, start=1):
                local_code, module_code, topic_codes, depth, name, coverage = offering
                programme_module = self._sync(
                    ProgrammeModule,
                    {'module': modules[module_code], 'name': name, 'depth_level': depth,
                     'description': coverage, 'order': order},
                    programme=programme, code=local_code,
                )
                self._seed_topics(programme_module, topic_codes, depth)

    def _seed_topics(self, programme_module, topic_codes, depth):
        """Give an offering its own copy of each topic it teaches, worded from
        the library template."""
        trend = TREND_ANALYSIS.get((programme_module.programme.full_code, programme_module.code), {})
        for order, topic_code in enumerate(topic_codes, start=1):
            _, title, reference, description = TOPIC_LIBRARY[topic_code]
            frequency = trend.get(topic_code, '')
            self._sync(
                Topic,
                {'title': title, 'reference': reference, 'description': description,
                 'depth': depth, 'frequency': frequency, 'order': order,
                 'is_non_negotiable': frequency in PINNED_FREQUENCIES},
                programme_module=programme_module, code=topic_code,
            )
