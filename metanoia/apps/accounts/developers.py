"""Developer accounts: administrators who also see the platform's own tools.

A developer is an admin with ``Person.is_developer`` set
(:func:`core.roles.is_developer`). Only they see the error log, audit log,
background jobs, backups and those models in the admin; everyone else,
ordinary admins included, gets a 404.

* ``settings.DEVELOPER_EMAILS`` accounts are granted automatically
  (:func:`grant_if_listed`, on confirmation and sign-in) — only for a
  **verified** address, so registering someone else's address grants nothing.
* Otherwise only a developer can grant or revoke it, and a non-developer admin
  cannot change a developer's role.
"""

from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404

from core.roles import is_developer


def developer_emails():
    """``settings.DEVELOPER_EMAILS``, lower-cased."""
    return {(e or '').strip().lower() for e in getattr(settings, 'DEVELOPER_EMAILS', ()) if e}


def is_listed(email):
    """True when ``email`` is in ``settings.DEVELOPER_EMAILS`` (case-insensitive)."""
    return bool(email) and email.strip().lower() in developer_emails()


def has_verified_listed_email(user):
    """True when one of ``user``'s *verified* e-mail addresses is on the list."""
    if user is None or not getattr(user, 'pk', None):
        return False
    listed = developer_emails()
    if not listed:
        return False
    from allauth.account.models import EmailAddress
    return any((e or '').lower() in listed for e in
               EmailAddress.objects.filter(user=user, verified=True).values_list('email', flat=True))


def make_developer(person):
    """Set the flag and make sure the account is an administrator with Django-admin
    access (the flag only counts on an administrator's account)."""
    changed = []
    if not person.is_developer:
        person.is_developer = True
        changed.append('is_developer')
    if person.user_type != 'admin':
        person.user_type = 'admin'
        changed.append('user_type')
    if changed:
        person.save(update_fields=changed)
    from .roles_admin import sync_django_admin_flag
    sync_django_admin_flag(person)
    return bool(changed)


def grant_if_listed(user):
    """Make ``user`` a developer if a verified address of theirs is listed.

    Returns True when something changed. Safe to call on every sign-in.
    """
    if not has_verified_listed_email(user):
        return False
    person = getattr(user, 'profile', None)
    if person is None:
        return False
    return make_developer(person)


def is_protected(person):
    """A listed developer: their flag/role is re-granted at every sign-in, so
    revoking it in the app would not stick. Remove the address from
    ``DEVELOPER_EMAILS`` first."""
    user = getattr(person, 'user', None)
    return bool(person is not None and person.is_developer and has_verified_listed_email(user))


def developer_change_error(actor, person, make_developer_flag):
    """``None`` if ``actor`` may set ``person.is_developer`` to the given value."""
    if not is_developer(actor):
        return 'Only a developer can grant or remove developer access.'
    if make_developer_flag:
        return None
    if not person.is_developer:
        return None
    if person.user_id == getattr(actor, 'pk', None):
        return 'You cannot remove your own developer access.'
    if is_protected(person):
        return ('That account is in DEVELOPER_EMAILS, so it would be made a developer again '
                'at its next sign-in. Remove it from that setting first.')
    return None


def require_developer(request):
    """Raise 404 unless the user is a developer (404, not 403: there is no
    reason to advertise to anybody else that these tools exist)."""
    if not is_developer(getattr(request, 'user', None)):
        raise Http404
    return True


def developer_required(view):
    """View decorator: developers only, everybody else a 404."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        """Run ``view`` after :func:`require_developer`."""
        require_developer(request)
        return view(request, *args, **kwargs)
    return wrapped


class DeveloperOnlyAdminMixin:
    """``ModelAdmin`` mixin: invisible to non-developers, superusers included.
    Developers can always view; changing still needs model permissions."""

    def has_module_permission(self, request):
        """Show the app in the admin index to developers only."""
        return is_developer(request.user)

    def has_view_permission(self, request, obj=None):
        """Developers may always view."""
        return is_developer(request.user)

    def has_change_permission(self, request, obj=None):
        """Developers with the model's change permission."""
        return is_developer(request.user) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        """Developers with the model's delete permission."""
        return is_developer(request.user) and super().has_delete_permission(request, obj)

    def has_add_permission(self, request, *args, **kwargs):
        """Developers with the model's add permission."""
        return is_developer(request.user) and super().has_add_permission(request, *args, **kwargs)


def on_email_address_saved(sender, instance, raw=False, **kwargs):
    """``post_save`` on allauth ``EmailAddress``: grant developer once a listed address is verified.

    Skips fixture loads (``raw``); a failure is logged and never breaks a
    sign-up or confirmation.
    """
    if raw or not instance.verified or not is_listed(instance.email):
        return
    try:
        grant_if_listed(instance.user)
    except Exception:  # pragma: no cover - never break a sign-up or confirmation
        import logging
        logging.getLogger('apps').exception('developers: could not grant on e-mail save')


def on_user_logged_in(sender, request, user, **kwargs):
    """``user_logged_in``: re-grant developer to a listed, verified account at each sign-in.

    The profile is refreshed when it changed, because ``role_flags`` reads the
    cached profile on this very request. A failure is logged and never blocks
    the sign-in.
    """
    try:
        if grant_if_listed(user) and hasattr(user, 'profile'):
            user.profile.refresh_from_db()
    except Exception:  # pragma: no cover - never block a sign-in
        import logging
        logging.getLogger('apps').exception('developers: could not grant on sign-in')
