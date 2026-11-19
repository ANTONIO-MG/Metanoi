"""Super student: admins and educators switch into a student's view."""

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment
from apps.livesessions.tests import make_user
from core import student_view as sv
from core.scoping import directory_users, taught_students
from core.testing import make_module, make_programme

from .models import Person


class StudentViewTests(TestCase):

    """Super student: who may switch, the scope, the swap, list exclusion and read-only chat."""
    @classmethod
    def setUpTestData(cls):
        cls.pgda = make_programme('MILPARK', 'PGDA')
        cls.unisa = make_programme('UNISA', 'PGDA')
        cls.frep = make_module('Financial Reporting', 'FREP', programme=cls.pgda, price=500)
        cls.taxa = make_module('Taxation', 'TAXA', programme=cls.pgda, price=500)
        cls.tax = make_module('Taxation (UNISA)', 'TAX', programme=cls.unisa, price=500)

        cls.admin = make_user('ada', 'admin', first_name='Ada', last_name='Admin')
        cls.edu = make_user('eve', 'educator', first_name='Eve', last_name='Naidoo')
        cls.edu.profile.taught_modules.add(cls.frep)
        cls.student = make_user('sam', 'student', first_name='Sam', last_name='Mokoena')
        cls.parent = make_user('pat', 'parent')

    def switch(self, user, **scope):
        """Sign in as ``user`` and start student view with ``scope``."""
        self.client.force_login(user)
        return self.client.post(reverse('accounts:student-view-start'), scope)

    def test_only_admin_staff_and_educators_may_switch(self):
        self.assertTrue(sv.can_use(self.admin))
        self.assertTrue(sv.can_use(self.edu))
        self.assertFalse(sv.can_use(self.student))
        self.assertFalse(sv.can_use(self.parent))
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse('accounts:student-view')).status_code, 403)
        self.assertEqual(self.switch(self.student).status_code, 403)

    def test_navbar_offers_the_toggle_to_staff_not_students(self):
        """The navbar offers the toggle to staff (``/myhub/`` sends them on to the staff desk), not to students."""
        self.client.force_login(self.admin)
        self.assertContains(self.client.get('/myhub/', follow=True), reverse('accounts:student-view'))
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get('/myhub/'), reverse('accounts:student-view'))

    def test_admin_global_view_registers_every_module(self):
        self.switch(self.admin)
        preview = sv.preview_user_for(self.admin, create=False)
        held = set(ModuleEnrolment.objects.filter(person=preview.profile)
                   .values_list('programme_module_id', flat=True))
        self.assertEqual(held, {self.frep.pk, self.taxa.pk, self.tax.pk})
        self.assertTrue(all(e.is_unlocked for e in ModuleEnrolment.objects.filter(person=preview.profile)))

    def test_switching_scope_replaces_the_enrolments(self):
        self.switch(self.admin)
        self.switch(self.admin, programme=self.unisa.pk)
        preview = sv.preview_user_for(self.admin, create=False)
        self.assertEqual(list(ModuleEnrolment.objects.filter(person=preview.profile)
                              .values_list('programme_module_id', flat=True)), [self.tax.pk])
        self.assertEqual(list(ProgrammeEnrolment.objects.filter(person=preview.profile)
                              .values_list('programme_id', flat=True)), [self.unisa.pk])

    def test_institution_scope_covers_its_programmes(self):
        self.switch(self.admin, institution=self.pgda.institution_id)
        preview = sv.preview_user_for(self.admin, create=False)
        self.assertEqual(set(preview.profile.selected_modules.values_list('pk', flat=True)),
                         {self.frep.pk, self.taxa.pk})

    def test_educator_sees_only_the_modules_they_teach(self):
        self.switch(self.edu)
        preview = sv.preview_user_for(self.edu, create=False)
        self.assertEqual(list(preview.profile.selected_modules.values_list('pk', flat=True)),
                         [self.frep.pk])

    def test_educator_cannot_pick_a_programme_they_do_not_teach(self):
        r = self.switch(self.edu, programme=self.unisa.pk)
        self.assertRedirects(r, reverse('accounts:student-view'), fetch_redirect_response=False)
        self.assertNotIn(sv.SESSION_KEY, self.client.session)

    def test_requests_run_as_the_preview_student_until_exit(self):
        self.switch(self.admin, programme=self.pgda.pk)
        r = self.client.get('/myhub/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.wsgi_request.user.profile.preview_of_id, self.admin.pk)
        self.assertEqual(r.wsgi_request.real_user, self.admin)
        self.assertTrue(r.context['is_student'])
        self.assertFalse(r.context['is_admin_staff'])
        self.assertContains(r, 'Exit student view')

        self.client.post(reverse('accounts:student-view-stop'))
        r = self.client.get('/myhub/', follow=True)
        self.assertEqual(r.wsgi_request.user, self.admin)
        self.assertNotContains(r, 'Exit student view')

    def test_management_pages_are_closed_in_student_view(self):
        self.switch(self.admin)
        r = self.client.get(reverse('accounts:manage-members'))
        self.assertNotEqual(r.status_code, 200)

    def test_preview_account_cannot_sign_in(self):
        """The preview account has no usable password, and wears its owner's picture rather than a file of its own."""
        self.switch(self.admin)
        preview = sv.preview_user_for(self.admin, create=False)
        self.assertFalse(preview.has_usable_password())
        self.assertTrue(sv.is_preview_email(preview.email))
        self.assertFalse(preview.profile.profile_picture)
        self.assertEqual(preview.profile.avatar_url, self.admin.profile.avatar_url)

    def test_preview_is_left_out_of_rosters_and_search(self):
        self.switch(self.admin)
        preview = sv.preview_user_for(self.admin, create=False)
        self.assertNotIn(preview.profile, self.frep.students)
        self.assertNotIn(preview, taught_students(self.admin))
        self.assertNotIn(preview, directory_users(self.admin))
        self.assertNotIn(preview, directory_users(self.student))
        self.assertTrue(Person.objects.filter(pk=preview.profile.pk, user_type='student').exists())

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_no_email_is_ever_sent_to_a_preview_account(self):
        from apps.communication.emails import send_branded_email
        self.switch(self.admin)
        preview = sv.preview_user_for(self.admin, create=False)
        mail.outbox = []
        self.assertFalse(send_branded_email('Hi', [preview.email], 'password_changed', {}))
        self.assertEqual(mail.outbox, [])

    def test_chat_is_read_only_in_student_view(self):
        """Chat is read-only in student view.

        Reading still works and marking a thread read is allowed; out of student
        view, posting works again for the module's coach.
        """
        from apps.communication.models import ChatGroup, Message
        from apps.communication.services import sync_module_chat_members
        self.switch(self.admin, programme=self.pgda.pk)
        preview = sv.preview_user_for(self.admin, create=False)
        group = sync_module_chat_members(self.frep)
        self.assertTrue(group.memberships.filter(user=preview).exists())

        r = self.client.post('/api/communication/messages/', {'group': group.pk, 'body': 'hello'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Message.objects.filter(group=group).exists())
        r = self.client.post('/api/communication/chat-groups/', {'name': 'New'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(ChatGroup.objects.filter(name='New').exists())
        r = self.client.get(f'/communication/chat/direct/{self.student.pk}/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(f'/communication/chat/{group.pk}/').status_code, 200)
        r = self.client.post(f'/api/communication/chat-groups/{group.pk}/mark_read/')
        self.assertEqual(r.status_code, 200)

        self.switch(self.edu)
        r = self.client.post('/api/communication/messages/', {'group': group.pk, 'body': 'blocked'})
        self.assertEqual(r.status_code, 403)
        self.client.post(reverse('accounts:student-view-stop'))
        r = self.client.post('/api/communication/messages/', {'group': group.pk, 'body': 'hello'})
        self.assertEqual(r.status_code, 201, r.content)


class CohortAndParentPreviewTests(TestCase):
    """Preview one intake of a programme, and preview as that student's parent."""

    @classmethod
    def setUpTestData(cls):
        from apps.learning.models import Cohort
        cls.pgda = make_programme('MILPARK', 'PGDA')
        cls.unisa = make_programme('UNISA', 'PGDA')
        cls.frep = make_module('Financial Reporting', 'FREP', programme=cls.pgda, price=500)
        cls.tax = make_module('Taxation (UNISA)', 'TAX', programme=cls.unisa, price=500)
        cls.p26s = Cohort.objects.create(programme=cls.pgda, code='P26S')
        cls.p27f = Cohort.objects.create(programme=cls.pgda, code='P27F')
        cls.other = Cohort.objects.create(programme=cls.unisa, code='2027')
        cls.admin = make_user('ada', 'admin', first_name='Ada', last_name='Admin')
        cls.edu = make_user('eve', 'educator')
        cls.edu.profile.taught_modules.add(cls.frep)

    def switch(self, user, **scope):
        """Sign in as ``user`` and start student view with ``scope``."""
        self.client.force_login(user)
        return self.client.post(reverse('accounts:student-view-start'), scope)

    def enrolment(self, owner, programme):
        """The preview student's programme enrolment for ``owner`` in ``programme``."""
        preview = sv.preview_user_for(owner, create=False)
        return ProgrammeEnrolment.objects.get(person=preview.profile, programme=programme)

    def test_picker_lists_each_programmes_cohorts(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse('accounts:student-view'))
        self.assertContains(r, 'P26S')
        self.assertContains(r, 'P27F')
        self.assertContains(r, f'name="cohort" value="{self.p27f.pk}"')

    def test_picking_a_cohort_places_the_preview_student_in_it(self):
        """Picking a cohort places the preview student in it, and it shows in the "seeing the platform as" bar."""
        self.switch(self.admin, cohort=self.p26s.pk)
        self.assertEqual(self.enrolment(self.admin, self.pgda).cohort, self.p26s)
        self.assertEqual(self.client.session[sv.SESSION_KEY]['cohort'], self.p26s.pk)
        self.switch(self.admin, cohort=self.p27f.pk)
        self.assertEqual(self.enrolment(self.admin, self.pgda).cohort, self.p27f)
        r = self.client.get('/myhub/')
        self.assertContains(r, 'P27F')

    def test_programme_scope_goes_back_to_the_default_cohort(self):
        self.switch(self.admin, cohort=self.p26s.pk)
        self.switch(self.admin, programme=self.pgda.pk)
        self.assertNotEqual(self.enrolment(self.admin, self.pgda).cohort, self.p26s)

    def test_cohort_must_belong_to_the_programme_and_be_allowed(self):
        """A cohort outside the programme, or a programme the educator does not teach, is refused."""
        r = self.switch(self.admin, programme=self.pgda.pk, cohort=self.other.pk)
        self.assertRedirects(r, reverse('accounts:student-view'), fetch_redirect_response=False)
        r = self.switch(self.edu, cohort=self.other.pk)
        self.assertRedirects(r, reverse('accounts:student-view'), fetch_redirect_response=False)
        self.assertNotIn(sv.SESSION_KEY, self.client.session)

    def test_parent_view_runs_as_the_preview_students_parent(self):
        """Parent view runs as the preview student's parent, reusing the same accounts rather than duplicating them."""
        from core.scoping import children_of
        self.switch(self.admin, cohort=self.p27f.pk, **{'as': 'parent'})
        r = self.client.get('/myhub/', follow=True)
        user = r.wsgi_request.user
        student = sv.preview_user_for(self.admin, create=False)
        self.assertEqual(user.profile.user_type, 'parent')
        self.assertEqual(user.profile.preview_of_id, student.pk)
        self.assertTrue(user.profile.is_preview)
        self.assertEqual(list(children_of(user)), [student])
        self.assertEqual(r.wsgi_request.real_user, self.admin)
        self.assertContains(r, 'Exit parent view')
        self.switch(self.admin, programme=self.unisa.pk, **{'as': 'parent'})
        self.assertEqual(Person.objects.filter(preview_of=student).count(), 1)
        self.client.post(reverse('accounts:student-view-stop'))
        r = self.client.get('/myhub/', follow=True)
        self.assertEqual(r.wsgi_request.user, self.admin)

    def test_preview_parent_is_left_out_of_rosters(self):
        self.switch(self.admin, **{'as': 'parent'})
        parent = sv.preview_parent_for(self.admin, create=False)
        self.assertFalse(directory_users(self.admin).filter(pk=parent.pk).exists())
        self.assertTrue(sv.is_preview_email(parent.email))
        self.assertFalse(sv.can_use(parent))
