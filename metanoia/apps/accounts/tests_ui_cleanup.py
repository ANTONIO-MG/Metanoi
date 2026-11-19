"""The owner's clean-up of the student hub and the profile page.

* Students no longer have Assessments (the list), Tasks or Revision: the nav
  hides them and opening one directly lands on the dashboard with a note
  (core.student_hidden). Staff and educators keep them.
* A person's profile is About + Subjects; the retired tabs redirect to About.
* The sidebar's Modules / Tests 1–4 / Readiness tiles are a student's numbers,
  not the admin team's.
"""

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.livesessions.tests import make_user
from core.student_hidden import NOTE

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=LOCMEM_CACHE)
class StudentHiddenSectionsTests(TestCase):

    """Assessments, Tasks and Revision are hidden from students but not from educators."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.student = make_user('uc_stu', 'student')
        self.educator = make_user('uc_edu', 'educator')

    HIDDEN = ('assessments:index', 'revision:home', 'revision:session')

    def test_student_is_sent_to_dashboard_with_a_note(self):
        self.client.force_login(self.student)
        for name in self.HIDDEN:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertRedirects(response, reverse('myhub:index'), fetch_redirect_response=False)
                self.assertIn(NOTE, [str(m) for m in get_messages(response.wsgi_request)])

    def test_educator_still_reaches_the_pages(self):
        self.client.force_login(self.educator)
        for name in ('assessments:index',):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_student_nav_and_dashboard_hide_the_entry_points(self):
        """The student nav and dashboard carry no links to the hidden sections.

        The POST-only reminder endpoint is never a plain link; "All notices" opens
        the student's notifications, not the staff announcements page; and the
        student keeps their own numbers (Readiness) in the sidebar card.
        """
        self.client.force_login(self.student)
        html = self.client.get(reverse('myhub:index')).content.decode()
        for href in ('href="/assessments/"', 'href="/tasks/"', 'href="/revision/"'):
            self.assertNotIn(href, html)
        self.assertNotIn('Your tasks', html)
        self.assertNotIn('Assessment readiness', html)
        self.assertNotIn(f'href="{reverse("myhub:add-reminder")}"', html)
        self.assertIn(f'href="{reverse("livesessions:calendar")}#add-reminder"', html)
        self.assertNotIn(f'href="{reverse("communication:announcements")}"', html)
        self.assertIn('Readiness', html)

    def test_profile_linked_at_most_twice(self):
        self.client.force_login(self.student)
        html = self.client.get(reverse('myhub:index')).content.decode()
        self.assertLessEqual(html.count('href="/community/profile/"'), 2)


@override_settings(CACHES=LOCMEM_CACHE)
class AdminSidebarTests(TestCase):

    """The admin team's sidebar shows no student metrics or Assessments/Reports links."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = make_user('uc_adm', 'admin')

    def test_no_student_metrics_or_assessments_reports_links(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse('accounts:my-profile')).content.decode()
        self.assertNotIn('<span>Readiness</span>', html)
        self.assertNotIn('<span>Tests 1–4</span>', html)
        self.assertNotIn('<span>Assessments</span>', html)
        self.assertNotIn('<span>Reports</span>', html)


@override_settings(CACHES=LOCMEM_CACHE)
class ProfileTabsTests(TestCase):

    """A profile is About + Subjects; the retired tabs redirect to About."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.student = make_user('uc_prof', 'student', first_name='Pat', last_name='Lee')
        self.person = self.student.profile

    def test_only_about_and_subjects(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('accounts:my-profile'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([t['key'] for t in response.context['tabs']], ['about', 'modules'])
        self.assertEqual(response.context['active_tab'], 'about')
        html = response.content.decode()
        for gone in ('/feed/', '/documents/', '/activity/', '/progress/'):
            self.assertNotIn(f'/community/profile/{self.person.pk}{gone}', html)
        self.assertNotIn('id="profActivity"', html)
        self.assertNotIn('id="profFiles"', html)

    def test_subjects_tab_renders(self):
        self.client.force_login(self.student)
        response = self.client.get(reverse('accounts:profile', args=[self.person.pk, 'modules']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Subjects')

    def test_retired_tabs_redirect_to_about(self):
        self.client.force_login(self.student)
        about = reverse('accounts:profile', args=[self.person.pk, 'about'])
        for tab in ('feed', 'documents', 'activity', 'progress', 'nonsense'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('accounts:profile', args=[self.person.pk, tab]))
                self.assertRedirects(response, about, fetch_redirect_response=False)
        response = self.client.get(reverse('accounts:profile', args=[self.person.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_tab'], 'about')
