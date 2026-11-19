"""Google sign-in: the three ways somebody arrives, and the one that used to break.

These drive allauth's **real** callback view through the test client. Only the
two outbound HTTP calls to Google are stubbed (the token exchange and the
userinfo fetch); the adapter, the username convention, the e-mail-trust decision
and the session login are all production code.

The case worth keeping a test for is :meth:`test_google_signs_into_an_existing
_password_account`. Nearly everybody here registers with a password first and
clicks "Continue with Google" months later. Before
``SOCIALACCOUNT_EMAIL_AUTHENTICATION`` was set, allauth read that as a new
sign-up colliding with a taken address: it mailed a confirmation link and left
the person on "check your inbox", signed out, with nothing linked. No error, no
duplicate account — the button just silently did nothing useful. A regression
here is invisible in exactly the same way, which is why it is asserted rather
than assumed.
"""
from unittest import mock
from urllib.parse import parse_qs, urlparse

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

User = get_user_model()

ADDRESS = 'info@metanoiagroup.co.za'
PASSWORD = 'Str0ng!Passw0rd42'
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

USERINFO = {
    'sub': '109876543210987654321',
    'email': ADDRESS,
    'email_verified': True,
    'name': 'Metanoia Group',
    'given_name': 'CTA',
    'family_name': 'Metanoia Coach',
}
"""What Google's userinfo endpoint returns for :data:`ADDRESS`. ``email_verified``
is the flag that makes linking to an existing account safe — allauth refuses to
hand over a local account to an unverified provider address."""


def _stub_userinfo(self, request, app, token, response=None, **kwargs):
    """Stand in for the userinfo round-trip; the provider mapping stays real."""
    return self.get_provider().sociallogin_from_response(request, USERINFO)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
                   ACCOUNT_RATE_LIMITS={}, CACHES=LOCMEM_CACHE, AXES_ENABLED=False)
class GoogleSignInTests(TestCase):

    """Google sign-in end to end, with only the token exchange and userinfo call stubbed."""
    def sign_in_with_google(self):
        """Walk ``/accounts/google/login/`` → Google → the callback, as a browser does.

        allauth signs the state into the session on the way out and checks it on
        the way back, so the callback is given the real one.
        """
        start = self.client.get('/accounts/google/login/?process=login')
        self.assertEqual(start.status_code, 302)
        self.assertIn('accounts.google.com', start['Location'])
        state = parse_qs(urlparse(start['Location']).query)['state'][0]

        from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
        from allauth.socialaccount.providers.oauth2.client import OAuth2Client
        with mock.patch.object(OAuth2Client, 'get_access_token',
                               return_value={'access_token': 'stub', 'id_token': 'stub'}), \
             mock.patch.object(GoogleOAuth2Adapter, 'complete_login', _stub_userinfo):
            return self.client.get('/accounts/google/login/callback/',
                                   {'code': 'stub-auth-code', 'state': state}, follow=True)

    def make_password_account(self):
        """A verified password account on :data:`ADDRESS`."""
        user = User.objects.create_user(username=ADDRESS, email=ADDRESS, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=ADDRESS, verified=True, primary=True)
        return user

    def test_google_creates_the_account_and_signs_in(self):
        """A first Google sign-in creates a verified, signed-in account.

        The platform is e-mail-only, so the username mirrors the address rather than
        allauth's local-part derivation (see ``HubSocialAccountAdapter``). Google
        already proved the address, so no verification loop may stand between them
        and the hub.
        """
        response = self.sign_in_with_google()

        user = User.objects.get(email__iexact=ADDRESS)
        self.assertTrue(response.context['user'].is_authenticated)
        self.assertEqual(response.context['user'].pk, user.pk)
        self.assertEqual(user.username, ADDRESS)
        self.assertTrue(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(SocialAccount.objects.get(user=user).uid, USERINFO['sub'])

    def test_returning_google_login_reuses_the_same_account(self):
        self.sign_in_with_google()
        first = User.objects.get(email__iexact=ADDRESS)
        self.client.logout()

        response = self.sign_in_with_google()

        self.assertEqual(User.objects.filter(email__iexact=ADDRESS).count(), 1)
        self.assertEqual(response.context['user'].pk, first.pk)

    def test_google_signs_into_an_existing_password_account(self):
        """Google links to and signs into an existing password account (the case that used to break).

        Once linked, the next visit is a direct provider match.
        """
        user = self.make_password_account()

        response = self.sign_in_with_google()

        self.assertTrue(response.context['user'].is_authenticated,
                        'Google must sign into the existing account, not bounce '
                        'to "confirmation e-mail sent" with nobody signed in')
        self.assertEqual(response.context['user'].pk, user.pk)
        self.assertEqual(User.objects.filter(email__iexact=ADDRESS).count(), 1)
        self.assertEqual(SocialAccount.objects.filter(user=user).count(), 1)

    def test_an_unverified_google_address_cannot_claim_an_account(self):
        """The flip side: linking is only safe while Google's verified flag is honoured."""
        user = self.make_password_account()

        with mock.patch.dict(USERINFO, {'email_verified': False}):
            self.sign_in_with_google()

        self.assertFalse(SocialAccount.objects.filter(user=user).exists())

    def test_password_login_still_works_for_that_address(self):
        user = self.make_password_account()

        response = self.client.post(reverse('myhub:page-login'),
                                    {'email': ADDRESS, 'password': PASSWORD}, follow=True)

        self.assertTrue(response.context['user'].is_authenticated)
        self.assertEqual(response.context['user'].pk, user.pk)
