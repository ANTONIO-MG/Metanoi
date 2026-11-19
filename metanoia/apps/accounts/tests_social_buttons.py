"""The sign-in page's social buttons: live Google, Apple greyed out until configured."""
from django.test import TestCase, override_settings
from django.urls import reverse

GOOGLE = {'google': {'APP': {'client_id': 'abc.apps.googleusercontent.com', 'secret': 's', 'key': ''}}}
APPLE = {'apple': {'APP': {'client_id': 'za.co.metanoia.web', 'secret': 'KEY', 'key': 'TEAM',
                           'settings': {'certificate_key': 'x'}}}}


class SocialButtonTests(TestCase):
    def page(self):
        return self.client.get(reverse('myhub:page-login')).content.decode()

    @override_settings(SOCIALACCOUNT_PROVIDERS=GOOGLE)
    def test_apple_shows_greyed_out_beside_google_until_configured(self):
        html = self.page()
        self.assertIn('social-btn--google', html)
        self.assertIn('/accounts/google/login/', html)
        self.assertIn('social-btn--apple is-pending', html)
        self.assertIn('aria-disabled="true"', html)
        self.assertNotIn('/accounts/apple/login/', html)

    @override_settings(SOCIALACCOUNT_PROVIDERS={**GOOGLE, **APPLE})
    def test_apple_becomes_a_live_link_once_configured(self):
        html = self.page()
        self.assertIn('/accounts/apple/login/', html)
        self.assertNotIn('social-btn--apple is-pending', html)

    @override_settings(SOCIALACCOUNT_PROVIDERS={})
    def test_no_buttons_at_all_without_any_provider(self):
        self.assertNotIn('social-btn', self.page())
