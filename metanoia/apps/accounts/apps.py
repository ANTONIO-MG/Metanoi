"""App configuration for :mod:`apps.accounts` — identity, onboarding and access.

:meth:`AccountsConfig.ready` wires the app's signal handlers and model hooks.
"""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    """Accounts app config; ``ready()`` connects the signal handlers."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'

    def ready(self):
        """Connect the signal handlers and install the model hooks.

        * :mod:`apps.accounts.signals` — imported for its receivers.
        * "Become a student": allauth's ``email_confirmed`` marks the upgrade request
          verified (:func:`apps.accounts.community_views.on_email_confirmed`).
        * Developer accounts: a listed address becomes a developer once it is
          verified, and again at each sign-in (:mod:`apps.accounts.developers`).
        * The old profile picture is deleted from disk when it is replaced or the
          Person is removed (Django leaves the file behind otherwise).
        * People are shown by name, never by login or e-mail (:mod:`apps.accounts.names`).
        """
        from . import signals  # noqa: F401  (import for side effects)

        from allauth.account.signals import email_confirmed
        from .community_views import on_email_confirmed
        email_confirmed.connect(on_email_confirmed, dispatch_uid='community-upgrade-confirmed')

        from allauth.account.models import EmailAddress
        from django.contrib.auth.signals import user_logged_in
        from django.db.models.signals import post_save
        from . import developers
        post_save.connect(developers.on_email_address_saved, sender=EmailAddress,
                          dispatch_uid='developer-email-verified')
        user_logged_in.connect(developers.on_user_logged_in, dispatch_uid='developer-sign-in')

        from core.file_cleanup import register
        register(self.get_model('Person'), ['profile_picture'])

        from .names import install
        install(self.get_model('Person'))
