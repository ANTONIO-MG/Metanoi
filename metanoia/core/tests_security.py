"""Security regressions: stored XSS in lessons, open redirects, upload types,
and educators reading results for modules they do not teach.

Sections: (1) the HTML sanitiser and lesson-block XSS, (2) open redirects,
(3) upload types, (4) educators and other modules' results, plus stale
sign-in forms (CSRF).
"""

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.livesessions.tests import make_user
from core.sanitize import clean_html, safe_url, script_json
from core.testing import enrol, make_module


class CleanHtmlTests(TestCase):
    """The sanitiser (:mod:`core.sanitize`): what editor HTML keeps and what it loses."""
    def test_keeps_editor_formatting(self):
        html = ('<h2>Heading</h2><h3>Sub</h3><p style="text-align: center;">Centred '
                '<b>bold</b> <i>it</i> <u>u</u> <strike>s</strike></p>'
                '<font size="5" color="#ff0000">big red</font>'
                '<span style="background-color: rgb(255, 243, 205);">hi</span>'
                '<blockquote style="margin: 0 0 0 40px;">q</blockquote><pre>code</pre>'
                '<ul><li>one</li></ul><ol><li>two</li></ol>'
                '<table><thead><tr><th>A</th></tr></thead><tbody><tr><td colspan="2">1</td></tr></tbody></table>'
                '<img src="/media/x.png" alt="x"><a href="https://example.com">link</a>')
        out = clean_html(html)
        for fragment in ('<h2>Heading</h2>', '<h3>Sub</h3>', 'text-align:center', 'margin:0 0 0 40px',
                         '<b>bold</b>', '<i>it</i>', '<u>u</u>', '<strike>s</strike>',
                         '<font size="5" color="#ff0000">', 'background-color',
                         '<blockquote', '<pre>code</pre>', '<ul><li>one</li></ul>',
                         '<ol><li>two</li></ol>', '<th>A</th>', 'colspan="2"',
                         '<img src="/media/x.png" alt="x">'):
            self.assertIn(fragment, out)
        self.assertIn('href="https://example.com"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_strips_script_handlers_and_bad_urls(self):
        out = clean_html(
            '<p>ok</p><script>alert(1)</script><img src=x onerror="alert(1)">'
            '<a href="javascript:alert(1)">x</a><a href="JaVaScRiPt:alert(1)">y</a>'
            '<a href="data:text/html,<script>alert(1)</script>">z</a>'
            '<iframe src="https://evil.example/"></iframe><iframe srcdoc="<script>"></iframe>'
            '<svg onload="alert(1)"></svg><p style="background-image:url(javascript:alert(1))">s</p>'
            '<div onclick="x()">d</div><style>body{}</style><form action="/x"><input></form>')
        lowered = out.lower()
        for bad in ('<script', 'alert(1)', 'onerror', 'javascript:', 'data:text', 'evil.example',
                    'srcdoc', '<svg', 'onload', 'background-image', 'onclick', '<style',
                    '<form', '<input'):
            self.assertNotIn(bad, lowered)
        self.assertIn('<p>ok</p>', out)

    def test_allows_known_video_iframes_only(self):
        ok = clean_html('<iframe src="https://www.youtube-nocookie.com/embed/abc" allowfullscreen></iframe>')
        self.assertIn('youtube-nocookie.com/embed/abc', ok)
        self.assertNotIn('src', clean_html('<iframe src="http://www.youtube.com/embed/abc"></iframe>'))

    def test_safe_url(self):
        self.assertEqual(safe_url('https://a.b/c'), 'https://a.b/c')
        self.assertEqual(safe_url('/relative/path'), '/relative/path')
        self.assertEqual(safe_url('mailto:a@b.c'), 'mailto:a@b.c')
        self.assertEqual(safe_url('javascript:alert(1)'), '')
        self.assertEqual(safe_url(' java\tscript:alert(1)'), '')
        self.assertEqual(safe_url('data:text/html,x'), '')

    def test_script_json_cannot_close_the_script_tag(self):
        out = script_json({'html': '</script><script>alert(1)</script>'})
        self.assertNotIn('</script>', out)
        self.assertEqual(json.loads(out)['html'], '</script><script>alert(1)</script>')


class LessonBlockXssTests(TestCase):
    """Stored XSS in lesson blocks is cleaned on save, on render and in the editor."""
    def setUp(self):
        """A published lesson on a module taught by an educator, plus an admin."""
        from apps.learning.models import Lesson
        self.module = make_module('Sanitise')
        self.educator = make_user('xss-coach', 'educator')
        self.module.educators.add(self.educator.profile)
        self.admin = make_user('xss-admin', 'admin', is_staff=True)
        self.lesson = Lesson.objects.create(title='L', module=self.module,
                                            status=Lesson.STATUS_PUBLISHED)

    def test_block_save_cleans_html_and_urls(self):
        from apps.learning.models import LessonBlock
        block = LessonBlock.objects.create(lesson=self.lesson, block_type='text', data={'html': ''})
        self.client.force_login(self.educator)
        payload = {'data': {'html': '<p>Hi <b>there</b></p><img src=x onerror=alert(1)>'
                                    '<script>alert(2)</script>',
                            'url': 'javascript:alert(3)'}}
        response = self.client.post(reverse('learning:block-save', args=[block.pk]),
                                    data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        block.refresh_from_db()
        self.assertIn('<p>Hi <b>there</b></p>', block.data['html'])
        self.assertNotIn('onerror', block.data['html'])
        self.assertNotIn('script', block.data['html'])
        self.assertEqual(block.data['url'], '')

    def test_stored_payload_is_cleaned_on_render_and_in_editor(self):
        """A row written before save-time cleaning existed is still cleaned when shown."""
        from apps.learning.models import LessonBlock
        block = LessonBlock.objects.create(lesson=self.lesson, block_type='text', data={'html': ''})
        LessonBlock.objects.filter(pk=block.pk).update(
            data={'html': '<p>Old</p><img src=x onerror="alert(101)"></script><script>alert(102)</script>'})
        LessonBlock.objects.create(lesson=self.lesson, block_type='embed', data={'url': 'javascript:alert(4)'})
        self.client.force_login(self.admin)
        page = self.client.get(reverse('learning:lesson-view', args=[self.lesson.pk])).content.decode()
        self.assertIn('<p>Old</p>', page)
        self.assertNotIn('alert(101)', page)
        self.assertNotIn('alert(102)', page)
        self.assertNotIn('javascript:alert(4)', page)
        editor = self.client.get(reverse('learning:lesson-editor', args=[self.lesson.pk])).content.decode()
        self.assertNotIn('alert(101)', editor)
        self.assertNotIn('alert(102)', editor)
        self.assertNotIn('</script><script>', editor)


@override_settings(ALLOWED_HOSTS=['testserver'])
class OpenRedirectTests(TestCase):
    """``next`` and ``Referer`` can never send a user to another site."""
    def setUp(self):
        """An admin with a known password."""
        self.user = make_user('redir@example.com', 'admin', is_staff=True)
        self.user.email = 'redir@example.com'
        self.user.set_password('pw-123456!')
        self.user.save()

    def _login(self, nxt):
        """Post the sign-in form with ``?next=<nxt>``."""
        return self.client.post(reverse('myhub:page-login') + f'?next={nxt}',
                                {'email': 'redir@example.com', 'password': 'pw-123456!'})

    def test_login_next_to_another_site_is_ignored(self):
        for nxt in ('https://evil.example/', '//evil.example/', '/\\evil.example/'):
            response = self._login(nxt)
            self.assertEqual(response.status_code, 302, nxt)
            self.assertNotIn('evil', response['Location'], nxt)
            self.client.logout()

    def test_login_next_on_this_site_is_kept(self):
        response = self._login('/learning/')
        self.assertEqual(response['Location'], '/learning/')

    def test_language_switch_ignores_foreign_referer(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('accounts:set-language') + '?lang=en',
                                   HTTP_REFERER='https://evil.example/phish')
        self.assertNotIn('evil', response['Location'])
        response = self.client.get(reverse('accounts:set-language') + '?lang=en',
                                   HTTP_REFERER='http://testserver/learning/')
        self.assertEqual(response['Location'], 'http://testserver/learning/')

    def test_helper(self):
        from core.redirects import safe_target
        request = RequestFactory().get('/')
        self.assertEqual(safe_target(request, 'https://evil.example/', '/home/'), '/home/')
        self.assertEqual(safe_target(request, '/\\evil.example', '/home/'), '/home/')
        self.assertEqual(safe_target(request, '/ok/?a=1', '/home/'), '/ok/?a=1')


class LessonUploadTypeTests(TestCase):
    """Lesson attachments and block uploads accept only safe file types."""
    def setUp(self):
        """A lesson on a module the signed-in educator teaches."""
        from apps.learning.models import Lesson
        self.module = make_module('Uploads')
        self.educator = make_user('upl-coach', 'educator')
        self.module.educators.add(self.educator.profile)
        self.lesson = Lesson.objects.create(title='L', module=self.module)
        self.client.force_login(self.educator)

    def test_attachment_rejects_html_and_svg_script(self):
        for name, ctype in (('evil.html', 'text/html'), ('x.js', 'text/javascript'),
                            ('page.htm', 'text/html'), ('run.exe', 'application/octet-stream')):
            response = self.client.post(
                reverse('learning:attachment-add', args=[self.lesson.pk]),
                {'files': [SimpleUploadedFile(name, b'<script>alert(1)</script>', content_type=ctype)]})
            self.assertEqual(response.status_code, 400, name)
        self.assertFalse(self.lesson.resources.exists())

    def test_attachment_accepts_pdf(self):
        response = self.client.post(
            reverse('learning:attachment-add', args=[self.lesson.pk]),
            {'files': [SimpleUploadedFile('notes.pdf', b'%PDF-1.4', content_type='application/pdf')]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.lesson.resources.count(), 1)

    def test_block_upload_checks_type_per_block(self):
        from apps.learning.models import LessonBlock
        image = LessonBlock.objects.create(lesson=self.lesson, block_type='image', data={})
        bad = self.client.post(reverse('learning:block-upload', args=[image.pk]),
                               {'file': SimpleUploadedFile('x.html', b'<script>', content_type='text/html')})
        self.assertEqual(bad.status_code, 400)
        pdf_on_image = self.client.post(reverse('learning:block-upload', args=[image.pk]),
                                        {'file': SimpleUploadedFile('x.pdf', b'%PDF', content_type='application/pdf')})
        self.assertEqual(pdf_on_image.status_code, 400)
        image.refresh_from_db()
        self.assertFalse(image.media)
        file_block = LessonBlock.objects.create(lesson=self.lesson, block_type='file', data={})
        ok = self.client.post(reverse('learning:block-upload', args=[file_block.pk]),
                              {'file': SimpleUploadedFile('x.pdf', b'%PDF', content_type='application/pdf')})
        self.assertEqual(ok.status_code, 200)


class EducatorResultScopeTests(TestCase):
    """An educator sees results only for the modules they teach."""
    def setUp(self):
        """A student with marked attempts and grades in a taught and an untaught module."""
        from apps.assessments.models import Assessment, AssessmentAttempt
        from apps.reports.models import Grade
        self.mine = make_module('Mine')
        self.theirs = make_module('Theirs')
        self.educator = make_user('scope-coach', 'educator')
        self.mine.educators.add(self.educator.profile)
        self.admin = make_user('scope-admin', 'admin', is_staff=True)
        self.student = make_user('scope-student')
        enrol(self.student.profile, self.mine)
        enrol(self.student.profile, self.theirs)
        a_mine = Assessment.objects.create(title='Mine test', module=self.mine, total_marks=10)
        a_theirs = Assessment.objects.create(title='Their test', module=self.theirs, total_marks=10)
        self.att_mine = AssessmentAttempt.objects.create(assessment=a_mine, student=self.student,
                                                         status='marked', score=7)
        self.att_theirs = AssessmentAttempt.objects.create(assessment=a_theirs, student=self.student,
                                                           status='marked', score=3)
        Grade.objects.update_or_create(student=self.student, module=self.mine,
                                       defaults={'final_pct': 71, 'passed': True})
        Grade.objects.update_or_create(student=self.student, module=self.theirs,
                                       defaults={'final_pct': 33, 'passed': False})

    def test_attempt_result_by_url(self):
        self.client.force_login(self.educator)
        self.assertEqual(self.client.get(
            reverse('assessments:result', args=[self.att_mine.public_id])).status_code, 200)
        self.assertEqual(self.client.get(
            reverse('assessments:result', args=[self.att_theirs.public_id])).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(
            reverse('assessments:result', args=[self.att_theirs.public_id])).status_code, 200)

    def test_marking_page_by_url(self):
        self.client.force_login(self.educator)
        response = self.client.get(reverse('assessments:mark-attempt', args=[self.att_theirs.public_id]))
        self.assertIn(response.status_code, (302, 403, 404))

    def test_analytics_is_scoped(self):
        from apps.analytics import services
        self.client.force_login(self.educator)
        data = self.client.get(reverse('analytics:dashboard')).context['data']
        self.assertEqual([r['module'] for r in data['modules']], ['Mine'])
        rows = services.module_performance(modules=None)
        self.assertEqual({r['module'] for r in rows}, {'Mine', 'Theirs'})
        self.client.force_login(self.admin)
        data = self.client.get(reverse('analytics:dashboard')).context['data']
        self.assertEqual({r['module'] for r in data['modules']}, {'Mine', 'Theirs'})

    def test_student_progress_is_scoped(self):
        """The progress board (``reports.progress_board``) shows one card per module the
        viewer may see, and results only from those modules.
        """
        self.client.force_login(self.educator)
        response = self.client.get(reverse('reports:my-progress') + f'?student={self.student.pk}')
        self.assertEqual(response.status_code, 200)
        board = response.context['board']
        self.assertEqual(len(board['cards']), 1)
        self.assertEqual(board['results']['count'], 1)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('reports:my-progress') + f'?student={self.student.pk}')
        self.assertEqual(len(response.context['board']['cards']), 2)


class StaleAuthFormTests(TestCase):
    """A sign-in form whose CSRF token went stale is sent somewhere useful."""

    def setUp(self):
        """A client that enforces CSRF checks."""
        from django.test import Client
        self.client = Client(enforce_csrf_checks=True)

    def test_a_stale_sign_in_form_returns_to_a_fresh_one(self):
        """A stale token (after a GET set the CSRF cookie) redirects back to the form."""
        from django.urls import reverse
        url = reverse('myhub:page-login')
        self.client.get(url, secure=True)
        response = self.client.post(url, {'email': 'a@b.c', 'password': 'x',
                                          'csrfmiddlewaretoken': 'stale' * 8}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], url)

    def test_someone_already_signed_in_goes_on_through(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        user = get_user_model().objects.create_user(username='in@example.com', email='in@example.com',
                                                    password='x')
        self.client.force_login(user)
        response = self.client.post(reverse('myhub:page-login'),
                                    {'csrfmiddlewaretoken': 'stale' * 8, 'next': '/calendar/'}, secure=True)
        self.assertEqual((response.status_code, response['Location']), (302, '/calendar/'))

    def test_other_forms_keep_the_standard_refusal(self):
        self.client.force_login(make_user('csrf-other'))
        response = self.client.post('/communication/chat/', {}, secure=True)
        self.assertEqual(response.status_code, 403)
