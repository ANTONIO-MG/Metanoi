"""The sample cohorts and their dated calendars (core.academic_spine.COHORT_CALENDAR)."""
import datetime as dt
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Person
from apps.learning.models import (AcademicCalendar, CalendarEvent, Cohort, Programme,
                                  ProgrammeEnrolment)
from core import academic_spine


def quiet(*_a, **_k):
    """Discard the seeder's progress output."""
    pass


class CohortCalendarTests(TestCase):
    """Seeding the sample cohorts and their dated calendars."""
    @classmethod
    def setUpTestData(cls):
        """Seed the spine (logos and template calendars off)."""
        academic_spine.seed(logos=False, verbose=False, calendar=False)

    def seed(self, **kw):
        """Run :func:`core.academic_spine.seed_cohort_calendar` quietly."""
        return academic_spine.seed_cohort_calendar(out=quiet, **kw)

    def test_cohorts_created_with_published_dates(self):
        """Every intake exists; an intake with dates to confirm (SAMPLE3) stores no start date."""
        self.seed()
        sample1 = Cohort.objects.get(programme__institution__code='METANOIA', programme__code='SAMPLE',
                                     code='SAMPLE1')
        self.assertEqual((sample1.start_date, sample1.end_date), (dt.date(2027, 2, 1), dt.date(2027, 11, 30)))
        for code in ('SAMPLE2', 'SAMPLE3', 'PACK2027'):
            self.assertTrue(Cohort.objects.filter(programme__institution__code='METANOIA',
                                                  programme__code='SAMPLE', code=code).exists(), code)
        self.assertIsNone(Cohort.objects.get(code='SAMPLE3').start_date)

    def test_dates_land_on_the_right_calendar_and_publication(self):
        """Dates land on the right year's calendar and are published correctly.

        A conflicting date (SAMPLE1 CORE) and an estimate (SAMPLE1 PRACT) stay
        unpublished (staff only), and new 2027 calendars carry the public holidays.
        """
        self.seed()
        exam = CalendarEvent.objects.get(cohort__code='SAMPLE1', programme_module__code='INTRO',
                                         kind=CalendarEvent.KIND_EXAM)
        self.assertEqual(exam.calendar.year, 2027)
        self.assertEqual(timezone.localtime(exam.start).date(), dt.date(2027, 10, 25))
        self.assertTrue(exam.is_published)
        core = CalendarEvent.objects.get(cohort__code='SAMPLE1', programme_module__code='CORE',
                                         kind=CalendarEvent.KIND_EXAM)
        self.assertFalse(core.is_published)
        self.assertIn('28 October', core.description)
        pract = CalendarEvent.objects.get(cohort__code='SAMPLE1', programme_module__code='PRACT',
                                          kind=CalendarEvent.KIND_EXAM)
        self.assertFalse(pract.is_published)
        closes = CalendarEvent.objects.get(cohort__code='SAMPLE2', kind=CalendarEvent.KIND_DEADLINE)
        self.assertEqual(closes.calendar.year, 2026)
        self.assertTrue(CalendarEvent.objects.filter(
            calendar__institution__code='METANOIA', calendar__year=2027,
            kind=CalendarEvent.KIND_HOLIDAY, title='Good Friday', is_published=True).exists())

    def test_template_row_is_corrected_not_duplicated(self):
        """A provisional template row for the same date and module is adopted by the first intake."""
        sample = Programme.objects.get(institution__code='METANOIA', code='SAMPLE')
        intro = sample.modules.get(code='INTRO')
        cal = AcademicCalendar.objects.create(institution=sample.institution, year=2027)
        template = CalendarEvent.objects.create(
            calendar=cal, programme=sample, programme_module=intro, kind=CalendarEvent.KIND_EXAM,
            title='Final examination', is_published=False,
            start=timezone.make_aware(dt.datetime(2027, 10, 20, 9)),
            description='Full-scope examination.' + academic_spine.PROVISIONAL)
        self.seed()
        template.refresh_from_db()
        self.assertEqual(template.cohort.code, 'SAMPLE2')
        self.assertEqual(timezone.localtime(template.start).date(), dt.date(2027, 4, 7))
        self.assertTrue(template.is_published)
        self.assertEqual(CalendarEvent.objects.filter(
            calendar=cal, programme_module=intro, title='Final examination', cohort=template.cohort).count(), 1)
        self.assertTrue(CalendarEvent.objects.filter(
            calendar=cal, programme_module=intro, title='Final examination', cohort__code='SAMPLE1').exists())

    def test_idempotent_and_admin_edits_survive(self):
        first = self.seed()
        self.assertEqual(first['created'], len(academic_spine.COHORT_CALENDAR))
        edited = CalendarEvent.objects.get(cohort__code='SAMPLE2', kind=CalendarEvent.KIND_RESULT)
        edited.description = 'Admin wrote this.'
        edited.is_published = False
        edited.save()
        again = self.seed()
        self.assertEqual((again['created'], again['updated'], again['cohorts']), (0, 0, 0))
        edited.refresh_from_db()
        self.assertEqual(edited.description, 'Admin wrote this.')
        self.assertFalse(edited.is_published)

    def test_command_dry_run_saves_nothing(self):
        before = CalendarEvent.objects.count()
        out = StringIO()
        call_command('seed_cohort_calendar', stdout=out)
        self.assertIn('DRY RUN', out.getvalue())
        self.assertEqual(CalendarEvent.objects.count(), before)
        self.assertFalse(Cohort.objects.filter(code='PACK2027').exists())
        call_command('seed_cohort_calendar', '--apply', stdout=StringIO())
        self.assertTrue(Cohort.objects.filter(code='PACK2027').exists())


class IntakeScopedDatesTests(TestCase):
    """A student sees their own intake's dates, not another intake's."""

    @classmethod
    def setUpTestData(cls):
        academic_spine.seed(logos=False, verbose=False, calendar=False)
        academic_spine.seed_cohort_calendar(out=quiet)
        cls.sample = Programme.objects.get(institution__code='METANOIA', code='SAMPLE')

    def student(self, email, cohort_code):
        """A student enrolled on the Sample Programme in ``cohort_code`` (or no intake)."""
        user = get_user_model().objects.create_user(username=email, email=email, password='x')
        person, _ = Person.objects.update_or_create(user=user, defaults={'user_type': 'student'})
        cohort = self.sample.cohorts.get(code=cohort_code) if cohort_code else None
        ProgrammeEnrolment.objects.create(person=person, programme=self.sample, cohort=cohort)
        return user

    def countdown(self, user):
        """The dashboard exam countdown for ``user`` as of 1 March 2027."""
        from apps.myhub.dashboard import exam_countdown
        now = timezone.make_aware(dt.datetime(2027, 3, 1, 8))
        with mock.patch('django.utils.timezone.now', return_value=now):
            return exam_countdown(user, cache={})

    def when(self, user):
        """The local date of the event ``user`` counts down to."""
        result = self.countdown(user)
        self.assertIsNotNone(result)
        return timezone.localtime(result['event'].start).date()

    def test_sample1_counts_down_to_its_own_test_not_sample2_exams(self):
        self.assertEqual(self.when(self.student('s1@example.com', 'SAMPLE1')), dt.date(2027, 3, 17))

    def test_sample2_counts_down_to_its_final_exams(self):
        self.assertEqual(self.when(self.student('s2@example.com', 'SAMPLE2')), dt.date(2027, 4, 7))
