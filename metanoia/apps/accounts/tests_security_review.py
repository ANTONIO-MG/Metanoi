"""Regression tests for the 2026-11-08 security review.

1. People lookup (person card, profile, @-mention picker) only reveals someone
   to a viewer who shares a context with them — not to any signed-in account,
   since /join/ hands those out on the spot — and honours the private-profile
   and show-e-mail choices.
2. ``/api/accounts/people/`` follows the website's role rule: only an
   administrator changes roles, and nobody else may edit an administrator.
3. The self-service API cannot change what the website locks (legal identity,
   the Microsoft account the Teams integration hosts classes under).
4. Production refuses to start without a real SECRET_KEY.
"""

import os
import subprocess
import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.communication.models import ChatGroup, Community, Feed
from apps.learning.models import Institution, Programme, ProgrammeEnrolment
from apps.livesessions.tests import make_user
from core.testing import enrol, make_module

from .models import ParentLink, Person, UserSettings

User = get_user_model()


def _settings(user, **kw):
    """Set ``kw`` on ``user``'s settings row and save it."""
    prefs = UserSettings.for_user(user)
    for k, v in kw.items():
        setattr(prefs, k, v)
    prefs.save()
    return prefs


@override_settings(SECURE_SSL_REDIRECT=False)
class PersonVisibilityTests(TestCase):
    """Who may open whose person card, and what it shows.

    Covers the hole (a ``/join/`` account walking every id), the legitimate
    contexts that must keep working, community feed authors (name yes, e-mail only
    by choice) and the @-mention picker.
    """
    @classmethod
    def setUpTestData(cls):
        cls.milpark = Institution.objects.create(code='MILPARK', name='Milpark Education')
        cls.unisa = Institution.objects.create(code='UNISA', name='UNISA')
        cls.pgda = Programme.objects.create(institution=cls.milpark, code='PGDA', name='PGDA')
        cls.upgda = Programme.objects.create(institution=cls.unisa, code='UPGDA', name='PGDA')
        Community.default()

        cls.staff = make_user('staffer', 'staff', first_name='Sam', last_name='Staff')
        cls.ann = make_user('ann', 'student', first_name='Ann', last_name='Mokoena')
        cls.ben = make_user('ben', 'student', first_name='Ben', last_name='Dlamini')
        cls.una = make_user('una', 'student', first_name='Una', last_name='Isa')
        ProgrammeEnrolment.objects.create(person=cls.ann.profile, programme=cls.pgda)
        ProgrammeEnrolment.objects.create(person=cls.ben.profile, programme=cls.pgda)
        ProgrammeEnrolment.objects.create(person=cls.una.profile, programme=cls.upgda)
        cls.member = make_user('member', 'community', first_name='Cara', last_name='Member')
        cls.parent = make_user('pa', 'parent', first_name='Pat', last_name='Parent')
        ParentLink.objects.create(parent=cls.parent, student=cls.una)

    def card(self, viewer, target):
        """Fetch ``target``'s card fragment as ``viewer``."""
        self.client.force_login(viewer)
        return self.client.get(
            reverse('accounts:person-card', args=[target.profile.pk]) + '?fragment=1')


    def test_community_member_gets_404_for_strangers_on_every_route(self):
        self.client.force_login(self.member)
        for target in (self.ann, self.una, self.parent):
            for url in (reverse('accounts:person-card', args=[target.profile.pk]),
                        reverse('accounts:person-card-user', args=[target.pk])):
                r = self.client.get(url + '?fragment=1')
                self.assertEqual(r.status_code, 404, url)
                self.assertNotContains(r, target.email, status_code=404)

    def test_unknown_id_and_stranger_look_the_same(self):
        self.client.force_login(self.member)
        missing = self.client.get(reverse('accounts:person-card', args=[999999]))
        stranger = self.client.get(reverse('accounts:person-card', args=[self.ann.profile.pk]))
        self.assertEqual(missing.status_code, stranger.status_code)

    def test_student_cannot_see_a_student_at_another_institution(self):
        self.assertEqual(self.card(self.ann, self.una).status_code, 404)
        self.client.force_login(self.ann)
        r = self.client.get(reverse('accounts:profile', args=[self.una.profile.pk]))
        self.assertEqual(r.status_code, 404)


    def test_same_programme_student_sees_the_card(self):
        self.assertContains(self.card(self.ann, self.ben), 'Ben Dlamini')

    def test_shared_module_across_institutions(self):
        offering = make_module('Tax', 'TAX101')
        enrol(self.ann.profile, offering)
        enrol(self.una.profile, offering)
        self.assertContains(self.card(self.ann, self.una), 'Una Isa')

    def test_shared_chat_counts_but_general_does_not(self):
        general = ChatGroup.objects.create(kind=ChatGroup.KIND_CUSTOM, name='General')
        general.add_member(self.ann)
        general.add_member(self.una)
        self.assertEqual(self.card(self.ann, self.una).status_code, 404)
        room = ChatGroup.objects.create(kind=ChatGroup.KIND_CUSTOM, name='Study buddies')
        room.add_member(self.ann)
        room.add_member(self.una)
        self.assertContains(self.card(self.ann, self.una), 'Una Isa')

    def test_parent_and_child_see_each_other(self):
        self.assertContains(self.card(self.parent, self.una), 'Una Isa')
        self.assertContains(self.card(self.una, self.parent), 'Pat Parent')
        self.assertEqual(self.card(self.parent, self.ann).status_code, 404)

    def test_staff_see_everyone_and_everyone_sees_staff(self):
        self.assertContains(self.card(self.staff, self.una), 'Una Isa')
        self.assertContains(self.card(self.staff, self.member), 'Cara Member')
        self.assertContains(self.card(self.member, self.staff), 'Sam Staff')
        self.assertContains(self.card(self.una, self.staff), 'Sam Staff')

    def test_own_card(self):
        self.assertContains(self.card(self.member, self.member), 'Cara Member')


    def test_community_member_sees_a_post_author_without_email(self):
        Feed.objects.create(author=self.ann, body='hi all')
        r = self.card(self.member, self.ann)
        self.assertContains(r, 'Ann Mokoena')
        self.assertNotContains(r, self.ann.email)

    def test_show_email_is_honoured_for_community_viewers(self):
        """``show_email`` reaches community viewers only on a public profile.

        The default visibility ("members": shares a module) keeps the address off a
        stranger's card; a public profile that chose to show it shows it.
        """
        Feed.objects.create(author=self.ann, body='hi all')
        _settings(self.ann, show_email=True)
        self.assertNotContains(self.card(self.member, self.ann), self.ann.email)
        _settings(self.ann, profile_visibility=UserSettings.VISIBILITY_PUBLIC)
        self.assertContains(self.card(self.member, self.ann), self.ann.email)

    def test_private_profile_wins_over_show_email(self):
        Feed.objects.create(author=self.ann, body='hi all')
        _settings(self.ann, show_email=True, profile_visibility=UserSettings.VISIBILITY_PRIVATE)
        r = self.card(self.member, self.ann)
        self.assertContains(r, 'keeps their profile private')
        self.assertNotContains(r, self.ann.email)

    def test_removed_post_no_longer_opens_the_card(self):
        Feed.objects.create(author=self.ann, body='gone', is_removed=True)
        self.assertEqual(self.card(self.member, self.ann).status_code, 404)


    def test_mention_picker_does_not_match_email_or_list_private_people(self):
        """The mention picker matches names, not e-mail prefixes, and leaves private people out."""
        self.client.force_login(self.member)
        url = reverse('communication:community-mentions')
        names = lambda q: [row['label'] for row in self.client.get(url, {'q': q}).json()['results']]
        self.assertIn('Ann Mokoena', names('Ann'))
        Person.objects.filter(user=self.ann).update(first_name='Zed', last_name='Zulu')
        User.objects.filter(pk=self.ann.pk).update(first_name='Zed', last_name='Zulu')
        self.assertNotIn('Zed Zulu', names('ann'))
        _settings(self.ann, profile_visibility=UserSettings.VISIBILITY_PRIVATE)
        self.assertNotIn('Zed Zulu', names('Zed'))


@override_settings(SECURE_SSL_REDIRECT=False)
class PersonApiRoleTests(TestCase):
    """Staff → admin escalation through ``/api/accounts/people/``."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('root', 'admin', first_name='Ada', last_name='Admin', is_staff=True)
        cls.staff = make_user('staffer', 'staff', first_name='Sam', last_name='Staff')
        cls.student = make_user('stu', 'student', first_name='Stu', last_name='Dent')

    def url(self, user):
        """The people API URL for ``user``'s profile."""
        return f'/api/accounts/people/{user.profile.pk}/'

    def patch(self, actor, target, data):
        """PATCH ``target`` through the people API as ``actor``."""
        self.client.force_login(actor)
        return self.client.patch(self.url(target), data, content_type='application/json')

    def test_staff_cannot_make_themselves_admin(self):
        self.patch(self.staff, self.staff, {'user_type': 'admin'})
        self.staff.profile.refresh_from_db()
        self.assertEqual(self.staff.profile.user_type, 'staff')
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_staff)

    def test_staff_cannot_change_anyones_role(self):
        for role in ('admin', 'staff', 'educator'):
            self.patch(self.staff, self.student, {'user_type': role})
            self.student.profile.refresh_from_db()
            self.assertEqual(self.student.profile.user_type, 'student')

    def test_staff_cannot_edit_or_delete_an_admin(self):
        r = self.patch(self.staff, self.admin, {'first_name': 'Pwned', 'ms_upn': 'x@evil.test'})
        self.assertEqual(r.status_code, 403)
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.first_name, 'Ada')
        r = self.client.delete(self.url(self.admin))
        self.assertEqual(r.status_code, 403)
        self.assertTrue(Person.objects.filter(pk=self.admin.profile.pk).exists())

    def test_staff_cannot_demote_an_admin(self):
        self.patch(self.staff, self.admin, {'user_type': 'student'})
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.user_type, 'admin')

    def test_staff_still_manage_students(self):
        r = self.patch(self.staff, self.student, {'phone': '+27 82 555 0000',
                                                  'ms_upn': 'stu@cta.onmicrosoft.com'})
        self.assertEqual(r.status_code, 200)
        self.student.profile.refresh_from_db()
        self.assertEqual(self.student.profile.phone, '+27 82 555 0000')
        self.assertEqual(self.student.profile.ms_upn, 'stu@cta.onmicrosoft.com')

    def test_admin_grants_roles_and_django_admin_follows(self):
        r = self.patch(self.admin, self.staff, {'user_type': 'admin'})
        self.assertEqual(r.status_code, 200)
        self.staff.profile.refresh_from_db()
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.profile.user_type, 'admin')
        self.assertTrue(self.staff.is_staff)
        r = self.patch(self.admin, self.staff, {'user_type': 'staff'})
        self.assertEqual(r.status_code, 200)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_staff)

    def test_admin_cannot_demote_the_last_admin_or_invent_a_role(self):
        r = self.patch(self.admin, self.admin, {'user_type': 'student'})
        self.assertEqual(r.status_code, 400)
        r = self.patch(self.admin, self.student, {'user_type': 'community'})
        self.assertEqual(r.status_code, 400)
        self.admin.profile.refresh_from_db()
        self.assertEqual(self.admin.profile.user_type, 'admin')

    def test_website_rule_unchanged(self):
        self.client.force_login(self.staff)
        self.client.post(reverse('accounts:member-role', args=[self.student.profile.pk]),
                         {'user_type': 'admin'})
        self.student.profile.refresh_from_db()
        self.assertEqual(self.student.profile.user_type, 'student')
        self.client.force_login(self.admin)
        self.client.post(reverse('accounts:member-role', args=[self.student.profile.pk]),
                         {'user_type': 'educator'})
        self.student.profile.refresh_from_db()
        self.assertEqual(self.student.profile.user_type, 'educator')

    def test_users_api_is_for_administrators(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get('/api/accounts/users/').status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/api/accounts/users/').status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False)
class SelfServiceLockedFieldsTests(TestCase):
    """A student cannot change through the API what the website locks for them."""
    @classmethod
    def setUpTestData(cls):
        cls.student = make_user('stu', 'student', first_name='Stu', last_name='Dent')
        Person.objects.filter(user=cls.student).update(
            gender='male', date_of_birth='2001-02-03')

    def test_student_cannot_change_identity_or_microsoft_account(self):
        """Identity fields and ``ms_upn`` stay put, while what the website lets them change (the phone) still saves."""
        self.client.force_login(self.student)
        url = f'/api/accounts/people/{self.student.profile.pk}/'
        r = self.client.patch(url, {
            'first_name': 'New', 'last_name': 'Name', 'title': 'Dr',
            'gender': 'female', 'date_of_birth': '1990-01-01',
            'ms_upn': 'educator@cta.onmicrosoft.com',
            'phone': '+27 82 111 2222',
        }, content_type='application/json')
        self.assertEqual(r.status_code, 200)
        p = Person.objects.get(user=self.student)
        self.assertEqual((p.first_name, p.last_name, p.title), ('Stu', 'Dent', ''))
        self.assertEqual(p.gender, 'male')
        self.assertEqual(str(p.date_of_birth), '2001-02-03')
        self.assertEqual(p.ms_upn, '')
        self.assertEqual(p.phone, '+27 82 111 2222')
        self.assertEqual(r.json()['first_name'], 'Stu')


class SecretKeyStartupTests(TestCase):
    """Settings refuse the published fallback key when DEBUG is off."""

    def _import_settings(self, **env):
        """Import ``config.settings`` in a subprocess with ``env``; return the completed process."""
        base = {k: v for k, v in os.environ.items()
                if k not in ('SECRET_KEY', 'DEBUG', 'PYTEST_CURRENT_TEST', 'DJANGO_SETTINGS_MODULE')}
        base.update(env)
        project = Path(__file__).resolve().parents[2]
        return subprocess.run(
            [sys.executable, '-c',
             'import config.settings as s; print("KEY=" + s.SECRET_KEY[:16])'],
            cwd=project, env=base, capture_output=True, text=True, timeout=120)

    def test_production_without_a_key_refuses_to_start(self):
        for empty in ('', '   '):
            r = self._import_settings(SECRET_KEY=empty, DEBUG='false')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('SECRET_KEY is not set', r.stderr)

    def test_production_with_a_key_starts(self):
        r = self._import_settings(SECRET_KEY='a-real-production-key-0123456789', DEBUG='false')
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn('KEY=a-real-productio', r.stdout)

    def test_development_still_falls_back(self):
        r = self._import_settings(SECRET_KEY='', DEBUG='true')
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn('KEY=django-insecure-', r.stdout)
