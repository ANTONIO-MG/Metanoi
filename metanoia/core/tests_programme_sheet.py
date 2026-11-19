"""The schedule sheet, the existing-only import, the folder command and the AI draft."""
import os
import shutil
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from core.programme_pack import ai, builder, intake, scanner, schedule, sheet
from core.tests_programme_pack import SAMPLE_PACK, FileStoreMixin, make_pack


class SheetTests(TestCase):
    """Parsing the schedule sheet (CSV and Excel)."""
    def test_parses_every_type_and_reports_bad_lines(self):
        rows, problems = sheet.parse_text(
            'Type,Module,Number,Date,End,Time,Minutes,Kind,Title,Details\n'
            'slot,FREP,,Sat,,08:00,120,,,\n'
            'session,,,2027-01-16,,10:00,90,revision,Full mock debrief,Bring the paper\n'
            'date,,,2027-01-26,,,,exam,Sample paper 1,\n'
            'unit,FREP,1,,,,,,Group accounting I,IFRS 5\n'
            'week,,10,2027-01-23,2027-01-28,,,extra,Final Q&A,\n'
            'mock_paper,MACF+CGAU,1,,,,,,,\n'
            'host,,,,,,,,,coach@example.com\n'
            'phase,,3,2027-07-01,2027-08-03,,,test,Test 3,\n'
            '# a comment row,,,,,,,,,\n'
            'slot,FREP,,Someday,,08:00,,,,\n'
            'date,,,26 Jan,,,,exam,Bad date,\n'
            'session,,,2027-01-16,,10:00,,lecture,Wrong kind,\n')
        self.assertEqual([r['type'] for r in rows],
                         ['slot', 'session', 'date', 'unit', 'week', 'mock_paper', 'host', 'phase'])
        self.assertEqual(len(problems), 3)
        self.assertTrue(all(p.startswith('Line ') for p in problems))
        self.assertEqual(sheet.slots(rows), {'FREP': ('saturday', '08:00', 120)})
        self.assertEqual(sheet.phase(rows)['kind'], 'test')

    def test_a_header_is_required(self):
        rows, problems = sheet.parse_text('slot,FREP,,Sat,,08:00,120,,,\n')
        self.assertEqual(rows, [])
        self.assertIn('header', problems[0])

    def test_excel_sheet_reads_like_csv(self):
        import openpyxl
        folder = tempfile.mkdtemp()
        try:
            book = openpyxl.Workbook()
            book.active.append(sheet.COLUMNS)
            book.active.append(['slot', 'TAX', '', 'Sun', '', '11:00', 120])
            path = os.path.join(folder, 'schedule.xlsx')
            book.save(path)
            rows, problems = sheet.read_file(path)
            self.assertEqual(problems, [])
            self.assertEqual(sheet.slots(rows), {'TAX': ('sunday', '11:00', 120)})
        finally:
            shutil.rmtree(folder)


class SampleParityTests(TestCase):
    """Folders + the sheet rebuild the hand-written sample spine: same weeks, sessions and dates."""

    def test_the_sample_pack_and_its_sheet_match_the_hand_written_spine(self):
        from core import academic_spine_sample as hand
        rows, problems = sheet.parse_text(sheet.to_csv(sheet.from_spine(hand.SPINE)))
        self.assertEqual(problems, [])
        plan = scanner.scan(SAMPLE_PACK, year=hand.YEAR)
        spine = schedule.spine_from_plan(plan, key='sample', institution={'code': 'METANOIA'},
                                         programme={'code': 'SAMPLE', 'name': 'Sample Programme'},
                                         cohort={'code': 'PACK2027'}, sheet_rows=rows)
        H = hand.SPINE
        weeks = lambda ws: [(w['number'], w['title'], w.get('starts'), w.get('ends')) for w in ws]  # noqa: E731
        self.assertEqual(weeks(spine['weeks']), weeks(H['weeks']))
        sessions = lambda ss: sorted((s['start'], s.get('module') or '', s['title'], s.get('kind'),  # noqa: E731
                                      s.get('minutes')) for s in ss)
        self.assertEqual(sessions(spine['sessions']), sessions(H['sessions']))
        events = lambda es: sorted((e['kind'], str(e['start'])[:10], e['title']) for e in es)  # noqa: E731
        self.assertEqual(events(spine['events']), events(H['events']))
        self.assertEqual(spine['mock_papers'], H['mock_papers'])
        self.assertEqual(spine['host_email'], H['host_email'])
        self.assertEqual({c: {int(n): u for n, u in us.items()} for c, us in spine['units'].items()},
                         {c: {int(n): u for n, u in us.items()} for c, us in H['units'].items()})

    def test_a_sheet_in_the_pack_is_found_and_never_listed_as_material(self):
        folder = tempfile.mkdtemp()
        try:
            make_pack(folder)
            start = os.path.join(folder, 'Demo', 'Demo_Student_Pack', '0_Start_Here')
            with open(os.path.join(start, 'schedule.csv'), 'w') as fh:
                fh.write(','.join(sheet.COLUMNS) + '\nslot,FREP,,Sat,,08:00,120,,,\n')
            plan = scanner.scan(folder, year=2026)
            self.assertTrue(plan['schedule_sheet'].endswith('schedule.csv'))
            names = [f['name'] for b in ('onboarding', 'strategy', 'other') for f in plan[b]]
            self.assertNotIn('schedule.csv', names)
            rows, problems, _text = intake.sheet_for(plan)
            self.assertEqual((len(rows), problems), (1, []))
        finally:
            shutil.rmtree(folder)


class ExistingOnlyTests(FileStoreMixin, TestCase):
    """Importing a pack into an existing cohort: the build, its progress bar, the
    ``import_programme_folder`` command and the AI-drafted sheet.
    """
    def setUp(self):
        """Build the demo pack and a PGDA programme with a FREP offering and a P27F intake."""
        super().setUp()
        from apps.learning.models import Cohort
        from core.testing import make_module, make_programme
        self.root = tempfile.mkdtemp(prefix='pack-')
        make_pack(self.root)
        self.programme = make_programme('MILPARK', 'PGDA')
        self.frep = make_module('Financial reporting', 'FREP', programme=self.programme, price=500)
        self.cohort = Cohort.objects.create(programme=self.programme, code='P27F')
        self.plan = scanner.scan(self.root, year=2026)

    def tearDown(self):
        """Remove the demo pack."""
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def test_modules_map_by_code_then_by_subject(self):
        from apps.learning.models import Module, ProgrammeModule
        tx, _ = Module.objects.get_or_create(code='TX', defaults={'name': 'Taxation'})
        taxa = ProgrammeModule.objects.create(programme=self.programme, code='TAXA', module=tx)
        mapped, missing = builder.map_modules(self.programme, ['FREP', 'TAX', 'XYZ'])
        self.assertEqual(mapped, {'FREP': self.frep, 'TAX': taxa})
        self.assertEqual(missing, ['XYZ'])

    def test_build_into_a_cohort_creates_nothing_programme_level_and_reruns_cleanly(self):
        """A build into a cohort creates nothing at programme level.

        The same block again keeps its key and updates in place.
        """
        from apps.learning.models import Cohort, ModuleMaterial, ModuleWeek, Programme, ProgrammeModule
        rows, _ = sheet.parse_text(','.join(sheet.COLUMNS) + '\nslot,FREP,,Sat,,08:00,120,,,\n'
                                   'phase,,3,2026-11-07,2026-11-20,,,test,Test 3,\n')
        before = (Programme.objects.count(), Cohort.objects.count(),
                  ProgrammeModule.objects.without_general().count())
        key = intake.check(self.plan, self.cohort, rows)['key']
        intake.build_into(self.plan, self.cohort, rows, key)
        self.assertEqual((Programme.objects.count(), Cohort.objects.count(),
                          ProgrammeModule.objects.without_general().count()), before)
        weeks = ModuleWeek.objects.filter(phase__programme_module=self.frep, phase__cohort=self.cohort)
        self.assertEqual(weeks.count(), len(self.plan['blocks']) + len(self.plan['mocks']))
        self.assertEqual(weeks.first().phase.kind, 'test')
        self.assertEqual(weeks.first().phase.sequence, 3)
        materials = ModuleMaterial.objects.filter(phase__cohort=self.cohort).count()
        self.assertEqual(builder.pack_key(self.cohort, sheet.phase(rows)), key)
        intake.build_into(self.plan, self.cohort, rows, key)
        self.assertEqual(ModuleMaterial.objects.filter(phase__cohort=self.cohort).count(), materials)

    def test_a_missing_module_stops_the_build(self):
        from core.testing import make_programme
        from apps.learning.models import Cohort
        other = make_programme('UNISA', 'UG')
        cohort = Cohort.objects.create(programme=other, code='2027')
        found = intake.check(self.plan, cohort, [])
        self.assertEqual(found['missing'], ['FREP'])
        with self.assertRaises(builder.BuildError):
            intake.build_into(self.plan, cohort, [], found['key'])

    def test_two_blocks_on_one_cohort_get_their_own_keys(self):
        test1 = builder.pack_key(self.cohort, {'kind': 'test', 'sequence': 1})
        test3 = builder.pack_key(self.cohort, {'kind': 'test', 'sequence': 3})
        self.assertNotEqual(test1, test3)
        self.assertEqual(builder.topic_code({'kind': 'test', 'sequence': 3}, 1), 'T3-U1')
        self.assertEqual(builder.topic_code({'kind': 'exam', 'sequence': 1}, 1), 'U1')

    def test_progress_runs_from_the_first_step_to_done(self):
        """The build's progress (the review page's bar) rises monotonically to ``Done``."""
        seen = []
        intake.build_into(self.plan, self.cohort, [], intake.check(self.plan, self.cohort, [])['key'],
                          progress=lambda done, total, label: seen.append((done, total, label)))
        self.assertTrue(seen)
        done = [d for d, _t, _l in seen]
        self.assertEqual(done, sorted(done))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual(seen[-1][2], 'Done')
        self.assertTrue(any(label.startswith('Loading FREP unit') for _d, _t, label in seen))

    def test_command_dry_run_writes_nothing_then_apply_loads(self):
        from apps.learning.models import ModuleMaterial
        out = StringIO()
        call_command('import_programme_folder', self.root, '--cohort', 'MILPARK-PGDA/P27F', '--year', '2026',
                     stdout=out)
        self.assertIn('Dry run', out.getvalue())
        self.assertIn('FREP → FREP', out.getvalue())
        self.assertFalse(ModuleMaterial.objects.filter(phase__cohort=self.cohort).exists())
        call_command('import_programme_folder', self.root, '--cohort', str(self.cohort.pk), '--apply',
                     stdout=StringIO())
        self.assertTrue(ModuleMaterial.objects.filter(phase__cohort=self.cohort).exists())

    def test_command_never_creates_a_cohort(self):
        with self.assertRaises(CommandError):
            call_command('import_programme_folder', self.root, '--cohort', 'MILPARK-PGDA/NOPE', stdout=StringIO())

    def test_ai_draft_goes_through_the_same_checks(self):
        reply = {'rows': [
            {'type': 'slot', 'module': 'FREP', 'date': 'Sat', 'time': '08:00', 'minutes': '120'},
            {'type': 'date', 'date': '2027-01-26', 'kind': 'exam', 'title': 'Paper 1'},
            {'type': 'date', 'date': 'sometime', 'kind': 'exam', 'title': 'Invented'},
        ]}
        with mock.patch.object(ai, 'is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.generate.generate_from_prompt', return_value=reply) as call:
            rows, problems = ai.draft_sheet(self.plan)
        self.assertTrue(call.called)
        self.assertEqual([r['type'] for r in rows], ['slot', 'date'])
        self.assertEqual(len(problems), 1)

    def test_ai_draft_without_a_key_says_so(self):
        with mock.patch.object(ai, 'is_enabled', return_value=False):
            rows, problems = ai.draft_sheet(self.plan)
        self.assertEqual(rows, [])
        self.assertIn('not switched on', problems[0])

