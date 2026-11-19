"""Regression tests for the faults cleared from the error log on 2026-10-22.

Each test pins one fix, so the entry it closed stays closed.
"""

from unittest import mock

from django.test import TestCase

from apps.livesessions.tests import make_user
from core.testing import enrol, make_module


class FeedMessagesTests(TestCase):
    """FEED-9001: the messages section asked a ChatGroup for a ``module`` field."""

    def test_messages_from_a_module_group_reach_the_feed(self):
        from apps.communication.feed import build_feed
        from apps.communication.models import ChatGroup, Message
        offering = make_module('Tax')
        student, peer = make_user('feed-a'), make_user('feed-b')
        enrol(student.profile, offering)
        group = ChatGroup.objects.create(kind=ChatGroup.KIND_SUBJECT, name='Tax group')
        offering.chat_group = group
        offering.save(update_fields=['chat_group'])
        group.add_member(student)
        group.add_member(peer)
        Message.objects.create(group=group, sender=peer, body='Anyone done Q3?')

        with mock.patch('apps.communication.feed.report') as report:
            items = build_feed(student, module_id=offering.pk)
        report.assert_not_called()
        self.assertTrue(any(i.get('body') == 'Anyone done Q3?' for i in items))


class SchedulerMigrationGuardTests(TestCase):
    """SCHD-9001 ×6: jobs ran new code before ``migrate`` had added its columns."""

    def test_a_tick_is_skipped_while_migrations_are_pending(self):
        from apps.scheduler import runner
        with mock.patch('apps.scheduler.runner.pending_migrations', return_value=['livesessions.0099_x']), \
                mock.patch('apps.scheduler.runner.run_job') as run_job:
            results = runner.tick()
        run_job.assert_not_called()
        self.assertIn('pending', results[0][3])

    def test_the_check_reports_nothing_on_a_migrated_database(self):
        from apps.scheduler import runner
        self.assertEqual(runner.pending_migrations(), [])


class SettingsBeforeFirstSaveTests(TestCase):
    """SCHD-9001: an unsaved settings row held a DatabaseDefault, not a string."""

    def test_coach_addresses_read_cleanly_from_an_unsaved_row(self):
        from apps.livesessions.models import LiveSessionSettings
        self.assertEqual(LiveSessionSettings().coach_email_set, set())


class GraphExpectedAnswersTests(TestCase):
    """SYS-9001 ×3,500: normal Graph answers (409 exists, 403 refused) logged as errors."""

    def _response(self, status):
        return mock.Mock(status_code=status, text='{"error": {}}', content=b'{}')

    def test_an_expected_status_is_logged_at_info(self):
        from apps.msteams import graph
        with mock.patch('apps.msteams.graph._get_token', return_value='t'), \
                mock.patch('requests.request', return_value=self._response(409)), \
                self.assertLogs('apps', level='INFO') as logs:
            status, data = graph._request_ex('POST', '/x', expected=(409,))
        self.assertEqual((status, data), (409, None))
        self.assertTrue(all(r.levelname == 'INFO' for r in logs.records))

    def test_an_unexpected_status_is_still_an_error(self):
        from apps.msteams import graph
        with mock.patch('apps.msteams.graph._get_token', return_value='t'), \
                mock.patch('requests.request', return_value=self._response(500)), \
                self.assertLogs('apps', level='INFO') as logs:
            graph._request_ex('GET', '/x', expected=(409,))
        self.assertEqual(logs.records[0].levelname, 'ERROR')

    def test_a_refused_transcript_list_is_reported_once_per_process(self):
        from apps.msteams import graph
        graph._refusals_logged.clear()
        with mock.patch('apps.msteams.graph.resolve_user_id', return_value='org'), \
                mock.patch('apps.msteams.graph._request_ex', return_value=(403, None)), \
                self.assertLogs('apps', level='ERROR') as logs:
            for meeting in ('m1', 'm2', 'm3'):
                self.assertEqual(graph.list_transcripts('o@x', meeting), [])
        self.assertEqual(len(logs.records), 1)
        graph._refusals_logged.clear()


class FaviconTests(TestCase):
    """SYS-9001 ×1,197: /favicon.ico answered 404."""

    def test_favicon_redirects_to_the_static_icon_without_signing_in(self):
        response = self.client.get('/favicon.ico', secure=True)
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response['Location'].endswith('favicon.ico'))
        self.assertIn('/static/', response['Location'])
