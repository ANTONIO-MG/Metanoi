"""Web sign-in (``myhub:page-login``) runs allauth's login stages, including 2FA.

The custom sign-in page used to call ``auth_login()`` directly, which skipped
allauth.mfa's "authenticate" stage: an account with an authenticator app was
signed in on the password alone. These tests pin that a correct password is no
longer enough for such an account, that the code step completes the sign-in and
still honours a safe ``next``, and that accounts without 2FA sign in as before.
"""

from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal.auth import (
    TOTP, format_hotp_value, generate_totp_secret, hotp_value, yield_hotp_counters_from_time,
)
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Person

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
PASSWORD = 'Correct-Horse-9!'


def make_user(email, *, role='student', **extra):
    """A verified user with a Person of ``role``, using :data:`PASSWORD`."""
    user = User.objects.create_user(username=email, email=email, password=PASSWORD, **extra)
    Person.objects.update_or_create(user=user, defaults={'user_type': role})
    EmailAddress.objects.update_or_create(
        user=user, email=email, defaults={'verified': True, 'primary': True})
    return user


def current_code(secret):
    """The current TOTP code for ``secret``."""
    counters = list(yield_hotp_counters_from_time())
    return format_hotp_value(hotp_value(secret, counters[len(counters) // 2]))


@override_settings(CACHES=LOCMEM_CACHE)
class WebLoginTwoFactorTests(TestCase):
    """The web sign-in page puts 2FA accounts through allauth's code step."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        try:
            from axes.utils import reset
            reset()
        except ImportError:  # pragma: no cover
            pass
        self.user = make_user('learner@example.com')

    def login(self, nxt=None):
        """Post the sign-in form, optionally with ``?next=nxt``."""
        url = reverse('myhub:page-login') + (f'?next={nxt}' if nxt else '')
        return self.client.post(url, {'email': self.user.email, 'password': PASSWORD})

    def signed_in_as(self):
        """The signed-in user id in the session, or ``None``."""
        return self.client.session.get('_auth_user_id')

    def test_without_2fa_a_correct_password_signs_in(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('myhub:index'))
        self.assertEqual(self.signed_in_as(), str(self.user.pk))

    def test_without_2fa_next_on_this_site_is_kept_and_foreign_next_ignored(self):
        self.assertEqual(self.login('/learning/')['Location'], '/learning/')
        self.client.logout()
        r = self.login('https://evil.example/')
        self.assertNotIn('evil', r['Location'])

    def test_next_in_the_posted_form_is_honoured(self):
        r = self.client.post(reverse('myhub:page-login'),
                             {'email': self.user.email, 'password': PASSWORD, 'next': '/learning/'})
        self.assertEqual(r['Location'], '/learning/')

    def test_login_page_carries_next_into_the_form(self):
        html = self.client.get(reverse('myhub:page-login') + '?next=/learning/').content.decode()
        self.assertIn('name="next" value="/learning/"', html)

    def test_wrong_password_is_still_an_error_on_the_page(self):
        r = self.client.post(reverse('myhub:page-login'),
                             {'email': self.user.email, 'password': 'nope'})
        self.assertEqual(r.status_code, 200, r.get("Location"))
        self.assertContains(r, 'Invalid e-mail or password.')
        self.assertIsNone(self.signed_in_as())

    def test_with_totp_a_correct_password_is_not_enough(self):
        """With TOTP on, the password alone leads to the code prompt; a protected page still bounces to sign-in."""
        TOTP.activate(self.user, generate_totp_secret())
        r = self.login('/learning/')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('mfa_authenticate'))
        self.assertIsNone(self.signed_in_as())
        r = self.client.get('/learning/')
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse('myhub:page-login'), r['Location'])

    def test_with_totp_a_wrong_code_does_not_sign_in(self):
        TOTP.activate(self.user, generate_totp_secret())
        self.login()
        r = self.client.post(reverse('mfa_authenticate'), {'code': '000000'})
        self.assertIsNone(self.signed_in_as())
        self.assertNotEqual(r.status_code, 302)

    def test_with_totp_the_code_step_completes_sign_in_and_keeps_next(self):
        secret = generate_totp_secret()
        TOTP.activate(self.user, secret)
        self.login('/learning/')
        r = self.client.post(reverse('mfa_authenticate'), {'code': current_code(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], '/learning/')
        self.assertEqual(self.signed_in_as(), str(self.user.pk))

    def test_with_totp_a_foreign_next_is_still_ignored_after_the_code(self):
        secret = generate_totp_secret()
        TOTP.activate(self.user, secret)
        self.login('https://evil.example/')
        r = self.client.post(reverse('mfa_authenticate'), {'code': current_code(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('evil', r['Location'])

    def test_welcome_message_is_the_hubs_own(self):
        r = self.login()
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertTrue(any(m.startswith('Welcome') for m in msgs), msgs)
        self.assertFalse(any('Successfully signed in' in m for m in msgs), msgs)

    @override_settings(MFA_REQUIRED_FOR_STAFF=True)
    def test_staff_without_a_factor_are_sent_to_set_one_up(self):
        staff = make_user('staff@example.com', role='staff', is_staff=True)
        r = self.client.post(reverse('myhub:page-login'), {'email': staff.email, 'password': PASSWORD})
        self.assertEqual(self.client.session.get('_auth_user_id'), str(staff.pk))
        r = self.client.get(r['Location'])
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], reverse('mfa_activate_totp'))


@override_settings(CACHES=LOCMEM_CACHE)
class RememberMeTests(TestCase):
    """The "Remember me" switch decides whether the sign-in outlives the browser."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = make_user('remember@example.com')

    def post(self, **extra):
        """Post the sign-in form with ``extra`` fields."""
        return self.client.post(reverse('myhub:page-login'),
                                {'email': self.user.email, 'password': PASSWORD, **extra})

    def test_ticked_keeps_the_session_for_the_normal_age(self):
        from django.conf import settings
        self.post(remember='1')
        self.assertFalse(self.client.session.get_expire_at_browser_close())
        self.assertEqual(self.client.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)
        self.assertTrue(self.client.cookies['sessionid']['expires'])

    def test_unticked_ends_the_session_with_the_browser(self):
        self.post()
        self.assertEqual(self.client.session.get('_auth_user_id'), str(self.user.pk))
        self.assertTrue(self.client.session.get_expire_at_browser_close())
        self.assertFalse(self.client.cookies['sessionid']['expires'])

    def test_unticked_survives_the_two_factor_step(self):
        secret = generate_totp_secret()
        TOTP.activate(self.user, secret)
        self.post()
        r = self.client.post(reverse('mfa_authenticate'), {'code': current_code(secret)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.session.get('_auth_user_id'), str(self.user.pk))
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_the_switch_is_part_of_the_form(self):
        html = self.client.get(reverse('myhub:page-login')).content.decode()
        self.assertIn('name="remember"', html)


@override_settings(CACHES=LOCMEM_CACHE)
class TwoFactorPagesLookLikeTheHubTests(TestCase):
    """allauth's 2FA pages render inside the hub's own layouts."""
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = make_user('pages@example.com')

    def test_code_prompt_uses_the_sign_in_layout(self):
        """The code prompt uses the hub's sign-in card and primary button, not allauth's bare page."""
        TOTP.activate(self.user, generate_totp_secret())
        self.client.post(reverse('myhub:page-login'), {'email': self.user.email, 'password': PASSWORD})
        html = self.client.get(reverse('mfa_authenticate')).content.decode()
        self.assertIn('allauth-hub', html)
        self.assertIn('allauth-form', html)
        self.assertIn('bg-gradient-info', html)
        self.assertNotIn('<strong>Menu:</strong>', html)

    def test_authenticator_setup_uses_the_hub_layout(self):
        """Authenticator setup renders inside the hub shell.

        allauth only opens the setup page after an explicit password confirmation —
        a fresh sign-in alone is not enough — so the test confirms first.
        """
        self.client.post(reverse('myhub:page-login'), {'email': self.user.email, 'password': PASSWORD})
        self.client.post(reverse('account_reauthenticate'), {'password': PASSWORD})
        r = self.client.get(reverse('mfa_activate_totp'))
        self.assertEqual(r.status_code, 200, r.get('Location'))
        html = r.content.decode()
        self.assertIn('id="appContentCol"', html)
        self.assertIn('allauth-qr', html)
        self.assertIn('allauth-form', html)
