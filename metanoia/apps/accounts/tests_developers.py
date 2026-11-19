"""Developer accounts (apps.accounts.developers): admins + the developer tools.

* error log, audit log, background jobs and backups are a 404 for ordinary
  administrators (and their sidebar links are gone); developers keep them;
* the same models are hidden from non-developers in the advanced admin;
* only a developer grants/revokes developer, and an administrator cannot change
  a developer's role (website, API, Django admin);
* settings.DEVELOPER_EMAILS become developers automatically — once verified.
"""

import importlib
import tempfile

from allauth.account.models import EmailAddress
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.livesessions.tests import make_user
from core.roles import is_developer

from . import developers
from .models import Person

User = get_user_model()

LISTED = ['dev@metanoiagroup.co.za', 'seeid24@gmail.com', 'cjmordaunt@gmail.com']


def make_developer(username):
    """An administrator with the developer flag and Django staff access."""
    user = make_user(username, 'admin')
    Person.objects.filter(user=user).update(is_developer=True)
    User.objects.filter(pk=user.pk).update(is_staff=True)
    return User.objects.get(pk=user.pk)


DEV_PAGES = ['diagnostics:log', 'diagnostics:dictionary', 'diagnostics:system', 'staffdesk:ops-audit',
             'staffdesk:ops-audit-export', 'staffdesk:ops-jobs', 'staffdesk:backups']


@override_settings(SECURE_SSL_REDIRECT=False, BACKUP_ROOT=tempfile.mkdtemp(prefix='devtest-'),
                   DEVELOPER_EMAILS=LISTED)
class DeveloperToolsAccessTests(TestCase):
    """Only developers reach the platform tools; ordinary admins, superusers and staff get 404s."""
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('plainadmin', 'admin')
        User.objects.filter(pk=cls.admin.pk).update(is_staff=True, is_superuser=True)
        cls.admin.refresh_from_db()
        cls.staff = make_user('staffer', 'staff')
        cls.dev = make_developer('thedev')

    def test_flag_only_counts_on_an_admin(self):
        """The flag counts only on an admin — a superuser alone is not enough, nor a flagged student."""
        self.assertTrue(is_developer(self.dev))
        self.assertFalse(is_developer(self.admin))
        student = make_user('flagged', 'student')
        Person.objects.filter(user=student).update(is_developer=True)
        self.assertFalse(is_developer(User.objects.get(pk=student.pk)))

    def test_ordinary_admins_and_staff_get_404(self):
        for who in (self.admin, self.staff):
            self.client.force_login(who)
            for name in DEV_PAGES:
                self.assertEqual(self.client.get(reverse(name)).status_code, 404, (who, name))
            r = self.client.post(reverse('staffdesk:backup-create'))
            self.assertEqual(r.status_code, 404)
            r = self.client.post(reverse('staffdesk:ops-jobs-run-due'))
            self.assertEqual(r.status_code, 404)

    def test_developers_keep_them(self):
        self.client.force_login(self.dev)
        for name in DEV_PAGES:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_activity_log_api_is_for_developers(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/api/accounts/activity-log/').status_code, 403)
        self.client.force_login(self.dev)
        self.assertEqual(self.client.get('/api/accounts/activity-log/').status_code, 200)

    def test_sidebar_links(self):
        labels = ('Error log', 'Audit log', 'Background jobs', 'Backups &amp; restore')
        self.client.force_login(self.admin)
        page = self.client.get(reverse('staffdesk:home')).content.decode()
        for label in labels:
            self.assertNotIn(label, page)
        self.client.force_login(self.dev)
        page = self.client.get(reverse('staffdesk:home')).content.decode()
        for label in labels:
            self.assertIn(label, page)

    def test_advanced_admin_hides_the_developer_models(self):
        """Even a superuser cannot see the developer-only models in the advanced admin."""
        urls = ['/admin/accounts/activitylog/', '/admin/diagnostics/errorevent/',
                '/admin/scheduler/jobrun/']
        self.client.force_login(self.admin)
        for url in urls:
            self.assertIn(self.client.get(url).status_code, (403, 404), url)
        index = self.client.get('/admin/').content.decode()
        self.assertNotIn('/admin/accounts/activitylog/', index)
        self.assertNotIn('/admin/diagnostics/errorevent/', index)
        self.client.force_login(self.dev)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_advanced_admin_locks_a_developers_records(self):
        self.client.force_login(self.admin)
        r = self.client.get(f'/admin/accounts/person/{self.dev.profile.pk}/change/')
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="is_developer"')
        self.assertNotContains(r, 'name="user_type"')
        self.assertEqual(
            self.client.get(f'/admin/auth/user/{self.dev.pk}/password/').status_code, 403)
        r = self.client.get(f'/admin/accounts/person/{self.staff.profile.pk}/change/')
        self.assertContains(r, 'name="user_type"')
        self.assertNotContains(r, 'name="is_developer"')


@override_settings(SECURE_SSL_REDIRECT=False, DEVELOPER_EMAILS=LISTED)
class DeveloperRoleRulesTests(TestCase):
    """Who may change a developer's role or grant developer access, on the website and the API."""
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('plainadmin', 'admin')
        cls.other_admin = make_user('otheradmin', 'admin')
        cls.dev = make_developer('thedev')
        cls.dev2 = make_developer('thedev2')

    def patch(self, actor, person, data):
        """PATCH ``person`` through the people API as ``actor``."""
        self.client.force_login(actor)
        return self.client.patch(f'/api/accounts/people/{person.pk}/', data,
                                 content_type='application/json')

    def test_admin_cannot_change_a_developers_role_on_the_website(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('accounts:member-role', args=[self.dev.profile.pk]),
                         {'user_type': 'student'})
        self.dev.profile.refresh_from_db()
        self.assertEqual(self.dev.profile.user_type, 'admin')
        self.assertTrue(self.dev.profile.is_developer)

    def test_admin_cannot_change_a_developer_through_the_api(self):
        r = self.patch(self.admin, self.dev.profile, {'user_type': 'student'})
        self.assertIn(r.status_code, (400, 403))
        r = self.patch(self.admin, self.admin.profile, {'is_developer': True})
        self.assertEqual(r.status_code, 400)
        r = self.patch(self.admin, self.other_admin.profile, {'is_developer': True})
        self.assertEqual(r.status_code, 400)
        self.client.force_login(self.admin)
        r = self.client.patch(f'/api/accounts/users/{self.dev.pk}/', {'email': 'x@evil.test'},
                              content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.delete(f'/api/accounts/people/{self.dev.profile.pk}/')
                         .status_code, 403)
        self.dev.refresh_from_db()
        self.assertEqual(self.dev.email, 'thedev@example.com')
        self.assertTrue(Person.objects.get(pk=self.dev.profile.pk).is_developer)

    def test_developer_grants_and_revokes_through_the_api(self):
        """A developer grants and changes roles via the API; the flag is dropped with the admin role."""
        r = self.patch(self.dev, self.other_admin.profile, {'is_developer': True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(Person.objects.get(pk=self.other_admin.profile.pk).is_developer)
        r = self.patch(self.dev, self.dev2.profile, {'user_type': 'staff'})
        self.assertEqual(r.status_code, 200, r.content)
        p = Person.objects.get(pk=self.dev2.profile.pk)
        self.assertEqual(p.user_type, 'staff')
        self.assertFalse(p.is_developer)

    def test_member_developer_page(self):
        """Only a developer uses the member-developer page; granting it to a student also makes them an administrator."""
        url = reverse('accounts:member-developer', args=[self.other_admin.profile.pk])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(url, {'grant': '1'}).status_code, 404)
        self.assertFalse(Person.objects.get(pk=self.other_admin.profile.pk).is_developer)
        self.client.force_login(self.dev)
        self.client.post(url, {'grant': '1'})
        self.assertTrue(Person.objects.get(pk=self.other_admin.profile.pk).is_developer)
        self.client.post(url, {'grant': '0'})
        p = Person.objects.get(pk=self.other_admin.profile.pk)
        self.assertFalse(p.is_developer)
        self.assertEqual(p.user_type, 'admin')
        student = make_user('stu', 'student')
        self.client.post(reverse('accounts:member-developer', args=[student.profile.pk]),
                         {'grant': '1'})
        p = Person.objects.get(user=student)
        self.assertTrue(p.is_developer)
        self.assertEqual(p.user_type, 'admin')
        self.assertTrue(User.objects.get(pk=student.pk).is_staff)

    def test_developer_cannot_revoke_a_listed_developer(self):
        listed = make_developer('listed')
        User.objects.filter(pk=listed.pk).update(email='SeeId24@gmail.com')
        EmailAddress.objects.create(user=listed, email='SeeId24@gmail.com', verified=True,
                                    primary=True)
        self.client.force_login(self.dev)
        self.client.post(reverse('accounts:member-developer', args=[listed.profile.pk]),
                         {'grant': '0'})
        self.assertTrue(Person.objects.get(user=listed).is_developer)
        self.client.post(reverse('accounts:member-role', args=[listed.profile.pk]),
                         {'user_type': 'staff'})
        self.assertEqual(Person.objects.get(user=listed).user_type, 'admin')


@override_settings(SECURE_SSL_REDIRECT=False, DEVELOPER_EMAILS=LISTED)
class AutomaticDeveloperTests(TestCase):
    """``DEVELOPER_EMAILS`` grants developer access to verified addresses automatically."""
    def test_setting_default_has_the_three_addresses(self):
        from django.conf import settings
        self.assertEqual(sorted(settings.DEVELOPER_EMAILS), sorted(LISTED))

    def test_verified_listed_address_becomes_developer(self):
        """A listed address becomes a developer only once it is verified."""
        user = make_user('cj', 'student')
        User.objects.filter(pk=user.pk).update(email='CJMordaunt@Gmail.com')
        addr = EmailAddress.objects.create(user=user, email='CJMordaunt@Gmail.com',
                                           verified=False, primary=True)
        self.assertFalse(Person.objects.get(user=user).is_developer)
        addr.verified = True
        addr.save()
        p = Person.objects.get(user=user)
        self.assertTrue(p.is_developer)
        self.assertEqual(p.user_type, 'admin')
        self.assertTrue(User.objects.get(pk=user.pk).is_staff)

    def test_unlisted_address_is_left_alone(self):
        user = make_user('someone', 'student')
        EmailAddress.objects.create(user=user, email='someone@example.com', verified=True)
        self.assertFalse(Person.objects.get(user=user).is_developer)

    def test_sign_in_regrants(self):
        """Signing in (which fires ``user_logged_in``) re-grants a listed developer."""
        user = make_user('gerson', 'admin')
        User.objects.filter(pk=user.pk).update(email='dev@metanoiagroup.co.za')
        EmailAddress.objects.create(user=user, email='dev@metanoiagroup.co.za', verified=True)
        Person.objects.filter(user=user).update(is_developer=False, user_type='staff')
        user = User.objects.get(pk=user.pk)
        self.client.force_login(user)
        p = Person.objects.get(user=user)
        self.assertTrue(p.is_developer)
        self.assertEqual(p.user_type, 'admin')

    def test_data_migration(self):
        migration = importlib.import_module('apps.accounts.migrations.0038_grant_developer_emails')
        verified = make_user('v', 'student')
        User.objects.filter(pk=verified.pk).update(email='seeid24@gmail.com')
        EmailAddress.objects.create(user=verified, email='seeid24@gmail.com', verified=True)
        trusted = make_user('t', 'admin')
        User.objects.filter(pk=trusted.pk).update(email='Dev@MetanoiaGroup.co.za')
        unverified = make_user('u', 'student')
        User.objects.filter(pk=unverified.pk).update(email='cjmordaunt@gmail.com')
        other = make_user('o', 'admin')
        Person.objects.filter(user__in=[verified, trusted]).update(is_developer=False)
        migration.grant(django_apps, None)
        self.assertTrue(Person.objects.get(user=verified).is_developer)
        self.assertEqual(Person.objects.get(user=verified).user_type, 'admin')
        self.assertTrue(Person.objects.get(user=trusted).is_developer)
        self.assertFalse(Person.objects.get(user=unverified).is_developer)
        self.assertFalse(Person.objects.get(user=other).is_developer)
        self.assertEqual(User.objects.filter(email__iexact='nobody-new@example.com').count(), 0)

    def test_grant_helper_needs_verification(self):
        user = make_user('x', 'student')
        User.objects.filter(pk=user.pk).update(email='seeid24@gmail.com')
        self.assertFalse(developers.grant_if_listed(User.objects.get(pk=user.pk)))
