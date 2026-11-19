"""Tests for the parts of the live-session layer that must not be got wrong.

The theme throughout is the invariant stated in :mod:`apps.livesessions.audience`:
**who is notified and who can see a session on the calendar are the same set.**
Most of these tests exist to catch that drifting apart, because the failure is
silent — a student simply never hears about a class, or finds one they cannot open.
"""

from datetime import timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person, UserSettings
from apps.communication.models import MeetingParticipant, MeetingRoom
from apps.learning.models import (Cohort, Institution, Module, Programme,
                                  ProgrammeEnrolment, ProgrammeModule, ModuleEnrolment)

from . import audience, reminders, services, sources, thumbnails
from .models import LiveSessionSettings, SessionArtifact, SessionReminder

User = get_user_model()


def make_user(username, user_type='student', *, first_name='', last_name='', **kwargs):
    """A signed-up user who is past onboarding.

    ``registered`` / ``profile_status`` matter for the view tests: without them
    OnboardingMiddleware bounces every request to the registration wizard, and
    every assertion about a page would be an assertion about a redirect.

    The names are set here rather than by the caller because a signal already
    created the Person and cached it on ``user.profile``; a caller that edits and
    saves that cached copy silently writes the pre-signal values back over
    everything set below. The user is re-read before it is returned, dropping
    the signal's stale copy so ``user.profile`` reads what was just saved.
    """
    user = User.objects.create_user(username=username, email=f'{username}@example.com',
                                    password='x', **kwargs)
    person, _ = Person.objects.get_or_create(user=user)
    person.user_type = user_type
    person.first_name = first_name
    person.last_name = last_name
    person.registered = True
    person.profile_status = True
    person.save()
    user.refresh_from_db()
    return user


class AudienceScopingTests(TestCase):
    """Notification audience and calendar visibility must stay identical."""

    @classmethod
    def setUpTestData(cls):
        """Create a PGDA FREP offering with its educator, an enrolled P26F student, an outsider and an admin."""
        cls.institution = Institution.objects.create(code='MILPARK', name='Milpark Education')
        cls.programme = Programme.objects.create(institution=cls.institution, code='PGDA',
                                                 name='Postgraduate Diploma in Accounting')
        cls.cohort = Cohort.objects.create(programme=cls.programme, code='P26F')
        canonical = Module.objects.create(code='FR', name='Financial Reporting')
        cls.module = ProgrammeModule.objects.create(programme=cls.programme,
                                                    module=canonical, code='FREP')

        cls.educator = make_user('coach', 'educator')
        cls.educator.profile.taught_modules.add(cls.module)

        cls.enrolled = make_user('enrolled')
        ProgrammeEnrolment.objects.create(person=cls.enrolled.profile, programme=cls.programme,
                                          cohort=cls.cohort)
        ModuleEnrolment.objects.create(person=cls.enrolled.profile, programme_module=cls.module)

        cls.outsider = make_user('outsider')
        cls.admin = make_user('boss', 'admin', is_staff=True)

    def _session(self, **kwargs):
        """Create a Teams session starting tomorrow, hosted by the educator, with ``kwargs`` overriding the defaults."""
        start = timezone.now() + timedelta(days=1)
        defaults = dict(title='Class', host=self.educator, scheduled_start=start,
                        scheduled_end=start + timedelta(hours=1),
                        provider=MeetingRoom.PROVIDER_TEAMS)
        defaults.update(kwargs)
        return MeetingRoom.objects.create(**defaults)

    def assert_audience_matches_visibility(self, meeting, expected_can_see):
        """The core invariant, asserted both ways round.

        Being told is a subset of being able to see: everyone notified can open
        the session, and everyone who can open it was notified *unless* they are
        looking through the supervisory widening (admin/staff, or an educator on
        their own module). See the module docstring of ``livesessions.audience``.
        """
        told = set(audience.recipients_for(meeting).values_list('pk', flat=True))
        for user, should_see in expected_can_see.items():
            self.assertEqual(audience.can_view(user, meeting), should_see,
                             f'{user} visibility')
            if should_see and user.pk != meeting.host_id and user.pk not in told:
                self.assertTrue(audience.is_supervisory_viewer(user, meeting),
                                f'{user} can see it, is not told, and has no supervisory reason to')
            if not should_see:
                self.assertNotIn(user.pk, told, f'{user} is told but cannot see it')

    def test_module_session_reaches_exactly_the_module(self):
        meeting = self._session(audience=MeetingRoom.AUDIENCE_MODULE, module=self.module)
        self.assert_audience_matches_visibility(meeting, {
            self.enrolled: True, self.outsider: False, self.educator: True, self.admin: True})

    def test_programme_session_reaches_the_programme(self):
        meeting = self._session(audience=MeetingRoom.AUDIENCE_PROGRAMME, programme=self.programme)
        self.assert_audience_matches_visibility(meeting, {
            self.enrolled: True, self.outsider: False, self.admin: True})

    def test_cohort_session_reaches_only_that_intake(self):
        other = Cohort.objects.create(programme=self.programme, code='P26S')
        elsewhere = make_user('other-intake')
        ProgrammeEnrolment.objects.create(person=elsewhere.profile, programme=self.programme,
                                          cohort=other)
        meeting = self._session(audience=MeetingRoom.AUDIENCE_COHORT, cohort=self.cohort)
        self.assert_audience_matches_visibility(meeting, {
            self.enrolled: True, elsewhere: False, self.outsider: False})

    def test_a_module_class_for_one_intake_skips_the_other_intakes(self):
        """FREP is shared by P26F and P26S; a P26F class is not P26S's.

        Students on the programme with no intake set still get it — nothing says
        it is not theirs (the rule of ``audience.class_students``).
        """
        other = Cohort.objects.create(programme=self.programme, code='P26S')
        elsewhere = make_user('p26s-student')
        ProgrammeEnrolment.objects.create(person=elsewhere.profile, programme=self.programme,
                                          cohort=other)
        ModuleEnrolment.objects.create(person=elsewhere.profile, programme_module=self.module)
        unplaced = make_user('no-intake')
        ProgrammeEnrolment.objects.create(person=unplaced.profile, programme=self.programme)
        ModuleEnrolment.objects.create(person=unplaced.profile, programme_module=self.module)

        meeting = self._session(audience=MeetingRoom.AUDIENCE_MODULE, module=self.module,
                                cohort=self.cohort)
        self.assert_audience_matches_visibility(meeting, {
            self.enrolled: True, unplaced: True, elsewhere: False,
            self.educator: True, self.admin: True})

    def test_private_session_reaches_invitees_only(self):
        meeting = self._session(audience=MeetingRoom.AUDIENCE_PRIVATE)
        MeetingParticipant.objects.create(meeting=meeting, user=self.outsider)
        self.assert_audience_matches_visibility(meeting, {
            self.outsider: True, self.enrolled: False})
        self.assertTrue(meeting.is_one_on_one)

    def test_everyone_session_reaches_everyone(self):
        meeting = self._session(audience=MeetingRoom.AUDIENCE_EVERYONE)
        self.assert_audience_matches_visibility(meeting, {
            self.enrolled: True, self.outsider: True, self.admin: True})

    def test_derived_audience_with_a_cleared_scope_tells_nobody(self):
        """A module session whose module was deleted must not fall open."""
        meeting = self._session(audience=MeetingRoom.AUDIENCE_MODULE, module=None)
        self.assertFalse(audience.recipients_for(meeting, include_host=False).exists())
        self.assertFalse(audience.can_view(self.enrolled, meeting))

    def test_admin_sees_the_whole_calendar(self):
        self._session(audience=MeetingRoom.AUDIENCE_MODULE, module=self.module)
        self._session(audience=MeetingRoom.AUDIENCE_PRIVATE, title='Private')
        self.assertEqual(audience.visible_sessions(self.admin).count(), 2)
        self.assertEqual(audience.visible_sessions(self.outsider).count(), 0)


class ReminderTests(TestCase):
    """Reminders fire once per lead, per person — and read in their own clock."""

    def setUp(self):
        """Create a private session 20 minutes away with one invited student."""
        self.host = make_user('host', 'educator')
        self.student = make_user('sam')
        self.meeting = MeetingRoom.objects.create(
            title='Deferred tax', host=self.host,
            audience=MeetingRoom.AUDIENCE_PRIVATE,
            scheduled_start=timezone.now() + timedelta(minutes=20),
            scheduled_end=timezone.now() + timedelta(minutes=80),
            provider=MeetingRoom.PROVIDER_TEAMS)
        MeetingParticipant.objects.create(meeting=self.meeting, user=self.student)

    def test_a_reminder_is_sent_once_however_often_the_job_runs(self):
        """One reminder per lead per person (the student and the host), however often the job runs."""
        first = reminders.send_for(self.meeting, 30)
        self.assertEqual(first, 2)
        for _ in range(3):
            self.assertEqual(reminders.send_for(self.meeting, 30), 0)
        self.assertEqual(
            SessionReminder.objects.filter(meeting=self.meeting, lead_minutes=30).count(), 2)

    def test_each_lead_is_tracked_separately(self):
        reminders.send_for(self.meeting, 30)
        self.assertEqual(reminders.send_for(self.meeting, 1440), 2)

    def test_the_run_only_sends_leads_that_have_arrived(self):
        """Only leads that have arrived are sent: 20 hours out, the 24-hour lead has arrived and the 30-minute one has not."""
        far = MeetingRoom.objects.create(
            title='Next week', host=self.host, audience=MeetingRoom.AUDIENCE_PRIVATE,
            scheduled_start=timezone.now() + timedelta(hours=20),
            provider=MeetingRoom.PROVIDER_TEAMS)
        MeetingParticipant.objects.create(meeting=far, user=self.student)
        reminders.run()
        leads = set(SessionReminder.objects.filter(meeting=far).values_list('lead_minutes', flat=True))
        self.assertEqual(leads, {1440})

    def _with_zone(self, user, zone):
        """Set a zone and re-read the user the way the reminder loop does.

        The loop iterates a fresh queryset, so it never sees a stale cached
        ``account_settings``; reloading here keeps the test honest about that.
        """
        row = UserSettings.for_user(user)
        row.timezone = zone
        row.save(update_fields=['timezone', 'updated_at'])
        return User.objects.get(pk=user.pk)

    def test_times_render_in_the_recipients_own_zone(self):
        """Times render in each recipient's zone: 12:00 UTC is 13:00 in Lagos (UTC+1) and 14:00 in Johannesburg (UTC+2)."""
        moment = timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
        lagos = reminders.local_when(moment, self._with_zone(self.student, 'Africa/Lagos'))
        joburg = reminders.local_when(moment, self._with_zone(self.host, 'Africa/Johannesburg'))

        self.assertIn('13:00', lagos)
        self.assertIn('14:00', joburg)
        self.assertEqual(reminders.zone_label(User.objects.get(pk=self.student.pk)),
                         'Africa/Lagos')

    def test_a_nonsense_zone_falls_back_instead_of_raising(self):
        student = self._with_zone(self.student, 'Mars/Olympus_Mons')
        self.assertIsNotNone(reminders.local_when(timezone.now(), student))


class StoragePathTests(TestCase):
    """The OneDrive tree is institution → programme → module → session."""

    def test_path_parts_follow_the_academic_spine(self):
        """Path parts follow the academic spine; the colon, illegal in a OneDrive item name, is stripped from the session part."""
        institution = Institution.objects.create(code='UNISA', name='University of South Africa')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='TAX', name='Taxation')
        offering = ProgrammeModule.objects.create(programme=programme, module=canonical, code='TAXA')
        start = timezone.now()
        meeting = MeetingRoom.objects.create(title='Q&A: IAS 12', module=offering,
                                             audience=MeetingRoom.AUDIENCE_MODULE,
                                             scheduled_start=start,
                                             provider=MeetingRoom.PROVIDER_TEAMS)
        parts = meeting.storage_path_parts
        self.assertEqual(parts[0], 'UNISA')
        self.assertEqual(parts[2], 'TAXA')
        self.assertNotIn(':', parts[3])
        self.assertIn('IAS 12', parts[3])

    def test_a_platform_wide_session_still_gets_a_folder(self):
        meeting = MeetingRoom.objects.create(title='Orientation',
                                             audience=MeetingRoom.AUDIENCE_EVERYONE,
                                             scheduled_start=timezone.now(),
                                             provider=MeetingRoom.PROVIDER_TEAMS)
        self.assertEqual(meeting.storage_path_parts[0], 'General')


class ThumbnailTests(TestCase):
    """Tests for :func:`thumbnails.render`."""
    def test_a_thumbnail_is_drawn_without_a_background_image(self):
        """A session with no background image still renders a JPEG thumbnail."""
        meeting = MeetingRoom.objects.create(title='Consolidations workshop',
                                             audience=MeetingRoom.AUDIENCE_EVERYONE,
                                             scheduled_start=timezone.now(),
                                             provider=MeetingRoom.PROVIDER_TEAMS)
        blob = thumbnails.render(meeting)
        self.assertTrue(blob and blob[:2] == b'\xff\xd8')


class YouTubeQuotaTests(TestCase):
    """The quota gate is what stops the seventh upload of the day failing."""

    def test_quota_runs_out_after_the_configured_number_of_uploads(self):
        from . import youtube

        conf = LiveSessionSettings.load()
        conf.youtube_daily_quota = 10000
        conf.save()

        self.assertTrue(youtube.quota_available())
        self.assertEqual(youtube.uploads_remaining_today(), 6)

        for i in range(6):
            meeting = MeetingRoom.objects.create(title=f'S{i}', scheduled_start=timezone.now(),
                                                 provider=MeetingRoom.PROVIDER_TEAMS)
            SessionArtifact.objects.create(meeting=meeting, youtube_uploaded_at=timezone.now())

        self.assertFalse(youtube.quota_available())
        self.assertEqual(youtube.uploads_remaining_today(), 0)


class FreeSlotTests(TestCase):
    """Tests for :func:`services.free_slots`."""
    def test_overlapping_sessions_do_not_produce_a_phantom_gap(self):
        """Overlapping sessions (10:00–11:00 and 10:30–11:30) merge into one busy block with no phantom gap inside it."""
        host = make_user('slots', 'educator')
        day = timezone.localdate() + timedelta(days=1)
        base = timezone.make_aware(
            timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=10)),
            timezone.get_current_timezone())
        for offset in (0, 30):
            MeetingRoom.objects.create(
                title=f'Class {offset}', host=host, provider=MeetingRoom.PROVIDER_TEAMS,
                scheduled_start=base + timedelta(minutes=offset),
                scheduled_end=base + timedelta(minutes=offset + 60))

        gaps = services.free_slots(day)
        self.assertTrue(all(end <= base or start >= base + timedelta(minutes=90)
                            for start, end in gaps),
                        f'a gap overlapped the merged block: {gaps}')


class SettingsTests(TestCase):
    """Tests for the :class:`~.models.LiveSessionSettings` singleton and its parsed fields."""
    def test_the_settings_row_is_a_singleton(self):
        first = LiveSessionSettings.load()
        first.onedrive_root = 'Archive'
        first.save()
        LiveSessionSettings.objects.create(onedrive_root='Other')
        self.assertEqual(LiveSessionSettings.objects.count(), 1)
        self.assertEqual(LiveSessionSettings.load().onedrive_root, 'Other')

    def test_reminder_leads_parse_longest_first(self):
        conf = LiveSessionSettings.load()
        conf.reminder_leads = '30, 1440, 30, bogus, 0'
        self.assertEqual(conf.lead_minutes, [1440, 30])


class ViewTests(TestCase):
    """The pages render, and the scoping holds at the HTTP layer too.

    Worth asserting separately from :class:`AudienceScopingTests`: a correct
    queryset that a view forgets to apply is exactly as leaky as a wrong one.
    """

    @classmethod
    def setUpTestData(cls):
        """Create a MACF module session with its educator, an enrolled student, a stranger and a superuser."""
        institution = Institution.objects.create(code='IAS', name='IAS')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='MAF', name='Management Accounting')
        cls.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                    code='MACF')
        cls.educator = make_user('teacher', 'educator')
        cls.educator.profile.taught_modules.add(cls.module)
        cls.student = make_user('candidate')
        ModuleEnrolment.objects.create(person=cls.student.profile, programme_module=cls.module)
        cls.stranger = make_user('nobody')
        cls.admin = make_user('chief', 'admin', is_staff=True, is_superuser=True)

        start = timezone.now() + timedelta(days=2)
        cls.meeting = MeetingRoom.objects.create(
            title='Variance analysis', host=cls.educator, module=cls.module,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=start,
            scheduled_end=start + timedelta(hours=1), provider=MeetingRoom.PROVIDER_TEAMS)

    def test_calendar_renders_for_every_role(self):
        for user in (self.student, self.educator, self.admin, self.stranger):
            self.client.force_login(user)
            response = self.client.get('/calendar/')
            self.assertEqual(response.status_code, 200, f'{user} on the calendar')

    def test_a_stranger_cannot_open_a_session_they_are_not_in(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.meeting.get_absolute_url()).status_code, 404)

        self.client.force_login(self.student)
        self.assertEqual(self.client.get(self.meeting.get_absolute_url()).status_code, 200)

    def test_the_ics_feed_only_carries_visible_sessions(self):
        self.client.force_login(self.stranger)
        body = self.client.get('/calendar/metanoia-calendar.ics').content.decode()
        self.assertNotIn('Variance analysis', body)

        self.client.force_login(self.student)
        body = self.client.get('/calendar/metanoia-calendar.ics').content.decode()
        self.assertIn('Variance analysis', body)
        self.assertIn('BEGIN:VCALENDAR', body)

    def test_the_json_feed_is_scoped_too(self):
        self.client.force_login(self.stranger)
        entries = self.client.get('/calendar/feed.json').json()['entries']
        self.assertEqual([e for e in entries if e['kind'] == 'session'], [])

    def test_only_schedulers_reach_the_new_session_form(self):
        self.client.force_login(self.student)
        self.assertRedirects(self.client.get('/calendar/sessions/new/'), '/calendar/')

        self.client.force_login(self.educator)
        self.assertEqual(self.client.get('/calendar/sessions/new/').status_code, 200)

    def test_settings_are_admin_only(self):
        self.client.force_login(self.educator)
        self.assertEqual(self.client.get('/calendar/settings/').status_code, 404)

        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/calendar/settings/').status_code, 200)

    def test_day_and_past_session_pages_render(self):
        self.client.force_login(self.admin)
        on = timezone.localdate().strftime('%Y-%m-%d')
        self.assertEqual(self.client.get(f'/calendar/day/{on}/').status_code, 200)
        self.assertEqual(self.client.get('/calendar/sessions/past/').status_code, 200)

    def test_a_bad_date_is_a_404_not_a_500(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/calendar/day/not-a-date/').status_code, 404)


class SchedulingTests(TestCase):
    """Creating a session wires up everything that hangs off it."""

    def setUp(self):
        """Create an APC offering with an educator and one enrolled student."""
        institution = Institution.objects.create(code='SAICA', name='SAICA')
        programme = Programme.objects.create(institution=institution, code='APC', name='APC')
        canonical = Module.objects.create(code='APC', name='Assessment of Professional Competence')
        self.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                     code='APC1')
        self.educator = make_user('mentor', 'educator')
        self.student = make_user('learner')
        ModuleEnrolment.objects.create(person=self.student.profile, programme_module=self.module)

    def test_creating_a_session_opens_the_register_and_the_pipeline(self):
        """Creating a session opens the register and the pipeline.

        Graph is not configured in tests, so it is still a Teams session (the only engine), saved without a
        meeting until the first join.
        """
        start = timezone.now() + timedelta(days=1)
        meeting = services.create_session(
            host=self.educator, title='Case study walkthrough', start=start,
            end=start + timedelta(hours=2), audience=MeetingRoom.AUDIENCE_MODULE,
            module=self.module, notify=False)

        self.assertEqual(meeting.audience, MeetingRoom.AUDIENCE_MODULE)
        self.assertTrue(meeting.thumbnail, 'a thumbnail should have been drawn')
        self.assertIsNotNone(getattr(meeting, 'class_session', None),
                             'a module session needs an attendance register')
        self.assertTrue(meeting.participants.filter(user=self.educator, role='host').exists())
        self.assertTrue(meeting.is_teams)
        self.assertEqual(meeting.teams_join_url, '')

    def test_a_platform_wide_session_gets_no_register(self):
        start = timezone.now() + timedelta(days=1)
        meeting = services.create_session(
            host=self.educator, title='Welcome', start=start, end=start + timedelta(hours=1),
            audience=MeetingRoom.AUDIENCE_EVERYONE, notify=False)
        self.assertIsNone(getattr(meeting, 'class_session', None))

    def test_announcing_a_session_notifies_the_audience_once_each(self):
        from apps.communication.models import Notification

        start = timezone.now() + timedelta(days=1)
        meeting = services.create_session(
            host=self.educator, title='Kick-off', start=start, end=start + timedelta(hours=1),
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.module, notify=True)

        told = Notification.objects.filter(meeting=meeting, verb='session scheduled')
        self.assertEqual(told.filter(recipient=self.student).count(), 1)
        self.assertEqual(told.filter(recipient=self.educator).count(), 1)


class SessionFormTests(TestCase):
    """The form is where a session that nobody can see gets stopped."""

    def setUp(self):
        """Create an admin scheduler and a BCTA auditing offering."""
        self.admin = make_user('scheduler', 'admin', is_staff=True)
        institution = Institution.objects.create(code='MILPARK2', name='Milpark Two')
        self.programme = Programme.objects.create(institution=institution, code='BCTA',
                                                  name='BCTA')
        canonical = Module.objects.create(code='AUD', name='Auditing')
        self.module = ProgrammeModule.objects.create(programme=self.programme, module=canonical,
                                                     code='AUDT')

    def _payload(self, **overrides):
        """Return a valid class-session form payload starting tomorrow, with ``overrides`` applied."""
        start = timezone.now() + timedelta(days=1)
        payload = {
            'target': 'class', 'title_suffix': 'Session', 'description': '', 'session_kind': 'class',
            'scheduled_start': start.strftime('%Y-%m-%dT%H:%M'),
            'scheduled_end': (start + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            'institution': self.programme.institution_id, 'programme': self.programme.pk,
            'module': self.module.pk, 'recurrence': '',
        }
        payload.update(overrides)
        return payload

    def test_a_class_needs_its_institution(self):
        from .forms import SessionForm
        form = SessionForm(self._payload(institution='', programme='', module=''), user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('institution', form.errors)

    def test_an_invite_only_session_needs_an_invitee(self):
        from .forms import SessionForm
        form = SessionForm(self._payload(target='people', module=''), user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('invitees', form.errors)

    def test_people_sessions_are_private_and_keep_their_invitees(self):
        from .forms import SessionForm
        guest = make_user('guest-person')
        form = SessionForm(self._payload(target='people', invitees=[guest.pk]), user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['audience'], MeetingRoom.AUDIENCE_PRIVATE)
        self.assertEqual(list(form.cleaned_data['invitees']), [guest])

    def test_a_session_cannot_end_before_it_starts(self):
        from .forms import SessionForm
        start = timezone.now() + timedelta(days=1)
        form = SessionForm(self._payload(
            scheduled_start=start.strftime('%Y-%m-%dT%H:%M'),
            scheduled_end=(start - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')), user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('scheduled_end', form.errors)

    def test_a_valid_module_session_passes_with_the_generated_title(self):
        from .forms import SessionForm
        form = SessionForm(self._payload(title_suffix='Group statements'), user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['audience'], MeetingRoom.AUDIENCE_MODULE)
        title = form.cleaned_data['title']
        self.assertTrue(title.startswith('MILPARK2 BCTA AUDT · '), title)
        self.assertTrue(title.endswith(' | Group statements'), title)

    def test_the_most_specific_choice_sets_the_audience(self):
        from apps.learning.models import Cohort
        from .forms import SessionForm
        cohort = Cohort.objects.create(programme=self.programme, code='B27S', name='B27S')
        cases = [({'module': '', 'cohort': cohort.pk}, MeetingRoom.AUDIENCE_COHORT),
                 ({'module': ''}, MeetingRoom.AUDIENCE_PROGRAMME),
                 ({'module': '', 'programme': ''}, MeetingRoom.AUDIENCE_INSTITUTION)]
        for overrides, audience in cases:
            form = SessionForm(self._payload(**overrides), user=self.admin)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data['audience'], audience)

    def test_choices_must_sit_under_each_other(self):
        from .forms import SessionForm
        other = Institution.objects.create(code='ELSEWHERE', name='Elsewhere')
        form = SessionForm(self._payload(institution=other.pk), user=self.admin)
        self.assertFalse(form.is_valid())
        self.assertIn('programme', form.errors)

    def test_an_educator_cannot_schedule_on_a_module_they_do_not_teach(self):
        from .forms import SessionForm
        educator = make_user('outsider-coach', 'educator')
        form = SessionForm(self._payload(), user=educator)
        self.assertFalse(form.is_valid())
        self.assertIn('module', form.errors)

    def test_the_form_page_renders_for_new_and_edit(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('livesessions:session-create'))
        self.assertContains(page, 'Who is it for?')
        self.assertContains(page, f'data-parent="{self.programme.pk}"')
        meeting = services.create_session(
            host=self.admin, title='SAICA X · Mon 01 Jan 2030 08:00 | Kept topic',
            start=timezone.now() + timedelta(days=2), end=timezone.now() + timedelta(days=2, hours=1),
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.module, notify=False)
        page = self.client.get(reverse('livesessions:session-edit', args=[meeting.pk]))
        self.assertContains(page, 'value="Kept topic"')

    def test_people_search_needs_two_letters_and_a_scheduler(self):
        make_user('findable', first_name='Thandi', last_name='Mokoena')
        self.client.force_login(self.admin)
        url = reverse('livesessions:session-people')
        self.assertEqual(self.client.get(url, {'q': 'T'}).json(), {'results': []})
        names = [r['name'] for r in self.client.get(url, {'q': 'thandi'}).json()['results']]
        self.assertIn('Thandi Mokoena', names)
        self.client.force_login(make_user('nosy-student'))
        self.assertEqual(self.client.get(url, {'q': 'thandi'}).status_code, 404)


class IdentityMatchingTests(TestCase):
    """Resolving a Teams attendance report back to real candidates.

    Students join anonymously, so Graph reports whatever they typed. These tests
    pin the two properties that matter: a name is only trusted inside the
    session's own roster, and an ambiguous one is refused rather than guessed.
    """

    @classmethod
    def setUpTestData(cls):
        """Create a FREP session with two enrolled students and a same-named stranger.

        The stranger shares Thabo's name but is enrolled on nothing, so must never be matched into this session.
        """
        institution = Institution.objects.create(code='MILPARK3', name='Milpark Three')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='FR3', name='Financial Reporting')
        cls.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                    code='FREP')
        cls.educator = make_user('lecturer', 'educator')
        cls.educator.profile.taught_modules.add(cls.module)

        cls.thabo = make_user('thabo', first_name='Thabo', last_name='Mokoena')
        ModuleEnrolment.objects.create(person=cls.thabo.profile, programme_module=cls.module)

        cls.ayanda = make_user('ayanda', first_name='Ayanda', last_name='Dlamini')
        ModuleEnrolment.objects.create(person=cls.ayanda.profile, programme_module=cls.module)

        cls.stranger = make_user('elsewhere', first_name='Thabo', last_name='Mokoena')

        start = timezone.now() - timedelta(hours=2)
        cls.meeting = MeetingRoom.objects.create(
            title='Deferred tax', host=cls.educator, module=cls.module,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=start,
            scheduled_end=start + timedelta(hours=1), provider=MeetingRoom.PROVIDER_TEAMS)

    def _index(self):
        """Build the identity index for the session; returns ``(index, ambiguous_keys)``."""
        from . import identity as ident
        return ident.build_index(self.meeting)

    def test_a_typed_name_matches_inside_the_roster(self):
        from . import identity as ident
        index, _ = self._index()
        user, key = ident.resolve({'identity': {'displayName': 'Thabo Mokoena'}}, index)
        self.assertEqual(user, self.thabo)
        self.assertEqual(key, 'thabo mokoena')

    def test_decoration_and_accents_are_ignored(self):
        from . import identity as ident
        index, _ = self._index()
        for typed in ('  THABO   mokoena  ', 'Thabo Mokoena (PGDA)', 'Thabo Mokoena 🎓'):
            user, _ = ident.resolve({'identity': {'displayName': typed}}, index)
            self.assertEqual(user, self.thabo, f'{typed!r} should have matched')

    def test_email_beats_a_misleading_display_name(self):
        from . import identity as ident
        index, _ = self._index()
        user, key = ident.resolve(
            {'emailAddress': self.ayanda.email, 'identity': {'displayName': 'Thabo Mokoena'}},
            index)
        self.assertEqual(user, self.ayanda, 'the address is the stronger evidence')
        self.assertEqual(key, self.ayanda.email)

    def test_someone_outside_the_session_is_never_matched(self):
        """The stranger shares Thabo's exact name but is on no roster."""
        from . import identity as ident
        index, _ = self._index()
        user, _ = ident.resolve({'identity': {'displayName': 'Thabo Mokoena'}}, index)
        self.assertNotEqual(user, self.stranger)

    def test_an_ambiguous_name_inside_the_roster_is_refused(self):
        """A name two roster members share is refused rather than guessed; their addresses still resolve them individually."""
        from . import identity as ident

        twin = make_user('thabo2', first_name='Thabo', last_name='Mokoena')
        ModuleEnrolment.objects.create(person=twin.profile, programme_module=self.module)

        index, ambiguous = self._index()
        self.assertIn('thabo mokoena', ambiguous)
        user, _ = ident.resolve({'identity': {'displayName': 'Thabo Mokoena'}}, index)
        self.assertIsNone(user, 'two candidates answer to that name — guessing would be wrong')
        by_email, _ = ident.resolve({'emailAddress': twin.email}, index)
        self.assertEqual(by_email, twin)

    def test_a_remembered_alias_matches_a_nickname(self):
        from . import identity as ident

        index, _ = self._index()
        self.assertIsNone(ident.resolve({'identity': {'displayName': 'Tebza 📱'}}, index)[0])

        ident.remember(self.thabo.profile, 'Tebza 📱', by=self.educator)

        index, _ = self._index()
        user, _ = ident.resolve({'identity': {'displayName': 'Tebza 📱'}}, index)
        self.assertEqual(user, self.thabo, 'a correction should only be needed once')

    def test_an_alias_is_never_silently_reassigned(self):
        from . import identity as ident
        from .models import TeamsIdentityAlias

        ident.remember(self.thabo.profile, 'shorty', by=self.educator)
        self.assertIsNone(ident.remember(self.ayanda.profile, 'shorty', by=self.educator))
        self.assertEqual(TeamsIdentityAlias.objects.get(identity='shorty').person,
                         self.thabo.profile)

    def test_clicking_join_puts_someone_in_the_roster(self):
        """Even a guest with no enrolment is matchable once they come through Metanoia."""
        from . import identity as ident
        from .models import SessionJoin

        visitor = make_user('visitor', first_name='Nomsa', last_name='Khumalo')

        index, _ = self._index()
        self.assertIsNone(ident.resolve({'identity': {'displayName': 'Nomsa Khumalo'}}, index)[0])

        SessionJoin.objects.create(meeting=self.meeting, user=visitor,
                                   display_name='Nomsa Khumalo', email=visitor.email)
        index, _ = self._index()
        user, _ = ident.resolve({'identity': {'displayName': 'Nomsa Khumalo'}}, index)
        self.assertEqual(user, visitor)

    def test_the_email_local_part_matches(self):
        from . import identity as ident
        index, _ = self._index()
        user, _ = ident.resolve({'identity': {'displayName': 'thabo'}}, index)
        self.assertEqual(user, self.thabo)


class JoinHandoffTests(TestCase):
    """The identified hand-off: we record who went in before Teams ever sees them."""

    def setUp(self):
        """Create a module session that starts now, with an enrolled student and an outsider."""
        institution = Institution.objects.create(code='UNISA2', name='UNISA Two')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='AUD2', name='Auditing')
        self.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                     code='AUDT')
        self.educator = make_user('host2', 'educator')
        self.student = make_user('joiner', first_name='Lerato', last_name='Ndlovu')
        ModuleEnrolment.objects.create(person=self.student.profile, programme_module=self.module)
        self.outsider = make_user('gatecrasher')

        start = timezone.now()
        self.meeting = services.create_session(
            host=self.educator, title='Audit evidence', start=start,
            end=start + timedelta(hours=1), audience=MeetingRoom.AUDIENCE_MODULE,
            module=self.module, notify=False)

    def test_joining_records_the_students_own_identity(self):
        from .models import SessionJoin

        self.client.force_login(self.student)
        response = self.client.get(f'/calendar/sessions/{self.meeting.pk}/join/')
        self.assertEqual(response.status_code, 302)

        join = SessionJoin.objects.get(meeting=self.meeting, user=self.student)
        self.assertEqual(join.display_name, 'Lerato Ndlovu')
        self.assertEqual(join.email, self.student.email)
        self.assertEqual(join.click_count, 1)

    def test_rejoining_counts_rather_than_duplicating(self):
        from .models import SessionJoin

        self.client.force_login(self.student)
        for _ in range(3):
            self.client.get(f'/calendar/sessions/{self.meeting.pk}/join/')
        self.assertEqual(SessionJoin.objects.filter(meeting=self.meeting).count(), 1)
        self.assertEqual(SessionJoin.objects.get(meeting=self.meeting).click_count, 3)

    def test_joining_opens_the_register_row_without_marking_anyone_present(self):
        self.client.force_login(self.student)
        self.client.get(f'/calendar/sessions/{self.meeting.pk}/join/')

        record = self.meeting.class_session.attendance.get(student=self.student)
        self.assertEqual(record.seconds_attended, 0)
        self.assertNotEqual(record.status, 'present',
                            'clicking join is not attending — presence has to be measured')

    def test_someone_outside_the_session_cannot_join(self):
        self.client.force_login(self.outsider)
        self.assertEqual(
            self.client.get(f'/calendar/sessions/{self.meeting.pk}/join/').status_code, 404)

    def test_the_register_is_managers_only(self):
        self.client.force_login(self.student)
        self.assertEqual(
            self.client.get(f'/calendar/sessions/{self.meeting.pk}/register/').status_code, 404)

        self.client.force_login(self.educator)
        self.assertEqual(
            self.client.get(f'/calendar/sessions/{self.meeting.pk}/register/').status_code, 200)

    def test_resolving_an_attendee_credits_them_and_remembers_it(self):
        from . import identity as ident
        from .models import SessionArtifact

        artifact, _ = SessionArtifact.objects.update_or_create(
            meeting=self.meeting,
            defaults={'unresolved_attendees': [{'label': 'Lolly', 'seconds': 2400, 'keys': ['lolly']}]})

        self.client.force_login(self.educator)
        response = self.client.post(
            f'/calendar/sessions/{self.meeting.pk}/register/resolve/',
            {'label': 'Lolly', 'seconds': '2400', 'keys': ['lolly'], 'user': self.student.pk})
        self.assertEqual(response.status_code, 302)

        record = self.meeting.class_session.attendance.get(student=self.student)
        self.assertEqual(record.seconds_attended, 2400)

        artifact.refresh_from_db()
        self.assertEqual(artifact.unresolved_attendees, [])

        index, _ = ident.build_index(self.meeting)
        self.assertEqual(ident.resolve({'identity': {'displayName': 'Lolly'}}, index)[0],
                         self.student)


class AttendanceRosterTests(TestCase):
    """The summary carries real names and e-mails, from the platform's register."""

    def test_the_roster_names_who_attended(self):
        from apps.communication import services as comm
        from apps.communication.models import Attendance
        from .ai_followup import attendance_roster

        institution = Institution.objects.create(code='IAS2', name='IAS Two')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='MAF2', name='Management Accounting')
        module = ProgrammeModule.objects.create(programme=programme, module=canonical, code='MACF')
        educator = make_user('coach2', 'educator')
        student = make_user('attendee', first_name='Sipho', last_name='Zulu')

        start = timezone.now() - timedelta(hours=2)
        meeting = services.create_session(
            host=educator, title='Budgeting', start=start, end=start + timedelta(hours=1),
            audience=MeetingRoom.AUDIENCE_MODULE, module=module, notify=False)

        comm.apply_measured_attendance(meeting.class_session, student, seconds=3000,
                                       source=Attendance.SOURCE_TEAMS, authoritative=True)

        roster = attendance_roster(meeting)
        self.assertIn('Sipho Zulu', roster)
        self.assertIn(student.email, roster)
        self.assertIn('50 min', roster)


class CalendarSourceTests(TestCase):
    """One aggregation feeds every calendar surface.

    The regression these guard is the reason the merge happened: the old myhub
    feed showed only meetings a user hosted or was individually invited to, so a
    module class never reached the calendar of the students it was for.
    """

    @classmethod
    def setUpTestData(cls):
        """Create a module class, a published academic test date, an institution-wide event and a student's own reminder."""
        from apps.learning.models import AcademicCalendar, CalendarEvent
        from apps.myhub.models import Event

        cls.institution = Institution.objects.create(code='SRC', name='Source Institute')
        programme = Programme.objects.create(institution=cls.institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='SRC1', name='Source Module')
        cls.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                    code='SRCM')
        cls.educator = make_user('src-coach', 'educator')
        cls.student = make_user('src-student')
        ProgrammeEnrolment.objects.create(person=cls.student.profile, programme=programme)
        ModuleEnrolment.objects.create(person=cls.student.profile, programme_module=cls.module)

        start = timezone.now() + timedelta(days=2)
        cls.meeting = MeetingRoom.objects.create(
            title='Module class', host=cls.educator, module=cls.module,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=start,
            scheduled_end=start + timedelta(hours=1), provider=MeetingRoom.PROVIDER_TEAMS)

        calendar = AcademicCalendar.objects.create(institution=cls.institution,
                                                  year=timezone.now().year)
        cls.exam = CalendarEvent.objects.create(
            calendar=calendar, title='Test 2', kind=CalendarEvent.KIND_TEST,
            start=timezone.now() + timedelta(days=5), is_published=True)

        cls.notice = Event.objects.create(title='Open day',
                                          start=timezone.now() + timedelta(days=3))
        cls.reminder = Event.objects.create(title='Revise IAS 12', owner=cls.student,
                                            start=timezone.now() + timedelta(days=1),
                                            category=Event.CATEGORY_REMINDER)

    def _entries(self, user):
        """Return the calendar entries for ``user`` across the default window."""
        start, end = sources.window_around()
        return sources.entries_for(user, start, end)

    def test_a_student_sees_every_source_that_applies_to_them(self):
        kinds = {e.kind for e in self._entries(self.student)}
        self.assertIn('session', kinds, 'the module class must reach its own students')
        self.assertIn('academic', kinds)
        self.assertIn('event', kinds)
        self.assertIn('reminder', kinds)

    def test_a_module_class_reaches_a_student_who_was_never_individually_invited(self):
        titles = {e.title for e in self._entries(self.student) if e.kind == 'session'}
        self.assertIn('Module class', titles)
        self.assertFalse(self.meeting.participants.filter(user=self.student).exists())

    def test_another_students_personal_reminder_stays_private(self):
        other = make_user('src-other')
        titles = {e.title for e in self._entries(other)}
        self.assertNotIn('Revise IAS 12', titles)
        self.assertIn('Open day', titles, 'an institution-wide event is for everybody')

    def test_academic_dates_are_only_the_students_own_programme_module_and_intake(self):
        """Academic dates are only those of the student's own programme, module and intake.

        A student not yet placed in an intake sees every intake's dates on their own modules.
        """
        from apps.learning.models import CalendarEvent
        programme = self.module.programme
        other_programme = Programme.objects.create(institution=self.institution, code='BCTA', name='BCTA')
        other_module = ProgrammeModule.objects.create(
            programme=programme, module=Module.objects.create(code='SRC2', name='Other'), code='OTHM')
        mine, theirs = (Cohort.objects.create(programme=programme, code=c) for c in ('S1', 'S2'))
        ProgrammeEnrolment.objects.filter(person=self.student.profile).update(cohort=mine)
        when = timezone.now() + timedelta(days=6)
        make = lambda title, **kw: CalendarEvent.objects.create(
            calendar=self.exam.calendar, title=title, start=when, is_published=True, **kw)
        make('Holiday')
        make('Own module exam', programme=programme, programme_module=self.module)
        make('Own intake date', programme=programme, programme_module=self.module, cohort=mine)
        make('Other intake date', programme=programme, programme_module=self.module, cohort=theirs)
        make('Other module exam', programme=programme, programme_module=other_module)
        make('Other programme exam', programme=other_programme)

        titles = {e.title.rsplit(' | ', 1)[-1] for e in self._entries(self.student) if e.kind == 'academic'}
        self.assertTrue({'Holiday', 'Own module exam', 'Own intake date', 'Test 2'} <= titles)
        self.assertFalse({'Other intake date', 'Other module exam', 'Other programme exam'} & titles)

        ProgrammeEnrolment.objects.filter(person=self.student.profile).update(cohort=None)
        titles = {e.title.rsplit(' | ', 1)[-1] for e in self._entries(self.student) if e.kind == 'academic'}
        self.assertTrue({'Own intake date', 'Other intake date'} <= titles)
        self.assertNotIn('Other programme exam', titles)

    def test_kinds_can_be_filtered(self):
        start, end = sources.window_around()
        only = sources.entries_for(self.student, start, end, kinds=['academic'])
        self.assertTrue(only)
        self.assertEqual({e.kind for e in only}, {'academic'})

    def test_entries_come_back_in_time_order(self):
        starts = [e.start for e in self._entries(self.student)]
        self.assertEqual(starts, sorted(starts))

    def test_the_myhub_feed_and_the_calendar_now_agree(self):
        self.client.force_login(self.student)
        feed = self.client.get('/myhub/events.json').json()
        calendar = self.client.get('/calendar/feed.json').json()['entries']
        self.assertEqual(len(feed), len(calendar))
        self.assertTrue(any('Module class' in row['title'] for row in feed))

    def test_a_broken_source_does_not_blank_the_calendar(self):
        from unittest import mock
        with mock.patch.object(sources, '_academic_dates', side_effect=RuntimeError('boom')):
            kinds = {e.kind for e in self._entries(self.student)}
        self.assertIn('session', kinds, 'the other sources must still render')
        self.assertNotIn('academic', kinds)


class CalendarExportTests(TestCase):
    """The subscribable feed: token-addressed, and scoped like everything else."""

    def setUp(self):
        """Create a module class with an enrolled student and an outsider, and keep the student's feed token."""
        from apps.accounts.models import UserSettings

        institution = Institution.objects.create(code='EXP', name='Export Institute')
        programme = Programme.objects.create(institution=institution, code='PGDA', name='PGDA')
        canonical = Module.objects.create(code='EXP1', name='Export Module')
        self.module = ProgrammeModule.objects.create(programme=programme, module=canonical,
                                                     code='EXPM')
        self.educator = make_user('exp-coach', 'educator')
        self.student = make_user('exp-student')
        ModuleEnrolment.objects.create(person=self.student.profile, programme_module=self.module)
        self.outsider = make_user('exp-outsider')

        start = timezone.now() + timedelta(days=1)
        MeetingRoom.objects.create(
            title='Consolidations class', host=self.educator, module=self.module,
            audience=MeetingRoom.AUDIENCE_MODULE, scheduled_start=start,
            scheduled_end=start + timedelta(hours=1), provider=MeetingRoom.PROVIDER_TEAMS)
        self.token = UserSettings.for_user(self.student).calendar_token

    def test_the_feed_needs_no_login_and_carries_the_users_calendar(self):
        response = self.client.get(f'/calendar/feed/{self.token}.ics')
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('BEGIN:VCALENDAR', body)
        self.assertIn('Consolidations class', body)
        self.assertIn('REFRESH-INTERVAL', body)

    def test_the_feed_is_scoped_to_its_owner(self):
        from apps.accounts.models import UserSettings
        other = UserSettings.for_user(self.outsider).calendar_token
        body = self.client.get(f'/calendar/feed/{other}.ics').content.decode()
        self.assertNotIn('Consolidations class', body)

    def test_an_unknown_token_is_a_404(self):
        import uuid
        self.assertEqual(
            self.client.get(f'/calendar/feed/{uuid.uuid4()}.ics').status_code, 404)

    def test_rotating_the_token_revokes_the_old_link(self):
        self.client.force_login(self.student)
        self.client.post('/calendar/connections/reset-link/')
        self.client.logout()
        self.assertEqual(self.client.get(f'/calendar/feed/{self.token}.ics').status_code, 404)

    def test_imported_events_are_not_published_back_out(self):
        """Imported events are not published back out in the feed, but still show on the user's own Metanoia calendar."""
        from .models import CalendarSubscription, ExternalEvent

        subscription = CalendarSubscription.objects.create(
            user=self.student, name='My Outlook', url='https://example.com/a.ics')
        ExternalEvent.objects.create(subscription=subscription, title='Dentist',
                                     start=timezone.now() + timedelta(days=2))

        body = self.client.get(f'/calendar/feed/{self.token}.ics').content.decode()
        self.assertNotIn('Dentist', body, 'republishing imported events would loop them back')

        self.client.force_login(self.student)
        titles = {row['title'] for row in self.client.get('/calendar/feed.json').json()['entries']}
        self.assertIn('Dentist', titles)


class CalendarImportTests(TestCase):
    """Importing an outside calendar, and the guards around doing so.

    ``SAMPLE_TEMPLATE`` is dated a few weeks from today in :meth:`setUp`: :func:`subscriptions.refresh`
    only keeps events inside its import window (60 days back, 240 ahead), so fixed far-future dates
    would drop out.
    """

    SAMPLE_TEMPLATE = (
        'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n'
        'BEGIN:VEVENT\r\nUID:a\r\nDTSTART:{d1}T090000Z\r\nDTEND:{d1}T100000Z\r\n'
        'SUMMARY:Faculty meeting\r\nEND:VEVENT\r\n'
        'BEGIN:VEVENT\r\nUID:b\r\nDTSTART:{d2}T090000Z\r\nDTEND:{d2}T100000Z\r\n'
        'SUMMARY:Out of office\r\nTRANSP:TRANSPARENT\r\nEND:VEVENT\r\n'
        'END:VCALENDAR\r\n')

    def setUp(self):
        """Build the sample feed dated three weeks ahead, inside the import window, and create an educator."""
        from datetime import timedelta
        from django.utils import timezone
        today = timezone.now().date()
        self.SAMPLE = self.SAMPLE_TEMPLATE.format(
            d1=(today + timedelta(days=21)).strftime('%Y%m%d'),
            d2=(today + timedelta(days=22)).strftime('%Y%m%d'))
        self.user = make_user('importer', 'educator')

    def _subscribe(self):
        """Create a calendar subscription for the test user."""
        from .models import CalendarSubscription
        return CalendarSubscription.objects.create(
            user=self.user, name='My diary', url='https://example.com/cal.ics')

    def test_refreshing_stores_the_events(self):
        """Refreshing stores the events; a ``TRANSPARENT`` event ("I am free") is shown but not busy."""
        from unittest import mock
        from . import subscriptions
        from .models import ExternalEvent

        subscription = self._subscribe()
        with mock.patch.object(subscriptions.ics, 'fetch', return_value=(self.SAMPLE, '')):
            ok, _detail = subscriptions.refresh(subscription)

        self.assertTrue(ok)
        subscription.refresh_from_db()
        self.assertEqual(subscription.last_status, subscription.STATUS_OK)
        self.assertEqual(ExternalEvent.objects.filter(subscription=subscription).count(), 2)
        self.assertFalse(ExternalEvent.objects.get(title='Out of office').busy)

    def test_a_refresh_replaces_rather_than_accumulates(self):
        from unittest import mock
        from . import subscriptions
        from .models import ExternalEvent

        subscription = self._subscribe()
        with mock.patch.object(subscriptions.ics, 'fetch', return_value=(self.SAMPLE, '')):
            subscriptions.refresh(subscription)
            subscriptions.refresh(subscription)
        self.assertEqual(ExternalEvent.objects.filter(subscription=subscription).count(), 2)

        start = self.SAMPLE.index('BEGIN:VEVENT\r\nUID:b')
        stop = self.SAMPLE.index('END:VEVENT\r\n', start) + len('END:VEVENT\r\n')
        shorter = self.SAMPLE[:start] + self.SAMPLE[stop:]
        with mock.patch.object(subscriptions.ics, 'fetch', return_value=(shorter, '')):
            subscriptions.refresh(subscription)
        self.assertEqual(ExternalEvent.objects.filter(subscription=subscription).count(), 1,
                         'a deleted meeting must disappear from Metanoia too')

    def test_a_persistently_failing_feed_is_switched_off_and_reported(self):
        from unittest import mock
        from apps.communication.models import Notification
        from . import subscriptions

        subscription = self._subscribe()
        with mock.patch.object(subscriptions.ics, 'fetch', return_value=('', 'gone')):
            for _ in range(subscriptions.DISABLE_AFTER_FAILURES):
                subscriptions.refresh(subscription)

        subscription.refresh_from_db()
        self.assertFalse(subscription.is_active)
        self.assertTrue(Notification.objects.filter(recipient=self.user,
                                                    verb='calendar disconnected').exists())

    def test_imported_busy_time_removes_a_free_slot(self):
        from unittest import mock
        from . import subscriptions

        day = (timezone.now() + timedelta(days=1)).date()
        before = services.free_slots(day, for_user=self.user)

        busy_start = timezone.make_aware(
            timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=10)),
            timezone.get_current_timezone())
        feed = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:z\r\n'
                f'DTSTART:{busy_start.astimezone(dt_timezone.utc):%Y%m%dT%H%M%SZ}\r\n'
                f'DTEND:{(busy_start + timedelta(hours=2)).astimezone(dt_timezone.utc):%Y%m%dT%H%M%SZ}\r\n'
                'SUMMARY:Lecture elsewhere\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')

        subscription = self._subscribe()
        with mock.patch.object(subscriptions.ics, 'fetch', return_value=(feed, '')):
            subscriptions.refresh(subscription)

        after = services.free_slots(day, for_user=self.user)
        self.assertNotEqual(before, after, 'their own diary should block the slot')
        self.assertTrue(all(end <= busy_start or start >= busy_start + timedelta(hours=2)
                            for start, end in after),
                        f'a free slot overlapped their existing commitment: {after}')

    def test_a_private_address_is_refused(self):
        from .forms import CalendarSubscriptionForm
        form = CalendarSubscriptionForm(
            {'name': 'Internal', 'url': 'http://127.0.0.1:8000/secret.ics',
             'provider': 'other', 'colour': '#888888'}, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn('url', form.errors)

    def test_the_same_calendar_cannot_be_connected_twice(self):
        from unittest import mock
        from .forms import CalendarSubscriptionForm

        self._subscribe()
        with mock.patch('apps.livesessions.ics.fetch', return_value=(self.SAMPLE, '')):
            form = CalendarSubscriptionForm(
                {'name': 'Again', 'url': 'https://example.com/cal.ics',
                 'provider': 'other', 'colour': '#888888'}, user=self.user)
            self.assertFalse(form.is_valid())
            self.assertIn('url', form.errors)


class IcsParsingTests(TestCase):
    """The hand-rolled reader, on the shapes real calendars actually emit."""

    def test_folded_lines_are_rejoined(self):
        from . import ics
        text = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:f\r\n'
                'DTSTART:20260901T090000Z\r\nDTEND:20260901T100000Z\r\n'
                'SUMMARY:A very long title that has been fol\r\n ded across two lines\r\n'
                'END:VEVENT\r\nEND:VCALENDAR')
        events = ics.parse(text, window_start=timezone.now() - timedelta(days=400),
                           window_end=timezone.now() + timedelta(days=400))
        self.assertEqual(events[0]['title'], 'A very long title that has been folded across two lines')

    def test_an_all_day_event_is_marked_as_such(self):
        from . import ics
        text = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:d\r\n'
                'DTSTART;VALUE=DATE:20260901\r\nDTEND;VALUE=DATE:20260902\r\n'
                'SUMMARY:Public holiday\r\nEND:VEVENT\r\nEND:VCALENDAR')
        events = ics.parse(text, window_start=timezone.now() - timedelta(days=400),
                           window_end=timezone.now() + timedelta(days=400))
        self.assertTrue(events[0]['all_day'])

    def test_a_weekly_rule_expands_inside_the_window_only(self):
        from . import ics
        base = timezone.now() + timedelta(days=1)
        text = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r\r\n'
                f'DTSTART:{base.astimezone(dt_timezone.utc):%Y%m%dT%H%M%SZ}\r\n'
                f'DTEND:{(base + timedelta(hours=1)).astimezone(dt_timezone.utc):%Y%m%dT%H%M%SZ}\r\n'
                'SUMMARY:Weekly\r\nRRULE:FREQ=WEEKLY;COUNT=52\r\nEND:VEVENT\r\nEND:VCALENDAR')
        events = ics.parse(text, window_start=timezone.now(),
                           window_end=timezone.now() + timedelta(days=28))
        self.assertEqual(len(events), 4, 'four weeks in a four-week window')

    def test_a_cancelled_event_is_dropped(self):
        from . import ics
        text = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:c\r\n'
                'DTSTART:20260901T090000Z\r\nSUMMARY:Gone\r\nSTATUS:CANCELLED\r\n'
                'END:VEVENT\r\nEND:VCALENDAR')
        self.assertEqual(ics.parse(text, window_start=timezone.now() - timedelta(days=400),
                                   window_end=timezone.now() + timedelta(days=400)), [])

    def test_an_unsupported_rule_yields_one_occurrence_not_a_phantom_series(self):
        from . import ics
        text = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\n'
                'DTSTART:20260901T090000Z\r\nDTEND:20260901T100000Z\r\n'
                'SUMMARY:Odd rule\r\nRRULE:FREQ=SECONDLY;COUNT=99\r\n'
                'END:VEVENT\r\nEND:VCALENDAR')
        events = ics.parse(text, window_start=timezone.now() - timedelta(days=400),
                           window_end=timezone.now() + timedelta(days=400))
        self.assertEqual(len(events), 1, 'under-report busy time rather than invent it')

    def test_round_tripping_escapes_separators(self):
        from . import ics
        body = ics.serialise([{'uid': 'x', 'title': 'Tax; part 1, revisited',
                               'start': timezone.now()}])
        self.assertIn(r'SUMMARY:Tax\; part 1\, revisited', body)


class ConnectedCalendarViewTests(TestCase):
    """Tests for the connected-calendars settings tab and removing a connection."""
    def setUp(self):
        """Create an educator who owns the connections."""
        self.user = make_user('connector', 'educator')

    def test_the_settings_page_offers_the_feed_and_the_connect_form(self):
        self.client.force_login(self.user)
        html = self.client.get('/community/settings/?tab=connected').content.decode()
        self.assertIn('/calendar/feed/', html)
        self.assertIn('Connect calendar', html)

    def test_disconnecting_removes_the_imported_events(self):
        from .models import CalendarSubscription, ExternalEvent

        subscription = CalendarSubscription.objects.create(
            user=self.user, name='Gone soon', url='https://example.com/x.ics')
        ExternalEvent.objects.create(subscription=subscription, title='Thing',
                                     start=timezone.now() + timedelta(days=1))

        self.client.force_login(self.user)
        self.client.post(f'/calendar/connections/{subscription.pk}/remove/')
        self.assertEqual(ExternalEvent.objects.count(), 0)

    def test_one_user_cannot_touch_anothers_connection(self):
        from .models import CalendarSubscription

        subscription = CalendarSubscription.objects.create(
            user=self.user, name='Mine', url='https://example.com/mine.ics')
        self.client.force_login(make_user('nosy'))
        self.assertEqual(
            self.client.post(f'/calendar/connections/{subscription.pk}/remove/').status_code, 404)
        self.assertTrue(CalendarSubscription.objects.filter(pk=subscription.pk).exists())
