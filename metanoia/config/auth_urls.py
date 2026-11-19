"""One login, one sign-up, one password reset — and one way into the admin.

The hub's own pages — ``/myhub/page-login/``, ``page-register/``,
``page-forgot-password/`` — are the only account entry pages. django-allauth
still has its own ``/accounts/…`` entry pages, and it cannot simply be unmounted:
it is the *engine* behind e-mail confirmation, the emailed password-reset link
and Google sign-in. So its duplicate **entry pages** are intercepted here and
redirected to the hub's, while allauth's machinery routes (confirm-email,
reset-from-key, social callbacks) stay exactly where they are.

The same module owns the Django-admin doorway:

* ``/admin/login/``  — never show a second login form. A signed-in admin goes
  straight through; a signed-in non-admin is sent back to the hub; an anonymous
  visitor is sent to the hub's login page with ``?next=``.
* ``/admin/logout/`` — "leaving the admin" returns you to the dashboard **still
  signed in**; it is not a sign-out. The real sign-out lives in the hub navbar
  (``myhub:logout``), which ends the session properly.
"""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


def _safe_next(request, fallback):
    """The ``?next=`` target, but only if it points back at this site."""
    nxt = request.GET.get('next') or request.POST.get('next')
    if nxt and url_has_allowed_host_and_scheme(
            nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return fallback


def _is_admin(user):
    return user.is_authenticated and user.is_active and (user.is_staff or user.is_superuser)


def to_login(request):
    """``/accounts/login/`` (allauth) → the hub's login, keeping ``?next=``."""
    target = reverse('myhub:page-login')
    nxt = _safe_next(request, '')
    return redirect(f'{target}?next={nxt}' if nxt else target)


def to_register(request):
    """``/accounts/signup/`` (allauth) → the hub's registration page."""
    return redirect('myhub:page-register')


def to_forgot_password(request):
    """``/accounts/password/reset/`` (allauth) → the hub's reset page.

    The hub's page posts through allauth's own ``ResetPasswordForm``, so the
    emailed link still lands on allauth's ``…/password/reset/key/<key>/`` view.
    """
    return redirect('myhub:page-forgot-password')


def to_login_after_reset(request):
    """``/accounts/password/reset/key/done/`` → the hub's login page.

    A finished password reset should hand the user straight back to the sign-in
    form, rather than parking them on allauth's stock "your password is changed"
    page (which is a dead end offering only a link onwards). The confirmation
    e-mail is sent separately by the ``password_reset`` signal — see
    apps.accounts.signals.
    """
    messages.success(request, 'Your password has been changed. Please sign in with it.')
    return redirect('myhub:page-login')


def admin_login(request):
    """Stand in front of ``/admin/login/`` so nobody is asked to sign in twice.

    The admin session *is* the hub session — an admin who is already signed in to
    the hub is simply let through.
    """
    nxt = _safe_next(request, reverse('admin:index'))
    if _is_admin(request.user):
        return redirect(nxt)
    if request.user.is_authenticated:
        messages.warning(request, 'You need an administrator account to open the Django admin.')
        return redirect('myhub:index')
    return redirect(f"{settings.LOGIN_URL}?next={nxt}")


def admin_logout(request):
    """Leaving the admin returns you to the hub — it does **not** end your session.

    (The hub navbar's *Sign out* is the real one.) Kept at ``/admin/logout/`` so
    the admin's own "Log out" control, and the theme's sidebar link, both land
    here instead of dumping the user on a dead account page.
    """
    messages.info(request, 'You left the Django admin. You are still signed in.')
    return redirect('myhub:index')

