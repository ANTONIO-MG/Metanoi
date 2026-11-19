"""The one-time "we've updated our Terms" prompt.

Due while the person's accepted terms version is older than TERMS_VERSION;
accepting stamps the profile so it does not come back for that version.
"""
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import Client, RequestFactory, TestCase, override_settings

from . import context_processors, views
from .forms import TERMS_VERSION

User = get_user_model()


class TermsUpdatePromptTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username='old@example.com', email='old@example.com', password='pw12345!x')
        self.profile = self.user.profile
        self.profile.terms_version = '2026-07'
        self.profile.save(update_fields=['terms_version'])

    def _request(self, method='get', path='/myhub/'):
        request = getattr(self.factory, method)(path)
        request.user = self.user
        SessionMiddleware(lambda r: None).process_request(request)
        return request

    def test_due_for_someone_on_an_older_version(self):
        ctx = context_processors.terms_update(self._request())
        self.assertTrue(ctx['terms_update_due'])

    def test_accepting_records_the_current_version_and_clears_the_prompt(self):
        response = views.accept_terms(self._request('post', '/community/terms/accept/'))
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.terms_version, TERMS_VERSION)
        self.assertIsNotNone(self.profile.terms_accepted_at)
        self.user.refresh_from_db()
        ctx = context_processors.terms_update(self._request())
        self.assertFalse(ctx['terms_update_due'])

    def test_never_due_when_signed_out(self):
        from django.contrib.auth.models import AnonymousUser
        request = self.factory.get('/')
        request.user = AnonymousUser()
        self.assertFalse(context_processors.terms_update(request)['terms_update_due'])


@override_settings(SECURE_SSL_REDIRECT=False)
class LegalLinksTests(TestCase):
    """The shareable links open for anyone, signed in or not."""

    def test_terms_and_conditions_link_lands_on_the_terms(self):
        response = Client().get('/social/terms-and-conditions/')
        self.assertRedirects(response, '/social/privacy-and-terms/#terms',
                             fetch_redirect_response=False)
        page = Client().get('/social/privacy-and-terms/')
        self.assertContains(page, 'id="terms"')
        self.assertContains(page, 'Refund Policy (clause 13)')

    def test_terms_of_service_page_is_public(self):
        response = Client().get('/social/terms-of-service/')
        self.assertContains(response, 'Terms of Service')
        self.assertContains(response, 'Using the website')

    def test_policy_links_land_on_their_sections(self):
        for path, anchor in (('/social/refund-policy/', 'refund-policy'),
                             ('/social/shipping-policy/', 'shipping-policy')):
            self.assertRedirects(Client().get(path), f'/social/privacy-and-terms/#{anchor}',
                                 fetch_redirect_response=False)
        page = Client().get('/social/privacy-and-terms/')
        self.assertContains(page, 'id="refund-policy"')
        self.assertContains(page, 'id="shipping-policy"')
        tos = Client().get('/social/terms-of-service/')
        self.assertContains(tos, '13. Refund Policy')
        self.assertContains(tos, '14. Shipping Policy')
