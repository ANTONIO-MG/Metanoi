"""Paid course files under ``/media/`` follow the page's access rule.

The finding: any signed-in account — a free ``/join/`` sign-up included — could
download a paid module's memo, solution or lesson file by its (guessable) media
URL. These tests drive the real ``/media/`` route with real files on disk, so a
200 is a file actually served and a 404 a refusal, not a missing file.

Covered: module materials, lessons, question media, and public / general
uploads that must keep working.
"""

import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person
from apps.assessments import models as am
from apps.learning import models
from apps.learning.general import general_module_for, general_phase_for
from core.storage import UnguessableUploadTo
from core.testing import enrol, make_module, make_programme

User = get_user_model()

MEMO = 'modules/materials/FREP_Mock_2_Memo.pdf'
GENERAL_DOC = 'modules/materials/Candidate_Guide.pdf'
PACK_FILE = 'modules/2026/IAC/FREP/abcd1234-Question_Pack.pdf'
BLOCK_IMG = 'lessons/blocks/diagram.png'
ATTACHMENT = 'lessons/Workbook.pdf'
QUESTION_IMG = 'questions/q1.png'
ORPHAN = 'modules/materials/Nobody_Links_This.pdf'
COVER = 'lessons/covers/cover.png'
LOGO = 'institutions/logo.png'

FILES = {name: f'%PDF-1.4 {name}'.encode() for name in (
    MEMO, GENERAL_DOC, PACK_FILE, BLOCK_IMG, ATTACHMENT, QUESTION_IMG, ORPHAN, COVER, LOGO)}

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _user(email, role, **extra):
    """Create a registered user with the given role (``student``, ``educator``,
    ``staff`` …).
    """
    user = User.objects.create_user(username=email, email=email, password='x',
                                    first_name=role.title(), last_name='User', **extra)
    person = Person.objects.get(user=user)
    person.user_type, person.registered, person.profile_status = role, True, True
    person.save()
    return user


class PaidCourseFileTests(TestCase):

    """Who may download each kind of course file, through the real ``/media/`` route.

    The accounts: enrolled, trialling, lapsed, a free ``/join/`` sign-up (signed in,
    on nothing), the module's educator, another educator and staff.
    """
    @classmethod
    def setUpClass(cls):
        """Write the test files to a temporary media root and point the settings at it.
        """
        super().setUpClass()
        cls.root = Path(tempfile.mkdtemp(prefix='content-files-'))
        for name, data in FILES.items():
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        cls._override = override_settings(MEDIA_ROOT=str(cls.root), FILES_ROOT=str(cls.root),
                                          CACHES=LOCMEM)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        """Restore the settings and remove the temporary media root."""
        cls._override.disable()
        shutil.rmtree(cls.root, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.programme = make_programme('CFINST', 'CFPROG')
        self.offering = make_module('Financial reporting', 'CFFR', programme=self.programme, price=450)

        self.enrolled = _user('paid@example.com', 'student')
        enrol(self.enrolled.profile, self.offering)
        self.trialling = _user('trial@example.com', 'student')
        trial = enrol(self.trialling.profile, self.offering, status=models.ModuleEnrolment.STATUS_TRIAL)
        trial.trial_ends_at = timezone.now() + timedelta(days=5)
        trial.save()
        self.lapsed = _user('lapsed@example.com', 'student')
        enrol(self.lapsed.profile, self.offering, active=False)
        self.free = _user('free@example.com', 'student')
        self.educator = _user('ed@example.com', 'educator')
        self.offering.educators.add(self.educator.profile)
        self.other_educator = _user('ed2@example.com', 'educator')
        self.staff = _user('staff@example.com', 'staff', is_staff=True)

        self.phase = models.ModulePhase.objects.create(programme_module=self.offering, kind='test',
                                                       sequence=1, order=0)
        self.memo = models.ModuleMaterial.objects.create(
            phase=self.phase, kind=models.ModuleMaterial.KIND_DOCUMENT, title='Mock 2 memo', file=MEMO)
        models.ModuleMaterial.objects.create(
            phase=self.phase, kind=models.ModuleMaterial.KIND_QUESTIONS, title='Pack', file=PACK_FILE)

        topic = models.Topic.objects.create(programme_module=self.offering, code='CF-1', title='Topic')
        self.lesson = models.Lesson.objects.create(module=self.offering, topic=topic, title='Guide',
                                                   status=models.Lesson.STATUS_PUBLISHED)
        models.LessonBlock.objects.create(lesson=self.lesson, block_type=models.LessonBlock.TYPE_IMAGE,
                                          media=BLOCK_IMG)
        models.LessonResource.objects.create(lesson=self.lesson, kind='pdf', title='Workbook',
                                             file=ATTACHMENT)

        self.assessment = am.Assessment.objects.create(
            module=self.offering, title='Quiz', kind=am.Assessment.KIND_QUIZ, total_marks=1,
            pass_mark_pct=50)
        section = am.Section.objects.create(assessment=self.assessment, title='Q', order=0)
        am.Question.objects.create(section=section, type='tf', text='?', marks=1, media=QUESTION_IMG)

    def get(self, path, user=None):
        """Fetch ``/media/<path>`` as ``user`` (signed out when ``None``)."""
        self.client.logout()
        if user is not None:
            self.client.force_login(user)
        return self.client.get(f'/media/{path}')

    def assertServed(self, path, user):
        """Assert ``path`` is served to ``user`` with its real bytes and a private cache
        header.
        """
        response = self.get(path, user)
        self.assertEqual(response.status_code, 200, f'{path} for {user}: expected 200')
        self.assertEqual(b''.join(response.streaming_content), FILES[path])
        self.assertIn('private', response['Cache-Control'])

    def assertRefused(self, path, user):
        """Assert ``path`` is refused to ``user`` with a 404."""
        self.assertEqual(self.get(path, user).status_code, 404, f'{path} for {user}: expected 404')

    def test_paid_material_is_refused_to_free_and_unpaid_accounts(self):
        for path in (MEMO, PACK_FILE):
            self.assertRefused(path, self.free)
            self.assertRefused(path, self.lapsed)
            self.assertRefused(path, self.other_educator)

    def test_paid_material_is_served_to_the_enrolled_the_educator_and_staff(self):
        for path in (MEMO, PACK_FILE):
            for user in (self.enrolled, self.trialling, self.educator, self.staff):
                self.assertServed(path, user)

    def test_signed_out_is_sent_to_sign_in(self):
        response = self.get(MEMO)
        self.assertEqual(response.status_code, 302)
        self.assertIn('next=', response['Location'])

    def test_unpublished_material_stays_with_its_authors(self):
        self.memo.is_published = False
        self.memo.save()
        self.assertRefused(MEMO, self.enrolled)
        self.assertServed(MEMO, self.educator)

    def test_a_single_item_purchase_opens_that_file(self):
        from apps.shop.models import Order, OrderItem, Product
        product = Product.objects.create(name='Mock 2 memo', price=50, status='active')
        self.memo.product = product
        self.memo.save()
        order = Order.objects.create(buyer=self.free, status=Order.STATUS_PAID)
        OrderItem.objects.create(order=order, product=product, quantity=1, unit_price=50)
        self.assertServed(MEMO, self.free)
        self.assertRefused(PACK_FILE, self.free)

    def test_general_module_files_follow_the_general_rule(self):
        general = general_module_for(self.programme)
        models.ModuleMaterial.objects.create(
            phase=general_phase_for(general), kind=models.ModuleMaterial.KIND_DOCUMENT,
            title='Candidate guide', file=GENERAL_DOC)
        self.assertServed(GENERAL_DOC, self.lapsed)
        self.assertServed(GENERAL_DOC, self.enrolled)

    def test_a_file_no_row_references_is_staff_only(self):
        self.assertRefused(ORPHAN, self.enrolled)
        self.assertRefused(ORPHAN, self.educator)
        self.assertServed(ORPHAN, self.staff)

    def test_the_material_link_is_the_gated_door(self):
        self.assertEqual(self.memo.target_url, reverse('learning:material-open', args=[self.memo.pk]))

    def test_lesson_files_follow_the_lesson_rule(self):
        for path in (BLOCK_IMG, ATTACHMENT):
            self.assertRefused(path, self.free)
            self.assertRefused(path, self.lapsed)
            self.assertServed(path, self.enrolled)
            self.assertServed(path, self.educator)
            self.assertServed(path, self.staff)

    def test_question_media_needs_an_attempt_or_the_teacher(self):
        self.assertRefused(QUESTION_IMG, self.free)
        self.assertRefused(QUESTION_IMG, self.enrolled)
        am.AssessmentAttempt.objects.create(assessment=self.assessment, student=self.enrolled)
        self.assertServed(QUESTION_IMG, self.enrolled)
        self.assertServed(QUESTION_IMG, self.educator)
        self.assertServed(QUESTION_IMG, self.staff)

    def test_public_and_listing_images_still_work(self):
        response = self.get(LOGO)
        self.assertEqual(response.status_code, 200)
        self.assertServed(COVER, self.free)


class UnguessableUploadToTests(TestCase):

    """New course uploads are stored under a random directory, so their URL cannot be
    guessed.
    """
    def test_new_uploads_get_a_random_directory(self):
        upload_to = UnguessableUploadTo('modules/materials')
        first = upload_to(None, 'Memo.pdf')
        second = upload_to(None, 'Memo.pdf')
        self.assertNotEqual(first, second)
        prefix, token, name = first.rsplit('/', 2)
        self.assertEqual(prefix, 'modules/materials')
        self.assertEqual(name, 'Memo.pdf')
        self.assertGreaterEqual(len(token), 16)

    def test_dated_variant_and_path_stripping(self):
        path = UnguessableUploadTo('uploads/content', dated=True)(None, '../../etc/passwd')
        self.assertTrue(path.startswith(f'uploads/content/{timezone.now():%Y/%m}/'))
        self.assertTrue(path.endswith('/passwd'))
        self.assertNotIn('..', path)

    def test_fields_use_it(self):
        from apps.learning.upload_models import ContentUploadFile
        for field in (models.ModuleMaterial._meta.get_field('file'),
                      models.LessonBlock._meta.get_field('media'),
                      models.LessonResource._meta.get_field('file'),
                      ContentUploadFile._meta.get_field('file'),
                      am.Question._meta.get_field('media')):
            self.assertIsInstance(field.upload_to, UnguessableUploadTo, field)
