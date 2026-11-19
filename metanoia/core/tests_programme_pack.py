"""Programme packs: the folder parser, the builder, the zip importer and the sample programme seed.

The sample seed runs against the neutral sample pack in
``fixtures/sample_programme`` (it ships with the repository). The importer is
tested on a small pack built here, with Word and Excel files made on the fly, so
it needs no fixture at all.
"""

import datetime as dt
import io
import os
import shutil
import tempfile
import zipfile
from unittest import mock, skipUnless
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.programme_pack import builder, codegen, docconv, scanner, schedule

User = get_user_model()
SAST = ZoneInfo('Africa/Johannesburg')
SAMPLE_PACK = os.path.join(settings.BASE_DIR, 'fixtures', 'sample_programme')


def _docx(path, title, parts):
    """Write a small Word handout at ``path``: title, a callout table, ``parts`` and a grid."""
    import docx
    document = docx.Document()
    document.add_paragraph('METANOIA GROUP')
    document.add_paragraph(title)
    document.add_paragraph('Candidate handout  |  Teaching weekend')
    callout = document.add_table(rows=1, cols=1)
    callout.rows[0].cells[0].text = 'Floor first\nYou need 50% in every module.'
    for heading, body in parts:
        document.add_heading(heading, level=1)
        document.add_paragraph(body)
        document.add_paragraph('A point to remember.', style='List Paragraph')
    grid = document.add_table(rows=2, cols=2)
    grid.rows[0].cells[0].text, grid.rows[0].cells[1].text = 'Step', 'What you do'
    grid.rows[1].cells[0].text, grid.rows[1].cells[1].text = 'Trigger', 'Read the required first.'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    document.save(path)


def _xlsx(path):
    """Write a one-cell Excel workbook at ``path``."""
    import openpyxl
    book = openpyxl.Workbook()
    book.active['A1'] = 'Mark yourself'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    book.save(path)


def make_pack(root):
    """A two-weekend, one-module pack with a tutor pack and a mock."""
    student = os.path.join(root, 'Demo', 'Demo_Student_Pack')
    tutor = os.path.join(root, 'Demo', 'Demo_Tutor_Pack')
    _docx(os.path.join(student, '0_Start_Here', '1_Candidate_Guide.docx'), 'Candidate guide',
          [('1. The exam', 'Three papers.'), ('2. How it runs', 'Weekends.')])
    unit1 = os.path.join(student, '1_Weekend_1_7-8_Nov', '1_FREP_Unit_1_Group_accounting_I')
    _docx(os.path.join(unit1, 'FREP_U1_2_Live_session_Handout.docx'), 'FREP Unit 1',
          [('1. The method', 'TFCC.'), ('3. Live question', 'Scenario.')])
    _xlsx(os.path.join(unit1, 'FREP_U1_3_Live_question_Workbook.xlsx'))
    _docx(os.path.join(unit1, 'FREP_U1_4_Topic_guide.docx'), 'FREP topic guide',
          [('1. Why this topic matters', 'Group accounting earns marks every sitting.'),
           ('2. The concept', 'Control.')])
    unit2 = os.path.join(student, '2_Weekend_2_14-15_Nov', '1_FREP_Unit_2_Assets')
    _docx(os.path.join(unit2, 'FREP_U2_4_Topic_guide.docx'), 'FREP topic guide 2',
          [('1. Why this topic matters', 'Assets.'), ('2. The concept', 'IAS 16.')])
    _docx(os.path.join(tutor, '1_Weekend_1_7-8_Nov', '1_FREP_Unit_1_Group_accounting_I',
                       'FREP_U1_0_Facilitator_pack.docx'), 'Facilitator pack',
          [('A. Unit snapshot', 'For tutors.'), ('B. Run sheet', 'Minutes.')])
    _docx(os.path.join(tutor, '7_Mocks', '1_Mini_Mock_1_28-29_Nov', '1_FREP', '1_Mock1_FREP_Paper.docx'),
          'Mock 1', [('Question 1', 'Scenario.'), ('Required', 'Do it.')])


def zip_of(folder):
    """An in-memory ZIP of ``folder``, paths relative to it, rewound for reading."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        for dirpath, _dirs, names in os.walk(folder):
            for name in names:
                full = os.path.join(dirpath, name)
                zf.write(full, os.path.relpath(full, folder))
    buffer.seek(0)
    return buffer


class FileStoreMixin:
    """Every file the builder copies goes to a throwaway folder."""

    @classmethod
    def setUpClass(cls):
        """Create the temporary ``files`` and media folders."""
        super().setUpClass()
        cls.files_dir = tempfile.mkdtemp(prefix='pack-files-')
        cls.media_dir = tempfile.mkdtemp(prefix='pack-media-')

    @classmethod
    def tearDownClass(cls):
        """Remove the temporary folders."""
        shutil.rmtree(cls.files_dir, ignore_errors=True)
        shutil.rmtree(cls.media_dir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        """Point the ``files`` and ``default`` storages at the temporary folders.

        Django's storage registry caches backends, so it is cleared and re-pointed
        by hand as well as overriding ``STORAGES``.
        """
        super().setUp()
        storages = dict(settings.STORAGES)
        storages['files'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage',
                             'OPTIONS': {'location': self.files_dir, 'base_url': '/files/'}}
        storages['default'] = {'BACKEND': 'django.core.files.storage.FileSystemStorage',
                               'OPTIONS': {'location': self.media_dir}}
        self._override = override_settings(STORAGES=storages, MEDIA_ROOT=self.media_dir)
        self._override.enable()
        from django.core.files.storage import storages as registry
        registry._storages.clear()
        registry.backends = storages

    def tearDown(self):
        """Restore the settings and the storage registry."""
        from django.core.files.storage import storages as registry
        self._override.disable()
        registry._storages.clear()
        registry.backends = settings.STORAGES
        super().tearDown()

    def user(self, email, role, **extra):
        """Create an onboarded user with ``role`` as their ``user_type``."""
        from apps.accounts.models import Person
        user = User.objects.create_user(username=email, email=email, password='x', **extra)
        person = Person.objects.get(user=user)
        person.user_type = role
        person.registered = True
        person.profile_status = True
        person.save()
        return User.objects.get(pk=user.pk)


class ScannerTests(TestCase):
    """Scanning a pack folder into a dated plan, and reading its Word documents."""
    def setUp(self):
        """Build the demo pack in a temporary folder."""
        self.root = tempfile.mkdtemp(prefix='pack-')
        make_pack(self.root)

    def tearDown(self):
        """Remove the demo pack."""
        shutil.rmtree(self.root, ignore_errors=True)

    def test_folders_become_dated_blocks_units_and_steps(self):
        """Folders become dated blocks, units and steps.

        The facilitator pack, from the tutor pack, appears as a step-0 staff file.
        """
        plan = scanner.scan(self.root, year=2026)
        self.assertEqual(plan['modules'], ['FREP'])
        self.assertEqual([(b['number'], b['starts'], b['ends']) for b in plan['blocks']],
                         [(1, '2026-11-07', '2026-11-08'), (2, '2026-11-14', '2026-11-15')])
        unit = plan['blocks'][0]['units'][0]
        self.assertEqual((unit['module'], unit['unit'], unit['title']), ('FREP', 1, 'Group accounting I'))
        steps = {(f['step'], f['audience']) for f in unit['files']}
        self.assertIn((2, 'students'), steps)
        self.assertIn((0, 'staff'), steps)
        self.assertEqual(plan['mocks'][0]['starts'], '2026-11-28')
        self.assertEqual(plan['mocks'][0]['files'][0]['group'], 'FREP')
        self.assertEqual([f['name'] for f in plan['onboarding']], ['1_Candidate_Guide.docx'])

    def test_dates_roll_into_the_next_year(self):
        start, end = scanner.parse_dates('3_Full_Mock_8-10_Jan', year=2026,
                                         after=dt.date(2026, 11, 7))
        self.assertEqual((start, end), (dt.date(2027, 1, 8), dt.date(2027, 1, 10)))

    def test_a_facilitator_file_is_never_a_student_file(self):
        path = os.path.join(self.root, 'Demo', 'Demo_Student_Pack', '1_Weekend_1_7-8_Nov',
                            '1_FREP_Unit_1_Group_accounting_I', 'FREP_U1_0_Facilitator_pack.docx')
        _docx(path, 'Oops', [('A', 'b c d')])
        plan = scanner.scan(self.root, year=2026)
        audience = {f['path']: f['audience'] for f in plan['blocks'][0]['units'][0]['files']}
        self.assertEqual(audience[os.path.relpath(path, self.root)], 'staff')

    def test_word_documents_read_in_order_with_callouts_and_tables(self):
        path = os.path.join(self.root, 'Demo', 'Demo_Student_Pack', '1_Weekend_1_7-8_Nov',
                            '1_FREP_Unit_1_Group_accounting_I', 'FREP_U1_4_Topic_guide.docx')
        doc = docconv.read_document(path)
        self.assertEqual(doc['title'], 'FREP topic guide')
        self.assertIn('lesson-callout', doc['intro_html'])
        self.assertEqual([s['heading'] for s in doc['sections']],
                         ['1. Why this topic matters', '2. The concept'])
        self.assertIn('<table', doc['sections'][-1]['html'])
        self.assertIn('<ul>', doc['sections'][0]['html'])


class ImporterTests(FileStoreMixin, TestCase):
    """Building a scanned pack into a cohort, updating it, and the upload screens."""
    def setUp(self):
        """Build the demo pack in a temporary folder."""
        super().setUp()
        self.root = tempfile.mkdtemp(prefix='pack-')
        make_pack(self.root)

    def tearDown(self):
        """Remove the demo pack."""
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def spine(self, plan):
        """A demo spine for ``plan``: one Saturday FREP slot and one exam date."""
        return schedule.spine_from_plan(
            plan, key='demo26', institution={'code': 'DEMOI', 'name': 'Demo Institute'},
            programme={'code': 'DEMO', 'name': 'Demo', 'full_name': 'Demo programme'},
            cohort={'code': 'D26', 'name': 'Demo 2026'},
            slots=schedule.parse_slots('FREP Sat 08:00 120'), exam_dates=['2027-01-26'])

    def test_deterministic_import_builds_once_and_reruns_cleanly(self):
        """A build is deterministic and a rerun changes nothing.

        Three weeks (two weekends and the mock), no Teams call made, and the access
        split holds: tutor files are staff-only rows that never publish.
        """
        from apps.communication.models import MeetingRoom
        from apps.learning.models import CalendarEvent, Lesson, ModuleMaterial, ModuleWeek

        plan = scanner.scan(self.root, year=2026)
        spine = self.spine(plan)
        made, _report = builder.build(spine, plan)
        first = made.totals()
        made_again, _ = builder.build(spine, plan)
        self.assertEqual(made_again.totals(), first)

        self.assertEqual(first['modules'], 1)
        self.assertEqual(first['weeks'], 3)
        self.assertEqual(first['sessions'], 2)
        weeks = list(ModuleWeek.objects.filter(phase__programme_module=made.offerings['FREP'])
                     .order_by('number').values_list('title', 'starts_on'))
        self.assertEqual(weeks[0][1], dt.date(2026, 11, 7))
        self.assertIn('Unit 1: Group accounting I', weeks[0][0])

        session = MeetingRoom.objects.get(external_event_id='seed:demo26:live-FREP-1')
        self.assertEqual(session.scheduled_start.astimezone(SAST), dt.datetime(2026, 11, 7, 8, 0, tzinfo=SAST))
        self.assertEqual(session.teams_join_url, '')
        exam = CalendarEvent.objects.get(description__contains='[demo26:exam-1]')
        self.assertEqual(exam.kind, 'exam')
        self.assertEqual(made.phases['FREP'].calendar_event, exam)

        lesson = Lesson.objects.get(slug='demo26-frep-u1')
        self.assertEqual([s.title for s in lesson.sections.order_by('order')],
                         ['Live session handout', 'Live question: suggested solution', 'Topic guide'])
        self.assertEqual(lesson.status, 'published')

        staff = ModuleMaterial.objects.filter(source_ref__startswith='demo26/', audience='staff')
        self.assertTrue(staff.exists())
        self.assertFalse(staff.filter(is_published=True).exists())
        self.assertTrue(all(m.file.name.startswith('modules/staff-only/') for m in staff if m.file))
        visible = ModuleMaterial.objects.filter(source_ref__startswith='demo26/', is_published=True)
        self.assertFalse([m for m in visible if 'Facilitator' in m.title or 'Mock1' in (m.file.name or '')])

    def test_a_new_version_of_the_pack_updates_a_live_cohort_in_place(self):
        """Version 2 of a pack updates a live cohort in place.

        The topic guide is rewritten, Start here is renumbered and gains a rundown,
        and the unit gets a one-pager. The same section is kept (with the
        student's progress); the renumbered guide ends as one row on the new file,
        the old row handed over; a second update changes nothing more.
        """
        from apps.learning.models import Lesson, LessonSectionProgress, ModuleMaterial

        plan = scanner.scan(self.root, year=2026)
        spine = self.spine(plan)
        builder.build(spine, plan)
        student = self.user('cand@example.com', 'student')
        lesson = Lesson.objects.get(slug='demo26-frep-u1')
        topic = lesson.sections.get(title='Topic guide')
        LessonSectionProgress.objects.create(section=topic, student=student, status='completed')
        guide_ref = 'demo26/D26/GEN/file/Demo_Student_Pack/0_Start_Here/1_Candidate_Guide.docx'
        self.assertTrue(ModuleMaterial.objects.filter(source_ref__endswith='1_Candidate_Guide.docx').exists())

        student_pack = os.path.join(self.root, 'Demo', 'Demo_Student_Pack')
        unit1 = os.path.join(student_pack, '1_Weekend_1_7-8_Nov', '1_FREP_Unit_1_Group_accounting_I')
        _docx(os.path.join(unit1, 'FREP_U1_4_Topic_guide.docx'), 'FREP topic guide',
              [('1. Why this topic matters', 'Corrected: control decides consolidation.'),
               ('2. The concept', 'Control.')])
        _docx(os.path.join(unit1, 'FREP_U1_8_IRR_one_pager.docx'), 'One pager',
              [('1. The method', 'Interpolate.'), ('2. Traps', 'Signs.')])
        start = os.path.join(student_pack, '0_Start_Here')
        os.rename(os.path.join(start, '1_Candidate_Guide.docx'), os.path.join(start, '2_Candidate_Guide.docx'))
        _docx(os.path.join(start, '0_Programme_Rundown.docx'), 'Rundown',
              [('1. The programme', 'Six weekends.'), ('2. The mocks', 'Three.')])

        plan2 = scanner.scan(self.root, year=2026)
        made, report = builder.build(self.spine(plan2), plan2, update=True)

        lesson.refresh_from_db()
        topic.refresh_from_db()
        self.assertIn('Corrected: control decides consolidation.',
                      ''.join(b.data.get('html', '') for b in topic.blocks.all()))
        self.assertTrue(LessonSectionProgress.objects.filter(section=topic, student=student,
                                                             status='completed').exists())
        self.assertIn('One-pager', [s.title for s in lesson.sections.order_by('order')])
        guides = ModuleMaterial.objects.filter(source_ref__startswith='demo26/', title__icontains='Candidate Guide')
        self.assertEqual(guides.filter(lesson__isnull=False).count(), 1)
        guides = guides.exclude(file='')
        self.assertEqual(guides.count(), 1)
        self.assertTrue(guides.get().source_ref.endswith('2_Candidate_Guide.docx'))
        self.assertFalse(ModuleMaterial.objects.filter(source_ref=guide_ref).exists())
        self.assertTrue(ModuleMaterial.objects.filter(source_ref__endswith='0_Programme_Rundown.docx').exists())
        self.assertGreaterEqual(report.created['files replaced (new version)'], 1)

        plan3 = scanner.scan(self.root, year=2026)
        made_again, _ = builder.build(self.spine(plan3), plan3, update=True)
        self.assertEqual(made_again.totals(), made.totals())

    def test_tutor_files_are_served_to_tutors_only(self):
        """Staff-only pack files are private to tutors; student pack files are course
        content, gated per row (:mod:`apps.learning.content_files`).
        """
        from core import media_access

        student = self.user('s@demo.test', 'student')
        educator = self.user('e@demo.test', 'educator')
        name = 'modules/staff-only/demo26/abc-FREP_U1_0_Facilitator_pack.docx'
        self.assertEqual(media_access.classify(name), 'private')
        self.assertFalse(media_access._may_read_private(student, name))
        self.assertTrue(media_access._may_read_private(educator, name))
        self.assertEqual(media_access.classify('modules/demo26/abc-FREP_U1_4_Topic_guide.docx'), 'course')

    def test_the_seed_file_round_trips(self):
        plan = scanner.scan(self.root, year=2026)
        spine = self.spine(plan)
        source = codegen.render(spine, plan_summary=scanner.summary(plan))
        path = os.path.join(self.root, 'academic_spine_demo26.py')
        with open(path, 'w') as fh:
            fh.write(source)
        self.assertEqual(codegen.load(path), spine)

    def test_upload_page_loads_a_zip_into_an_existing_cohort(self):
        """The upload screen loads a ZIP into an existing cohort.

        Nothing is created at the programme level, and the schedule sheet is read
        but never published as material.
        """
        from apps.learning import programme_pack_views as views
        from apps.learning.models import Cohort, Institution, Programme, ProgrammeModule
        from core.testing import make_module, make_programme

        programme = make_programme('DEMOI', 'DEMO')
        make_module('Financial reporting', 'FREP', programme=programme, price=500)
        cohort = Cohort.objects.create(programme=programme, code='D26')
        with open(os.path.join(self.root, 'Demo', 'Demo_Student_Pack', '0_Start_Here', 'schedule.csv'), 'w') as fh:
            fh.write('type,module,number,date,end,time,minutes,kind,title,details\n'
                     'slot,FREP,,Sat,,08:00,120,,,\n'
                     'date,,,2027-01-26,,,,exam,Examination · Paper 1,\n')
        counts = (Institution.objects.count(), Programme.objects.count(), Cohort.objects.count())
        offerings = set(ProgrammeModule.objects.without_general().values_list('pk', flat=True))

        admin = self.user('admin@demo.test', 'admin')
        self.client.force_login(admin)
        archive = zip_of(self.root)
        archive.name = 'demo.zip'
        with mock.patch.object(views.threading, 'Thread') as thread:
            response = self.client.post(reverse('learning:programme-pack'),
                                        {'archive': archive, 'cohort': cohort.pk, 'year': '2026'})
            self.assertEqual(response.status_code, 302)
            token = response['Location'].rstrip('/').rsplit('/', 1)[-1]
            page = self.client.get(response['Location'])
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, 'FREP → FREP')
            state = views._load(token)
            self.assertEqual(state['sheet']['source'], 'pack')
            self.client.post(reverse('learning:programme-pack-build', args=[token]))
            self.assertTrue(thread.called)
            status = self.client.get(reverse('learning:programme-pack-status', args=[token])).json()
            self.assertEqual(status['build'], 'running')
            self.assertContains(self.client.get(reverse('learning:programme-pack-review', args=[token])),
                                'Build progress')
        views._run_build(token, admin.pk, close_connection=False)
        state = views._load(token)
        self.assertEqual(state['build']['status'], 'built', state['build'])
        status = self.client.get(reverse('learning:programme-pack-status', args=[token])).json()
        self.assertEqual(status['build'], 'built')
        self.assertEqual(state['build']['totals']['sessions'], 2)
        self.assertEqual((Institution.objects.count(), Programme.objects.count(), Cohort.objects.count()), counts)
        self.assertEqual(set(ProgrammeModule.objects.without_general().values_list('pk', flat=True)), offerings)
        from apps.learning.models import ModuleMaterial
        self.assertFalse(ModuleMaterial.objects.filter(title__icontains='schedule').exists())
        csv_file = self.client.get(reverse('learning:programme-pack-sheet-file', args=[token]))
        self.assertIn(b'slot,FREP', csv_file.content)

    def test_upload_refuses_a_pack_whose_module_the_programme_lacks(self):
        from apps.learning import programme_pack_views as views
        from apps.learning.models import Cohort
        from core.testing import make_module, make_programme

        programme = make_programme('DEMOI', 'OTHER')
        make_module('Taxation', 'TAXX', programme=programme)
        cohort = Cohort.objects.create(programme=programme, code='O26')
        self.client.force_login(self.user('admin2@demo.test', 'admin'))
        archive = zip_of(self.root)
        archive.name = 'demo.zip'
        response = self.client.post(reverse('learning:programme-pack'), {'archive': archive, 'cohort': cohort.pk})
        token = response['Location'].rstrip('/').rsplit('/', 1)[-1]
        page = self.client.get(response['Location'])
        self.assertContains(page, 'FREP → no module')
        self.client.post(reverse('learning:programme-pack-build', args=[token]))
        self.assertEqual(views._load(token)['build']['status'], 'not started')

    def test_the_importer_is_for_administrators(self):
        self.client.force_login(self.user('st@demo.test', 'student'))
        self.assertEqual(self.client.get(reverse('learning:programme-pack')).status_code, 403)

    def test_unsafe_zip_paths_are_refused(self):
        from apps.learning.programme_pack_views import safe_extract
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as zf:
            zf.writestr('../escape.txt', 'x')
        buffer.seek(0)
        target = tempfile.mkdtemp(prefix='unzip-')
        try:
            with self.assertRaises(ValueError):
                safe_extract(buffer, target)
        finally:
            shutil.rmtree(target, ignore_errors=True)


class SingleIntakeTests(TestCase):
    """Registration on a programme with a single running intake."""
    def test_registration_joins_the_only_running_intake(self):
        from apps.accounts.models import Person
        from apps.learning import enrolment
        from apps.learning.models import Cohort
        from core.testing import make_module, make_programme

        programme = make_programme('METANOIA', 'SAMPLE')
        offering = make_module('Introduction', 'INTRO', programme=programme)
        cohort = Cohort.objects.create(programme=programme, code='PACK2027', name='Sample pack intake 2027')
        user = User.objects.create_user(username='c@x.test', email='c@x.test', password='x')
        person = Person.objects.get(user=user)
        enrolment.register_modules(person, programme, [offering.pk])
        self.assertEqual(person.programme_enrolments.get(programme=programme).cohort, cohort)


@skipUnless(os.path.isdir(SAMPLE_PACK), 'fixtures/sample_programme is not in this checkout')
class SampleProgrammeSeedTests(FileStoreMixin, TestCase):
    """``manage.py seed_sample_programme`` on an empty database, twice."""

    def test_seed_is_complete_split_dated_and_idempotent(self):
        """The sample programme seed is complete, correctly split, dated and idempotent.

        There are 3 × 4 module weeks plus two on General: the mock week (3) and the
        debrief week (4), where the all-module debrief sits. Onboarding and
        programme-wide material sit once, on General, nowhere else, as does the
        mock debrief (no module of its own) and the whole-sitting marking sheet.
        Module-specific items — units and each module's own mock paper — stay put,
        and no item sits on two modules. Tutor files are only ever for staff.
        """
        from django.core.management import call_command

        from apps.communication.models import MeetingRoom
        from apps.learning.models import CalendarEvent, Cohort, Lesson, ModuleMaterial, Programme
        from core import academic_spine_sample as sample
        from core.programme_pack import programme_totals

        call_command('seed_sample_programme', stdout=io.StringIO())
        programme = Programme.objects.get(institution__code='METANOIA', code='SAMPLE')
        cohort = Cohort.objects.get(programme=programme, code='PACK2027')
        first = programme_totals(programme, cohort, sample.KEY)
        call_command('seed_sample_programme', stdout=io.StringIO())
        self.assertEqual(programme_totals(programme, cohort, sample.KEY), first)

        self.assertEqual(cohort.name, 'Sample pack intake 2027')
        self.assertEqual(sorted(programme.modules.without_general().values_list('code', flat=True)),
                         ['CORE', 'INTRO', 'PRACT'])
        self.assertEqual({str(p) for p in programme.modules.without_general()
                          .values_list('price_per_month', flat=True)}, {'500.00'})
        self.assertEqual(first['weeks'], 3 * 4 + 2)
        self.assertEqual(first['general'], 1)

        general = programme.modules.general().get()
        self.assertEqual(programme.modules.order_by('order', 'pk').first(), general)
        self.assertEqual(general.price_per_month, 0)
        on_general = ModuleMaterial.objects.filter(phase__programme_module=general)
        elsewhere = ModuleMaterial.objects.filter(phase__programme_module__programme=programme).exclude(
            phase__programme_module=general)
        for needle in ('0_Start_Here', '7_Programme_Overview', 'Marking_Sheet'):
            with self.subTest(file=needle):
                self.assertTrue(on_general.filter(source_ref__icontains=needle).exists())
                self.assertFalse(elsewhere.filter(source_ref__icontains=needle).exists())
        for title in ('Start here · How to use this pack', 'Mini Mock 1 · how it runs'):
            with self.subTest(title=title):
                self.assertEqual(on_general.filter(title=title).count(), 1)
                self.assertFalse(elsewhere.filter(title=title).exists())
        self.assertEqual(on_general.get(title='Mini Mock 1 · how it runs').week.number, 3)
        self.assertEqual(on_general.filter(kind='live').count(), 1)
        self.assertTrue(elsewhere.filter(source_ref__contains='/INTRO/U1/lesson').exists())
        self.assertTrue(elsewhere.filter(phase__programme_module__code='CORE',
                                         file__icontains='Mock1_CORE').exists())
        from collections import Counter
        placed = Counter((m.lesson_id, m.meeting_id, m.file.name or '') for m in
                         ModuleMaterial.objects.filter(phase__programme_module__programme=programme))
        self.assertEqual([k for k, n in placed.items() if n > 1], [])
        call_command('organise_general_modules', '--programme', 'SAMPLE', stdout=(out := io.StringIO()))
        self.assertIn('items moved: 0 · duplicates deleted: 0', out.getvalue())
        self.assertEqual(first['sessions'], 7)
        self.assertEqual(first['events'], len(sample.SPINE['events']))
        self.assertEqual(Lesson.objects.filter(slug__startswith='sample-', status='published',
                                               module__isnull=False).count(), 6)

        materials = ModuleMaterial.objects.filter(source_ref__startswith='sample/')
        tutor = materials.filter(file__startswith='modules/staff-only/')
        self.assertTrue(tutor.exists())
        self.assertFalse(tutor.exclude(audience='staff').exists())
        self.assertFalse(materials.filter(audience='staff', is_published=True).exists())
        self.assertFalse(materials.filter(audience='students', title__icontains='facilitator').exists())
        self.assertFalse(materials.filter(audience='students', file__icontains='Marking_Sheet').exists())
        self.assertFalse(materials.filter(audience='students', is_published=False).exists())

        def local(ref):
            """The SAST start of the calendar event tagged ``[sample:<ref>]``."""
            return CalendarEvent.objects.get(description__contains=f'[sample:{ref}]').start.astimezone(SAST)

        self.assertEqual(local('programme-start').date(), dt.date(2027, 2, 6))
        self.assertEqual(local('mm1-INTRO'), dt.datetime(2027, 2, 20, 9, 0, tzinfo=SAST))
        self.assertEqual(local('mm1-PRACT'), dt.datetime(2027, 2, 21, 9, 0, tzinfo=SAST))
        self.assertEqual(local('exam-1'), dt.datetime(2027, 3, 2, 9, 0, tzinfo=SAST))

        first_live = MeetingRoom.objects.get(external_event_id='seed:sample:live-PRACT-1')
        self.assertEqual(first_live.scheduled_start.astimezone(SAST), dt.datetime(2027, 2, 7, 9, 0, tzinfo=SAST))
        self.assertEqual(first_live.week.number, 1)
        self.assertEqual(first_live.cohort, cohort)
        self.assertEqual(MeetingRoom.objects.get(external_event_id='seed:sample:mock1-debrief').session_kind,
                         'test_review')
