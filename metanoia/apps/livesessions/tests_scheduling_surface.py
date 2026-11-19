"""The scheduling surface: finding a slot, sharing the link, adding to a calendar.

Three things a session has to do that are easy to get subtly wrong:

* the free-slot finder must never offer a gap that is not really free — not one
  already booked, not one in the past, and not one shorter than asked for;
* the joining link must reach exactly the session's audience and nobody else,
  and only a person who may manage the session may fire it;
* the ``.ics`` download must be gated like the session page, or it becomes a way
  to read the details of a session you cannot open.

The student-visibility test at the bottom is the other half of the same promise:
a session on a module has to turn up on that module's page for the people
enrolled on it, because that — not the calendar — is where students actually
look for their week.
"""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.communication.models import MeetingRoom
from apps.learning.models import (Cohort, Institution, Module, ModuleEnrolment,
                                  ModuleMaterial, Programme, ProgrammeModule)

from . import services
from .tests import make_user


def local_at(day, hour, minute=0):
    """Return an aware datetime for ``hour:minute`` on ``day`` in the current time zone."""
    return timezone.make_aware(
        timezone.datetime.combine(day, timezone.datetime.min.time().replace(
            hour=hour, minute=minute)),
        timezone.get_current_timezone())


class AvailabilityTests(TestCase):
    """`services.availability` — the data behind the free-slot page."""

    def setUp(self):
        """Create an educator host and pick a day three days ahead."""
        self.host = make_user('avail-host', 'educator')
        self.day = timezone.localdate() + timedelta(days=3)

    def book(self, start_hour, end_hour):
        """Book a class for the host from ``start_hour`` to ``end_hour`` on the test day."""
        return MeetingRoom.objects.create(
            title=f'Class {start_hour}', host=self.host,
            provider=MeetingRoom.PROVIDER_TEAMS,
            scheduled_start=local_at(self.day, start_hour),
            scheduled_end=local_at(self.day, end_hour))

    def row_for(self, rows, day):
        """Return the availability row for ``day``."""
        return next(row for row in rows if row['day'] == day)

    def test_a_booked_block_is_not_offered_as_free(self):
        self.book(10, 12)

        rows = services.availability(self.day, days=1)
        slots = self.row_for(rows, self.day)['slots']

        booked_start, booked_end = local_at(self.day, 10), local_at(self.day, 12)
        for start, end, _minutes in slots:
            self.assertFalse(start < booked_end and end > booked_start,
                             f'{start:%H:%M}–{end:%H:%M} overlaps the 10:00–12:00 class')

    def test_a_fully_booked_day_is_kept_with_no_slots(self):
        """Dropping the day would read as though the day did not exist."""
        self.book(8, 20)

        rows = services.availability(self.day, days=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['slots'], [])
        self.assertEqual(len(rows[0]['sessions']), 1)

    def test_slots_shorter_than_the_minimum_are_not_offered(self):
        """A 20-minute gap between two classes is not offered when 45 minutes are asked for."""
        self.book(8, 10)
        self.book(10, 20)
        rows = services.availability(self.day, days=1, minimum_minutes=45)
        for _start, _end, minutes in self.row_for(rows, self.day)['slots']:
            self.assertGreaterEqual(minutes, 45)

    def test_todays_slots_do_not_start_in_the_past(self):
        """A gap from 09:00 that it is now 14:00 on is not bookable."""
        today = timezone.localdate()
        now = local_at(today, 14)

        rows = services.availability(today, days=1, now=now)

        for start, _end, _minutes in self.row_for(rows, today)['slots']:
            self.assertGreaterEqual(start, now,
                                    'a slot was offered that has already passed')

    def test_weekends_can_be_skipped(self):
        monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()) % 7 or 7)
        rows = services.availability(monday, days=7, skip_weekends=True)
        self.assertTrue(all(row['day'].weekday() < 5 for row in rows))
        self.assertEqual(len(rows), 5)


class AvailabilityViewTests(TestCase):
    """Tests for the free-slot page: who may open it and how it treats bad query parameters."""

    def setUp(self):
        """Resolve the availability page URL."""
        self.url = reverse('livesessions:availability')

    def test_a_scheduler_sees_the_page(self):
        self.client.force_login(make_user('av-staff', 'staff'))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'livesessions/availability.html')

    def test_a_student_is_turned_away(self):
        """The gaps span sessions a student may not see, so the page is not theirs."""
        self.client.force_login(make_user('av-student', 'student'))
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('livesessions:calendar'))

    def test_nonsense_query_parameters_give_the_default_page(self):
        """A bad filter should not 500, and must not look like "no slots".

        ``to_hour <= from_hour`` is nonsense, so the day falls back to 08:00–20:00.
        """
        self.client.force_login(make_user('av-admin', 'admin'))
        response = self.client.get(self.url, {'days': 'lots', 'min': '', 'to_hour': '2',
                                              'from_hour': '19'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['days'], 14)
        self.assertEqual(response.context['minimum'], 30)
        self.assertEqual((response.context['open_from'], response.context['open_to']), (8, 20))


class SessionIcsTests(TestCase):
    """Tests for the per-session ``.ics`` download: content, stable UID and audience gating."""

    def setUp(self):
        """Create a private session two days ahead hosted by an educator."""
        self.host = make_user('ics-host', 'educator')
        self.meeting = MeetingRoom.objects.create(
            title='Audit intensive', host=self.host, provider=MeetingRoom.PROVIDER_TEAMS,
            audience=MeetingRoom.AUDIENCE_PRIVATE,
            scheduled_start=timezone.now() + timedelta(days=2),
            scheduled_end=timezone.now() + timedelta(days=2, hours=1))
        self.url = reverse('livesessions:session-ics', args=[self.meeting.pk])

    def test_the_host_can_download_it(self):
        self.client.force_login(self.host)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/calendar'))
        body = response.content.decode()
        self.assertIn('BEGIN:VEVENT', body)
        self.assertIn('Audit intensive', body)

    def test_the_uid_is_stable_so_a_second_download_updates_in_place(self):
        self.client.force_login(self.host)
        first = self.client.get(self.url).content.decode()
        second = self.client.get(self.url).content.decode()

        def uid(body):
            """Return the ``UID:`` line of an iCalendar body."""
            return next(line for line in body.splitlines() if line.startswith('UID:'))

        self.assertEqual(uid(first), uid(second))

    def test_somebody_outside_the_audience_cannot_download_it(self):
        """Otherwise the file leaks the details of a session they cannot open."""
        self.client.force_login(make_user('ics-outsider', 'student'))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)


class SessionShareTests(TestCase):
    """Tests for re-sharing a session's joining link: audience only, managers only, POST only."""

    def setUp(self):
        """Create a private session with one invited student and one outsider."""
        self.host = make_user('share-host', 'educator')
        self.invitee = make_user('share-invitee', 'student')
        self.outsider = make_user('share-outsider', 'student')
        self.meeting = MeetingRoom.objects.create(
            title='Coaching call', host=self.host, provider=MeetingRoom.PROVIDER_TEAMS,
            audience=MeetingRoom.AUDIENCE_PRIVATE,
            scheduled_start=timezone.now() + timedelta(days=1),
            scheduled_end=timezone.now() + timedelta(days=1, hours=1))
        self.meeting.participants.create(user=self.invitee)
        self.url = reverse('livesessions:session-share', args=[self.meeting.pk])

    def test_the_host_can_resend_the_link_to_the_audience(self):
        self.client.force_login(self.host)
        response = self.client.post(self.url, {'email': '0'}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            self.invitee.notifications.filter(meeting=self.meeting).exists(),
            'the invitee should have been told')
        self.assertFalse(
            self.outsider.notifications.filter(meeting=self.meeting).exists(),
            'a re-share must never reach beyond the original audience')

    def test_a_student_cannot_broadcast(self):
        self.client.force_login(self.invitee)
        response = self.client.post(self.url, {'email': '0'})
        self.assertEqual(response.status_code, 404)

    def test_get_is_not_allowed(self):
        """It is a broadcast; a link-prefetch must not fire one."""
        self.client.force_login(self.host)
        self.assertEqual(self.client.get(self.url).status_code, 405)


class SessionOnTheModulePageTests(TestCase):
    """A module session must show up under the module, not only on the calendar."""

    def setUp(self):
        """Create a PGDA module offering with an educator and one enrolled student."""
        self.institution = Institution.objects.create(code='MP', name='Milpark')
        self.programme = Programme.objects.create(
            institution=self.institution, code='PGDA', name='PGDA')
        self.cohort = Cohort.objects.create(programme=self.programme, code='P26F')
        module = Module.objects.create(code='AUD', name='Auditing')
        self.offering = ProgrammeModule.objects.create(
            programme=self.programme, module=module, code='AUD1', is_active=True)

        self.educator = make_user('mod-educator', 'educator')
        self.student = make_user('mod-student', 'student')
        ModuleEnrolment.objects.create(person=self.student.profile,
                                       programme_module=self.offering)

    def test_scheduling_files_the_session_on_the_module(self):
        meeting = services.create_session(
            host=self.educator, title='Auditing week 1',
            start=timezone.now() + timedelta(days=1),
            end=timezone.now() + timedelta(days=1, hours=2),
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.offering,
            notify=False)

        material = ModuleMaterial.objects.filter(kind=ModuleMaterial.KIND_LIVE,
                                                 meeting=meeting).first()
        self.assertIsNotNone(
            material,
            'the session should appear on the module schedule before it happens, '
            'so students find it where they actually look')
        self.assertEqual(material.title, 'Auditing week 1')

    def test_an_enrolled_student_can_see_it(self):
        from . import audience as aud

        meeting = services.create_session(
            host=self.educator, title='Auditing week 1',
            start=timezone.now() + timedelta(days=1),
            end=timezone.now() + timedelta(days=1, hours=2),
            audience=MeetingRoom.AUDIENCE_MODULE, module=self.offering,
            notify=False)

        self.assertTrue(aud.can_view(self.student, meeting))
        self.assertTrue(
            aud.recipients_for(meeting).filter(pk=self.student.pk).exists(),
            'anyone who can see it must also have been told about it')
