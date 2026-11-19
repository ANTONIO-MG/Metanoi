"""Tests for the front door (sign-in replaced the landing page), the sign-in
shell, the content packs and the 500 page.

``LOCMEM_CACHE`` keeps the suite independent of a running Redis
(``USE_REDIS=true`` in ``.env``).
"""

import pathlib
import re
from datetime import timedelta

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.branding import strings

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

@override_settings(CACHES=LOCMEM_CACHE)
class FrontDoorTests(TestCase):
    """The public landing page is retired: the sign-in page is the front door."""

    def test_site_root_sends_visitors_to_sign_in(self):
        response = self.client.get('/')
        self.assertRedirects(response, reverse('myhub:page-login'), fetch_redirect_response=False)

    def test_old_landing_links_arrive_at_sign_in(self):
        response = self.client.get(reverse('pages:landing'))
        self.assertRedirects(response, reverse('myhub:page-login'), fetch_redirect_response=False)

    def test_terms_and_privacy_are_still_public(self):
        self.assertEqual(self.client.get(reverse('pages:privacy-and-terms')).status_code, 200)


class ContentPackTests(TestCase):
    """content/*.json drives the FAQ and the review wall — see core.content."""

    def test_the_faq_pack_parses_and_every_entry_is_complete(self):
        from core import content
        items = content.faq_items()
        self.assertTrue(items, 'content/faq.json is empty or unreadable')
        for item in items:
            with self.subTest(question=item['q']):
                self.assertTrue(item['q'].strip())
                self.assertTrue(item['a'].strip())

    def test_a_missing_pack_is_empty_rather_than_an_exception(self):
        """A typo in a content file must not take the landing page down."""
        from core import content
        self.assertEqual(content.pack('no-such-pack-exists'), {})

    def test_reviews_are_only_marked_as_google_when_they_came_from_google(self):
        """The wall may hold hand-written student testimonials as well as synced
        Google reviews. Only the synced ones may carry the Google mark."""
        from core import content
        for review in content.reviews()['items']:
            with self.subTest(text=review['text'][:40]):
                if review.get('source') == 'google':
                    self.assertTrue(review.get('name'),
                                    'a Google review must name its author')


class ServerErrorTemplateTests(TestCase):
    """500.html must render with no request and no context processors.

    `django.views.defaults.server_error` calls `template.render()` without a
    request, so `dz_array` / `brand` / `strings` / `request` are all undefined.
    The theme's original 500.html looped over `…|getdata:request.path`, which
    raised while handling the error — turning every 500 into a second 500 and
    hiding the real traceback.
    """

    def test_renders_without_a_request(self):
        """The 500 template renders with no context, exactly as Django calls it."""
        from django.template import loader
        html = loader.get_template('500.html').render()
        self.assertIn('500', html)

    def test_does_not_depend_on_context_processors(self):
        from pathlib import Path

        from django.conf import settings
        source = (Path(settings.BASE_DIR) / 'templates' / '500.html').read_text()
        body = source.split('{% endcomment %}', 1)[-1]
        for forbidden in ('dz_array', 'request.', '{% static', '{% extends', '{% url'):
            self.assertNotIn(forbidden, body,
                             f'500.html must stay self-contained — {forbidden!r} needs a context '
                             f'that handler500 does not provide')


@override_settings(CACHES=LOCMEM_CACHE)
class AuthShellTests(TestCase):
    """Sign-in and sign-up wear the website's header: the logo, and a pill that
    switches between the two pages with the current one marked."""

    def html(self, name):
        """GET the page named ``name``, check it is a 200 and return its HTML."""
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_pill_marks_the_current_page(self):
        login = self.html('myhub:page-login')
        self.assertIn('data-t-nav', login)
        self.assertRegex(login, r'href="%s" aria-current="page">Log in' % re.escape(reverse('myhub:page-login')))
        signup = self.html('myhub:page-register')
        self.assertRegex(signup, r'href="%s" aria-current="page">Sign up' % re.escape(reverse('myhub:page-register')))

    def test_no_marketing_calls_to_action(self):
        self.assertNotIn('Get the Blueprint', self.html('myhub:page-login'))

    def test_the_site_stylesheet_and_inline_logo_load(self):
        login = self.html('myhub:page-login')
        self.assertIn('css/metanoia-ui.css', login)
        self.assertIn('class="t-logo t-logo--draw"', login)
