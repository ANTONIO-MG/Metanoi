"""API sign-in must be no weaker than the web sign-in.

``/api/token/`` hands out a bearer credential. These tests pin the gates that
make it no easier to obtain than a web session: the django-axes lockout, the
strict ``auth`` throttle, refusal for accounts with (or required to have) a
second factor, refusal for unverified e-mail — and that dj-rest-auth's
unguarded login / registration endpoints are no longer mounted.
"""

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Person

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
PASSWORD = 'Correct-Horse-9!'


def make_user(email, *, verified=True, role='student', **extra):
    """A user with a Person of ``role`` and a (by default verified) primary e-mail."""
    user = User.objects.create_user(username=email, email=email, password=PASSWORD, **extra)
    Person.objects.update_or_create(user=user, defaults={'user_type': role})
    EmailAddress.objects.update_or_create(
        user=user, email=email, defaults={'verified': verified, 'primary': True})
    return user


@override_settings(CACHES=LOCMEM_CACHE, AXES_ENABLED=True, AXES_FAILURE_LIMIT=5)
class TokenLoginTests(TestCase):
    """``/api/token/`` and ``/api/token/refresh/`` apply the web's sign-in rules."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        from axes.utils import reset
        reset()
        self.client = APIClient()
        self.user = make_user('learner@example.com')

    def obtain(self, username, password=PASSWORD):
        """Post ``/api/token/`` with these credentials."""
        return self.client.post('/api/token/', {'username': username, 'password': password},
                                format='json')

    def test_a_verified_account_without_2fa_gets_tokens(self):
        """A verified account without 2FA gets tokens, and the access token works against the API."""
        r = self.obtain(self.user.username)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertIn('access', r.json())
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {r.json()["access"]}')
        self.assertEqual(self.client.get('/api/communication/notifications/').status_code, 200)

    def test_an_account_with_2fa_is_refused(self):
        Authenticator.objects.create(user=self.user, type=Authenticator.Type.TOTP, data={'secret': 'x'})
        r = self.obtain(self.user.username)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'mfa_required')
        self.assertIn('two-factor', r.json()['detail'])
        self.assertNotIn('access', r.json())

    @override_settings(MFA_REQUIRED_FOR_STAFF=True)
    def test_staff_who_must_have_2fa_are_refused(self):
        boss = make_user('boss@example.com', role='staff', is_staff=True)
        r = self.obtain(boss.username)
        self.assertEqual(r.status_code, 403)
        self.assertIn('two-factor', r.json()['detail'])

    def test_refresh_stops_once_2fa_is_enabled(self):
        refresh = self.obtain(self.user.username).json()['refresh']
        Authenticator.objects.create(user=self.user, type=Authenticator.Type.TOTP, data={'secret': 'x'})
        r = self.client.post('/api/token/refresh/', {'refresh': refresh}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertNotIn('access', r.json())

    def test_an_unverified_account_is_refused(self):
        newbie = make_user('new@example.com', verified=False)
        r = self.obtain(newbie.username)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['code'], 'email_not_verified')

    def test_repeated_failures_lock_the_account(self):
        """After five failures axes locks the pair, so even the right password gets 429 ``locked_out``."""
        for _ in range(5):
            self.assertEqual(self.obtain(self.user.username, 'wrong').status_code, 401)
        r = self.obtain(self.user.username)
        self.assertEqual(r.status_code, 429, r.content[:300])
        self.assertEqual(r.json().get('code'), 'locked_out')
        self.assertNotIn('access', r.json())

    def test_the_auth_endpoint_is_throttled(self):
        codes = [self.obtain(f'nobody{i}@example.com', 'wrong').status_code for i in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)
        self.assertIn('throttled', self.obtain(self.user.username).content.decode().lower())


class UnusedAuthEndpointsAreGoneTests(TestCase):
    """dj-rest-auth's own login/registration routes are not mounted."""
    def test_dj_rest_auth_login_and_registration_are_not_mounted(self):
        client = APIClient()
        for url in ('/api/auth/login/', '/api/auth/registration/', '/api/auth/password/reset/',
                    '/api/auth/user/'):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {}, format='json').status_code, 404)
