"""The upload format, its samples, and the upload → draft → approve → publish queue."""

import json
import os
import shutil
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core import content_pack
from core.content_pack import authoring, upload_format

SAMPLES = os.path.join(settings.BASE_DIR, 'docs', 'upload_format')
FILES_DIR = tempfile.mkdtemp()
STORAGES = dict(settings.STORAGES, files={
    'BACKEND': 'django.core.files.storage.FileSystemStorage',
    'OPTIONS': {'location': FILES_DIR, 'base_url': '/files/'}})


def _sample(kind, ext):
    """``(filename, text)`` of the first shipped sample of ``kind`` ending in ``ext``."""
    folder = os.path.join(SAMPLES, kind)
    name = next(n for n in sorted(os.listdir(folder)) if n.endswith(ext))
    return name, open(os.path.join(folder, name), encoding='utf-8').read()


def _sidecars():
    """Every shipped sample sidecar, parsed, in folder order."""
    found = []
    for kind in sorted(os.listdir(SAMPLES)):
        folder = os.path.join(SAMPLES, kind)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith('.json'):
                with open(os.path.join(folder, name), encoding='utf-8') as fh:
                    found.append(json.load(fh))
    return found


def _spine_for_samples():
    """Create the institution, programme, module and intake each shipped sample is placed in.

    The rows come from the samples' own sidecars, so the tests follow whatever
    placement the samples name instead of depending on the seeded spine.
    """
    from apps.learning.models import Cohort
    from core.testing import make_module, make_programme

    for meta in _sidecars():
        programme = make_programme(meta['institution'], meta['programme'])
        if meta.get('module'):
            make_module(meta['module'], meta['module'], programme=programme, price=500)
        if meta.get('cohort'):
            Cohort.objects.get_or_create(programme=programme, code=meta['cohort'])


def _placement(kind):
    """``(institution, programme, module)`` codes the first ``kind`` sample is placed in."""
    meta = json.loads(_sample(kind, '.json')[1])
    return meta['institution'], meta['programme'], meta['module']


@override_settings(STORAGES=STORAGES)
class UploadFormatTests(TestCase):
    """The ``metanoia-upload/1`` sidecar + template format and its shipped samples."""
    @classmethod
    def setUpTestData(cls):
        """Create the programmes, modules and intakes the samples are placed in."""
        _spine_for_samples()

    @classmethod
    def tearDownClass(cls):
        """Remove the temporary file storage."""
        shutil.rmtree(FILES_DIR, ignore_errors=True)
        super().tearDownClass()

    def _pack_for(self, kind):
        """Validate, place and convert the ``kind`` sample; return its content pack."""
        _n, raw = _sample(kind, '.json')
        meta, problems = upload_format.normalise_sidecar(json.loads(raw))
        self.assertEqual(problems, [], kind)
        offering, cohort, placement = upload_format.resolve_placement(meta)
        self.assertIsNotNone(offering, kind)
        _n, text = _sample(kind, '.md')
        self.assertTrue(upload_format.looks_like_template(meta['doc_type'], text), kind)
        pack, problems = upload_format.pack_from_template(text, meta, offering)
        self.assertEqual(problems + content_pack.validate(pack), [], kind)
        return pack

    def test_every_sample_sidecar_is_valid_and_places(self):
        for kind in os.listdir(SAMPLES):
            if not os.path.isdir(os.path.join(SAMPLES, kind)):
                continue
            _n, raw = _sample(kind, '.json')
            meta, problems = upload_format.normalise_sidecar(json.loads(raw))
            self.assertEqual(problems, [], kind)
            self.assertEqual(meta['doc_type'], kind)
            offering, _cohort, _p = upload_format.resolve_placement(meta)
            self.assertIsNotNone(offering, kind)

    def test_study_guide_and_blueprint_samples_convert(self):
        guide = self._pack_for('study_guide')
        item = guide['items'][0]
        self.assertEqual(item['type'], 'study_guide')
        self.assertGreaterEqual(len(item['sections']), 4)
        self.assertTrue(any(s.get('video_url') for s in item['sections']))
        self.assertEqual(guide['topic']['code'], 'T1-W1')
        self.assertEqual(self._pack_for('blueprint')['items'][0]['type'], 'blueprint')

    def test_questions_sample_mixes_rubric_and_mcq(self):
        pack = self._pack_for('questions')
        item = pack['items'][0]
        self.assertEqual(item['total_marks'], 13)
        a, b, c = item['parts']
        self.assertEqual(len(a['rubric']), 4)
        self.assertEqual(c['marking'], 'auto')
        self.assertEqual([ch['correct'] for ch in c['choices']], [False, False, True, False])

    def test_mock_sample_has_a_computation(self):
        item = self._pack_for('mock_exam')['items'][0]
        self.assertEqual(item['type'], 'exam')
        self.assertEqual(item['parts'][0]['marking'], 'auto')
        self.assertEqual(item['parts'][0]['lines'][-1]['amount'], 16.53)
        self.assertEqual(item['minutes'], 36)

    def test_imported_quiz_creates_choices_and_publishes(self):
        from apps.assessments.models import Assessment, Choice
        pack = self._pack_for('questions')
        result = content_pack.import_bundle(pack)
        paper = Assessment.objects.get(pk=result.assessment_ids[0])
        self.assertEqual(paper.status, 'draft')
        self.assertEqual(Choice.objects.filter(question__section__assessment=paper, is_correct=True).count(), 1)
        self.assertEqual(content_pack.publish_imported(result.as_dict()), [])
        paper.refresh_from_db()
        self.assertEqual(paper.status, 'open')

    def test_master_topic_codes_do_not_collide_across_tests(self):
        self.assertNotEqual(authoring.topic_code('test', 1, 1), authoring.topic_code('test', 2, 1))


@override_settings(STORAGES=STORAGES)
class QueueTests(TestCase):
    """The upload queue: upload, review, approve and publish, staff only."""
    @classmethod
    def setUpTestData(cls):
        """Create the samples' programmes, modules and intakes, and an administrator."""
        _spine_for_samples()
        User = get_user_model()
        cls.admin = User.objects.create_superuser('queue-admin@example.com', 'queue-admin@example.com', 'x')

    def setUp(self):
        """Sign in as the administrator."""
        self.client.force_login(self.admin)

    def _upload(self, kind, *, extra=None):
        """Post the ``kind`` sample's files (plus ``extra``) to the upload screen."""
        files = []
        for ext in ('.json', '.md'):
            folder = os.path.join(SAMPLES, kind)
            for name in sorted(os.listdir(folder)):
                if name.endswith(ext):
                    files.append(SimpleUploadedFile(name, open(os.path.join(folder, name), 'rb').read()))
        files += extra or []
        return self.client.post(reverse('learning:content-upload'), {'files': files, 'auto_draft': '1'})

    def test_upload_review_approve_publish(self):
        """An upload drafts, approves (as drafts) and publishes.

        The accompanying PDF is placed beside the lesson, in the same week.
        """
        from apps.learning.models import Lesson, ModuleMaterial
        from apps.learning.upload_models import ContentUpload
        pdf = SimpleUploadedFile('CGAU_B26S_T1_W1_Controls_StudyGuide_v1.pdf', b'%PDF-1.4 sample')
        response = self._upload('study_guide', extra=[pdf])
        item = ContentUpload.objects.get()
        self.assertRedirects(response, reverse('learning:content-review', args=[item.public_id]))
        self.assertEqual(item.status, ContentUpload.STATUS_DRAFTED, item.problems)
        self.assertEqual(item.draft_method, 'template')
        self.assertEqual(item.files.count(), 3)

        page = self.client.get(reverse('learning:content-review', args=[item.public_id]))
        self.assertContains(page, 'The four words students confuse')

        self.client.post(reverse('learning:content-action', args=[item.public_id]), {'action': 'approve'})
        item.refresh_from_db()
        self.assertEqual(item.status, ContentUpload.STATUS_APPROVED)
        lesson = Lesson.objects.get(pk=item.result['lessons'][0])
        self.assertEqual(lesson.status, 'draft')
        self.assertFalse(ModuleMaterial.objects.filter(pk__in=item.result['materials'], is_published=True).exists())
        self.assertTrue(ModuleMaterial.objects.filter(pk__in=item.result['materials'], file__endswith='.pdf').exists())

        self.client.post(reverse('learning:content-action', args=[item.public_id]), {'action': 'publish'})
        item.refresh_from_db()
        lesson.refresh_from_db()
        self.assertEqual(item.status, ContentUpload.STATUS_PUBLISHED)
        self.assertEqual(lesson.status, 'published')
        self.assertFalse(ModuleMaterial.objects.filter(pk__in=item.result['materials'], is_published=False).exists())
        material = ModuleMaterial.objects.filter(lesson=lesson).get()
        self.assertEqual((material.week.number, material.phase.cohort.code), (1, 'B26S'))

    def test_staff_only_upload_never_publishes(self):
        from apps.learning.models import ModuleMaterial
        from apps.learning.upload_models import ContentUpload
        self._upload('blueprint')
        item = ContentUpload.objects.get()
        self.client.post(reverse('learning:content-action', args=[item.public_id]),
                         {'action': 'place', 'offering': item.programme_module_id, 'cohort': item.cohort_id,
                          'doc_type': 'blueprint', 'phase_kind': 'test', 'phase_sequence': 2,
                          'audience': 'staff'})
        item.refresh_from_db()
        item.pack['audience'] = 'staff'
        item.save()
        self.client.post(reverse('learning:content-action', args=[item.public_id]), {'action': 'approve_publish'})
        item.refresh_from_db()
        self.assertEqual(item.status, ContentUpload.STATUS_PUBLISHED)
        self.assertFalse(ModuleMaterial.objects.filter(pk__in=item.result['materials'], is_published=True).exists())

    def test_prompt_creates_a_reviewable_bundle(self):
        """A prompt drafts a bundle whose placement is pinned by the form, not the model."""
        from apps.learning.models import ModuleWeek, ProgrammeModule
        from apps.learning.upload_models import ContentUpload
        institution, programme, module = _placement('blueprint')
        offering = ProgrammeModule.objects.get(programme__institution__code=institution,
                                               programme__code=programme, code=module)
        reply = {'pack': 'metanoia-bundle/1', 'packs': [
            {'pack': 'x', 'module': 'WRONG', 'programme': 'WRONG', 'week': 1, 'week_title': 'Week 1 · s45',
             'topic': {'code': 'zz', 'title': 'Companies Act'},
             'items': [{'type': 'blueprint', 'title': 'Test 2 blueprint',
                        'sections': [{'heading': 'What is tested', 'body_html': '<p>s45, s46, s75</p>'}]},
                       {'type': 'quiz', 'title': 'Week 1 self-check', 'total_marks': 1,
                        'parts': [{'ref': '1', 'required': 'Which section governs loans to directors?',
                                   'marks': 1, 'marking': 'auto',
                                   'choices': [{'text': 's45', 'correct': True},
                                               {'text': 's46', 'correct': False}]}]}]},
            {'pack': 'x', 'week': 2, 'week_title': 'Week 2 · King V', 'topic': {'title': 'King V'},
             'items': [{'type': 'study_guide', 'title': 'King V', 'sections': [
                 {'heading': 'Principle 5', 'body_html': '<p>Composition.</p>'}]}]}]}
        with mock.patch('core.content_pack.authoring.is_enabled', return_value=True), \
                mock.patch('apps.ai_assistant.generate.generate_from_prompt', return_value=reply):
            response = self.client.post(reverse('learning:content-create'), {
                'prompt': 'Write the Test 2 blueprint and two weeks', 'offering': offering.pk,
                'phase_kind': 'test', 'phase_sequence': 2, 'week_from': 1, 'week_to': 2,
                'wants': ['blueprint', 'study_guide', 'quiz']})
        item = ContentUpload.objects.get()
        self.assertRedirects(response, reverse('learning:content-review', args=[item.public_id]))
        self.assertEqual(item.problems, [])
        packs = item.pack['packs']
        self.assertEqual({p['module'] for p in packs}, {'CGAU'})
        self.assertEqual([p['topic']['code'] for p in packs], ['T2-W1', 'T2-W2'])

        self.client.post(reverse('learning:content-action', args=[item.public_id]), {'action': 'approve_publish'})
        item.refresh_from_db()
        self.assertEqual(item.status, ContentUpload.STATUS_PUBLISHED)
        weeks = ModuleWeek.objects.filter(pk__in=item.result['weeks'])
        self.assertEqual(sorted(w.number for w in weeks), [1, 2])
        self.assertTrue(all(w.is_published for w in weeks))

    def test_samples_download_and_format_page(self):
        self.assertContains(self.client.get(reverse('learning:content-format')), 'metanoia-upload/1')
        response = self.client.get(reverse('learning:content-samples'))
        self.assertEqual(response['Content-Type'], 'application/zip')

    def test_non_staff_are_refused(self):
        User = get_user_model()
        student = User.objects.create_user('queue-student@example.com', 'queue-student@example.com', 'x')
        self.client.force_login(student)
        for name in ('content-queue', 'content-upload', 'content-create'):
            self.assertIn(self.client.get(reverse(f'learning:{name}')).status_code, (302, 403))


@override_settings(STORAGES=STORAGES, CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class AnalyticsTests(TestCase):
    """A student sits the sample quiz; the coach ticks points; everyone reads the result."""

    def _user(self, email, role):
        """Create an onboarded user with ``role`` as their ``user_type``."""
        from apps.accounts.models import Person
        user = get_user_model().objects.create_user(username=email, email=email, password='x',
                                                    first_name=role.title(), last_name='User')
        person = Person.objects.get(user=user)
        person.user_type, person.registered, person.profile_status = role, True, True
        person.save()
        return user

    def setUp(self):
        """Import and publish the sample quiz and enrol a student on its module."""
        from apps.assessments.models import Assessment
        from core.testing import enrol
        _spine_for_samples()
        _n, raw = _sample('questions', '.json')
        meta, _ = upload_format.normalise_sidecar(json.loads(raw))
        offering, _c, _p = upload_format.resolve_placement(meta)
        pack, _ = upload_format.pack_from_template(_sample('questions', '.md')[1], meta, offering)
        result = content_pack.import_bundle(pack)
        content_pack.publish_imported(result.as_dict())
        self.paper = Assessment.objects.get(pk=result.assessment_ids[0])
        self.offering = offering
        self.student = self._user('an-stu@example.com', 'student')
        enrol(self.student.profile, offering)
        self.coach = get_user_model().objects.create_superuser('an-coach@example.com', 'an-coach@example.com', 'x')

    def test_submit_mark_by_points_and_read_the_analytics(self):
        """Submit, mark by rubric points, then read the analytics and the result.

        The coach's page counts the wrong MCQ choice (Advocacy); the student's
        result lists the biggest loss first (5 of 7); students never see the coach's
        analytics page.
        """
        from apps.assessments.models import AssessmentAttempt
        questions = list(self.paper.sections.first().questions.order_by('order'))
        wrong = questions[2].choices.get(text='Advocacy')
        self.client.force_login(self.student)
        self.client.get(reverse('assessments:take', args=[self.paper.pk]))
        attempt = AssessmentAttempt.objects.get(assessment=self.paper, student=self.student)
        self.client.post(reverse('assessments:submit', args=[attempt.public_id]), {
            f'q_{questions[0].pk}': 'The partner holds shares — a self-interest threat.',
            f'q_{questions[1].pk}': 'Report to the IRBA.',
            f'q_{questions[2].pk}': str(wrong.pk)})

        self.client.force_login(self.coach)
        attempt.refresh_from_db()
        answers = {a.question_id: a for a in attempt.answers.all()}
        self.client.post(reverse('assessments:mark-attempt', args=[attempt.public_id]), {
            f'marks_{answers[questions[0].pk].pk}': '2', f'points_{answers[questions[0].pk].pk}': ['0'],
            f'marks_{answers[questions[1].pk].pk}': '1', f'points_{answers[questions[1].pk].pk}': ['2']})
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, 'marked')
        self.assertEqual(float(attempt.score), 3.0)

        page = self.client.get(reverse('assessments:analytics', args=[self.paper.pk]))
        self.assertContains(page, 'Question by question')
        self.assertEqual(page.context['questions'][0]['points'][0]['share'], 100.0)
        self.assertEqual(page.context['questions'][2]['choices'][1]['count'], 1)
        self.assertContains(self.client.get(reverse('learning:course-analytics', args=[self.offering.pk])),
                            'Course report')

        self.client.force_login(self.student)
        result = self.client.get(reverse('assessments:result', args=[attempt.public_id]))
        self.assertContains(result, 'How you could have scored more')
        best = result.context['best']
        self.assertEqual(best['lost'], 10.0)
        self.assertEqual(best['rows'][0]['question'], questions[0])
        self.assertEqual(len(best['rows'][0]['missed_points']), 3)
        mcq = next(r for r in best['rows'] if r['question'] == questions[2])
        self.assertEqual(mcq['correct_choice'], ['Intimidation'])
        self.assertEqual(self.client.get(reverse('assessments:analytics', args=[self.paper.pk])).status_code, 403)
