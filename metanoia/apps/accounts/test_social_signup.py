"""Google / Apple sign-up: buttons, profile import, and the hand-off to registration."""

from unittest import mock

from allauth.core.context import request_context

from django.contrib.auth import get_user_model
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

PROVIDERS = {
    'google': {'APP': {'client_id': 'g-id', 'secret': 'g-secret', 'key': ''}},
    'apple': {'APP': {'client_id': 'a-id', 'secret': 'KEYID', 'key': 'TEAMID',
                      'settings': {'certificate_key': 'unused-in-these-tests'}}},
}


def _jpeg():
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, 'JPEG')
    return out.getvalue()


@override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class SocialButtonsTests(TestCase):
    def test_google_then_apple_side_by_side_on_both_pages(self):
        for name in ('myhub:page-register', 'myhub:page-login'):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('social-btn--google', html, name)
            self.assertIn('social-btn--apple', html, name)
            self.assertLess(html.index('social-btn--google'), html.index('social-btn--apple'))
            self.assertIn('/accounts/apple/login/', html)

    @override_settings(SOCIALACCOUNT_PROVIDERS={})
    def test_no_credentials_no_buttons(self):
        html = self.client.get(reverse('myhub:page-register')).content.decode()
        self.assertNotIn('social-btn--', html)


@override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class SocialSignupFlowTests(TestCase):
    def _request(self):
        request = RequestFactory().get('/accounts/google/login/callback/')
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        MessageMiddleware(lambda r: None).process_request(request)
        from django.contrib.auth.models import AnonymousUser
        request.user = AnonymousUser()
        return request

    def _sociallogin(self, provider='google', extra=None):
        from allauth.account.models import EmailAddress
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        User = get_user_model()
        user = User(email='lerato@example.test', first_name='Lerato', last_name='Mokoena')
        account = SocialAccount(provider=provider, uid='uid-1', extra_data=extra or {
            'picture': 'https://lh3.googleusercontent.com/a/photo=s96-c'})
        login = SocialLogin(user=user, account=account, email_addresses=[
            EmailAddress(email=user.email, verified=True, primary=True)])
        from allauth.socialaccount.adapter import get_adapter
        login.provider = get_adapter().get_provider(self._request(), provider)
        login.state = {'process': 'login'}
        return login

    @mock.patch('apps.accounts.adapters._download_avatar', return_value=_jpeg())
    def test_new_google_user_lands_on_registration_prefilled(self, download):
        from allauth.socialaccount.helpers import complete_social_login

        request = self._request()
        with request_context(request):
            response = complete_social_login(request, self._sociallogin())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('accounts:register'))
        person = get_user_model().objects.get(email='lerato@example.test').profile
        self.assertEqual((person.first_name, person.last_name), ('Lerato', 'Mokoena'))
        self.assertTrue(person.profile_picture)
        download.assert_called_once_with('https://lh3.googleusercontent.com/a/photo=s96-c')

    @mock.patch('apps.accounts.adapters._download_avatar', return_value=None)
    def test_apple_signup_without_photo_still_completes(self, _download):
        from allauth.socialaccount.helpers import complete_social_login

        request = self._request()
        with request_context(request):
            response = complete_social_login(request, self._sociallogin('apple', extra={}))

        self.assertEqual(response['Location'], reverse('accounts:register'))
        person = get_user_model().objects.get(email='lerato@example.test').profile
        self.assertEqual(person.first_name, 'Lerato')
        self.assertFalse(person.profile_picture)


class DownloadAvatarTests(TestCase):
    def test_refuses_non_https_and_sharpens_google_thumbnail(self):
        from apps.accounts.adapters import _download_avatar
        self.assertIsNone(_download_avatar('http://example.test/a.jpg'))
        with mock.patch('urllib.request.urlopen') as urlopen:
            resp = urlopen.return_value.__enter__.return_value
            resp.headers = {'Content-Type': 'image/jpeg'}
            resp.read.return_value = _jpeg()
            self.assertTrue(_download_avatar('https://lh3.googleusercontent.com/a/x=s96-c'))
            self.assertEqual(urlopen.call_args[0][0], 'https://lh3.googleusercontent.com/a/x=s512-c')
