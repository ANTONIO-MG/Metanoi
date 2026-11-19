"""Attendance is automatic only.

The manual register (``/communication/attendance/`` — list, mark roll, self
check-in) was removed at the client's request (2026-11-09). What remains is the
presence heartbeat the meeting room posts while a student is in class, plus the
Teams attendance-report import. The heartbeat is scoped: only someone the
session is *for* (:func:`apps.livesessions.audience.can_view`, or the module's
students for a free-standing session) accrues time.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from core.testing import enrol, make_module, make_programme

from .models import Attendance, ClassSession, MeetingRoom

User = get_user_model()


def _user(username, role):
    """Create a registered user with a profile of the given ``role``."""
    user = User.objects.create_user(username=username, email=f'{username}@example.com', password='x')
    person = Person.objects.get(user=user)
    person.user_type = role
    person.registered = True
    person.profile_status = True
    person.save()
    user.refresh_from_db()
    return user


class AttendanceScopeTests(TestCase):

    """The manual register is gone, and the automatic heartbeat only tracks people the session is for."""
    @classmethod
    def setUpTestData(cls):
        programme = make_programme('ATT', 'PGDA')
        cls.module = make_module('Auditing', 'ATTAUD', programme=programme)
        cls.other_module = make_module('Tax', 'ATTTAX', programme=programme)

        cls.educator = _user('att-coach', 'educator')
        cls.module.educators.add(cls.educator.profile)
        cls.outsider = _user('att-other-coach', 'educator')
        cls.other_module.educators.add(cls.outsider.profile)
        cls.staff = _user('att-staff', 'staff')

        cls.student = _user('att-student', 'student')
        enrol(cls.student.profile, cls.module)
        cls.stranger = _user('att-stranger', 'student')
        enrol(cls.stranger.profile, cls.other_module)

        cls.session = ClassSession.objects.create(module=cls.module, title='Week 1', is_open=True)

    def _ping(self, session=None):
        """Post an attendance heartbeat for ``session`` (default: the open module session)."""
        return self.client.post(reverse('communication:attendance-ping', args=[(session or self.session).pk]))

    def test_the_register_pages_are_gone(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/communication/attendance/').status_code, 404)
        self.assertEqual(self.client.get(f'/communication/attendance/{self.session.pk}/').status_code, 404)

    def test_no_sidebar_links_to_the_register(self):
        for user in (self.staff, self.educator, self.student):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(reverse('myhub:index'), follow=True),
                                   'href="/communication/attendance/"')

    def test_a_student_not_on_the_module_accrues_nothing(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self._ping().status_code, 404)
        self.assertFalse(Attendance.objects.filter(student=self.stranger).exists())

    def test_a_student_on_the_module_is_tracked(self):
        self.client.force_login(self.student)
        response = self._ping()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(Attendance.objects.filter(session=self.session, student=self.student).exists())

    def test_a_closed_session_is_not_tracked(self):
        self.session.is_open = False
        self.session.save(update_fields=['is_open'])
        self.client.force_login(self.student)
        self.assertEqual(self._ping().status_code, 409)

    def test_a_session_on_a_meeting_follows_the_meetings_audience(self):
        """A student on the module but not invited to the private call accrues nothing."""
        start = timezone.now()
        meeting = MeetingRoom.objects.create(
            title='Private tutorial', host=self.educator, module=self.module,
            audience=MeetingRoom.AUDIENCE_PRIVATE, scheduled_start=start,
            scheduled_end=start + timedelta(hours=1))
        session = ClassSession.objects.create(module=self.module, title='1:1', meeting=meeting,
                                              is_open=True)
        self.client.force_login(self.student)
        self.assertEqual(self._ping(session).status_code, 404)
        self.assertFalse(Attendance.objects.filter(session=session).exists())

    def test_a_general_module_session_is_for_everyone_on_the_programme(self):
        from apps.learning.general import general_module_for
        general = general_module_for(self.module.programme)
        session = ClassSession.objects.create(module=general, title='Orientation', is_open=True)
        self.client.force_login(self.student)
        self.assertEqual(self._ping(session).status_code, 200)
        self.assertTrue(Attendance.objects.filter(session=session, student=self.student).exists())
