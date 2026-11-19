"""Tests for the content-pack contract and importer.

The validator gets the most attention here. It is the only thing standing
between a mis-read solution workbook — or a language model's best guess — and a
paper that cannot be marked out of its own total, so every rule it enforces has
a test that proves it fires.
"""

import copy
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase

from core import content_pack
from core.content_pack.importer import PackImportError
from core.content_pack.schema import ValidationError

FIXTURE = Path(settings.BASE_DIR) / 'fixtures' / 'content_packs' / 'sample-training-budget.json'


def load_pack():
    """A fresh copy of the shipped example pack (``sample-training-budget.json``)."""
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def spine():
    """The smallest slice of the academic spine a pack can import into."""
    from apps.learning.models import Institution, Module, Programme, ProgrammeModule

    institution = Institution.objects.create(name='Metanoia Group', code='METANOIA')
    programme = Programme.objects.create(institution=institution, name='Sample Programme', code='SAMPLE')
    module = Module.objects.create(name='Introduction', code='INTRO')
    return ProgrammeModule.objects.create(programme=programme, module=module, code='INTRO')


class SchemaTests(TestCase):

    """The content-pack contract: every rule the validator enforces fires."""
    def test_the_shipped_fixture_is_valid(self):
        """If this ever fails, the example we tell people to copy is broken."""
        self.assertEqual(content_pack.validate(load_pack()), [])

    def test_a_pack_that_is_not_an_object_fails_cleanly(self):
        for junk in ('a string', 42, [1, 2], None):
            errors = content_pack.validate(junk)
            self.assertEqual(len(errors), 1, junk)
            self.assertIn('must be a JSON object', errors[0])

    def test_the_version_must_match(self):
        pack = load_pack()
        pack['pack'] = 'metanoia-pack/2'
        self.assertTrue(any('metanoia-pack/1' in e for e in content_pack.validate(pack)))

    def test_part_marks_must_equal_the_sum_of_its_rows(self):
        """The check that catches a mis-read solution sheet (a row's marks raised from 1 to 5)."""
        pack = load_pack()
        pack['items'][1]['parts'][0]['lines'][0]['marks'] = 5
        errors = content_pack.validate(pack)
        self.assertTrue(any('rows allocate' in e and 'must agree' in e for e in errors), errors)

    def test_parts_must_add_up_to_the_paper_total(self):
        """A paper total of 60 is refused while its parts still sum to 50."""
        pack = load_pack()
        pack['items'][1]['total_marks'] = 60
        errors = content_pack.validate(pack)
        self.assertTrue(any('total_marks says 60' in e for e in errors), errors)

    def test_an_auto_part_with_no_lines_is_refused(self):
        """Otherwise it silently scores every candidate zero."""
        pack = load_pack()
        part = pack['items'][1]['parts'][0]
        part['lines'] = []
        errors = content_pack.validate(pack)
        self.assertTrue(any('no lines to mark against' in e for e in errors), errors)

    def test_a_discussion_part_with_no_rubric_is_refused(self):
        pack = load_pack()
        part = pack['items'][1]['parts'][2]
        part['rubric'] = []
        errors = content_pack.validate(pack)
        self.assertTrue(any('no rubric to mark against' in e for e in errors), errors)

    def test_a_part_cannot_be_both_computation_and_discussion(self):
        pack = load_pack()
        part = pack['items'][1]['parts'][2]
        part['lines'] = [{'label': 'x', 'amount': 1, 'marks': 1}]
        errors = content_pack.validate(pack)
        self.assertTrue(any('not both' in e for e in errors), errors)

    def test_duplicate_part_refs_are_refused(self):
        pack = load_pack()
        pack['items'][1]['parts'][1]['ref'] = 'a'
        errors = content_pack.validate(pack)
        self.assertTrue(any('used more than once' in e for e in errors), errors)

    def test_errors_name_their_path(self):
        """A model's output has to be correctable by a person reading the error."""
        pack = load_pack()
        pack['items'][1]['parts'][0]['lines'][2]['marks'] = 'one mark'
        errors = content_pack.validate(pack)
        self.assertTrue(any(e.startswith('items[1].parts[0].lines[2].marks') for e in errors), errors)

    def test_every_problem_is_reported_not_just_the_first(self):
        pack = load_pack()
        pack['module'] = ''
        pack['programme'] = ''
        del pack['topic']
        self.assertGreaterEqual(len(content_pack.validate(pack)), 3)

    def test_validate_or_raise_carries_the_errors(self):
        from core.content_pack.schema import validate_or_raise
        pack = load_pack()
        pack['pack'] = 'nope'
        with self.assertRaises(ValidationError) as ctx:
            validate_or_raise(pack)
        self.assertTrue(ctx.exception.errors)


class ImportTests(TestCase):

    """The importer: a valid pack lands as a topic, guide and paper, idempotently."""
    def setUp(self):
        """Create the minimal spine the fixture imports into."""
        self.offering = spine()

    def test_it_imports_the_guide_and_the_paper(self):
        from apps.assessments.models import Assessment
        from apps.learning.models import Lesson

        result = content_pack.import_pack(load_pack())

        self.assertEqual(result.topic.code, 'T1')
        self.assertEqual(Lesson.objects.filter(topic=result.topic).count(), 1)
        self.assertEqual(Assessment.objects.filter(topic=result.topic).count(), 1)
        self.assertEqual(result.questions, 5)
        self.assertEqual(result.marks, Decimal('50'))

    def test_nothing_is_published(self):
        """An import must never put a paper in front of a candidate."""
        from apps.assessments.models import Assessment
        from apps.learning.models import Lesson

        result = content_pack.import_pack(load_pack())
        self.assertEqual(Lesson.objects.get(topic=result.topic).status, Lesson.STATUS_DRAFT)
        self.assertEqual(Assessment.objects.get(topic=result.topic).status,
                         Assessment.STATUS_DRAFT)

    def test_computational_parts_are_auto_markable_and_discussion_parts_are_not(self):
        result = content_pack.import_pack(load_pack())
        from apps.assessments.models import Assessment
        questions = list(Assessment.objects.get(topic=result.topic)
                         .sections.first().questions.all())

        auto = [q for q in questions if q.is_auto_marked]
        manual = [q for q in questions if not q.is_auto_marked]
        self.assertEqual([q.marks for q in auto], [Decimal('18'), Decimal('14')])
        self.assertEqual(len(manual), 3)
        self.assertTrue(all(q.config['kind'] == 'schedule' for q in auto))
        self.assertTrue(all(q.config['kind'] == 'rubric' for q in manual))

    def test_every_auto_line_carries_an_amount_marks_and_a_tolerance(self):
        """Every auto-marked line has a parseable amount, positive marks and a tolerance."""
        result = content_pack.import_pack(load_pack())
        from apps.assessments.models import Assessment
        first = Assessment.objects.get(topic=result.topic).sections.first().questions.first()
        for line in first.config['lines']:
            self.assertIn('amount', line)
            self.assertIn('marks', line)
            self.assertIn('tolerance', line)
            Decimal(line['amount'])
            self.assertGreater(Decimal(line['marks']), 0)

    def test_the_professional_skills_row_is_flagged(self):
        """Communication marks are a human judgement and must be labelled."""
        result = content_pack.import_pack(load_pack())
        from apps.assessments.models import Assessment
        parts = Assessment.objects.get(topic=result.topic).sections.first().questions.all()
        flags = [row.get('professional_skill')
                 for q in parts for row in (q.config.get('rubric') or [])]
        self.assertIn(True, flags)

    def test_re_importing_updates_rather_than_duplicates(self):
        from apps.assessments.models import Assessment
        from apps.learning.models import Lesson

        content_pack.import_pack(load_pack())
        pack = load_pack()
        pack['items'][1]['title'] = 'Sample practice paper (revised)'
        result = content_pack.import_pack(pack)

        self.assertEqual(Assessment.objects.count(), 1)
        self.assertEqual(Lesson.objects.count(), 1)
        self.assertEqual(result.papers_updated, 1)
        self.assertEqual(result.papers_created, 0)
        self.assertEqual(Assessment.objects.get().title,
                         'Sample practice paper (revised)')

    def test_re_importing_does_not_leave_orphan_questions(self):
        from apps.assessments.models import Question
        content_pack.import_pack(load_pack())
        content_pack.import_pack(load_pack())
        self.assertEqual(Question.objects.count(), 5)

    def test_it_refuses_a_module_the_spine_does_not_have(self):
        pack = load_pack()
        pack['module'] = 'NOPE'
        with self.assertRaises(PackImportError) as ctx:
            content_pack.import_pack(pack)
        self.assertIn('do not create', str(ctx.exception))

    def test_an_invalid_pack_writes_nothing(self):
        from apps.assessments.models import Assessment
        from apps.learning.models import Lesson, Topic

        pack = load_pack()
        pack['items'][1]['total_marks'] = 999
        with self.assertRaises(ValidationError):
            content_pack.import_pack(pack)
        self.assertEqual(Topic.objects.count(), 0)
        self.assertEqual(Lesson.objects.count(), 0)
        self.assertEqual(Assessment.objects.count(), 0)

    def test_the_solution_is_withheld_until_submission(self):
        from apps.assessments.models import Assessment
        result = content_pack.import_pack(load_pack())
        paper = Assessment.objects.get(topic=result.topic)
        self.assertEqual(paper.release_solution, Assessment.RELEASE_ON_SUBMIT)

    def test_a_second_topic_does_not_disturb_the_first(self):
        content_pack.import_pack(load_pack())
        other = load_pack()
        other['topic'] = {'code': 'T2', 'title': 'Sample topic two'}
        for item in other['items']:
            item['source_ref'] = item['source_ref'].replace('/T1/', '/T2/')
        content_pack.import_pack(other)

        from apps.assessments.models import Assessment
        from apps.learning.models import Topic
        self.assertEqual(Topic.objects.count(), 2)
        self.assertEqual(Assessment.objects.count(), 2)
