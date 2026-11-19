"""The schedule's week gate and its progressive loader.

The rule (apps.learning.week_access): a student opens a week's content when
EITHER their enrolment is paid (a live trial counts, for its own window) AND the
week is inside the paid window (payment date + 33 days), is the previous /
current / next week, or has already started (past weeks always stay open);
OR an admin / educator ticked the week "make available".
Staff, admins and the module's educators always see everything.

Sections: the 33-day computation (:class:`PaidDaysTests`); the rule
(:class:`WeekRuleTests`, :class:`WeekDatesTests`); the server refuses — not
just the template (:class:`DirectAccessTests`); progressive loading
(:class:`ProgressiveLoadingTests`); and "make available" — who may set it
(:class:`MakeAvailableTests`).
"""

from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from core.testing import enrol, make_module, make_programme

from . import models
from .access import Gate, LOCK_OUT_OF_WINDOW
from .week_access import (LOCK_EXPIRED, LOCK_UNPAID, REASON_MADE_AVAILABLE, REASON_PAID,
                          REASON_PAID_BEFORE, REASON_TRIAL, access_window, week_is_unlocked)

User = get_user_model()


def _user(email, role):
    """Create a registered user with ``role``, re-read so ``user.profile`` is current.
    """
    user = User.objects.create_user(username=email, email=email, password='x')
    person = Person.objects.get(user=user)
    person.user_type = role
    person.registered = True
    person.profile_status = True
    person.save()
    return User.objects.get(pk=user.pk)


def _aware(day):
    """``day`` at 09:00, as an aware datetime."""
    return timezone.make_aware(datetime.combine(day, time(9, 0)))


class ScheduleFixture(TestCase):
    """Ten dated weeks across two blocks; today sits on the first day of week 5
    (W0…W9), so the previous / current / next weeks are W4, W5, W6.

    ``TODAY = None`` means the real today (for the HTTP tests).
    """

    TODAY = None
    WEEKS = 10

    def setUp(self):
        super().setUp()
        self.today = self.TODAY or timezone.localdate()
        self.programme = make_programme('WKINST', 'WKPROG')
        self.offering = make_module('Financial reporting', 'WKFR', programme=self.programme, price=450)
        self.student = _user('stu@example.com', 'student')
        self.enrolment = enrol(self.student.profile, self.offering, active=False)
        self.educator = _user('ed@example.com', 'educator')
        self.offering.educators.add(self.educator.profile)
        self.staff = _user('staff@example.com', 'staff')
        self.staff.is_staff = True
        self.staff.save()

        t0 = self.today - timedelta(days=35)
        self.phases = [
            models.ModulePhase.objects.create(programme_module=self.offering, kind='test', sequence=1, order=0),
            models.ModulePhase.objects.create(programme_module=self.offering, kind='test', sequence=2, order=1),
        ]
        self.weeks, self.materials, self.lessons = [], [], []
        for i in range(self.WEEKS):
            phase = self.phases[0 if i < self.WEEKS // 2 else 1]
            start = t0 + timedelta(days=7 * i)
            week = models.ModuleWeek.objects.create(
                phase=phase, number=i + 1, order=i, title=f'W{i}',
                starts_on=start, ends_on=start + timedelta(days=6))
            topic = models.Topic.objects.create(programme_module=self.offering, code=f'WK-{i}', title=f'Topic {i}')
            models.WeekTopic.objects.create(week=week, topic=topic)
            lesson = models.Lesson.objects.create(module=self.offering, topic=topic, title=f'Guide {i}',
                                                  status=models.Lesson.STATUS_PUBLISHED)
            material = models.ModuleMaterial.objects.create(
                phase=phase, week=week, topic=topic, kind=models.ModuleMaterial.KIND_STUDY_GUIDE,
                title=f'Study guide for week {i}', lesson=lesson)
            self.weeks.append(week)
            self.materials.append(material)
            self.lessons.append(lesson)

    def pay(self, paid_from, days=33):
        """Make the enrolment active, paid from ``paid_from`` for ``days`` days."""
        self.enrolment.status = models.ModuleEnrolment.STATUS_ACTIVE
        self.enrolment.paid_from = paid_from
        self.enrolment.paid_until = paid_from + timedelta(days=days)
        self.enrolment.save()

    def gate(self, user=None):
        """The :class:`Gate` for ``user`` (the student by default) on the fixture's
        today.
        """
        return Gate(user or self.student, self.offering, today=self.today)

    def open_weeks(self, user=None):
        """The indexes of the weeks open to ``user``."""
        gate = self.gate(user)
        return [i for i, w in enumerate(self.weeks) if gate.week_verdict(w)[0]]


class PaidDaysTests(TestCase):
    """A payment buys 33 days per month, extended while paid and restarted after a
    lapse.
    """
    def setUp(self):
        self.offering = make_module('Tax', 'WKTX', programme=make_programme('WKI2', 'WKP2'), price=300)
        self.student = _user('p@example.com', 'student')
        self.enrolment = enrol(self.student.profile, self.offering, active=False)

    def test_a_payment_buys_33_days_from_the_payment_date(self):
        now = timezone.now()
        self.enrolment.activate(months=1, now=now)
        today = timezone.localdate(now)
        self.assertEqual(self.enrolment.paid_from, today)
        self.assertEqual(self.enrolment.paid_until, today + timedelta(days=33))
        self.assertEqual(self.enrolment.status, models.ModuleEnrolment.STATUS_ACTIVE)

    def test_paying_again_while_paid_extends_from_paid_until(self):
        now = timezone.now()
        today = timezone.localdate(now)
        self.enrolment.activate(months=1, now=now)
        self.enrolment.activate(months=1, now=now + timedelta(days=10))
        self.assertEqual(self.enrolment.paid_until, today + timedelta(days=66))
        self.assertEqual(self.enrolment.paid_from, today, 'an unbroken paid run keeps its start')

    def test_paying_after_lapse_starts_a_new_window(self):
        now = timezone.now()
        self.enrolment.activate(months=1, now=now - timedelta(days=60))
        self.enrolment.activate(months=1, now=now)
        today = timezone.localdate(now)
        self.assertEqual(self.enrolment.paid_from, today)
        self.assertEqual(self.enrolment.paid_until, today + timedelta(days=33))

    def test_several_months_are_33_days_each(self):
        now = timezone.now()
        self.enrolment.activate(months=3, now=now)
        self.assertEqual(self.enrolment.paid_until, timezone.localdate(now) + timedelta(days=99))

    def test_the_invoice_hook_uses_the_same_33_days(self):
        from .enrolment import activate_modules_for_invoice

        class _Invoice:
            """A stand-in invoice: only its ``public_id`` is read."""
            public_id = '7f3c9a52-8b7e-4c1e-9a55-1d1f0e8c2b11'
        self.enrolment.invoice_uid = _Invoice.public_id
        self.enrolment.save()
        activate_modules_for_invoice(_Invoice)
        self.enrolment.refresh_from_db()
        self.assertEqual(self.enrolment.paid_until, timezone.localdate() + timedelta(days=33))


class WeekRuleTests(ScheduleFixture):
    """The week rule, case by case, on a fixed date."""
    TODAY = timezone.localdate().replace(year=2030, month=3, day=15)

    def test_paid_in_window_opens_weeks_inside_the_33_days(self):
        """Paid from two days ago: W4 … W9 are inside the window."""
        self.pay(self.today - timedelta(days=2))
        gate = self.gate()
        self.assertEqual(gate.week_verdict(self.weeks[8]), (True, REASON_PAID))
        self.assertEqual(self.open_weeks(), list(range(10)))

    def test_a_future_week_out_of_window_stays_locked(self):
        self.pay(self.today, days=1)
        self.assertEqual(self.gate().week_verdict(self.weeks[8]), (False, LOCK_OUT_OF_WINDOW))

    def test_every_past_week_stays_open_while_paid(self):
        """On W5, paid only from today: W0–W3 are before the window but stay open."""
        self.pay(self.today)
        for i in range(5):
            self.assertEqual(self.gate().week_verdict(self.weeks[i]), (True, REASON_PAID))

    def test_past_weeks_close_when_unpaid(self):
        self.assertFalse(any(self.gate().week_verdict(self.weeks[i])[0] for i in range(5)))

    def test_previous_current_and_next_week_open_to_a_payer(self):
        """Paid today: the window starts today, so last week is outside it — but it is
        the
        previous week, which the rule opens anyway (as is every week before it).
        """
        self.pay(self.today)
        self.assertIn(4, self.open_weeks())
        self.assertIn(5, self.open_weeks())
        self.assertIn(6, self.open_weeks())
        self.assertIn(3, self.open_weeks())

    def test_a_short_payment_still_opens_prev_current_next(self):
        """A window of today and tomorrow (W5) still opens every past week and the next.
        """
        self.pay(self.today, days=1)
        self.assertEqual(self.open_weeks(), [0, 1, 2, 3, 4, 5, 6])

    def test_a_live_trial_counts_as_paid_for_its_window(self):
        """A live trial opens the past weeks and its own window (W5–W6)."""
        self.enrolment.status = models.ModuleEnrolment.STATUS_TRIAL
        self.enrolment.started_at = _aware(self.today)
        self.enrolment.trial_ends_at = _aware(self.today + timedelta(days=7))
        self.enrolment.save()
        gate = self.gate()
        self.assertEqual(gate.week_verdict(self.weeks[5]), (True, REASON_TRIAL))
        self.assertEqual(self.open_weeks(), [0, 1, 2, 3, 4, 5, 6])
        self.assertFalse(gate.week_verdict(self.weeks[8])[0])

    def test_an_expired_trial_locks_everything(self):
        self.enrolment.status = models.ModuleEnrolment.STATUS_TRIAL
        self.enrolment.started_at = _aware(self.today - timedelta(days=20))
        self.enrolment.trial_ends_at = _aware(self.today - timedelta(days=13))
        self.enrolment.save()
        self.assertEqual(self.open_weeks(), [])
        self.assertEqual(self.gate().week_verdict(self.weeks[5]), (False, LOCK_EXPIRED))

    def test_unpaid_is_locked_even_this_week(self):
        self.assertEqual(self.open_weeks(), [])
        self.assertEqual(self.gate().week_verdict(self.weeks[5]), (False, LOCK_UNPAID))

    def test_a_lapsed_payment_keeps_the_weeks_it_paid_for(self):
        """Paid for W0–W4, lapsed 7 days ago: those weeks stay open."""
        self.pay(self.today - timedelta(days=40))
        self.assertEqual(self.open_weeks(), [0, 1, 2, 3, 4])
        self.assertEqual(self.gate().week_verdict(self.weeks[2]), (True, REASON_PAID_BEFORE))
        self.assertEqual(self.gate().week_verdict(self.weeks[5]), (False, LOCK_EXPIRED))

    def test_paying_again_after_a_lapse_keeps_both_paid_periods(self):
        """Paid for W0–W4, then a lapse, then paid again: up to date again, so every
        week
        behind them is open — the gap week W5 too.
        """
        first = _aware(self.today - timedelta(days=40))
        self.enrolment.activate(months=1, now=first)
        self.enrolment.activate(months=1, now=_aware(self.today + timedelta(days=14)))
        self.assertEqual(self.enrolment.paid_periods.count(), 2)
        later = Gate(self.student, self.offering, today=self.today + timedelta(days=14))
        opened = [i for i, w in enumerate(self.weeks) if later.week_verdict(w)[0]]
        self.assertEqual(opened, list(range(10)))

    def test_an_undated_week_locks_once_the_payment_lapses(self):
        self.weeks[1].starts_on = self.weeks[1].ends_on = None
        self.weeks[1].save()
        self.phases[0].starts_on = None
        self.phases[0].save()
        self.pay(self.today - timedelta(days=40))
        self.assertFalse(self.gate().week_verdict(self.weeks[1])[0])

    def test_an_old_trial_is_not_a_payment(self):
        self.enrolment.status = models.ModuleEnrolment.STATUS_TRIAL
        self.enrolment.started_at = _aware(self.today - timedelta(days=34))
        self.enrolment.trial_ends_at = _aware(self.today - timedelta(days=27))
        self.enrolment.save()
        self.assertEqual(self.open_weeks(), [])

    def test_each_payment_is_recorded(self):
        self.enrolment.activate(months=2, now=_aware(self.today))
        period = self.enrolment.paid_periods.get()
        self.assertEqual((period.starts_on, period.ends_on), (self.today, self.today + timedelta(days=66)))

    def test_make_available_opens_a_week_to_an_unpaid_student(self):
        self.weeks[8].make_available = True
        self.weeks[8].save()
        self.assertEqual(self.gate().week_verdict(self.weeks[8]), (True, REASON_MADE_AVAILABLE))
        self.assertEqual(self.open_weeks(), [8])

    def test_make_available_opens_a_week_outside_the_paid_window(self):
        self.pay(self.today)
        self.weeks[0].make_available = True
        self.weeks[0].save()
        self.assertIn(0, self.open_weeks())

    def test_staff_and_educators_see_everything(self):
        self.assertEqual(self.open_weeks(self.staff), list(range(self.WEEKS)))
        self.assertEqual(self.open_weeks(self.educator), list(range(self.WEEKS)))

    def test_an_educator_of_another_module_is_not_bypassed(self):
        other = make_module('Other', 'WKOT', programme=self.programme)
        outsider = _user('out@example.com', 'educator')
        other.educators.add(outsider.profile)
        self.assertEqual(self.open_weeks(outsider), [])

    def test_a_free_module_is_open(self):
        self.offering.price_per_month = 0
        self.offering.save()
        if not self.offering.is_free:
            self.skipTest('is_free is not derived from price')
        self.assertEqual(self.open_weeks(), list(range(self.WEEKS)))

    def test_week_is_unlocked_is_the_same_rule(self):
        self.pay(self.today)
        self.assertTrue(week_is_unlocked(self.student.profile, self.weeks[5], today=self.today))
        self.assertTrue(week_is_unlocked(self.student.profile, self.weeks[1], today=self.today))
        self.pay(self.today, days=1)
        self.assertFalse(week_is_unlocked(self.student.profile, self.weeks[9], today=self.today))
        self.assertTrue(week_is_unlocked(self.staff.profile, self.weeks[9], today=self.today))

    def test_a_material_in_a_locked_week_is_locked_and_not_listed(self):
        """A material in a week still to come is locked and left out of the listings."""
        self.pay(self.today, days=1)
        gate = self.gate()
        shut = gate.for_material(self.materials[9])
        self.assertFalse(shut.open)
        self.assertEqual(shut.reason, LOCK_OUT_OF_WINDOW)
        self.assertFalse(shut.listed)
        self.assertTrue(gate.for_material(self.materials[5]).open)

    def test_a_free_preview_in_a_locked_week_still_opens(self):
        self.materials[1].is_preview = True
        self.materials[1].save()
        self.assertTrue(self.gate().for_material(self.materials[1]).open)


class WeekDatesTests(ScheduleFixture):
    """How weeks are dated, and which are previous / current / next before, during and
    after the module.
    """
    TODAY = timezone.localdate().replace(year=2030, month=3, day=15)

    def test_before_the_module_starts_the_first_weeks_are_near(self):
        self.today = self.today - timedelta(days=200)
        self.pay(self.today, days=1)
        self.assertEqual(self.open_weeks(), [0, 1])

    def test_after_the_module_ends_the_last_weeks_are_near(self):
        self.today = self.today + timedelta(days=200)
        self.pay(self.today, days=1)
        self.assertEqual(self.open_weeks(), list(range(10)))

    def test_undated_weeks_follow_payment(self):
        models.ModuleWeek.objects.filter(phase__programme_module=self.offering).update(
            starts_on=None, ends_on=None)
        self.assertEqual(self.open_weeks(), [])
        self.pay(self.today)
        self.assertEqual(self.open_weeks(), list(range(self.WEEKS)))

    def test_week_dates_fall_back_to_the_phase_start(self):
        """Block 1 starts today: its week 1 is current, week 2 is next."""
        models.ModuleWeek.objects.filter(phase__programme_module=self.offering).update(
            starts_on=None, ends_on=None)
        self.phases[0].starts_on = self.today
        self.phases[0].save()
        self.pay(self.today, days=1)
        self.assertEqual(self.gate().weeks.near, {self.weeks[0].pk, self.weeks[1].pk})

    def test_access_window(self):
        self.assertIsNone(access_window(self.enrolment, self.today))
        self.pay(self.today - timedelta(days=3))
        self.assertEqual(access_window(self.enrolment, self.today),
                         (self.today - timedelta(days=3), self.today + timedelta(days=30)))


class DirectAccessTests(ScheduleFixture):
    """Real today; paid for today and tomorrow (W5), so W7–W9 — weeks still to come,
    outside the window and past "next" — are locked. Past weeks stay open."""

    def setUp(self):
        super().setUp()
        self.pay(self.today, days=1)
        self.client.force_login(self.student)

    def test_a_past_week_outside_the_window_still_opens(self):
        """Going back is always allowed: W1 is long before the paid window."""
        r = self.client.get(reverse('learning:material-open', args=[self.materials[1].pk]))
        self.assertIn(reverse('learning:lesson-view', args=[self.lessons[1].pk]), r['Location'])

    def test_material_open_refuses_a_locked_week(self):
        r = self.client.get(reverse('learning:material-open', args=[self.materials[8].pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('learning:module-feed', args=[self.offering.pk]), r['Location'])
        self.assertNotIn(reverse('learning:lesson-view', args=[self.lessons[8].pk]), r['Location'])

    def test_material_open_lets_an_open_week_through(self):
        r = self.client.get(reverse('learning:material-open', args=[self.materials[5].pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('learning:lesson-view', args=[self.lessons[5].pk]), r['Location'])

    def test_the_lesson_page_refuses_a_locked_week(self):
        r = self.client.get(reverse('learning:lesson-view', args=[self.lessons[8].pk]))
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('lessons/%d/view' % self.lessons[8].pk, r['Location'])

    def test_the_lesson_page_opens_for_an_open_week(self):
        r = self.client.get(reverse('learning:lesson-view', args=[self.lessons[5].pk]))
        self.assertEqual(r.status_code, 200)

    def _quiz_on_week(self, i):
        from apps.assessments.models import Assessment
        quiz = Assessment.objects.create(module=self.offering, title=f'Quiz {i}', kind='test',
                                         total_marks=10, status=Assessment.STATUS_OPEN)
        models.ModuleMaterial.objects.create(
            phase=self.weeks[i].phase, week=self.weeks[i], kind=models.ModuleMaterial.KIND_ASSESSMENT,
            title=f'Quiz {i}', assessment=quiz)
        return quiz

    def test_an_assessment_on_a_locked_week_cannot_be_taken_by_url(self):
        quiz = self._quiz_on_week(8)
        r = self.client.get(reverse('assessments:take', args=[quiz.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('learning:module-feed', args=[self.offering.pk]), r['Location'])

    def test_an_assessment_on_an_open_week_is_not_refused_by_the_week_gate(self):
        quiz = self._quiz_on_week(5)
        r = self.client.get(reverse('assessments:take', args=[quiz.pk]))
        self.assertNotIn(reverse('learning:module-feed', args=[self.offering.pk]), r.get('Location', ''))

    def test_staff_open_a_locked_week_material(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse('learning:material-open', args=[self.materials[8].pk]))
        self.assertIn(reverse('learning:lesson-view', args=[self.lessons[8].pk]), r['Location'])

    def test_the_lazy_endpoint_never_returns_locked_material(self):
        url = reverse('learning:module-weeks', args=[self.offering.pk])
        r = self.client.get(url, {'ids': f'{self.weeks[8].pk},{self.weeks[5].pk}'})
        self.assertEqual(r.status_code, 200)
        weeks = {w['id']: w for w in r.json()['weeks']}
        self.assertFalse(weeks[self.weeks[8].pk]['open'])
        self.assertIn('Locked: pay to unlock', weeks[self.weeks[8].pk]['html'])
        self.assertNotIn('Study guide for week 8', weeks[self.weeks[8].pk]['html'])
        self.assertTrue(weeks[self.weeks[5].pk]['open'])
        self.assertIn('Study guide for week 5', weeks[self.weeks[5].pk]['html'])

    def test_the_schedule_does_not_list_locked_material_anywhere(self):
        """The locked week's title shows; its material appears nowhere on the page."""
        r = self.client.get(reverse('learning:module-feed', args=[self.offering.pk]) + '?weeks=all')
        body = r.content.decode()
        self.assertIn('W8', body)
        self.assertNotIn('Study guide for week 8', body)
        self.assertIn('Study guide for week 5', body)
        r = self.client.get(reverse('learning:module-feed-tab', args=[self.offering.pk, 'documents']))
        self.assertNotIn('Study guide for week 8', r.content.decode())


class ProgressiveLoadingTests(ScheduleFixture):
    """First paint renders the previous, current and next week; the loader serves the
    rest, gated.
    """
    def setUp(self):
        """Sign in a student who has paid for every week."""
        super().setUp()
        self.pay(self.today - timedelta(days=60), days=400)
        self.client.force_login(self.student)
        self.url = reverse('learning:module-feed', args=[self.offering.pk])

    def test_first_paint_renders_only_previous_current_and_next(self):
        """A schedule row carries ``id="mat-<pk>"`` (the rails only link to it); a
        deferred
        week keeps its header.
        """
        body = self.client.get(self.url).content.decode()
        for i in (4, 5, 6):
            self.assertIn(f'id="mat-{self.materials[i].pk}"', body)
            self.assertNotIn(f'data-mf-lazy="{self.weeks[i].pk}"', body)
        for i in (0, 1, 2, 3, 7, 8, 9):
            self.assertNotIn(f'id="mat-{self.materials[i].pk}"', body)
            self.assertIn(f'data-mf-lazy="{self.weeks[i].pk}"', body)
            self.assertIn(f'id="week-{self.weeks[i].pk}"', body)

    def test_weeks_all_renders_every_body(self):
        import re
        body = self.client.get(self.url + '?weeks=all').content.decode()
        self.assertIsNone(re.search(r'data-mf-lazy="\d', body))
        for material in self.materials:
            self.assertIn(f'id="mat-{material.pk}"', body)

    def test_the_endpoint_returns_the_weeks_asked_for(self):
        ids = [self.weeks[i].pk for i in (0, 1, 2)]
        r = self.client.get(reverse('learning:module-weeks', args=[self.offering.pk]),
                            {'ids': ','.join(map(str, ids))})
        data = r.json()['weeks']
        self.assertEqual([w['id'] for w in data], ids)
        self.assertIn('Study guide for week 0', data[0]['html'])
        self.assertNotIn('Study guide for week 1', data[0]['html'])

    def test_the_endpoint_ignores_weeks_of_other_modules_and_drafts(self):
        other = make_module('Other', 'WKOT2', programme=self.programme)
        phase = models.ModulePhase.objects.create(programme_module=other, kind='test', sequence=1)
        foreign = models.ModuleWeek.objects.create(phase=phase, number=1, title='Foreign')
        self.weeks[2].is_published = False
        self.weeks[2].save()
        r = self.client.get(reverse('learning:module-weeks', args=[self.offering.pk]),
                            {'ids': f'{foreign.pk},{self.weeks[2].pk},{self.weeks[3].pk}'})
        self.assertEqual([w['id'] for w in r.json()['weeks']], [self.weeks[3].pk])

    def test_the_endpoint_refuses_the_unregistered(self):
        self.client.force_login(_user('nobody@example.com', 'student'))
        r = self.client.get(reverse('learning:module-weeks', args=[self.offering.pk]),
                            {'ids': str(self.weeks[0].pk)})
        self.assertEqual(r.status_code, 403)

    def test_query_count_does_not_grow_with_the_schedule(self):
        """No N+1: the page costs the same number of queries for 10 weeks as for 20."""
        self.client.get(self.url)
        with CaptureQueriesContext(connection) as small:
            self.client.get(self.url)
        for i in range(10, 20):
            phase = self.phases[1]
            week = models.ModuleWeek.objects.create(phase=phase, number=i + 1, order=i, title=f'W{i}')
            topic = models.Topic.objects.create(programme_module=self.offering, code=f'WK-{i}', title=f'T{i}')
            models.WeekTopic.objects.create(week=week, topic=topic)
            models.ModuleMaterial.objects.create(
                phase=phase, week=week, topic=topic, kind=models.ModuleMaterial.KIND_DOCUMENT,
                title=f'Doc {i}')
        with CaptureQueriesContext(connection) as large:
            self.client.get(self.url)
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        with CaptureQueriesContext(connection) as lazy:
            self.client.get(reverse('learning:module-weeks', args=[self.offering.pk]),
                            {'ids': ','.join(str(w.pk) for w in self.weeks[:3])})
        print(f'\n[week_access] schedule tab queries: {len(small.captured_queries)} '
              f'(10 weeks) / {len(large.captured_queries)} (20 weeks); '
              f'lazy batch of 3 weeks: {len(lazy.captured_queries)}')


class MakeAvailableTests(ScheduleFixture):
    """Educators of the module and staff may tick "make available"; students may not."""
    def url(self, week):
        """The availability toggle URL for ``week``."""
        return reverse('learning:module-week-availability', args=[self.offering.pk, week.pk])

    def test_an_educator_of_the_module_can_make_a_week_available(self):
        self.client.force_login(self.educator)
        self.client.post(self.url(self.weeks[8]), {'make_available': '1'})
        self.weeks[8].refresh_from_db()
        self.assertTrue(self.weeks[8].make_available)
        self.client.post(self.url(self.weeks[8]), {'make_available': '0'})
        self.weeks[8].refresh_from_db()
        self.assertFalse(self.weeks[8].make_available)

    def test_staff_can_make_a_week_available(self):
        self.client.force_login(self.staff)
        self.client.post(self.url(self.weeks[8]), {'make_available': '1'})
        self.weeks[8].refresh_from_db()
        self.assertTrue(self.weeks[8].make_available)

    def test_a_student_cannot(self):
        self.client.force_login(self.student)
        r = self.client.post(self.url(self.weeks[8]), {'make_available': '1'})
        self.assertEqual(r.status_code, 403)
        self.weeks[8].refresh_from_db()
        self.assertFalse(self.weeks[8].make_available)

    def test_the_builder_add_week_form_can_set_it(self):
        self.client.force_login(self.educator)
        self.client.post(reverse('learning:module-week-add', args=[self.offering.pk]),
                         {'phase': self.phases[0].pk, 'title': 'Bonus', 'make_available': 'on',
                          'is_published': 'on'})
        self.assertTrue(models.ModuleWeek.objects.get(title='Bonus').make_available)
