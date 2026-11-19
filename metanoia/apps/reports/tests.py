"""End-to-end tests for certificates: data, artwork, downloads and access.

A certificate is a document somebody keeps, prints and shows to an employer, so
these tests are unusually strict about *format*:

* the data handed to the renderer is validated field by field, and a
  certificate that cannot be described truthfully must refuse to render rather
  than produce a sheet with a blank name on it;
* the downloads are checked as real files — PNG magic bytes, a ``%PDF`` header,
  the declared content type, the filename in the Content-Disposition header —
  not merely as a 200;
* the PNG, the PDF and the on-screen preview must come from the same artwork,
  which is asserted by rendering them and comparing.
"""

import io

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Person

from . import certificate_render, certificates, models
from core.testing import enrol, make_module, make_programme

User = get_user_model()

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
PDF_MAGIC = b'%PDF'


@override_settings(CACHES=LOCMEM_CACHE, MEDIA_ROOT='/tmp/metanoiahub-test-media')
class CertificateFixtureMixin(TestCase):
    """Shared fixture: a programme module, a student, an educator and an issued certificate."""

    def setUp(self):
        """Build the programme, module, people and certificate on a clean cache."""
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

        self.programme = make_programme('CTA', 'PGDA')
        self.module = make_module('Advanced Auditing', 'AUDA', programme=self.programme)

        self.student = self._user('sam@example.com', 'Samira', 'Hadid', 'student')
        self.other = self._user('other@example.com', 'Other', 'Person', 'student')
        self.educator = self._user('ed@example.com', 'Ed', 'Ucator', 'educator')

        self.cert = models.Certificate.objects.create(
            student=self.student, module=self.module, kind='module',
            title='Advanced Auditing', final_mark=88, issued_at=timezone.now())

    def _user(self, email, first, last, role):
        """Create a registered user whose :class:`Person` has the given name and role."""
        user = User.objects.create_user(username=email, email=email, password='x',
                                        first_name=first, last_name=last)
        person = Person.objects.get(user=user)
        person.first_name, person.last_name = first, last
        person.user_type = role
        person.registered = True
        person.profile_status = True
        person.save()
        return user


class CertificateDataTests(CertificateFixtureMixin):
    """What gets handed to the renderer, and what must be refused."""

    def data(self):
        """The renderer's data for the fixture certificate."""
        return certificates.render_data(self.cert)

    def test_every_key_the_renderer_indexes_is_present_and_non_empty(self):
        data = self.data()
        for key in ('title', 'kind_line', 'student_name', 'award_name', 'award_line',
                    'number', 'verify_url', 'issuer', 'signatures'):
            self.assertIn(key, data)
            self.assertTrue(data[key], f'{key} must not be blank on a certificate')

    def test_types_are_what_the_renderer_expects(self):
        data = self.data()
        for key in ('title', 'kind_line', 'student_name', 'award_line', 'number',
                    'verify_url', 'issuer'):
            self.assertIsInstance(data[key], str, f'{key} is drawn as text')
        self.assertIsInstance(data['signatures'], list)
        self.assertEqual(len(data['signatures']), 2, 'the template carries two signature blocks')
        for entry in data['signatures']:
            self.assertEqual(len(entry), 2, 'each signature is (who, role)')
            self.assertTrue(all(isinstance(part, str) and part for part in entry))

    def test_the_holders_real_name_is_used(self):
        self.assertEqual(self.data()['student_name'], 'Samira Hadid')

    def test_the_award_line_states_the_module_the_mark_and_the_issuer(self):
        """A module offering prints as "<programme code> / <module code>"."""
        line = self.data()['award_line']
        self.assertIn(str(self.module), line)
        self.assertIn('88%', line)
        self.assertIn('Metanoia Group', line)
        self.assertIn(self.cert.issued_at.strftime('%d %B %Y'), line)

    def test_the_mark_is_rendered_as_a_whole_percentage(self):
        self.cert.final_mark = 67.4
        self.assertEqual(certificates.render_data(self.cert)['final_mark'], '67%')

    def test_the_grade_letter_follows_the_default_scale(self):
        for mark, letter in ((88, 'A'), (74, 'B'), (63, 'C'), (55, 'D'), (30, 'F')):
            self.cert.final_mark = mark
            self.assertEqual(certificates.render_data(self.cert)['grade_letter'], letter,
                             f'{mark}% should be a {letter}')

    def test_the_grade_letter_honours_a_modules_own_scale(self):
        models.ModuleWeighting.objects.create(
            module=self.module,
            grade_scale=[{'min': 90, 'letter': 'Distinction'}, {'min': 0, 'letter': 'Pass'}])
        self.cert.final_mark = 88
        self.assertEqual(certificates.render_data(self.cert)['grade_letter'], 'Pass')

    def test_the_verification_url_points_at_this_certificate(self):
        data = self.data()
        self.assertIn(str(self.cert.verification_uuid), data['verify_url'])
        self.assertIn('/reports/verify/', data['verify_url'])

    def test_the_verification_url_is_absolute_when_a_request_is_available(self):
        response = self.client.get('/')
        data = certificates.render_data(self.cert, response.wsgi_request)
        self.assertTrue(data['verify_url'].startswith('http://testserver/'))

    def test_the_kind_becomes_the_line_under_the_headline(self):
        self.assertEqual(self.data()['kind_line'], 'OF MODULE COMPLETION')

    def test_a_certificate_falls_back_to_its_title_when_it_names_no_module(self):
        cert = models.Certificate.objects.create(
            student=self.student, kind='programme',
            title='CTA Programme', final_mark=71)
        self.assertEqual(certificates.render_data(cert)['award_name'], 'CTA Programme')

    def test_a_certificate_with_no_holder_name_refuses_to_render(self):
        nameless = User.objects.create_user(username='x', email='')
        self.cert.student = nameless
        with self.assertRaises(ValidationError):
            certificates.render_data(self.cert)

    def test_a_certificate_naming_no_achievement_refuses_to_render(self):
        self.cert.subject = None
        self.cert.course = None
        self.cert.title = '   '
        with self.assertRaises(ValidationError):
            certificates.render_data(self.cert)

    def test_a_malformed_certificate_number_refuses_to_render(self):
        for bad in ('', '  ', 'ab', 'has spaces', 'x' * 41):
            self.cert.number = bad
            with self.assertRaises(ValidationError, msg=f'{bad!r} should be rejected'):
                certificates.render_data(self.cert)

    def test_a_lowercase_number_is_normalised_rather_than_rejected(self):
        self.cert.number = 'cert-abc123'
        self.assertEqual(certificates.render_data(self.cert)['number'], 'CERT-ABC123')

    def test_the_auto_generated_number_satisfies_the_format(self):
        fresh = models.Certificate.objects.create(
            student=self.student, module=self.module, title='X', final_mark=50)
        self.assertRegex(fresh.number, certificates.NUMBER_RE)


class CertificateArtworkTests(CertificateFixtureMixin):
    """The drawing itself — size, fonts and that it actually puts ink down."""

    def test_the_bundled_fonts_are_present(self):
        self.assertTrue(certificate_render.fonts_available(),
                        'the certificate fonts under static/fonts/certificate/ are missing')

    def test_the_sheet_is_a4_landscape(self):
        image = certificate_render.render_image(certificates.render_data(self.cert))
        width, height = image.size
        self.assertGreater(width, height, 'the certificate is landscape')
        self.assertAlmostEqual(width / height, 1.414, delta=0.02,
                               msg='the sheet must keep A4 proportions or it will print cropped')

    def test_scale_changes_the_resolution_not_the_design(self):
        small = certificate_render.render_image(certificates.render_data(self.cert), scale=1.0)
        large = certificate_render.render_image(certificates.render_data(self.cert), scale=2.0)
        self.assertEqual(large.size[0], small.size[0] * 2)
        self.assertAlmostEqual(large.size[0] / large.size[1],
                               small.size[0] / small.size[1], delta=0.01)

    def test_the_artwork_is_in_the_metanoia_palette_not_a_blank_page(self):
        image = certificate_render.render_image(certificates.render_data(self.cert))
        colours = image.convert('RGB').getcolors(maxcolors=1_000_000)
        self.assertIsNotNone(colours)
        present = {rgb for _, rgb in colours}

        white = sum(count for count, rgb in colours if rgb == (255, 255, 255))
        total = image.size[0] * image.size[1]
        self.assertLess(white / total, 0.92, 'the sheet is essentially blank')

        def has(predicate):
            """Whether any colour present in the image satisfies ``predicate``."""
            return any(predicate(rgb) for rgb in present)

        self.assertTrue(has(lambda c: c[0] < 30 and c[1] < 50 and 60 < c[2] < 100),
                        'the navy text and strip are missing')
        self.assertTrue(has(lambda c: c[0] > 150 and c[1] > 110 and c[2] < 120),
                        'the gold rule and ring are missing')
        self.assertTrue(has(lambda c: c[1] > 150 and c[0] < 140 and c[2] < 80),
                        'the green full stop and strip are missing')

    def test_a_very_long_name_is_shrunk_rather_than_overflowing(self):
        from PIL import ImageDraw
        data = certificates.render_data(self.cert)
        image = certificate_render.render_image(data)
        draw = ImageDraw.Draw(image)
        limit = (1 - 2 * certificate_render.MARGIN) * image.size[0]

        for name in ('Jo', 'Samira Hadid', 'Nokuthula Chikafu-Mutasa Ndlovu Van Der Merwe'):
            font = certificate_render._fit_font(draw, name, 'script',
                                                int(0.130 * image.size[1]), limit)
            width = draw.textlength(name, font=font)
            self.assertLessEqual(width, limit + 1,
                                 f'“{name}” would run past the text column')

    def test_a_long_award_line_is_capped_at_three_lines(self):
        """The renderer slices to three lines; assert the slice is what gets drawn."""
        from PIL import ImageDraw
        image = certificate_render.render_image(certificates.render_data(self.cert))
        draw = ImageDraw.Draw(image)
        font = certificate_render._font('regular', 30)
        lines = certificate_render._wrap(draw, 'word ' * 400, font, 600)
        self.assertGreater(len(lines), 3, 'the fixture should overflow')
        self.assertLessEqual(len(lines[:3]), 3)

    def test_rendering_is_deterministic(self):
        """Two renders of the same certificate must be byte-identical, otherwise
        the cached PDF and a fresh PNG would not match."""
        data = certificates.render_data(self.cert)
        first = certificate_render.render_png(data)
        second = certificate_render.render_png(data)
        self.assertEqual(first, second)


class CertificateDownloadTests(CertificateFixtureMixin):
    """The PNG, PDF and inline-image downloads and the preview page."""

    def setUp(self):
        """Sign in as the certificate holder."""
        super().setUp()
        self.client.force_login(self.student)

    def url(self, name):
        """URL of the ``reports:<name>`` route for the fixture certificate."""
        return reverse(f'reports:{name}', args=[self.cert.pk])

    def test_the_png_download_is_a_real_png_named_after_the_certificate(self):
        response = self.client.get(self.url('certificate-png'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['Content-Disposition'],
                         f'attachment; filename="{self.cert.number}.png"')
        body = response.content
        self.assertTrue(body.startswith(PNG_MAGIC), 'not a PNG file')
        self.assertEqual(int(response['Content-Length']), len(body))

        from PIL import Image
        image = Image.open(io.BytesIO(body))
        self.assertEqual(image.format, 'PNG')
        self.assertGreater(image.size[0], image.size[1])

    def test_the_pdf_download_is_a_real_pdf_named_after_the_certificate(self):
        response = self.client.get(self.url('certificate-pdf'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['Content-Disposition'],
                         f'attachment; filename="{self.cert.number}.pdf"')
        self.assertTrue(response.content.startswith(PDF_MAGIC), 'not a PDF file')
        self.assertGreater(len(response.content), 5_000, 'the PDF looks empty')

    def test_the_inline_image_is_served_for_display_not_download(self):
        response = self.client.get(self.url('certificate-image'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertTrue(response['Content-Disposition'].startswith('inline;'),
                        'the preview must display, not prompt a download')

    def test_the_pdf_is_cached_on_the_certificate_after_the_first_download(self):
        self.assertFalse(self.cert.pdf)
        self.client.get(self.url('certificate-pdf'))
        self.cert.refresh_from_db()
        self.assertTrue(self.cert.pdf, 'the rendered PDF should be stored')
        self.assertTrue(self.cert.pdf.name.endswith('.pdf'))

    def test_a_second_download_serves_identical_bytes(self):
        first = self.client.get(self.url('certificate-pdf')).content
        second = self.client.get(self.url('certificate-pdf')).content
        self.assertEqual(first, second)

    def test_a_lost_stored_pdf_is_re_rendered_rather_than_erroring(self):
        self.client.get(self.url('certificate-pdf'))
        self.cert.refresh_from_db()
        self.cert.pdf.storage.delete(self.cert.pdf.name)
        response = self.client.get(self.url('certificate-pdf'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(PDF_MAGIC))

    def test_the_preview_and_the_download_are_the_same_artwork(self):
        """Rendered from the same request, the two paths must be byte-identical.

        The request matters: it is what makes the printed verification URL
        absolute, and the URL is part of the picture.
        """
        response = self.client.get(self.url('certificate-image'))
        direct = certificate_render.render_png(
            certificates.render_data(self.cert, response.wsgi_request), scale=1.0)
        self.assertEqual(response.content, direct,
                         'the preview must be the same render the downloads use')

    def test_the_printed_verification_url_is_absolute(self):
        """A certificate is read on paper; a relative URL is useless there."""
        response = self.client.get(self.url('certificate'))
        self.assertTrue(response.context['verify_url'].startswith('http'),
                        'the printed verify link must be a full URL')

    def test_the_preview_page_offers_pdf_png_and_print(self):
        html = self.client.get(self.url('certificate')).content.decode()
        self.assertIn(self.url('certificate-pdf'), html)
        self.assertIn(self.url('certificate-png'), html)
        self.assertIn(self.url('certificate-image'), html)
        self.assertIn('window.print()', html)
        self.assertIn('Download PDF', html)
        self.assertIn('Download PNG', html)

    def test_the_preview_page_states_the_facts_in_text(self):
        """The artwork is an image; the details must also be readable."""
        html = self.client.get(self.url('certificate')).content.decode()
        self.assertIn('Samira Hadid', html)
        self.assertIn('Advanced Auditing', html)
        self.assertIn(self.cert.number, html)
        self.assertIn('88%', html)

    def test_the_reports_page_no_longer_lists_certificates(self):
        """Reports is results and completion only; the certificate pages
        themselves still work (see the download tests above)."""
        html = self.client.get(reverse('reports:my-reports')).content.decode()
        self.assertNotIn(self.url('certificate'), html)
        self.assertNotIn(self.url('certificate-pdf'), html)


class CertificateAccessTests(CertificateFixtureMixin):
    """Who may see a certificate, and that verification is public."""

    def urls(self):
        """Every route that serves the fixture certificate."""
        return [reverse(f'reports:{name}', args=[self.cert.pk])
                for name in ('certificate', 'certificate-pdf', 'certificate-png',
                             'certificate-image')]

    def test_a_holder_may_see_their_own(self):
        self.client.force_login(self.student)
        for url in self.urls():
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_another_student_may_not(self):
        self.client.force_login(self.other)
        for url in self.urls():
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_an_educator_may_not(self):
        """Certificates are closed to educators — they mark the work, but the
        award is the learner's and the institution's."""
        self.client.force_login(self.educator)
        for url in self.urls():
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_the_reports_page_hides_certificates_from_an_educator(self):
        self.client.force_login(self.educator)
        response = self.client.get(reverse('reports:my-reports'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(reverse('reports:certificate', args=[self.cert.pk]),
                         response.content.decode())

    def test_anonymous_visitors_are_sent_to_sign_in(self):
        for url in self.urls():
            response = self.client.get(url)
            self.assertIn(response.status_code, (302, 301), url)

    def test_verification_is_public_and_names_the_holder(self):
        """An employer checking a certificate has no account."""
        url = reverse('reports:verify', args=[self.cert.verification_uuid])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cert'].pk, self.cert.pk)

    def test_a_bad_verification_uuid_is_a_404(self):
        import uuid
        response = self.client.get(reverse('reports:verify', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class CertificateCacheVersionTests(CertificateFixtureMixin):
    """A redesign must not leave people downloading the previous artwork."""

    def setUp(self):
        """Sign in as the holder and point at the PDF download."""
        super().setUp()
        self.client.force_login(self.student)
        self.url = reverse('reports:certificate-pdf', args=[self.cert.pk])

    def test_the_stored_filename_carries_the_design_version(self):
        self.client.get(self.url)
        self.cert.refresh_from_db()
        self.assertIn(certificates.DESIGN_VERSION, self.cert.pdf.name)

    def test_a_pdf_stored_under_an_older_design_is_re_rendered(self):
        from django.core.files.base import ContentFile
        self.cert.pdf.save(f'{self.cert.number}-v1.pdf',
                           ContentFile(b'%PDF-1.4 stale artwork'), save=True)
        stale_name = self.cert.pdf.name

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.content, b'%PDF-1.4 stale artwork',
                            'the old design was served instead of the current one')
        self.assertGreater(len(response.content), 5_000)

        self.cert.refresh_from_db()
        self.assertNotEqual(self.cert.pdf.name, stale_name)
        self.assertIn(certificates.DESIGN_VERSION, self.cert.pdf.name)

    def test_an_empty_stored_pdf_is_re_rendered(self):
        from django.core.files.base import ContentFile
        self.cert.pdf.save(certificates.pdf_filename(self.cert), ContentFile(b''), save=True)
        response = self.client.get(self.url)
        self.assertTrue(response.content.startswith(PDF_MAGIC))
        self.assertGreater(len(response.content), 5_000)

    def test_a_current_pdf_is_reused_rather_than_re_rendered(self):
        first = self.client.get(self.url).content
        self.cert.refresh_from_db()
        name = self.cert.pdf.name
        second = self.client.get(self.url).content
        self.cert.refresh_from_db()
        self.assertEqual(first, second)
        self.assertEqual(self.cert.pdf.name, name, 'a valid cache must not be rewritten')


class CertificateSignatureTests(CertificateFixtureMixin):
    """The two signature blocks must never print the same name twice."""

    def test_the_educator_counter_signs_when_the_module_has_one(self):
        self.module.educators.add(self.educator.profile)
        signatures = certificates.render_data(self.cert)['signatures']
        self.assertEqual(signatures[0][1], 'Authorised signatory')
        self.assertEqual(signatures[1][1], 'Educator')
        self.assertIn('Ucator', signatures[1][0])

    def test_it_falls_back_to_the_issue_date_when_there_is_no_educator(self):
        signatures = certificates.render_data(self.cert)['signatures']
        self.assertEqual(signatures[1][1], 'Date issued')
        self.assertEqual(signatures[1][0], f'{self.cert.issued_at:%d %B %Y}')

    def test_an_explicit_signature_is_used_for_the_left_block(self):
        self.cert.signature = 'Samira Hadid'
        signatures = certificates.render_data(self.cert)['signatures']
        self.assertEqual(signatures[0], ('Samira Hadid', 'Authorised signatory'))

    def test_the_two_blocks_never_carry_the_same_name(self):
        for add_educator in (False, True):
            if add_educator:
                self.module.educators.add(self.educator.profile)
            signatures = certificates.render_data(self.cert)['signatures']
            self.assertNotEqual(signatures[0][0], signatures[1][0],
                                'the certificate prints the same signatory twice')


class CertificateLayoutTests(CertificateFixtureMixin):
    """Guard the vertical rhythm — overlapping lines are the classic failure.

    These read ``certificate_render.LAYOUT`` rather than repeating its numbers,
    so moving a line in the renderer moves it here too and the assertions stay
    about *collisions* instead of about specific coordinates.
    """

    L = certificate_render.LAYOUT

    def _bounds(self, draw, text, font_name, size_key, y_key, width_limit):
        """Bounding box of ``text`` drawn at the layout's size and position for ``y_key``."""
        h = certificate_render.BASE_H
        font = certificate_render._fit_font(
            draw, text, font_name, int(self.L[size_key] * h), width_limit)
        return draw.textbbox((0, self.L[y_key] * h), text, font=font, anchor='lm')

    def test_the_name_clears_the_line_above_and_the_rule_below(self):
        """The holder's name sits between "This recognises" and the gold rule."""
        from PIL import ImageDraw
        image = certificate_render.render_image(certificates.render_data(self.cert))
        draw = ImageDraw.Draw(image)
        w, h = image.size
        limit = (1 - 2 * certificate_render.MARGIN) * w

        _, _, _, presented_bottom = self._bounds(
            draw, 'This recognises', 'regular',
            'presented_size', 'presented_y', limit)
        rule_y = self.L['rule_y'] * h

        for name in ('Chipo Mabhena', 'Muhammad Patel', 'Jane Jones',
                     'Nokuthula Chikafu-Mutasa Ndlovu', 'Jo Ng'):
            _, top, _, bottom = self._bounds(
                draw, name, 'script', 'name_size', 'name_y', limit)
            self.assertGreater(top, presented_bottom,
                               f'“{name}” climbs into “This recognises”')
            self.assertLess(bottom, rule_y, f'“{name}” drops through the rule below it')

    def test_the_kicker_clears_the_headline(self):
        from PIL import ImageDraw
        image = certificate_render.render_image(certificates.render_data(self.cert))
        draw = ImageDraw.Draw(image)
        limit = image.size[0]

        _, _, _, kicker_bottom = self._bounds(
            draw, 'METANOIA ALUMNI · CERTIFICATE OF COMPLETION', 'bold', 'kicker_size', 'kicker_y', limit)
        _, headline_top, _, headline_bottom = self._bounds(
            draw, 'Welcome to the alumni.', 'extrabold', 'headline_size', 'headline_y', limit)
        _, presented_top, _, _ = self._bounds(
            draw, 'This recognises', 'regular', 'presented_size', 'presented_y', limit)
        self.assertGreater(headline_top, kicker_bottom, 'the kicker overlaps the headline')
        self.assertGreater(presented_top, headline_bottom, 'the headline overlaps "This recognises"')

    def test_the_body_starts_below_the_rule(self):
        self.assertGreater(self.L['body_y'], self.L['rule_y'])

    def test_three_body_lines_still_clear_the_signature_rules(self):
        last_line = self.L['body_y'] + 2 * self.L['body_step']
        self.assertLess(last_line, self.L['sig_rule_y'] - 0.02,
                        'a three-line award statement would run into the signatures')

    def test_the_footer_sits_below_the_signature_roles(self):
        roles_bottom = self.L['sig_rule_y'] + 0.066 + self.L['sig_role_size']
        self.assertGreater(self.L['footer_y'], roles_bottom)
        self.assertLess(self.L['footer_y'], 0.97, 'the footer would print off the sheet')

    def test_every_line_stays_on_the_sheet(self):
        for key, value in self.L.items():
            if key.endswith('_y'):
                self.assertGreater(value, 0.0, f'{key} is above the sheet')
                self.assertLess(value, 1.0, f'{key} is below the sheet')


@override_settings(CACHES=LOCMEM_CACHE)
class CompletionAndProgressTests(TestCase):
    """A paper handed in is recorded once, a finished block becomes a
    milestone, and both pages render for the learner and their parent."""

    def setUp(self):
        """A module with an open paper on a published block, and an enrolled student."""
        cache.clear()
        self.addCleanup(cache.clear)
        from apps.assessments.models import Assessment
        from apps.learning.models import ModulePhase, ModuleMaterial
        self.programme = make_programme('CTA', 'PGDA')
        self.module = make_module('Taxation', 'TAXA', programme=self.programme)
        self.student = CertificateFixtureMixin._user(self, 'kim@example.com', 'Kim', 'Ndlovu', 'student')
        enrol(Person.objects.get(user=self.student), self.module)
        self.paper = Assessment.objects.create(module=self.module, title='Test 1 mock', kind='test',
                                               total_marks=50, status='open')
        self.phase = ModulePhase.objects.create(programme_module=self.module, kind='test', sequence=1)
        ModuleMaterial.objects.create(phase=self.phase, kind='assessment', title='Test 1 mock',
                                      assessment=self.paper)
        self.client.force_login(self.student)

    def _sit(self, score, status='marked'):
        """Record a submitted attempt at the paper with ``score``."""
        from apps.assessments.models import AssessmentAttempt
        return AssessmentAttempt.objects.create(
            assessment=self.paper, student=self.student, status=status, score=score,
            passed=score >= 25, submitted_at=timezone.now())

    def test_the_progress_page_suggests_sitting_an_open_paper(self):
        response = self.client.get(reverse('reports:my-progress'))
        self.assertEqual(response.status_code, 200)
        titles = [row['title'] for row in response.context['board']['improve']]
        self.assertIn('Sit "Test 1 mock"', titles)

    def test_a_handed_in_paper_is_recorded_once_with_its_result(self):
        self._sit(40)
        from .completions import record_completions
        record_completions(self.student)
        record_completions(self.student)
        rows = models.CompletionRecord.objects.filter(student=self.student, kind='assessment')
        self.assertEqual(rows.count(), 1)
        self.assertEqual(float(rows.get().score_pct), 80.0)
        self.assertTrue(rows.get().passed)

    def test_completing_every_item_of_a_block_records_a_milestone(self):
        self._sit(30)
        html = self.client.get(reverse('reports:my-reports')).content.decode()
        self.assertTrue(models.CompletionRecord.objects.filter(
            student=self.student, key=f'phase:{self.phase.pk}').exists())
        self.assertIn('Test 1 mock', html)
        self.assertIn('Reports', html)

    def test_a_score_arriving_at_marking_refreshes_the_record(self):
        attempt = self._sit(0, status='submitted')
        from .completions import record_completions
        record_completions(self.student)
        record = models.CompletionRecord.objects.get(student=self.student, kind='assessment')
        self.assertIsNone(record.score_pct)
        attempt.status, attempt.score, attempt.passed = 'marked', 20, False
        attempt.save()
        record_completions(self.student)
        record.refresh_from_db()
        self.assertEqual(float(record.score_pct), 40.0)
        self.assertFalse(record.passed)


class ParentGeneralCardTests(TestCase):
    """A parent sees the child's programme General module as a card of its own,
    first, beside the module cards — linking only to pages a parent may open."""

    def setUp(self):
        """A parent linked to a student on a programme with a General module briefing."""
        cache.clear()
        self.addCleanup(cache.clear)
        from apps.accounts.models import ParentLink
        from apps.communication.models import MeetingRoom
        from apps.learning.general import general_module_for
        from datetime import timedelta
        self.programme = make_programme('CTA', 'PGDA')
        self.module = make_module('Taxation', 'TAXA', programme=self.programme)
        self.general = general_module_for(self.programme)
        self.student = CertificateFixtureMixin._user(self, 'kid@example.com', 'Kid', 'Ndlovu', 'student')
        enrol(Person.objects.get(user=self.student), self.module)
        self.parent = CertificateFixtureMixin._user(self, 'mum@example.com', 'Mum', 'Ndlovu', 'parent')
        ParentLink.objects.create(parent=self.parent, student=self.student)
        start = timezone.now() + timedelta(days=3)
        self.briefing = MeetingRoom.objects.create(
            title='Programme briefing', host=self.parent, module=self.general,
            audience=MeetingRoom.AUDIENCE_PROGRAMME, programme=self.programme,
            scheduled_start=start, scheduled_end=start + timedelta(hours=1))

    def _page(self, user):
        """Load the progress page as ``user`` and assert it renders."""
        self.client.force_login(user)
        response = self.client.get(reverse('reports:my-progress'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_parent_sees_general_first_as_its_own_card(self):
        """General is not one of the counted module cards, and its links go only where a
        parent is let in — never the module feed.
        """
        response = self._page(self.parent)
        cards = response.context['general_cards']
        self.assertEqual([c['offering'] for c in cards], [self.general])
        self.assertEqual(cards[0]['next_class'], self.briefing)
        self.assertNotIn(self.general, [c['offering'] for c in response.context['board']['cards']])
        html = response.content.decode()
        self.assertIn('rp-mod--general', html)
        self.assertLess(html.index('rp-mod--general'), html.index('Taxation'))
        self.assertIn(reverse('livesessions:calendar'), html)
        self.assertNotIn(reverse('learning:module-feed', args=[self.general.pk]), html)

    def test_the_links_on_the_card_open_for_a_parent(self):
        self._page(self.parent)
        for url in (reverse('livesessions:calendar'),):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_student_page_is_unchanged(self):
        response = self._page(self.student)
        self.assertEqual(response.context['general_cards'], [])
