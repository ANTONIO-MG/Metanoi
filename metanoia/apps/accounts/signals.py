"""Accounts signals: auto-create a Person per User, audit user changes and
logins in ActivityLog, and react to paid registration invoices.

For security, every password change or reset (allauth's ``password_changed`` /
``password_reset``) also e-mails the account holder a confirmation. allauth is
always installed, but the wiring stays defensive so a missing signal never stops
the app from starting.
"""

import logging

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .middleware import get_current_request
from .models import ActivityLog, Person
logger = logging.getLogger(__name__)


def _request_context():
    """Return (actor, ip_address, request_path) safely."""
    request = get_current_request()

    if request is None:
        return None, None, None

    ip = request.META.get('REMOTE_ADDR')
    path = request.path

    actor = None
    if hasattr(request, "user") and request.user.is_authenticated:
        actor = getattr(request.user, 'profile', None)

    return actor, ip, path


def _profile_of(user):
    """Return the ``Person`` linked to ``user``, or ``None`` if there is none."""
    if user is None:
        return None
    try:
        return user.profile
    except Person.DoesNotExist:
        return None


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_person_profile(sender, instance, created, raw=False, **kwargs):
    """Create a :class:`Person` for every new user.

    Raw loads (fixtures, account restores) are skipped: they carry their own
    Person, which a new one would collide with.
    """
    if created and not raw:
        Person.objects.create(user=instance)


def _send_paid_registration_summary(invoice):
    """The two registration e-mails for a card-paid *registration* invoice (one
    with module enrolments). ``finance.services._on_invoice_paid`` skips its
    generic receipt for exactly these, so they are not doubled.

    Never raises: a settled payment must stand whatever happens to the e-mail.
    """
    try:
        from apps.learning.models import ModuleEnrolment

        enrolments = list(
            ModuleEnrolment.objects
            .filter(invoice_uid=invoice.public_id)
            .select_related('programme_module__programme__institution', 'person__user'))
        if not enrolments:
            return

        person = enrolments[0].person
        programme = enrolments[0].programme_module.programme
        from . import registration_billing
        registration_billing.deliver_registration(
            person, invoice, enrolments, paid=True, programme=programme)
    except Exception:  # pragma: no cover
        logger.exception('accounts: registration e-mails failed for invoice %s',
                         getattr(invoice, 'number', '?'))


try:
    from apps.finance.dispatch import invoice_paid

    @receiver(invoice_paid)
    def enrol_on_invoice_paid(sender, invoice, **kwargs):
        """Invoice settled: unlock its modules, send the registration e-mails,
        and clear the onboarding gate of whoever was waiting on it."""
        from core.errors import capture
        with capture('FIN-6002', reraise=False, context={'invoice': str(invoice.public_id)}):
            from apps.learning.enrolment import activate_modules_for_invoice
            activate_modules_for_invoice(invoice)

        _send_paid_registration_summary(invoice)

        Person.objects.filter(pending_invoice_uid=invoice.public_id).update(
            pending_invoice_uid=None)
except Exception:  # pragma: no cover - finance app always present, defensive only
    logger.exception('accounts: could not connect invoice_paid receiver')


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def log_user_save(sender, instance, created, raw=False, **kwargs):
    """Audit a user being created or updated.

    A raw load is a restore, not somebody doing something — and the profile it
    would be logged against has not been inserted yet — so it is not logged.
    """
    if raw:
        return
    actor, ip, path = _request_context()
    ActivityLog.objects.create(
        actor=actor,
        target_user=_profile_of(instance),
        action='create' if created else 'update',
        description=f"User {'created' if created else 'updated'}: {instance}",
        ip_address=ip,
        request_path=path,
    )
    logger.info('USER %s | id=%s | email=%s',
                'CREATED' if created else 'UPDATED', instance.pk, instance.email)


@receiver(post_delete, sender=settings.AUTH_USER_MODEL)
def log_user_delete(sender, instance, **kwargs):
    """Audit a user being deleted.

    The row has no ``target_user``: the Person was deleted with the User
    (CASCADE) in this same transaction. Linking the audit row to it — which
    happened whenever ``user.profile`` was already loaded, as in the admin —
    broke the foreign key and rolled the whole delete back. The description keeps
    who it was.
    """
    actor, ip, path = _request_context()
    ActivityLog.objects.create(
        actor=actor,
        target_user=None,
        action='delete',
        description=f'User deleted: {instance} <{instance.email}>',
        ip_address=ip,
        request_path=path,
    )
    logger.warning('USER DELETED | id=%s | email=%s', instance.pk, instance.email)


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs):
    """Audit a sign-in."""
    person = _profile_of(user)
    ActivityLog.objects.create(
        actor=person,
        target_user=person,
        action='login',
        description='User logged in',
        ip_address=request.META.get('REMOTE_ADDR', None),
        request_path=request.path or None,
    )


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs):
    """Audit a sign-out."""
    person = _profile_of(user)
    ActivityLog.objects.create(
        actor=person,
        target_user=person,
        action='logout',
        description='User logged out',
        ip_address=request.META.get('REMOTE_ADDR', None),
        request_path=request.path or None,
    )


try:
    from allauth.account.signals import password_changed as _pw_changed
    from allauth.account.signals import password_reset as _pw_reset

    def _email_password_changed(request, user, **kwargs):
        """E-mail a security confirmation after a password change or reset; failures are only logged."""
        try:
            from apps.communication.emails import send_password_changed
            login_url = getattr(settings, 'SITE_URL', '').rstrip('/') + '/myhub/page-login/'
            send_password_changed(user, login_url=login_url)
        except Exception:  # pragma: no cover - never break the password flow
            logger.exception('Failed to send password-changed e-mail')

    receiver(_pw_changed)(_email_password_changed)
    receiver(_pw_reset)(_email_password_changed)
except Exception:  # pragma: no cover
    logger.exception('Could not wire allauth password signals')
