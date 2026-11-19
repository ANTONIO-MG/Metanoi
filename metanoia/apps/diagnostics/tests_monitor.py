"""The system monitor (apps.diagnostics.monitor) and the upload-progress redirect."""
import json
from datetime import timedelta
from unittest import mock

from django.http import HttpResponse, HttpResponseRedirect
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests_developers import make_developer
from apps.livesessions.tests import make_user
from core.upload_progress import UploadRedirectMiddleware

from . import monitor
from .models import SystemSample


def fresh_store():
    """Empty the in-process monitor store between tests."""
    monitor._memory.data.clear()


@override_settings(SECURE_SSL_REDIRECT=False)
class TrafficTests(TestCase):
    """Request counting into minute buckets and reading traffic back."""
    def setUp(self):
        fresh_store()
        self.factory = RequestFactory()

    def _hit(self, path='/my-hub/', status=200, ms=50, user=None):
        """Count one request with the given path, status, duration and user."""
        request = self.factory.get(path)
        if user is not None:
            request.user = user
        monitor.count_request(request, status, ms)

    def test_requests_users_errors_and_slow_ones_are_counted(self):
        user = make_user('mon-user', 'student')
        self._hit(user=user)
        self._hit(user=user, status=500, ms=2500)
        self._hit(status=404)
        self._hit('/static/css/app.css')
        now = monitor._minute()
        seen = monitor.traffic(now - 1, now)
        self.assertEqual((seen['requests'], seen['errors'], seen['client_errors'], seen['slow']), (3, 1, 1, 1))
        self.assertEqual((seen['users'], seen['visitors']), (1, 1))
        self.assertEqual(seen['avg_ms'], round(2600 / 3))

    def test_the_middleware_counts_a_crash_and_lets_it_through(self):
        def boom(request):
            raise ValueError('x')
        with self.assertRaises(ValueError):
            monitor.TrafficMiddleware(boom)(self.factory.get('/x/'))
        now = monitor._minute()
        self.assertEqual(monitor.traffic(now, now)['errors'], 1)

    def test_a_broken_store_never_breaks_a_request(self):
        with mock.patch.object(monitor, 'store', side_effect=RuntimeError('redis down')):
            response = monitor.TrafficMiddleware(lambda r: HttpResponse('ok'))(self.factory.get('/x/'))
        self.assertEqual(response.status_code, 200)

    def test_pages_are_ranked(self):
        for _ in range(3):
            self._hit('/media/a.png')
        self._hit()
        self.assertEqual(monitor.top_pages(1)[0], ('media files', 3))


class SampleTests(TestCase):
    """Recording system samples and building history from them."""
    def setUp(self):
        fresh_store()

    def test_a_sample_reads_the_machine_and_the_next_uses_deltas(self):
        monitor.record_sample()
        first = SystemSample.objects.get()
        self.assertGreater(first.mem_total_mb, 0)
        self.assertGreater(first.disk_total_gb, 0)
        self.assertIn('cpu_total', first.raw)
        monitor.count_request(RequestFactory().get('/x/'), 200, 10)
        SystemSample.objects.filter(pk=first.pk).update(at=timezone.now() - timedelta(minutes=5))
        monitor.record_sample()
        self.assertEqual(SystemSample.objects.count(), 2)
        self.assertGreaterEqual(SystemSample.objects.order_by('-at').first().cpu_pct, 0)

    def test_old_samples_are_pruned(self):
        SystemSample.objects.create(at=timezone.now() - timedelta(days=monitor.KEEP_DAYS + 1))
        monitor.record_sample()
        self.assertEqual(SystemSample.objects.count(), 1)

    def test_history_buckets_and_summary(self):
        now = timezone.now()
        for i in range(12):
            SystemSample.objects.create(at=now - timedelta(minutes=5 * i), cpu_pct=10 + i, mem_used_mb=500,
                                        mem_total_mb=1000, disk_used_gb=10, disk_total_gb=50, requests=50,
                                        minutes=5, users=i, avg_ms=100)
        found = monitor.history('24h')
        self.assertTrue(found['series']['labels'])
        self.assertEqual(found['series']['mem'][0], 50.0)
        self.assertEqual(found['summary']['requests'], 600)
        self.assertEqual(found['summary']['peak_users'], 11)
        self.assertEqual(found['summary']['cpu_peak'], 21)
        self.assertEqual(len(found['summary']['hours']), 24)

    def test_advice_flags_a_full_disk_and_tight_memory(self):
        now = monitor.live()
        now['disks'] = [{'label': 'System disk', 'pct': 91.0, 'free_gb': 4.0}]
        now['memory'] = dict(now['memory'], pct=90.0, swap_used_mb=1024)
        tips = monitor.advice(now, {'mem_p95': 90, 'mem_peak': 95, 'cpu_p95': 10})
        titles = [t[1] for t in tips]
        self.assertIn('Memory is tight', titles)
        self.assertTrue(any('91.0% full' in t for t in titles))

    def test_the_job_is_registered_on_the_five_minute_timer(self):
        from apps.scheduler.jobs import JOBS
        job = next(j for j in JOBS if j.name == 'system-sample')
        self.assertEqual((job.tick, job.dotted), ('recordings', 'apps.diagnostics.monitor:record_sample'))


@override_settings(SECURE_SSL_REDIRECT=False)
class PageTests(TestCase):
    """The system monitor page, its live feed and the staff dashboard card (developers only)."""
    def setUp(self):
        fresh_store()

    def test_developers_only(self):
        self.client.force_login(make_user('mon-admin', 'admin'))
        self.assertEqual(self.client.get(reverse('diagnostics:system')).status_code, 404)
        self.assertEqual(self.client.get(reverse('diagnostics:system-live')).status_code, 404)

    def test_the_page_and_the_live_feed(self):
        self.client.force_login(make_developer('mon-dev'))
        SystemSample.objects.create(at=timezone.now(), cpu_pct=12, mem_used_mb=400, mem_total_mb=1000,
                                    disk_used_gb=10, disk_total_gb=50, requests=20)
        for span in ('24h', '7d', '30d', '90d', 'nonsense'):
            response = self.client.get(reverse('diagnostics:system'), {'range': span})
            self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Memory (RAM)')
        self.assertContains(response, 'Requests per minute')
        data = self.client.get(reverse('diagnostics:system-live')).json()
        self.assertIn('cpu', data)
        self.assertGreater(data['memory']['total_mb'], 0)

    def test_the_staff_dashboard_shows_the_server_card_to_developers_only(self):
        self.client.force_login(make_user('mon-admin2', 'admin'))
        self.assertNotContains(self.client.get(reverse('staffdesk:home')), 'System monitor')
        self.client.force_login(make_developer('mon-dev2'))
        response = self.client.get(reverse('staffdesk:home'))
        self.assertContains(response, 'System monitor')
        self.assertContains(response, 'requests this hour')


class UploadRedirectTests(TestCase):
    """:class:`UploadRedirectMiddleware` and the upload-progress header."""
    def setUp(self):
        self.factory = RequestFactory()

    def _run(self, response, marked):
        """Pass a POST (optionally marked as an upload) through the middleware."""
        headers = {'HTTP_X_UPLOAD_PROGRESS': '1'} if marked else {}
        request = self.factory.post('/up/', **headers)
        return UploadRedirectMiddleware(lambda r: response)(request)

    def test_a_marked_redirect_becomes_json_and_keeps_cookies(self):
        redirect = HttpResponseRedirect('/done/')
        redirect.set_cookie('messages', 'x')
        out = self._run(redirect, True)
        self.assertEqual(out.status_code, 200)
        self.assertEqual(json.loads(out.content), {'redirect': '/done/'})
        self.assertEqual(out['X-Upload-Redirect'], '1')
        self.assertFalse(out.has_header('Location'))
        self.assertIn('messages', out.cookies)

    def test_anything_else_is_untouched(self):
        self.assertEqual(self._run(HttpResponseRedirect('/done/'), False).status_code, 302)
        self.assertEqual(self._run(HttpResponse('form with errors'), True).content, b'form with errors')
