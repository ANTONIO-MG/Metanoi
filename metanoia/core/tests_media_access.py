"""Who may fetch what under ``/media/`` (core.media_access).

Run against the real ``config.urls`` route, with ``MEDIA_ROOT`` pointed at a
throw-away directory holding real files, so every 200 is a file actually served
and every 404 is a refusal rather than a missing file.

``FILES`` lists the fixture files written under ``MEDIA_ROOT``, one per access
class: public (institution logos), general (login), private staff-only
(finance, paid downloads, AI uploads, tasks), private owner + teachers (exam
answers, submissions) and private chat members.
"""

import shutil
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.accounts.models import ParentLink, Person
from apps.assessments import models as am
from apps.communication.models import ChatGroup, ChatMembership, Message, MessageAttachment
from core.media_access import classify, normalise
from core.testing import enrol, make_module, make_programme

User = get_user_model()

FILES = {
    'institutions/logo.png': b'\x89PNG\r\n\x1a\n',
    'profile_pics/me.png': b'\x89PNG\r\n\x1a\n',
    'feed/2026/10/post.png': b'\x89PNG\r\n\x1a\n',
    'evil.svg': b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    'payment_proofs/2026/10/slip.pdf': b'%PDF-1.4 slip',
    'expenses/2026/10/receipt.pdf': b'%PDF-1.4 receipt',
    'shop/downloads/pack.zip': b'PK\x03\x04',
    'shop/products/cover.png': b'\x89PNG\r\n\x1a\n',
    'answers/answer.pdf': b'%PDF-1.4 answer',
    'assignments/essay.pdf': b'%PDF-1.4 essay',
    'chat/2026/10/photo.png': b'\x89PNG\r\n\x1a\n',
    'certificates/C-1.pdf': b'%PDF-1.4 cert',
    'ai_uploads/notes.pdf': b'%PDF-1.4 notes',
    'tasks/brief.pdf': b'%PDF-1.4 brief',
}

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


class MediaAccessTests(TestCase):

    """Access classes, security headers and path traversal for ``/media/``."""
    @classmethod
    def setUpClass(cls):
        """Write :data:`FILES` to a temporary ``MEDIA_ROOT``.

        A ``-secret.txt`` file is also written *beside* (outside) that root: it must
        never be reachable.
        """
        super().setUpClass()
        cls.root = Path(tempfile.mkdtemp(prefix='media-access-'))
        for name, data in FILES.items():
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (cls.root.parent / f'{cls.root.name}-secret.txt').write_text('secret')
        cls._override = override_settings(MEDIA_ROOT=str(cls.root), CACHES=LOCMEM)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        """Restore settings and remove the temporary files."""
        cls._override.disable()
        shutil.rmtree(cls.root, ignore_errors=True)
        (cls.root.parent / f'{cls.root.name}-secret.txt').unlink(missing_ok=True)
        super().tearDownClass()

    def setUp(self):
        """Create a student, another student, an educator, the student's parent and staff."""
        cache.clear()
        self.student = self._user('stu@example.com', 'student')
        self.other = self._user('other@example.com', 'student')
        self.educator = self._user('ed@example.com', 'educator')
        self.parent = self._user('mum@example.com', 'parent')
        ParentLink.objects.create(parent=self.parent, student=self.student)
        self.staff = self._user('staff@example.com', 'staff', is_staff=True)

    def _user(self, email, role, **extra):
        """Create an onboarded user with ``role`` as their ``user_type``."""
        user = User.objects.create_user(username=email, email=email, password='x',
                                        first_name=role.title(), last_name='User', **extra)
        person = Person.objects.get(user=user)
        person.user_type, person.registered, person.profile_status = role, True, True
        person.save()
        return user

    def get(self, path, user=None):
        """GET ``/media/<path>`` signed in as ``user`` (anonymous when ``None``)."""
        self.client.logout()
        if user is not None:
            self.client.force_login(user)
        return self.client.get(f'/media/{path}')

    def assertServed(self, response, name):
        """Assert a 200 whose streamed body is exactly the fixture file ``name``."""
        self.assertEqual(response.status_code, 200, f'{name}: expected 200')
        self.assertEqual(b''.join(response.streaming_content), FILES[name])

    def test_institution_logos_are_public(self):
        self.assertServed(self.get('institutions/logo.png'), 'institutions/logo.png')

    def test_general_uploads_need_a_login(self):
        for name in ('profile_pics/me.png', 'feed/2026/10/post.png', 'shop/products/cover.png'):
            response = self.get(name)
            self.assertEqual(response.status_code, 302, name)
            self.assertIn('next=', response['Location'])
            self.assertServed(self.get(name, self.student), name)
            self.assertServed(self.get(name, self.staff), name)

    def test_private_responses_are_not_publicly_cacheable(self):
        response = self.get('feed/2026/10/post.png', self.student)
        self.assertIn('private', response['Cache-Control'])

    def test_security_headers_still_apply(self):
        """Served files keep nosniff, attachment and DENY; a 404 from the gate still carries nosniff."""
        response = self.get('evil.svg', self.student)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(self.get('payment_proofs/2026/10/slip.pdf')['X-Content-Type-Options'], 'nosniff')

    def test_finance_and_paid_downloads_are_staff_only(self):
        for name in ('payment_proofs/2026/10/slip.pdf', 'expenses/2026/10/receipt.pdf',
                     'shop/downloads/pack.zip', 'ai_uploads/notes.pdf', 'tasks/brief.pdf'):
            self.assertEqual(self.get(name).status_code, 404, f'{name} anonymous')
            self.assertEqual(self.get(name, self.student).status_code, 404, f'{name} student')
            self.assertEqual(self.get(name, self.educator).status_code, 404, f'{name} educator')
            self.assertServed(self.get(name, self.staff), name)

    def test_a_staff_role_without_is_staff_counts_as_staff(self):
        admin = self._user('admin@example.com', 'admin')
        self.assertServed(self.get('expenses/2026/10/receipt.pdf', admin), 'expenses/2026/10/receipt.pdf')

    def _answer(self):
        """Give the student an exam answer file and an assignment submission."""
        programme = make_programme('CTA', 'PGDA')
        module = make_module('Advanced Auditing', 'AUDA', programme=programme)
        enrol(self.student.profile, module)
        assessment = am.Assessment.objects.create(
            module=module, title='Quiz', kind=am.Assessment.KIND_QUIZ, total_marks=1,
            pass_mark_pct=50, created_by=self.educator)
        section = am.Section.objects.create(assessment=assessment, title='Q', order=0)
        question = am.Question.objects.create(section=section, type='tf', text='?', marks=1)
        attempt = am.AssessmentAttempt.objects.create(assessment=assessment, student=self.student)
        am.Answer.objects.create(attempt=attempt, question=question, file='answers/answer.pdf')
        am.AssignmentSubmission.objects.create(assessment=assessment, student=self.student,
                                               file='assignments/essay.pdf')

    def test_exam_answers_and_submissions(self):
        self._answer()
        for name in ('answers/answer.pdf', 'assignments/essay.pdf'):
            self.assertEqual(self.get(name).status_code, 404, f'{name} anonymous')
            self.assertEqual(self.get(name, self.other).status_code, 404, f'{name} other student')
            self.assertServed(self.get(name, self.student), name)
            self.assertServed(self.get(name, self.parent), name)
            self.assertServed(self.get(name, self.educator), name)
            self.assertServed(self.get(name, self.staff), name)

    def test_an_answer_file_with_no_owning_row_is_staff_only(self):
        self.assertEqual(self.get('answers/answer.pdf', self.student).status_code, 404)

    def test_chat_files_are_for_members_of_that_chat(self):
        group = ChatGroup.objects.create(kind=ChatGroup.KIND_DIRECT)
        ChatMembership.objects.create(group=group, user=self.student)
        message = Message.objects.create(group=group, sender=self.student, body='look')
        MessageAttachment.objects.create(message=message, file='chat/2026/10/photo.png',
                                         kind=MessageAttachment.KIND_IMAGE)
        name = 'chat/2026/10/photo.png'
        self.assertEqual(self.get(name).status_code, 404)
        self.assertEqual(self.get(name, self.other).status_code, 404)
        self.assertEqual(self.get(name, self.educator).status_code, 404)
        self.assertServed(self.get(name, self.student), name)
        self.assertServed(self.get(name, self.staff), name)

    def test_traversal_cannot_reclassify_a_private_file(self):
        response = self.get('institutions/../payment_proofs/2026/10/slip.pdf')
        self.assertEqual(response.status_code, 404)
        response = self.get('institutions/%2e%2e/payment_proofs/2026/10/slip.pdf')
        self.assertEqual(response.status_code, 404)
        response = self.get('institutions/../payment_proofs/2026/10/slip.pdf', self.student)
        self.assertEqual(response.status_code, 404)

    def test_nothing_outside_media_root_is_reachable(self):
        """Relative escapes never reach the outside file.

        ``/etc/passwd`` resolves to ``MEDIA_ROOT/etc/passwd`` (a general path), never
        the real one.
        """
        outside = f'../{self.root.name}-secret.txt'
        for user in (None, self.student, self.staff):
            self.assertEqual(self.get(outside, user).status_code, 404)
            self.assertEqual(self.get(f'institutions/../../{self.root.name}-secret.txt', user).status_code, 404)
            self.assertIn(self.get('/etc/passwd', user).status_code, (302, 404))

    def test_directories_are_not_listed(self):
        self.assertEqual(self.get('institutions/', self.staff).status_code, 404)
        self.assertEqual(self.get('', self.staff).status_code, 404)

    def test_normalise_and_classify(self):
        """Path normalisation and classification.

        Course content is gated per row (:mod:`apps.learning.content_files`), except
        the cover/author images shown in listings (general) and staff packs
        (private).
        """
        self.assertEqual(normalise('a/./b/../c.png'), 'a/c.png')
        self.assertIsNone(normalise('../x'))
        self.assertIsNone(normalise('a\\..\\x'))
        self.assertEqual(normalise('//etc/passwd'), 'etc/passwd')
        self.assertEqual(classify('institutions/x.png'), 'public')
        self.assertEqual(classify('payment_proofs/x.pdf'), 'private')
        self.assertEqual(classify('lessons/x.pdf'), 'course')
        self.assertEqual(classify('modules/materials/memo.pdf'), 'course')
        self.assertEqual(classify('questions/q.png'), 'course')
        self.assertEqual(classify('uploads/content/2026/11/x.pdf'), 'course')
        self.assertEqual(classify('lessons/covers/c.png'), 'general')
        self.assertEqual(classify('modules/staff-only/pack.pdf'), 'private')
