"""The General module: one per programme, free and open, kept out of every count
and price, and the command that moves programme-wide material onto it.

The IAC seed's side of it (onboarding seeded once, on General) is asserted in
``core/tests_programme_pack.IacSeedTests``, which already pays for a full seed.
"""
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from apps.accounts.models import Person
from apps.learning import general_organise
from apps.learning.access import Gate
from apps.learning.general import general_module_for, general_offerings_for, general_phase_for, \
    is_general_content_path
from apps.learning.models import (GENERAL_CODE, Institution, Lesson, ModuleEnrolment, ModuleMaterial,
                                  ModulePhase, ModuleWeek, Programme, ProgrammeModule, StudySession)
from core.testing import enrol, make_module

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def make_user(email, role='student'):
    """Create a registered user with ``role``, re-read so ``user.profile`` is current.
    """
    user = User.objects.create_user(username=email, email=email, password='pw12345!')
    Person.objects.update_or_create(user=user, defaults={
        'user_type': role, 'registered': True, 'profile_status': True, 'first_name': 'T'})
    return User.objects.get(pk=user.pk)


def make_programme(code='GPROG'):
    """Create a programme ``code`` under a shared test institution."""
    institution, _ = Institution.objects.get_or_create(code='GINST', defaults={'name': 'G Institute'})
    return Programme.objects.create(institution=institution, code=code, name=code)


class GeneralModuleTests(TestCase):
    """Every programme's General module: one, first, free, open to all, and out of every
    count.
    """
    def test_every_programme_gets_exactly_one_general_first_and_free(self):
        """Asking again (or for a second programme) never makes a second one; General is
        shown first even when a module was already at order 0, and never priced,
        whatever someone types in.
        """
        programme = make_programme()
        frep = make_module('Financial reporting', 'GFREP', programme=programme, price=Decimal('750'))
        general = programme.modules.general().get()
        self.assertEqual((general.code, general.module.code, general.display_name), (GENERAL_CODE, 'GEN', 'General'))
        self.assertTrue(general.is_general)
        self.assertFalse(frep.is_general)
        self.assertEqual(general_module_for(programme), general)
        self.assertEqual(general_module_for(programme.pk), general)
        self.assertEqual(programme.modules.general().count(), 1)
        other = make_programme('GPROG2')
        self.assertEqual(ProgrammeModule.objects.general().count(), 2)
        self.assertNotEqual(general_module_for(other), general)
        frep.order = 0
        frep.save()
        ProgrammeModule.objects.filter(pk=general.pk).delete()
        recreated = general_module_for(programme)
        self.assertEqual(programme.modules.order_by('order', 'pk').first(), recreated)
        recreated.price_per_month = Decimal('750')
        recreated.save()
        recreated.refresh_from_db()
        self.assertEqual(recreated.price_per_month, 0)
        self.assertEqual(programme.taught_module_count, 1)

    def test_general_is_open_to_the_programme_without_payment_or_enrolment(self):
        """The paid module is shut but General is not (a dated week too), seen without
        an
        enrolment row on it; the expired-trial lock lets General's items through, and
        nothing else.
        """
        programme = make_programme()
        frep = make_module('Financial reporting', 'GFREP', programme=programme, price=Decimal('750'))
        general = general_module_for(programme)
        phase = general_phase_for(programme)
        week = ModuleWeek.objects.create(phase=phase, number=9, title='Full mock')
        guide = ModuleMaterial.objects.create(phase=phase, kind='document', title='Candidate guide',
                                              url='https://example.com/guide')
        debrief = ModuleMaterial.objects.create(phase=phase, week=week, kind='live', title='Debrief',
                                                url='https://example.com/debrief')
        student = make_user('locked@example.com')
        enrol(student.profile, frep, status=ModuleEnrolment.STATUS_EXPIRED)

        self.assertFalse(Gate(student, frep).module_open)
        gate = Gate(student, general)
        self.assertTrue(gate.module_open)
        self.assertTrue(gate.for_material(guide).open)
        self.assertTrue(gate.for_material(debrief).open)
        self.assertEqual(list(general_offerings_for(student.profile)), [general])
        self.assertFalse(ModuleEnrolment.objects.filter(programme_module=general).exists())
        stranger = make_user('stranger@example.com')
        self.assertEqual(list(general_offerings_for(stranger.profile)), [])
        self.assertTrue(is_general_content_path(f'/learning/materials/{guide.pk}/open/'))
        paid_phase = ModulePhase.objects.create(programme_module=frep, kind='exam', sequence=1)
        paid = ModuleMaterial.objects.create(phase=paid_phase, kind='document', title='Unit 1',
                                             url='https://example.com/u1')
        self.assertFalse(is_general_content_path(f'/learning/materials/{paid.pk}/open/'))

    @override_settings(CACHES=LOCMEM_CACHE)
    def test_pages_list_general_first(self):
        programme = make_programme()
        frep = make_module('Financial reporting', 'GFREP', programme=programme, price=Decimal('750'))
        general = general_module_for(programme)
        student = make_user('s@example.com')
        enrol(student.profile, frep)
        self.client.force_login(student)
        page = self.client.get('/learning/modules/')
        self.assertContains(page, 'Open General')
        self.assertContains(page, f'/learning/modules/{general.pk}/feed/')
        feed = self.client.get(f'/learning/modules/{general.pk}/feed/')
        self.assertEqual(feed.status_code, 200)
        self.assertContains(feed, 'Open to everyone on the programme')

    @override_settings(CACHES=LOCMEM_CACHE)
    def test_no_free_preview_tag_on_general(self):
        """General is free for everyone, so its items carry no "Free preview"
        tag; a preview item on a paid module still does."""
        programme = make_programme()
        frep = make_module('Financial reporting', 'GFREP', programme=programme, price=Decimal('750'))
        general = general_module_for(programme)
        ModuleMaterial.objects.create(phase=general_phase_for(programme), kind='document',
                                      title='Candidate guide', url='https://example.com/guide',
                                      is_preview=True)
        paid_phase = ModulePhase.objects.create(programme_module=frep, kind='exam', sequence=1)
        ModuleMaterial.objects.create(phase=paid_phase, kind='document', title='Unit 1 sample',
                                      url='https://example.com/u1', is_preview=True)
        student = make_user('p@example.com')
        enrol(student.profile, frep)
        self.client.force_login(student)
        feed = self.client.get(f'/learning/modules/{general.pk}/feed/')
        self.assertContains(feed, 'Candidate guide')
        self.assertNotContains(feed, 'Free preview')
        paid = self.client.get(f'/learning/modules/{frep.pk}/feed/')
        self.assertContains(paid, 'Unit 1 sample')
        self.assertContains(paid, 'Free preview')

    def test_general_is_left_out_of_fees_bundles_registration_and_invoices(self):
        """The "All 4 modules" bundle is still four modules and General has no fee item;
        registration never enrols anyone on it, so it is never invoiced or trialled.
        """
        from apps.learning import enrolment as enrol_service
        from apps.shop import catalogue, pricing
        from apps.shop.models import Discount, Product

        programme = make_programme()
        modules = [make_module(f'Module {i}', f'GM{i}', programme=programme) for i in range(1, 5)]
        general = general_module_for(programme)
        call_command('apply_module_pricing', stdout=io.StringIO())
        general.refresh_from_db()
        self.assertEqual(general.price_per_month, 0)
        for module in modules:
            module.refresh_from_db()
            self.assertEqual(module.price_per_month, Decimal('750.00'))
        bundle = Discount.objects.get(programme=programme, name='All 4 modules')
        self.assertEqual(bundle.products.count(), 4)
        self.assertIsNone(catalogue.sync_module(general))
        self.assertFalse(Product.objects.filter(module=general).exists())
        everything = list(programme.modules.all())
        self.assertEqual(pricing.quote_modules([(m, m.price_per_month) for m in everything])['total'],
                         Decimal('2200.00'))
        student = make_user('reg@example.com')
        rows = enrol_service.register_modules(student.profile, programme, [m.pk for m in everything])
        self.assertEqual(len(rows), 4)
        self.assertNotIn(general, [r.programme_module for r in rows])
        invoice = enrol_service.invoice_for_modules(student.profile, rows)
        self.assertEqual(invoice.items.count(), 4)
        self.assertFalse(any('General' in i.description for i in invoice.items.all()))


class OrganiseCommandTests(TestCase):
    """``organise_general_modules`` on a programme laid out the old way: the same
    onboarding copied onto every module, and no General yet."""

    def setUp(self):
        """Two modules, each with its own copy of the onboarding, plus module-specific
        items.

        General is deleted first to reproduce the live state. The topic guide is
        module-specific but titled like the other module's copy, so it must stay.
        """
        self.programme = make_programme('OLD')
        ProgrammeModule.objects.general().filter(programme=self.programme).delete()
        self.frep = make_module('Financial reporting', 'OFREP', programme=self.programme, price=Decimal('750'))
        self.tax = make_module('Taxation', 'OTAX', programme=self.programme, price=Decimal('750'))
        self.guide_lesson = Lesson.objects.create(title='Candidate guide', status='published',
                                                  visibility=Lesson.VIS_MODULE, module=self.frep)
        self.rows = {}
        for offering in (self.frep, self.tax):
            phase = ModulePhase.objects.create(programme_module=offering, kind='exam', sequence=1)
            mock_week = ModuleWeek.objects.create(phase=phase, number=4, title='Mini mock 1')
            code = offering.code
            self.rows[code] = {
                'guide': ModuleMaterial.objects.create(
                    phase=phase, kind='study_guide', title='Start here · Candidate guide', lesson=self.guide_lesson,
                    source_ref=f'pack/C1/{code}/onboarding/lesson/guide'),
                'timetable': ModuleMaterial.objects.create(
                    phase=phase, kind='document', title='Timetable (PDF)',
                    file='modules/pack/aa11bb22-Timetable.pdf', source_ref=f'pack/C1/{code}/file/Timetable.pdf'),
                'sheet': ModuleMaterial.objects.create(
                    phase=phase, week=mock_week, kind='document', title='Tutor · Marking sheet',
                    audience='staff', file='modules/staff-only/pack/cc33dd44-Sheet.xlsx'),
                'topic': ModuleMaterial.objects.create(
                    phase=phase, week=mock_week, kind='study_guide', title='Topic guide (PDF)',
                    file=f'modules/pack/{code}-Topic_guide.pdf'),
                'paper': ModuleMaterial.objects.create(
                    phase=phase, week=mock_week, kind='mock_exam', title='Mock paper (PDF)',
                    file=f'modules/pack/{code}-Mock_paper.pdf'),
            }
        self.student = make_user('progress@example.com')
        enrol(self.student.profile, self.frep)
        StudySession.objects.create(student=self.student, lesson=self.guide_lesson, status='completed')

    def run_command(self, *args):
        """Run ``organise_general_modules`` with ``args`` and return its output."""
        out = io.StringIO()
        call_command('organise_general_modules', *args, stdout=out)
        return out.getvalue()

    def snapshot(self):
        """Everything the command could change, for comparing before and after a dry
        run.
        """
        return (sorted(ModuleMaterial.objects.values_list('pk', 'phase_id', 'week_id', 'source_ref')),
                sorted(ProgrammeModule.objects.values_list('pk', 'code', 'name', 'order')),
                ModulePhase.objects.count(), ModuleWeek.objects.count())

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        before = self.snapshot()
        out = self.run_command('--programme', 'OLD')
        self.assertEqual(self.snapshot(), before)
        self.assertIn('DRY RUN', out)
        self.assertIn('General: CREATE', out)
        self.assertIn('MOVE to General', out)
        self.assertIn(f'#{self.rows["OTAX"]["guide"].pk}', out)
        self.assertIn('items moved: 3 · duplicates deleted: 3', out)

    def test_apply_dedupes_onto_general_and_keeps_module_specific_items(self):
        """One copy is kept and the rest are gone, with keys re-written so the seeder
        finds
        them (and a kept item keeps its week). Module-specific items, even with the
        same
        titles, are untouched; progress and references survive; running it again
        changes
        nothing.
        """
        from apps.assessments.models import Assessment, PaperSource

        built = Assessment.objects.create(title='Sittable sheet')
        source = PaperSource.objects.create(assessment=built)
        source.source_materials.add(self.rows['OTAX']['sheet'])

        self.run_command('--programme', 'OLD', '--apply')
        general = self.programme.modules.general().get()
        self.assertEqual(self.programme.modules.order_by('order', 'pk').first(), general)
        on_general = ModuleMaterial.objects.filter(phase__programme_module=general)
        self.assertEqual(sorted(on_general.values_list('title', flat=True)),
                         ['Start here · Candidate guide', 'Timetable (PDF)', 'Tutor · Marking sheet'])
        for key in ('guide', 'timetable', 'sheet'):
            self.assertFalse(ModuleMaterial.objects.filter(pk=self.rows['OTAX'][key].pk).exists())
            self.assertEqual(on_general.filter(title=self.rows['OFREP'][key].title).count(), 1)
        self.assertEqual(on_general.get(lesson=self.guide_lesson).source_ref, 'pack/C1/GEN/onboarding/lesson/guide')
        self.assertEqual(on_general.get(title='Tutor · Marking sheet').week.number, 4)
        for code in ('OFREP', 'OTAX'):
            for key in ('topic', 'paper'):
                row = ModuleMaterial.objects.get(pk=self.rows[code][key].pk)
                self.assertEqual(row.phase.programme_module.code, code)
        self.assertTrue(StudySession.objects.filter(lesson=self.guide_lesson, student=self.student).exists())
        self.assertEqual(list(source.source_materials.all()), [on_general.get(title='Tutor · Marking sheet')])
        lesson = Lesson.objects.get(pk=self.guide_lesson.pk)
        self.assertEqual(lesson.visibility, Lesson.VIS_PROGRAMME)
        self.assertIn(self.programme, lesson.target_programmes.all())
        self.assertIn('items moved: 0 · duplicates deleted: 0', self.run_command('--programme', 'OLD', '--apply'))
        self.assertEqual(self.programme.modules.general().count(), 1)

    def test_an_existing_overall_module_is_reused_not_duplicated(self):
        overall = make_module('Onboarding', 'ONB', programme=self.programme)
        result = general_organise.plan(self.programme)
        self.assertEqual(result.reuse, overall)
        self.run_command('--programme', 'OLD', '--apply')
        overall.refresh_from_db()
        self.assertTrue(overall.is_general)
        self.assertEqual((overall.code, overall.name), ('GEN', 'General'))
        self.assertEqual(self.programme.modules.general().count(), 1)
