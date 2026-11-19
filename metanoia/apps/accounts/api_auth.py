"""API sign-in (``/api/token/`` and ``/api/token/refresh/``), held to the web's rules.

Stock SimpleJWT skips the web sign-in gates; these views apply them:

* **Lockout** — axes is consulted *before* the password check, so a locked
  pair gets a clear 429; failures are still recorded via ``authenticate()``.
* **Throttling** — the strict ``auth`` scope on top of anon/user throttles.
* **Two-factor** — the API has no 2FA step, so an account with (or required to
  have) a second factor is refused with ``code: "mfa_required"`` and must use
  the web session. Also checked on refresh, so enabling 2FA stops older tokens
  within one access-token lifetime.
* **Unverified e-mail** — refused (staff exempt), as on the web.
"""

from django.conf import settings
from django.contrib.auth.models import update_last_login
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer, TokenObtainSerializer, TokenRefreshSerializer,
)
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView


class AuthRateThrottle(ScopedRateThrottle):
    """The strict per-IP budget for credential endpoints (scope ``auth``)."""

    scope_attr = 'throttle_scope'


AUTH_THROTTLES = [AnonRateThrottle, UserRateThrottle, AuthRateThrottle]


MFA_REQUIRED_MESSAGE = ('This account uses two-factor authentication, which the API '
                        'sign-in does not support. Sign in on the website instead.')
EMAIL_NOT_VERIFIED_MESSAGE = 'Please verify your e-mail address before signing in.'


def _refuse(message, code):
    """403 whose body carries a machine-readable ``code`` next to ``detail``."""
    return exceptions.PermissionDenied({'detail': message, 'code': code})


def requires_second_factor(user):
    """True when the web would put ``user`` through (or into) a second factor.

    With ``MFA_REQUIRED_FOR_STAFF`` on, "privileged" mirrors
    :class:`apps.accounts.middleware.MfaRequiredMiddleware`: Django staff,
    superusers and admin/staff/educator profiles.
    """
    try:
        from allauth.mfa.models import Authenticator
    except ImportError:  # pragma: no cover - allauth.mfa not installed
        return False
    if Authenticator.objects.filter(user=user).exists():
        return True
    if getattr(settings, 'MFA_REQUIRED_FOR_STAFF', False):
        person = getattr(user, 'profile', None)
        if (user.is_staff or user.is_superuser
                or (person is not None and person.user_type in ('admin', 'staff', 'educator'))):
            return True
    return False


def _email_is_verified(user):
    """True when ``user`` has at least one verified allauth e-mail address."""
    from allauth.account.models import EmailAddress
    return EmailAddress.objects.filter(user=user, verified=True).exists()


def check_api_login_allowed(user):
    """Raise if ``user`` may not hold an API credential. Shared by obtain + refresh."""
    if requires_second_factor(user):
        raise _refuse(MFA_REQUIRED_MESSAGE, 'mfa_required')
    if not user.is_staff and not _email_is_verified(user):
        raise _refuse(EMAIL_NOT_VERIFIED_MESSAGE, 'email_not_verified')


class SecureTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Token pair serializer that applies :func:`check_api_login_allowed`."""
    def validate(self, attrs):
        """Authenticate, apply the web's sign-in rules, then mint the token pair.

        ``TokenObtainSerializer.validate`` authenticates (axes records failures)
        and sets ``self.user``. The checks run before any token is minted, so a
        refused login leaves no outstanding refresh token behind.
        """
        data = TokenObtainSerializer.validate(self, attrs)
        check_api_login_allowed(self.user)
        refresh = self.get_token(self.user)
        data['refresh'] = str(refresh)
        data['access'] = str(refresh.access_token)
        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, self.user)
        return data


class SecureTokenRefreshSerializer(TokenRefreshSerializer):
    """Refresh serializer that re-applies :func:`check_api_login_allowed`."""
    def validate(self, attrs):
        """Refuse the refresh if the token's user is gone or may no longer sign in via the API.

        This is what makes enabling 2FA stop older tokens within one access-token lifetime.
        """
        from django.contrib.auth import get_user_model

        token = self.token_class(attrs['refresh'])
        user_id = token.payload.get(api_settings.USER_ID_CLAIM)
        user = get_user_model().objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first()
        if user is None:
            raise exceptions.AuthenticationFailed('No active account found for the given token.',
                                                  'no_active_account')
        check_api_login_allowed(user)
        return super().validate(attrs)


class SecureTokenObtainPairView(TokenObtainPairView):
    """``/api/token/``: SimpleJWT obtain with axes lockout, auth throttles and the web's rules."""
    serializer_class = SecureTokenObtainPairSerializer
    throttle_classes = AUTH_THROTTLES
    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        """Answer 429 ``locked_out`` for a locked username before the password is checked.

        The lockout is noted as ``AUTH-2002``; otherwise the normal obtain runs.
        """
        username = request.data.get(TokenObtainSerializer.username_field) if hasattr(request.data, 'get') else None
        try:
            from axes.handlers.proxy import AxesProxyHandler
        except ImportError:  # pragma: no cover
            AxesProxyHandler = None
        if AxesProxyHandler is not None and username:
            if not AxesProxyHandler.is_allowed(request._request, {'username': username}):
                try:
                    from core.errors import note
                    note('AUTH-2002', request._request, via='api-token')
                except Exception:  # pragma: no cover - logging must not break the reply
                    pass
                return Response(
                    {'detail': 'Too many failed sign-in attempts. This account is locked '
                               'for a while; try again later or reset your password.',
                     'code': 'locked_out'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS)
        return super().post(request, *args, **kwargs)


class SecureTokenRefreshView(TokenRefreshView):
    """``/api/token/refresh/``: SimpleJWT refresh with auth throttles and the web's rules."""
    serializer_class = SecureTokenRefreshSerializer
    throttle_classes = AUTH_THROTTLES
    throttle_scope = 'auth'

