"""Query-count budgets for the busiest pages.

A page that renders people, badges and per-module numbers can quietly turn into
hundreds of queries: one per person for their profile, one per module for its
progress, a dozen COUNTs for navbar badges on every single page. These tests pin
an upper bound on the number of queries so a regression (a missing
``select_related``, a counter that stops being cached) fails loudly instead of
slowly.

The bounds are deliberately generous — they are tripwires, not exact counts —
but every one sits well under what the pages cost before they were tuned
(dashboard 136, chat room 117, ~43 per page for the navbar counters).

Run with ``PERF_REPORT=1`` to print the actual counts.
"""

import io
import os

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from core.testing import enrol, make_module, make_programme

User = get_user_model()

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _png():
    """A tiny uploaded PNG, for profile pictures."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2), (20, 120, 200)).save(buf, format='PNG')
    return SimpleUploadedFile('face.png', buf.getvalue(), content_type='image/png')


@override_settings(CACHES=LOCMEM_CACHE, MEDIA_ROOT='/tmp/metanoiahub-test-media')
class PagePerformanceFixture(TestCase):
    """A student with three modules, classmates, two chats and notifications."""

    def setUp(self):
        """Build the student, classmates and traffic.

        A group room holds everyone, with plenty of traffic from many senders, and
        there are direct chats with several classmates, each with unread messages.
        Study history and assessments give every dashboard panel rows to draw (an
        empty panel short-circuits and hides its queries).
        """
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        from apps.communication import models as comm
        from apps.communication import services

        programme = make_programme('PERF', 'PGDA')
        self.modules = [make_module(f'Module {i}', f'PERF{i}', programme=programme)
                        for i in range(3)]
        self.alice = self._person('alice@example.com', 'Alice', 'Ndlovu', avatar=True)
        self.classmates = [
            self._person(f'c{i}@example.com', f'Class{i}', f'Mate{i}', avatar=bool(i % 2))
            for i in range(6)]
        self.staff = self._person('staff@example.com', 'Sam', 'Staff', role='staff')
        self.staff.is_staff = True
        self.staff.save(update_fields=['is_staff'])

        self.room = comm.ChatGroup.objects.create(
            kind=comm.ChatGroup.KIND_CUSTOM if hasattr(comm.ChatGroup, 'KIND_CUSTOM') else 'custom',
            name='Study group', created_by=self.staff)
        for member in [self.alice, self.staff, *self.classmates]:
            comm.ChatMembership.objects.create(group=self.room, user=member)
        for n in range(24):
            comm.Message.objects.create(group=self.room, sender=self.classmates[n % 6],
                                        body=f'Group message {n}')
        for mate in self.classmates[:4]:
            direct = services.get_or_create_direct_chat(self.alice, mate)
            for n in range(3):
                comm.Message.objects.create(group=direct, sender=mate, body=f'Hi {n}')

        for n in range(6):
            comm.Notification.objects.create(
                recipient=self.alice, actor=self.classmates[n % 6],
                title=f'Notification {n}', body='Something happened')
        for n in range(3):
            comm.Announcement.objects.create(sender=self.staff, title=f'Notice {n}',
                                             body='Body', sent_at=timezone.now())

        from apps.assessments import models as am
        from apps.learning.models import Lesson, StudySession
        for module in self.modules:
            lessons = [Lesson.objects.create(module=module, title=f'{module.code} lesson {n}',
                                             status=Lesson.STATUS_PUBLISHED) for n in range(4)]
            for lesson in lessons[:2]:
                StudySession.objects.create(student=self.alice, lesson=lesson,
                                            status=StudySession.STATUS_COMPLETED,
                                            total_seconds=1800, completion_pct=100)
            for n in range(2):
                assessment = am.Assessment.objects.create(
                    module=module, title=f'{module.code} test {n}', kind=am.Assessment.KIND_QUIZ,
                    total_marks=20, time_limit_minutes=30, status=am.Assessment.STATUS_OPEN,
                    created_by=self.staff)
                am.AssessmentAttempt.objects.create(
                    assessment=assessment, student=self.alice, status='marked', score=14,
                    passed=True, time_spent_seconds=900,
                    submitted_at=timezone.now())

    def _person(self, email, first, last, *, avatar=False, role='student'):
        """A registered user with a profile; students are enrolled on the fixture modules."""
        user = User.objects.create_user(username=email, email=email, password='x',
                                        first_name=first, last_name=last)
        person = Person.objects.get(user=user)
        person.first_name, person.last_name = first, last
        person.user_type = role
        person.registered = True
        person.profile_status = True
        if avatar:
            person.profile_picture = _png()
        person.save()
        if role == 'student':
            for module in self.modules:
                enrol(person, module)
        return user

    def count_get(self, user, url, *, cold=False):
        """Queries for one GET of ``url`` as ``user``.

        Always after a warm-up request, so one-off lazy setup (joining the
        General room, …) is not counted. ``cold=True`` then empties the cache,
        measuring a request whose navbar counters must be computed afresh.
        """
        self.client.force_login(user)
        self.client.get(url)
        if cold:
            cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        if os.environ.get('PERF_REPORT'):
            print(f'\n[perf] {user.email} GET {url}{" (cold)" if cold else ""}: '
                  f'{len(ctx.captured_queries)} queries')
            if os.environ.get('PERF_REPORT') == '2':
                for q in ctx.captured_queries:
                    print('   ', q['sql'][:300])
        return len(ctx.captured_queries), response


class DashboardQueryBudgetTests(PagePerformanceFixture):
    """Query budgets for the student dashboard."""

    def test_student_dashboard_query_budget(self):
        """Was 137 queries; now ~35 with the navbar counters cached.

        Recent messages show the sender by name.
        """
        queries, response = self.count_get(self.alice, reverse('myhub:index'))
        self.assertContains(response, 'Alice')
        self.assertContains(response, 'Class1 Mate1')
        self.assertLess(queries, 40)

    def test_every_panel_builds_without_error(self):
        """The panels swallow their own exceptions (a broken panel must not 500
        the page), so a query refactor that breaks one would otherwise pass
        silently as an empty card."""
        from apps.myhub import dashboard
        with self.assertNoLogs('apps.myhub.dashboard', level='ERROR'):
            context = dashboard.student_dashboard(self.alice)
        self.assertEqual(context['summary']['lessons_total'], 12)
        self.assertEqual(context['summary']['lessons_done'], 6)
        self.assertEqual(context['summary']['assessments_taken'], 6)
        self.assertEqual(context['summary']['programmes'], 1)
        self.assertEqual(len(context['module_rows']), 3)
        self.assertEqual(context['standing']['modules_total'], 3)
        self.assertTrue(context['pace']['has_data'])
        self.assertTrue(context['trend']['has_data'])
        self.assertIsNotNone(context['prediction'])
        self.assertEqual(len(context['recent_messages']), 5)

    def test_student_dashboard_cold_cache_budget(self):
        """Even with every counter to recompute it stays far below the old 137."""
        queries, _ = self.count_get(self.alice, reverse('myhub:index'), cold=True)
        self.assertLess(queries, 60)


class ChatRoomQueryBudgetTests(PagePerformanceFixture):
    """Query budgets for a chat room page."""

    def test_chat_room_query_budget(self):
        """Was 72 here (117 on production data): a query per member, twice over."""
        queries, response = self.count_get(
            self.alice, reverse('communication:chat-room', args=[self.room.pk]))
        self.assertContains(response, 'Study group')
        self.assertLess(queries, 20)

    def test_chat_room_cost_does_not_grow_with_members(self):
        url = reverse('communication:chat-room', args=[self.room.pk])
        before, _ = self.count_get(self.alice, url)
        from apps.communication import models as comm
        for i in range(5):
            comm.ChatMembership.objects.create(
                group=self.room, user=self._person(f'extra{i}@example.com', 'Extra', f'P{i}'))
        after, _ = self.count_get(self.alice, url)
        self.assertEqual(before, after)

    def test_chat_room_cold_cache_budget(self):
        queries, _ = self.count_get(
            self.alice, reverse('communication:chat-room', args=[self.room.pk]), cold=True)
        self.assertLess(queries, 35)


class NavCounterTests(PagePerformanceFixture):
    """The navbar / sidebar badges on every page."""

    PAGE = '/communication/notifications/'

    def _processor_queries(self, user):
        """Queries made by every configured context processor, reading the keys the layouts read."""
        from django.template import engines
        request = RequestFactory().get(self.PAGE)
        request.user = user
        request.session = {}
        engine = engines['django'].engine
        with CaptureQueriesContext(connection) as ctx:
            values = {}
            for processor in engine.template_context_processors:
                values.update(processor(request))
            for name, keys in (('comm_stats', ('unread_messages', 'unread_notifications',
                                               'alerts_total', 'alerts')),
                               ('shop_stats', ('cart_count',)),
                               ('account_stats', ('profile', 'module_count',
                                                  'completion_pct', 'readiness'))):
                for key in keys:
                    _ = values[name][key]
                    if key == 'alerts':
                        list(values[name][key])
            _ = values['my_programmes_count']
        if os.environ.get('PERF_REPORT'):
            print(f'\n[perf] {user.email} context processors: {len(ctx.captured_queries)} queries')
            if os.environ.get('PERF_REPORT') == '2':
                for q in ctx.captured_queries:
                    print('   ', q['sql'][:300])
        return len(ctx.captured_queries)

    def test_counters_are_cached_between_requests(self):
        """After a cold run, a warm one makes at most three queries."""
        for user in (self.alice, self.staff):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                self._processor_queries(user)
                self.assertLessEqual(self._processor_queries(user), 3)

    def test_typical_page_query_budget(self):
        """Was 47 (student) / 35 (staff) queries; the counters were ~40 of them."""
        for user in (self.alice, self.staff):
            with self.subTest(user=user.email):
                queries, _ = self.count_get(user, self.PAGE)
                self.assertLess(queries, 15)
                cold, _ = self.count_get(user, self.PAGE, cold=True)
                self.assertLess(cold, 30)

    def _badge(self, key):
        """One of Alice's ``comm_stats`` badge values, freshly computed."""
        from apps.communication.context_processors import comm_stats
        request = RequestFactory().get('/')
        request.user = self.alice
        return comm_stats(request)['comm_stats'][key]

    def test_new_notification_shows_at_once(self):
        from apps.communication.models import Notification
        before = self._badge('unread_notifications')
        Notification.objects.create(recipient=self.alice, title='Fresh')
        self.assertEqual(self._badge('unread_notifications'), before + 1)

    def test_reading_a_notification_clears_the_bell_at_once(self):
        from apps.communication.models import Notification
        before = self._badge('unread_notifications')
        note = Notification.objects.filter(recipient=self.alice, is_read=False).first()
        self.client.force_login(self.alice)
        self.client.get(reverse('communication:notification-detail', args=[note.pk]))
        self.assertEqual(self._badge('unread_notifications'), before - 1)

    def test_mark_all_read_clears_the_bell_at_once(self):
        """A bulk ``update()`` fires no signal — the write-request middleware covers it."""
        self.assertGreater(self._badge('unread_notifications'), 0)
        self.client.force_login(self.alice)
        self.client.post(reverse('communication:notifications'), {'action': 'mark_all_read'})
        self.assertEqual(self._badge('unread_notifications'), 0)

    def test_new_message_updates_members_badges_at_once(self):
        from apps.communication.models import Message
        before = self._badge('unread_messages')
        Message.objects.create(group=self.room, sender=self.classmates[0], body='New!')
        self.assertEqual(self._badge('unread_messages'), before + 1)

    def test_unused_counters_are_lazy(self):
        """A counter no template reads is never queried."""
        from apps.finance.context_processors import finance_stats
        from apps.shop.context_processors import shop_stats
        request = RequestFactory().get('/')
        request.user = self.alice
        with CaptureQueriesContext(connection) as ctx:
            finance_stats(request)
            shop_stats(request)
        self.assertEqual(len(ctx.captured_queries), 0)
