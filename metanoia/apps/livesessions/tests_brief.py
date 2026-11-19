"""The session summary layout, built from Teams Copilot's insight — with and without Claude.

Graph and Claude are mocked throughout — nothing here talks to Microsoft or spends tokens.

:mod:`.tests_summaries` is imported as a module, not its test class: importing the class here would
make the runner collect and re-run its tests.
"""

import datetime as dt
import json
from unittest import mock

from django.test import TestCase, override_settings

from . import brief
from . import tests_summaries as base
from .tests_summaries import CLASS_END, NOW

INSIGHT = {
    'id': 'ins-1', 'createdDateTime': '2026-10-13T18:05:00Z',
    'meetingNotes': [
        {'title': 'Deferred tax on assessed losses',
         'text': 'The coach explained when a deferred tax asset may be recognised. Probability was debated.',
         'subpoints': [{'title': 'IAS 12.34', 'text': 'probable future taxable profit is required'},
                       {'title': 'Evidence', 'text': 'budgets alone are not enough'}]},
        {'title': 'Karoo Highlands question',
         'text': 'Part (b) was left unfinished. Next session we will finish part (b) and start IAS 16.',
         'subpoints': []},
    ],
    'actionItems': [
        {'title': 'Redo question 2', 'text': 'Students must redo question 2 under exam conditions before the next session.',
         'ownerDisplayName': 'Class'},
        {'title': 'Share the solution', 'text': 'Share the marked solution to question 2.',
         'ownerDisplayName': 'Coach Person'},
        {'title': 'Email the IAS 12 notes', 'text': 'Thabo will email his IAS 12 summary notes to the group.',
         'ownerDisplayName': 'Thabo Mokoena'},
        {'title': 'Read IAS 16', 'text': 'Read IAS 16 paragraphs 31-42.', 'ownerDisplayName': ''},
    ],
}

LLM_ANSWER = {
    'overview': 'We covered when a deferred tax asset on assessed losses can be recognised.',
    'highlights': ['IAS 12.34 needs probable future taxable profit.'],
    'next_agenda': ['Finish Karoo Highlands part (b).'],
    'actions': {'students': [{'task': 'Redo question 2', 'kind': 'must', 'who': ''}],
                'educator': [{'task': 'Share the marked solution', 'kind': 'will', 'who': 'Coach Person'}],
                'everyone': []},
}


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class BriefFromCopilotTests(TestCase):
    """Tests for :func:`brief.from_copilot`, :func:`brief.from_llm` and the fixed summary layout.

    Reuses the fixtures of :class:`~.tests_summaries.SummaryPipelineTests` by borrowing its methods.
    """
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user
    _import = base.SummaryPipelineTests._import

    def _room(self):
        """Import the class and make the coach its host, named "Coach Person" as Copilot names the educator."""
        from apps.communication.models import MeetingRoom
        self._import()
        room = MeetingRoom.objects.get()
        self.coach.first_name, self.coach.last_name = 'Coach', 'Person'
        self.coach.save()
        self.coach.profile.first_name, self.coach.profile.last_name = 'Coach', 'Person'
        self.coach.profile.save()
        room.host = self.coach
        room.save()
        return room

    def test_without_an_llm_copilot_is_mapped_into_the_fixed_sections(self):
        """Without an LLM, Copilot's insight is mapped into the fixed sections.

        The agenda is what the notes say about next time — not a deadline — and an owner already named in
        the task text is not repeated after it.
        """
        room = self._room()
        data = brief.from_copilot([INSIGHT], room)
        text = brief.to_markdown(data, title=room.title)

        headings = [line[3:] for line in text.splitlines() if line.startswith('## ')]
        self.assertEqual(headings, ['Overview', 'Key highlights', 'Agenda for the next session',
                                    'Action items — students', 'Action items — educator',
                                    'Action items — everyone'])
        self.assertIn('This session covered Deferred tax on assessed losses and Karoo Highlands question.',
                      data['overview'])
        self.assertIn('IAS 12.34: probable future taxable profit is required', text)
        self.assertEqual(data['next_agenda'], ['Next session we will finish part (b) and start IAS 16.'])

        students = data['actions']['students']
        self.assertEqual([(a['kind'], a['who']) for a in students], [('must', ''), ('will', 'Thabo Mokoena')])
        self.assertEqual([(a['kind'], a['who']) for a in data['actions']['educator']],
                         [('will', 'Coach Person')])
        self.assertEqual([a['who'] for a in data['actions']['everyone']], [''])
        self.assertIn('- **Must do:** Students must redo question 2', text)
        self.assertIn('- **Will do:** Thabo will email', text)
        self.assertIn('Share the marked solution to question 2. (Coach Person)', text)
        self.assertNotIn('(Thabo Mokoena)', text)

    def test_empty_sections_are_left_out(self):
        room = self._room()
        data = brief.from_copilot([{'meetingNotes': [{'title': 'Intro', 'text': 'We met the class today.'}]}], room)
        text = brief.to_markdown(data)
        self.assertNotIn('Agenda for the next session', text)
        self.assertNotIn('Action items', text)

    def test_owner_groups(self):
        educators = {'coach person', 'coach', 'thembi'}
        self.assertEqual(brief.owner_group('Coach Person', educators), ('educator', 'Coach Person'))
        self.assertEqual(brief.owner_group('Thembi', educators), ('educator', 'Thembi'))
        self.assertEqual(brief.owner_group('Lecturer', educators), ('educator', ''))
        self.assertEqual(brief.owner_group('Students', educators), ('students', ''))
        self.assertEqual(brief.owner_group('Everyone', educators), ('everyone', ''))
        self.assertEqual(brief.owner_group('', educators), ('everyone', ''))
        self.assertEqual(brief.owner_group('Ayanda Dube', educators), ('students', 'Ayanda Dube'))

    def test_with_an_llm_copilot_and_the_transcript_go_into_the_fixed_prompt(self):
        room = self._room()
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.admin_ai.ask',
                           return_value=(json.dumps(LLM_ANSWER), {})) as ask:
            data = brief.from_llm(room, insights=[INSIGHT], transcript='Coach Person: Today, deferred tax.')
        prompt = ask.call_args.args[1]
        for fixed in ('"Overview"', '"Key highlights"', '"Agenda for the next session"', '"Action items"',
                      'TEAMS COPILOT MEETING INSIGHT', 'ACTION (Thabo Mokoena)', 'TRANSCRIPT',
                      'Today, deferred tax.', 'Educator: Coach Person'):
            self.assertIn(fixed, prompt)
        text = brief.to_markdown(data)
        self.assertIn('## Overview\n\nWe covered when a deferred tax asset', text)
        self.assertIn('- **Will do:** Share the marked solution (Coach Person)', text)

    def test_an_llm_failure_or_nonsense_gives_none(self):
        room = self._room()
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=True):
            with mock.patch('apps.ai_assistant.admin_ai.ask', return_value=('not json', {})):
                self.assertIsNone(brief.from_llm(room, insights=[INSIGHT]))
            with mock.patch('apps.ai_assistant.admin_ai.ask',
                            return_value=('Error', {'error': 'credit balance too low'})):
                self.assertIsNone(brief.from_llm(room, insights=[INSIGHT]))
        with mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=False), \
                mock.patch('apps.ai_assistant.admin_ai.ask') as ask:
            self.assertIsNone(brief.from_llm(room, insights=[INSIGHT]))
            ask.assert_not_called()


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class WaitForCopilotTests(TestCase):
    """The summary waits for Copilot (up to 48 hours), then falls back to the transcript."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user
    _import = base.SummaryPipelineTests._import

    VTT = ('WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v Coach>Today we cover deferred tax on assessed '
           'losses in detail.</v>\n\n00:00:06.000 --> 00:00:09.000\n<v Coach>Next week we will start '
           'IAS 16 revaluations.</v>\n')
    TRANSCRIPTS = (200, [{'id': 't1', 'createdDateTime': '2026-10-13T18:01:00Z'}])

    def _process(self, *, now, insights=(200, []), detail=None, llm=False, answer=None):
        """Run :func:`summaries.process` at ``now`` with Graph, OneDrive, Claude and notifications mocked.

        Returns ``(status, notify_mock, ask_mock)``.
        """
        from apps.communication.models import MeetingRoom
        from apps.livesessions import summaries
        room = MeetingRoom.objects.get()
        folder = {'drive_id': 'D', 'folder_id': 'F', 'path': 'p', 'web_url': '', 'owner_upn': 'c'}
        with mock.patch('apps.msteams.graph.ai_insights_with_status', return_value=insights), \
                mock.patch('apps.msteams.graph.get_ai_insight', return_value=detail), \
                mock.patch('apps.msteams.graph.transcripts_with_status', return_value=self.TRANSCRIPTS), \
                mock.patch('apps.msteams.graph.get_transcript_content', return_value=self.VTT), \
                mock.patch('apps.livesessions.pipeline.transcript_text', return_value=self.VTT), \
                mock.patch('apps.msteams.onedrive.ensure_path', return_value=folder), \
                mock.patch('apps.msteams.onedrive.put_text', return_value={'id': 'I', 'webUrl': 'u'}), \
                mock.patch('apps.msteams.onedrive.anonymous_link', return_value=''), \
                mock.patch('apps.livesessions.ai_followup.is_enabled', return_value=False), \
                mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=llm), \
                mock.patch('apps.ai_assistant.admin_ai.ask', return_value=(answer or '', {})) as ask, \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            status = summaries.process(room, now=now)
        return status, notify, ask

    def test_waits_for_copilot_even_when_a_transcript_exists(self):
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, notify, _ask = self._process(now=NOW)
        self.assertEqual(status, 'waiting')
        artifact = SessionArtifact.objects.get()
        self.assertTrue(artifact.summary_note.startswith('Waiting for the Teams Copilot summary'))
        self.assertFalse(artifact.summary_published_at)
        notify.assert_not_called()

    def test_copilot_arrives_and_the_class_gets_the_structured_summary_by_email(self):
        """Copilot arrives and the class gets the structured summary by e-mail.

        Copilot names the educator "Coach Person", which is set as the host's name; no LLM is configured, so
        Claude is never asked.
        """
        from apps.ai_assistant.models import AiReport
        from apps.livesessions.models import SessionArtifact
        self._import()
        self.coach.profile.first_name, self.coach.profile.last_name = 'Coach', 'Person'
        self.coach.profile.save()
        self._process(now=NOW)
        status, notify, ask = self._process(now=NOW, insights=(200, [INSIGHT]), detail=INSIGHT)
        self.assertEqual(status, 'published')
        ask.assert_not_called()
        artifact = SessionArtifact.objects.get()
        self.assertEqual(artifact.summary_source, SessionArtifact.SUMMARY_TEAMS)
        recap = AiReport.objects.get(linked_ref=f'meeting:{artifact.meeting_id}').content
        self.assertIn('## Overview', recap)
        self.assertIn('## Action items — students', recap)
        call = notify.call_args_list[0].kwargs
        self.assertTrue(call['email'])
        for expected in ('OVERVIEW', 'KEY HIGHLIGHTS', 'AGENDA FOR THE NEXT SESSION',
                         'ACTION ITEMS — STUDENTS', 'ACTION ITEMS — EDUCATOR', 'Must do: Students must redo'):
            self.assertIn(expected, call['body'])
        told = {c.args[0] for c in notify.call_args_list}
        self.assertEqual(told, {self.mine, self.coach})

    def test_with_an_llm_copilot_plus_transcript_are_summarised_once(self):
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, _notify, ask = self._process(now=NOW, insights=(200, [INSIGHT]), detail=INSIGHT,
                                             llm=True, answer=json.dumps(LLM_ANSWER))
        self.assertEqual(status, 'published')
        self.assertEqual(ask.call_count, 1)
        self.assertIn('Today we cover deferred tax', ask.call_args.args[1])
        self.assertEqual(SessionArtifact.objects.get().summary_source, SessionArtifact.SUMMARY_TEAMS)

    def test_after_48_hours_the_transcript_is_used_in_the_same_layout(self):
        from apps.ai_assistant.models import AiReport
        from apps.livesessions.models import SessionArtifact
        self._import()
        later = CLASS_END + dt.timedelta(hours=48, minutes=1)
        status, notify, _ask = self._process(now=later)
        self.assertEqual(status, 'published')
        artifact = SessionArtifact.objects.get()
        self.assertEqual(artifact.summary_source, SessionArtifact.SUMMARY_EXTRACTIVE)
        recap = AiReport.objects.get(linked_ref=f'meeting:{artifact.meeting_id}').content
        self.assertIn('## Overview', recap)
        self.assertIn('## Agenda for the next session', recap)
        self.assertIn('Next week we will start IAS 16 revaluations.', recap)
        self.assertTrue(notify.called)

    def test_a_403_does_not_wait(self):
        from apps.livesessions.models import SessionArtifact
        self._import()
        status, _n, _a = self._process(now=NOW, insights=(403, []))
        self.assertEqual(status, 'published')
        self.assertEqual(SessionArtifact.objects.get().summary_source, SessionArtifact.SUMMARY_EXTRACTIVE)


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
                   MS_GRAPH_DEFAULT_ORGANIZER='coach@metanoia.example')
class PipelineWaitsForCopilotTests(TestCase):
    """The pipeline's summary step holds the session until Copilot's insight is in."""
    setUp = base.SummaryPipelineTests.setUp
    _user = base.SummaryPipelineTests._user

    def _session(self, ended_ago):
        """Create a module session that ended ``ended_ago`` and its artifact waiting in the summarising state."""
        from django.utils import timezone

        from apps.communication.models import MeetingRoom
        from apps.livesessions.models import SessionArtifact
        end = timezone.now() - ended_ago
        room = MeetingRoom.objects.create(
            title='MP PGDA P26F | TAX', host=self.coach, provider=MeetingRoom.PROVIDER_TEAMS,
            teams_meeting_id='MEETING-1', organizer_upn='coach@metanoia.example',
            scheduled_start=end - dt.timedelta(hours=2), scheduled_end=end,
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.pm, cohort=self.p26f,
            programme=self.pm.programme, publish_recording=True)
        return SessionArtifact.objects.create(
            meeting=room, state=SessionArtifact.STATE_SUMMARISING,
            recording_item_id='REC', recording_share_url='https://1drv.ms/recording')

    def _step(self, artifact, insights):
        """Run one :func:`pipeline.step` with Graph returning ``insights``; return ``(moved, sync_mock, notify_mock)``."""
        from apps.livesessions import pipeline
        detail = insights[1][0] if insights[1] else None
        with mock.patch('apps.msteams.services.sync_meeting') as sync, \
                mock.patch('apps.msteams.graph.ai_insights_with_status', return_value=insights), \
                mock.patch('apps.msteams.graph.get_ai_insight', return_value=detail), \
                mock.patch('apps.msteams.graph.transcripts_with_status', return_value=(200, [])), \
                mock.patch('apps.msteams.onedrive.ensure_path', return_value=None), \
                mock.patch('apps.livesessions.ai_followup.is_enabled', return_value=False), \
                mock.patch('apps.ai_assistant.admin_ai.is_enabled', return_value=False), \
                mock.patch('apps.communication.services.notify', return_value=True) as notify:
            moved = pipeline.step(artifact)
        artifact.refresh_from_db()
        return moved, sync, notify

    def test_holds_then_publishes_when_copilot_arrives(self):
        """The step holds the session (backing off between quick-run asks), then publishes once Copilot arrives."""
        from django.utils import timezone

        from apps.livesessions import pipeline
        from apps.livesessions.models import SessionArtifact
        artifact = self._session(dt.timedelta(hours=1))
        moved, sync, notify = self._step(artifact, (200, []))
        self.assertFalse(moved)
        self.assertEqual(artifact.state, SessionArtifact.STATE_SUMMARISING)
        self.assertTrue(artifact.announced_at, 'the recording is announced without waiting')
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(artifact.attempts, 0, 'waiting is not a failure')
        self.assertFalse(pipeline._due_for_quick_run(artifact, timezone.now()))
        self.assertTrue(pipeline._due_for_quick_run(artifact, timezone.now() + dt.timedelta(minutes=31)))

        insight = dict(INSIGHT, createdDateTime=(artifact.meeting.scheduled_end + dt.timedelta(minutes=5))
                       .strftime('%Y-%m-%dT%H:%M:%SZ'))
        moved, _sync, notify = self._step(artifact, (200, [insight]))
        self.assertTrue(moved)
        self.assertEqual(artifact.state, SessionArtifact.STATE_PUBLISHED)
        self.assertEqual(artifact.summary_source, SessionArtifact.SUMMARY_TEAMS)

    def test_gives_up_waiting_after_48_hours(self):
        from apps.livesessions.models import SessionArtifact
        artifact = self._session(dt.timedelta(hours=49))
        moved, _sync, _notify = self._step(artifact, (200, []))
        self.assertTrue(moved)
        self.assertEqual(artifact.state, SessionArtifact.STATE_PUBLISHED)

    def test_the_quick_run_still_sees_a_class_waiting_past_36_hours(self):
        from apps.livesessions import pipeline
        artifact = self._session(dt.timedelta(hours=40))
        with mock.patch.object(pipeline, 'step', return_value=False) as step, \
                mock.patch.object(pipeline, 'enqueue_finished_sessions', return_value=0):
            pipeline.run(recent_only=True)
        self.assertEqual([c.args[0].pk for c in step.call_args_list], [artifact.pk])
