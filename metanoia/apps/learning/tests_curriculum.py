"""The sample academic structure, asserted.

These tests are the check that ``manage.py seed_sample_structure`` still builds
what its data tables say: the institutions and their codes, the programmes and
their full codes, the canonical module catalogue, the topic library, and each
offering's own copy of its topics.

They also assert the two properties the structure exists to guarantee:

* every offering owns its topics, so editing one programme's copy of a topic
  leaves another's untouched — while the shared topic **code** still reports
  them together; and
* weighting columns are **blank** wherever past papers have not been analysed,
  rather than carrying guessed numbers.

:class:`CurriculumSeedTests` follows the seed in order: institutions,
programmes, the canonical module catalogue, the topic library, the
offering-to-topic mapping, then trend analysis, the naming convention and
idempotency.
"""

from django.core.management import call_command
from django.db.utils import IntegrityError
from django.test import TestCase

from apps.learning.management.commands import seed_sample_structure as sample
from apps.learning.models import (
    DEPTH_ADVANCED, DEPTH_FOUNDATIONAL, Cohort, Institution, Lesson, Module, Programme, ProgrammeModule,
    ProgrammePhase, Topic,
)

ADV, FND = DEPTH_ADVANCED, DEPTH_FOUNDATIONAL


def topic_codes(offering):
    """The offering's topic codes, in syllabus order."""
    return list(offering.topics.values_list('code', flat=True))


class CurriculumSeedTests(TestCase):
    """Everything the sample seed builds, checked against its data tables."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_sample_structure', verbosity=0)

    def offering(self, full_code, code):
        """The offering ``code`` on the programme with ``full_code`` (``METANOIA-SAMPLE``)."""
        institution, programme = full_code.split('-', 1)
        return ProgrammeModule.objects.get(
            programme__institution__code=institution, programme__code=programme, code=code)

    def test_the_institutions_exist_under_their_own_codes(self):
        self.assertEqual(list(Institution.objects.order_by('order').values_list('code', flat=True)),
                         ['METANOIA', 'OTHER'])

    def test_each_institution_carries_its_accent_colour_and_description(self):
        accents = dict(Institution.objects.values_list('code', 'accent_name'))
        self.assertEqual(accents, {'METANOIA': 'Brown', 'OTHER': 'Slate'})
        self.assertFalse(Institution.objects.filter(description='').exists())

    def test_every_programme_exists_under_its_full_code(self):
        self.assertEqual(sorted(p.full_code for p in Programme.objects.all()),
                         ['METANOIA-SAMPLE', 'OTHER-CURATED'])

    def test_programme_names_levels_and_default_depths(self):
        sample_programme = Programme.objects.get(institution__code='METANOIA', code='SAMPLE')
        self.assertEqual((sample_programme.full_name, sample_programme.level, sample_programme.depth_default,
                          sample_programme.modules.without_general().count()),
                         ('Sample Programme', Programme.LEVEL_CTA, FND, 3))
        self.assertFalse(Programme.objects.filter(is_competency_based=True).exists())

    def test_the_sample_programme_has_its_intakes_and_phases(self):
        sample_programme = Programme.objects.get(institution__code='METANOIA', code='SAMPLE')
        self.assertEqual(sorted(Cohort.objects.filter(programme=sample_programme).values_list('code', flat=True)),
                         ['SAMPLE1', 'SAMPLE2', 'SAMPLE3'])
        self.assertEqual(list(ProgrammePhase.objects.filter(programme=sample_programme)
                              .order_by('order').values_list('name', flat=True)), ['Foundation', 'Practice'])
        self.assertFalse(Cohort.objects.filter(programme__code='CURATED').exists())

    def test_the_canonical_modules_are_the_sample_catalogue(self):
        self.assertEqual(sorted(Module.objects.exclude(code='GEN').values_list('code', flat=True)),
                         ['CORE', 'INTRO', 'PRACT'])
        self.assertFalse(Module.objects.filter(is_competency_area=True).exists())

    def test_an_institutions_own_module_code_sits_on_the_offering(self):
        self.assertEqual(self.offering('METANOIA-SAMPLE', 'INTRO').module.code, 'INTRO')
        self.assertEqual(self.offering('OTHER-CURATED', 'PRACT').module.code, 'PRACT')

    def test_the_library_holds_exactly_the_documented_topics(self):
        expected = sorted(code for _m, code, *_ in sample.TOPICS)
        self.assertEqual(sorted(set(Topic.objects.values_list('code', flat=True))), expected)

    def test_each_topic_sits_under_its_canonical_module(self):
        for topic in Topic.objects.select_related('programme_module__module'):
            self.assertEqual(topic.programme_module.module.code, sample.TOPIC_LIBRARY[topic.code][0], topic)

    def test_every_topic_carries_a_description(self):
        self.assertFalse(Topic.objects.filter(description='').exists())

    def test_each_offering_owns_its_own_copy_of_a_topic(self):
        self.assertEqual(sorted(str(topic) for topic in Topic.objects.filter(code='INTRO-01')),
                         ['METANOIA-SAMPLE / INTRO / INTRO-01', 'OTHER-CURATED / INTRO / INTRO-01'])

    def test_the_topic_code_still_reports_the_copies_together(self):
        self.assertEqual(sorted(Topic.objects.filter(code='CORE-01')
                                .values_list('programme_module__programme__institution__code', flat=True)),
                         ['METANOIA', 'OTHER'])

    def test_editing_one_offerings_topic_leaves_every_other_untouched(self):
        mine = self.offering('METANOIA-SAMPLE', 'CORE').topics.get(code='CORE-01')
        mine.title = 'Key terms (revised)'
        mine.save()
        other = self.offering('OTHER-CURATED', 'CORE').topics.get(code='CORE-01')
        self.assertEqual(other.title, 'Key terms')

    def test_every_offering_carries_its_modules_topics_in_order(self):
        for full_code in ('METANOIA-SAMPLE', 'OTHER-CURATED'):
            for code in ('INTRO', 'CORE', 'PRACT'):
                with self.subTest(full_code=full_code, code=code):
                    self.assertEqual(topic_codes(self.offering(full_code, code)), [f'{code}-01', f'{code}-02'])
                    self.assertEqual(self.offering(full_code, code).depth_level, FND)

    def test_an_unanalysed_offering_says_so_rather_than_guessing(self):
        self.assertFalse(Topic.objects.exclude(frequency='').exists())
        self.assertFalse(Topic.objects.filter(is_non_negotiable=True).exists())

    def test_trend_analysis_pins_the_topics_that_appear_every_sitting(self):
        """A frequency added to ``TREND_ANALYSIS`` is written, and an every-sitting topic is pinned."""
        from unittest import mock
        trend = {('METANOIA-SAMPLE', 'CORE'): {'CORE-01': Topic.FREQ_EVERY_SITTING,
                                               'CORE-02': Topic.FREQ_PERIODIC}}
        with mock.patch.object(sample, 'TREND_ANALYSIS', trend):
            call_command('seed_sample_structure', '--update', verbosity=0)
        topics = self.offering('METANOIA-SAMPLE', 'CORE').topics
        self.assertEqual(dict(topics.values_list('code', 'frequency')),
                         {'CORE-01': Topic.FREQ_EVERY_SITTING, 'CORE-02': Topic.FREQ_PERIODIC})
        self.assertEqual(list(topics.filter(is_non_negotiable=True).values_list('code', flat=True)), ['CORE-01'])
        self.assertFalse(self.offering('OTHER-CURATED', 'CORE').topics.exclude(frequency='').exists())

    def test_the_lms_naming_convention_reads_correctly(self):
        offering = self.offering('METANOIA-SAMPLE', 'INTRO')
        self.assertEqual(offering.programme.label, 'METANOIA SAMPLE')
        self.assertEqual(offering.label, 'METANOIA SAMPLE | INTRO')
        self.assertEqual(offering.reference, 'METANOIA-SAMPLE / INTRO')
        topic = offering.topics.get(code='INTRO-01')
        self.assertEqual(topic.label, 'METANOIA SAMPLE | INTRO | Welcome and orientation')

    def test_seeding_twice_changes_nothing(self):
        counts = (Institution.objects.count(), Programme.objects.count(), Module.objects.count(),
                  ProgrammeModule.objects.count(), Topic.objects.count(), Cohort.objects.count())
        call_command('seed_sample_structure', verbosity=0)
        self.assertEqual((Institution.objects.count(), Programme.objects.count(), Module.objects.count(),
                          ProgrammeModule.objects.count(), Topic.objects.count(), Cohort.objects.count()),
                         counts)


class SpineIntegrityTests(TestCase):
    """The constraints that stop the structure drifting."""

    @classmethod
    def setUpTestData(cls):
        cls.institution = Institution.objects.create(code='TEST', name='Test College')
        cls.programme = Programme.objects.create(
            institution=cls.institution, code='PGDA', name='PGDA', depth_default=ADV)
        cls.module = Module.objects.create(code='XX', name='Test Module')
        cls.offering = ProgrammeModule.objects.create(
            programme=cls.programme, module=cls.module, code='XXX')
        cls.topic = Topic.objects.create(
            programme_module=cls.offering, code='XX-01', title='A topic')

    def test_a_programme_code_is_unique_within_its_institution(self):
        with self.assertRaises(IntegrityError):
            Programme.objects.create(institution=self.institution, code='PGDA', name='Duplicate')

    def test_the_same_programme_code_may_be_reused_at_another_institution(self):
        other = Institution.objects.create(code='OTHER', name='Other College')
        programme = Programme.objects.create(institution=other, code='PGDA', name='PGDA')
        self.assertEqual(programme.full_code, 'OTHER-PGDA')

    def test_a_module_code_is_unique_within_its_programme(self):
        with self.assertRaises(IntegrityError):
            ProgrammeModule.objects.create(
                programme=self.programme, module=self.module, code='XXX')

    def test_a_topic_code_is_unique_within_its_offering(self):
        with self.assertRaises(IntegrityError):
            Topic.objects.create(programme_module=self.offering, code='XX-01', title='Duplicate')

    def test_the_same_topic_code_may_be_reused_on_another_offering(self):
        other = ProgrammeModule.objects.create(
            programme=self.programme, module=self.module, code='YYY')
        topic = Topic.objects.create(programme_module=other, code='XX-01', title='A topic')
        self.assertEqual(str(topic), 'TEST-PGDA / YYY / XX-01')

    def test_depth_falls_through_from_the_programme_when_not_set(self):
        """A topic's depth is inherited from its offering, and the offering's from the
        programme.
        """
        self.assertEqual(self.offering.resolved_depth, ADV)
        self.assertEqual(self.topic.resolved_depth, ADV)

    def test_an_offering_may_override_the_programmes_depth(self):
        self.offering.depth_level = FND
        self.offering.save()
        topic = Topic.objects.create(programme_module=self.offering, code='XX-02', title='Another')
        self.assertEqual(topic.resolved_depth, FND)

    def test_deleting_an_offering_takes_its_topics_with_it(self):
        offering = ProgrammeModule.objects.create(
            programme=self.programme, module=self.module, code='ZZZ')
        Topic.objects.create(programme_module=offering, code='XX-09', title='Doomed')
        offering.delete()
        self.assertFalse(Topic.objects.filter(code='XX-09').exists())


class TopicContentTests(TestCase):
    """Content hangs off a topic, and a topic belongs to one offering — so one
    programme's material for a topic is not another's."""

    @classmethod
    def setUpTestData(cls):
        """Seed the sample structure and pick two programmes' copies of the same topic."""
        call_command('seed_sample_structure', verbosity=0)
        cls.sample = Topic.objects.get(
            programme_module__programme__institution__code='METANOIA',
            programme_module__programme__code='SAMPLE',
            programme_module__code='CORE', code='CORE-01')
        cls.curated = Topic.objects.get(
            programme_module__programme__institution__code='OTHER',
            programme_module__programme__code='CURATED',
            programme_module__code='CORE', code='CORE-01')

    def test_a_lesson_knows_its_whole_path_up_the_spine(self):
        lesson = Lesson.objects.create(topic=self.sample, title='Key terms walk-through')
        self.assertEqual(lesson.institution.code, 'METANOIA')
        self.assertEqual(lesson.programme.full_code, 'METANOIA-SAMPLE')
        self.assertEqual(lesson.programme_module.code, 'CORE')
        self.assertEqual(lesson.topic.code, 'CORE-01')

    def test_two_programmes_teach_the_same_topic_with_different_content(self):
        """Same topic code, separate topic rows, separate material."""
        Lesson.objects.create(topic=self.sample, title='Sample key terms')
        Lesson.objects.create(topic=self.curated, title='Curated key terms')
        self.assertEqual(self.sample.code, self.curated.code)
        self.assertNotEqual(self.sample.pk, self.curated.pk)
        self.assertEqual([lesson.title for lesson in self.sample.lessons.all()], ['Sample key terms'])
        self.assertEqual([lesson.title for lesson in self.curated.lessons.all()], ['Curated key terms'])

    def test_a_topic_holds_its_own_lessons(self):
        """A topic is where material hangs, and progress through it is progress
        through its *published* lessons. The ``CourseItem`` segment layer that
        used to be measured here went with the MyHub course player — see
        ``apps.myhub.dashboard._completion_index`` for what counts now."""
        Lesson.objects.create(topic=self.sample, title='Key terms', status=Lesson.STATUS_PUBLISHED)
        Lesson.objects.create(topic=self.sample, title='Practice', status=Lesson.STATUS_DRAFT)

        published = self.sample.lessons.filter(status=Lesson.STATUS_PUBLISHED)
        self.assertEqual([lesson.title for lesson in published], ['Key terms'])
        self.assertEqual(self.sample.lessons.count(), 2)
