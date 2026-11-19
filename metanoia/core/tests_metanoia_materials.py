"""The 2026 seed and its parsers, run against the real archive.

The archive lives in the repository's working tree (``Metanoia Material/``), so the
seed test is skipped where it is absent — a CI checkout without the 300 MB of
coaching documents still runs the parser tests on their own fixtures.
"""

import os
import shutil
import tempfile
from unittest import skipUnless

from django.conf import settings
from django.test import TestCase, override_settings

from core.metanoia_materials import catalogue, dates, markgrid, textconv

ARCHIVE = os.path.join(settings.BASE_DIR, 'Metanoia Material')
HAS_ARCHIVE = os.path.isdir(ARCHIVE)


class TextConversionTests(TestCase):
    """:mod:`core.metanoia_materials.textconv`: sections, callouts, script stripping and "required" lines."""
    def test_sections_split_on_markdown_and_word_headings(self):
        text = ('# Title\n\nIntro words ' + 'w ' * 300 + '\n\n'
                '|  | **SECTION 1   ****The four words** |\n| --- | --- |\n\n' + 'body ' * 300 + '\n\n'
                '## 2. Second\n\n' + 'more ' * 50)
        sections = textconv.sections_from_markdown(text)
        self.assertEqual([s['heading'] for s in sections], ['Overview', '1. The four words', '2. Second'])
        self.assertTrue(all(s['minutes'] >= 1 for s in sections))

    def test_one_cell_table_becomes_callout(self):
        html = textconv.sections_from_markdown('| **WHY** it matters |\n| --- |\n\n' + 'x ' * 20)[0]['html']
        self.assertIn('lesson-callout', html)
        self.assertNotIn('<table', html)

    def test_scripts_are_stripped(self):
        html = textconv.sections_from_markdown('<script>alert(1)</script>\n\n' + 'safe ' * 10)[0]['html']
        self.assertNotIn('<script', html)

    def test_required_from_word_table_and_pdf_text(self):
        table = ('| **REQUIRED** |\n| --- |\n| **Q** | **Requirement** | **Marks** |\n'
                 '| **(a)(i)** | Describe risks. | **6** |\n| **(a)(ii)** | Describe responses. | **6** |\n'
                 '| **(b)** | Discuss the CPC. | **12** |\n|  | **TOTAL MARKS** | **24** |\n')
        found = textconv.required_by_letter(table)
        self.assertIn('Describe risks', found['a'])
        self.assertIn('Describe responses', found['a'])
        self.assertEqual(found['b'], 'Discuss the CPC.')
        pdf = 'REQUIRED\nRequirement Marks\n(a)\nCalculate the WACC.\n20\n(b) Advise the board. 15\nTOTAL MARKS 35'
        found = textconv.required_by_letter(pdf)
        self.assertEqual(found, {'a': 'Calculate the WACC.', 'b': 'Advise the board.'})


class MarkGridTests(TestCase):
    """:mod:`core.metanoia_materials.markgrid`: marking-sheet rubrics."""
    def test_sheet_with_group_rows_and_total_named_row(self):
        """Group rows are skipped and "MAXIMUM MARKS" is the total.

        "Total fees …" is an answer point, not a total row, so the rubric keeps two
        points.
        """
        rows = [('SUGGESTED SOLUTION — (a) SAICA CPC  |  MAX 6 MARKS',),
                ('#', 'Answer point', 'Marks'),
                (None, 'FINANCIAL INTEREST', None),
                ('1', 'Holds shares — self-interest threat.', 2),
                ('2', 'Total fees are 18% of firm income — fee dependency.', 2),
                (None, 'MAXIMUM MARKS', 6)]
        part = markgrid.parse_sheet('Part (a)', rows)
        self.assertEqual(part['ref'], 'a')
        self.assertEqual(len(part['rubric']), 2)
        self.assertEqual(part['stated_max'], 6)

    def test_rows_scale_down_to_the_maximum_never_up(self):
        rubric = [{'point': 'x', 'marks': 2}, {'point': 'y', 'marks': 2}, {'point': 'z', 'marks': 2}]
        capped = markgrid._cap_rubric(rubric, markgrid.Decimal('4'))
        self.assertEqual(sum(p['marks'] for p in capped), 4)


class CatalogueTests(TestCase):
    """File-name normalisers and the date tables of :mod:`core.metanoia_materials.catalogue` / ``dates``."""
    def test_normalisers(self):
        self.assertEqual(catalogue.doc_stem('X_Guide_v2.pdf.md'), 'X_Guide_v2')
        self.assertEqual(catalogue.doc_stem('X_Guide_v2_TEXT.md'), 'X_Guide_v2')
        self.assertEqual(catalogue.norm_assessment('', 'CGAU_B26S_T2_Mock', '', 'BCTA'), ('test', 2))
        self.assertEqual(catalogue.norm_assessment('Final Exam', '', '', 'PGDA'), ('exam', 1))
        self.assertEqual(catalogue.norm_week('', 'CGAU_B26S_Week1_Topic3'), 1)
        self.assertEqual(catalogue.norm_module('', '', 'TAXA_P26S_T1_Guide'), 'TAX')
        self.assertEqual(catalogue.md_text_kind('**Status:** ORIGINAL_NOT_IN_ZIP\n## Retrieval\n'), 'card')

    def test_session_rows_move_to_july_intake(self):
        rows = ('| 26 Mar 2026 | Thursday | MP BCTA B26F | 18:00 | 20:00 | 2h |\n'
                '| 06 Aug 2026 | Thursday | MP BCTA B26F | 18:00 | 20:00 | 2h |\n')
        sessions = dates.coaching_sessions(rows)
        self.assertEqual([s[2] for s in sessions], ['B26F', 'B26S'])

    def test_master_calendar_overrides(self):
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False) as fh:
            fh.write('| Date | Event |\n|---|---|\n| 12 Nov 2026 | BCTA B26S CGAU Final exam |\n')
        try:
            self.assertEqual(dates.load_master_calendar(fh.name), 1)
            on, source, confirmed = dates.assessment_date('BCTA', 'B26S', 'CGAU', 'exam', 1)
            self.assertEqual(str(on), '2026-11-12')
            self.assertTrue(confirmed)
        finally:
            dates.CONFIRMED.pop(('BCTA', 'B26S', 'CGAU', 'exam', 1), None)
            os.unlink(fh.name)


def _archive_structure():
    """The institutions, programmes and modules the archive seeder places material on.

    The seeder (:mod:`core.metanoia_materials.seed`) is written for a fixed set
    of codes, which the shipped sample spine does not carry, so the test builds
    them itself.
    """
    from apps.learning.models import Institution, Module, Programme, ProgrammeModule

    milpark, _ = Institution.objects.get_or_create(code='MILPARK', defaults={'name': 'MILPARK'})
    Institution.objects.get_or_create(code='SAICA', defaults={'name': 'SAICA'})
    for canonical in ('FR', 'TX', 'MA', 'GA'):
        Module.objects.get_or_create(code=canonical, defaults={'name': canonical})
    for code in ('PGDA', 'BCTA'):
        programme, _ = Programme.objects.get_or_create(institution=milpark, code=code, defaults={'name': code})
        for own, canonical in (('FREP', 'FR'), ('TAX', 'TX'), ('MACF', 'MA'), ('CGAU', 'GA')):
            ProgrammeModule.objects.get_or_create(programme=programme, code=own,
                                                  defaults={'module': Module.objects.get(code=canonical)})


@skipUnless(HAS_ARCHIVE, 'Metanoia Material archive not present')
class SeedTests(TestCase):
    """The whole archive, seeded into an empty spine, twice."""

    @classmethod
    def setUpClass(cls):
        """Create a temporary file store for the seeded material."""
        super().setUpClass()
        cls.files_dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        """Remove the temporary file store."""
        shutil.rmtree(cls.files_dir, ignore_errors=True)
        super().tearDownClass()

    def test_seed_places_teaches_and_is_idempotent(self):
        """Seeding twice adds nothing the second time, and spot checks hold.

        * The CGAU B26S Week 1 challenge is a quiz, open, in week 1 of Test 1, with
          its solution in the same week (so it is gated on submission).
        * Real dates are used: CGAU B26S Test 1 sits on 17 August 2026.
        * Nothing staff-only is published.
        """
        from apps.assessments.models import Assessment
        from apps.learning.models import (CalendarEvent, Lesson, ModuleMaterial, ModulePhase)
        from core.metanoia_materials import seed

        storages = dict(settings.STORAGES)
        storages['files'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage',
                             'OPTIONS': {'location': self.files_dir, 'base_url': '/files/'}}
        manifest = os.path.join(self.files_dir, 'manifest.json')
        with override_settings(STORAGES=storages), \
                self.settings(BASE_DIR=settings.BASE_DIR):
            from django.core.files.storage import storages as registry
            registry._storages.pop('files', None)
            registry.backends = storages
            _archive_structure()
            seed.MANIFEST = manifest
            first = seed.Seeder(ARCHIVE).run()
            counts = {
                'materials': ModuleMaterial.objects.filter(source_ref__startswith='seed2026/').count(),
                'lessons': Lesson.objects.filter(slug__startswith='seed2026-').count(),
                'quizzes': Assessment.objects.filter(source_ref__startswith='seed2026/').count(),
                'events': CalendarEvent.objects.filter(description__contains='[seed2026]').count(),
            }
            seed.Seeder(ARCHIVE).run()
            again = {
                'materials': ModuleMaterial.objects.filter(source_ref__startswith='seed2026/').count(),
                'lessons': Lesson.objects.filter(slug__startswith='seed2026-').count(),
                'quizzes': Assessment.objects.filter(source_ref__startswith='seed2026/').count(),
                'events': CalendarEvent.objects.filter(description__contains='[seed2026]').count(),
            }
            registry._storages.pop('files', None)

        print('\nseed report:\n' + '\n'.join(first.lines()))
        print('counts:', counts)
        self.assertEqual(counts, again, 'a second run must not add rows')
        self.assertGreater(counts['materials'], 300)
        self.assertGreater(counts['lessons'], 20)
        self.assertEqual(counts['quizzes'], 5)

        for q in Assessment.objects.filter(source_ref__startswith='seed2026/'):
            print('quiz:', q.title, '|', q.status, q.total_marks, '|', q.module)
        quiz = Assessment.objects.get(source_ref__startswith='seed2026/quiz/',
                                      source_ref__contains='Topic1_Conduct')
        self.assertEqual(quiz.status, 'open')
        self.assertEqual(float(quiz.total_marks), 28.0)
        row = ModuleMaterial.objects.get(assessment=quiz)
        self.assertEqual(row.week.number, 1)
        self.assertEqual(row.phase.cohort.code, 'B26S')
        self.assertTrue(ModuleMaterial.objects.filter(week=row.week, kind='answers').exists())

        phase = ModulePhase.objects.get(programme_module=row.phase.programme_module,
                                        cohort__code='B26S', kind='test', sequence=1)
        self.assertEqual(str(phase.ends_on), '2026-08-17')

        self.assertFalse(ModuleMaterial.objects.filter(audience='staff', is_published=True).exists())
