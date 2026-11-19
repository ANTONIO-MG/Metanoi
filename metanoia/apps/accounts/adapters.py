"""django-allauth adapters: one place for sign-up rules shared by the HTML page,
the DRF serializer and social sign-in.

:class:`HubAccountAdapter` keeps usernames equal to e-mail addresses, greets on
sign-in and brands allauth's e-mails; :class:`HubSocialAccountAdapter` applies
the same username rule to Google / Apple sign-ups and imports the provider's
name and photo (:func:`import_social_profile`).
"""

import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from core.branding import strings as _brand_strings

logger = logging.getLogger(__name__)


class HubAccountAdapter(DefaultAccountAdapter):
    """Keeps ``username`` equal to the e-mail address.

    ``page_login`` authenticates on ``username=<e-mail>``; allauth's default
    (the local part) would make every login use the fallback and collide
    "learner@a" with "learner@b". Also brands allauth's e-mails (:meth:`render_mail`).
    """

    def populate_username(self, request, user):
        """Set ``username`` to the e-mail, truncated to the column's 150 characters.

        The e-mail is already validated and unique-checked by the form. Without
        one, allauth's default applies.
        """
        email = user_email(user)
        if email:
            user.username = email[:150]
        else:
            super().populate_username(request, user)

    def get_signup_redirect_url(self, request):
        """A brand-new account goes straight to registration step 1."""
        from django.urls import reverse
        return reverse('accounts:register')

    def add_message(self, request, level, message_template=None, message_context=None,
                    extra_tags='', message=None):
        """Swap allauth's "Successfully signed in as …" for the hub's greeting.

        Login runs through allauth's stages (for 2FA), so the message must come
        from here. "Welcome", not "Welcome back": this fires on first sign-in too.
        """
        if message_template == 'account/messages/logged_in.txt' and not message:
            from core.branding import t
            from core.utils import display_name
            user = (message_context or {}).get('user') or getattr(request, 'user', None)
            if user is not None:
                message = t('messages.welcome', 'Welcome, {name}.', name=display_name(user))
        return super().add_message(request, level, message_template, message_context,
                                   extra_tags=extra_tags, message=message)

    def render_mail(self, template_prefix, email, context, headers=None):
        """Build allauth's message like the platform's own branded e-mails.

        The shared base layout references its images as ``cid:`` parts; allauth
        attaches none, so they arrived broken. Add the brand context and the
        inline image parts (``{{ inline.banner }}`` etc. resolve to ``cid:`` names).

        Verification, password reset and every other allauth message is account
        business, so it goes out from ``accounts@``, signed in as ``accounts@``.
        """
        from apps.communication import emails as branded

        catalog = _brand_strings()
        brand = catalog.get('brand', {})
        inline_map = branded._email_inline_map(brand)

        context = dict(context or {})
        context.setdefault('brand', brand)
        context.setdefault('strings', catalog)
        context.setdefault('module', '')
        context['inline'] = {name: f'cid:{name}' for name in inline_map}
        from apps.communication import senders as _senders
        context.setdefault('signature', _senders.signature(_senders.ACCOUNTS))

        msg = super().render_mail(template_prefix, email, context, headers=headers)

        for name, static_path in inline_map.items():
            branded._attach_inline_image(msg, static_path, name)
        from apps.communication import senders
        return senders.apply(msg, senders.ACCOUNTS)


class HubSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Google / Apple sign-up: same username convention, and the provider's name
    (and Google photo) copied onto the Person. Apple sends the name only on the
    first authorisation, which is when this runs.
    """

    def populate_user(self, request, sociallogin, data):
        """Build the new user and set ``username`` to the provider's e-mail."""
        user = super().populate_user(request, sociallogin, data)
        email = data.get('email') or user_email(user)
        if email:
            user.username = email[:150]
        return user

    def save_user(self, request, sociallogin, form=None):
        """Save the user, then import the provider's profile.

        A failed import (a missing photo, say) is logged and never blocks sign-up.
        """
        user = super().save_user(request, sociallogin, form)
        try:
            import_social_profile(user, sociallogin)
        except Exception:  # pragma: no cover - a missing photo must never block sign-up
            logger.exception('social sign-up: could not import the profile for %s', user.pk)
        return user


def import_social_profile(user, sociallogin):
    """Copy the provider's name and photo onto the user's Person, filling blanks only."""
    person = getattr(user, 'profile', None)
    if person is None:
        return
    changed = []
    for field in ('first_name', 'last_name'):
        if not getattr(person, field) and getattr(user, field, ''):
            setattr(person, field, getattr(user, field)[:50])
            changed.append(field)
    if changed:
        person.save(update_fields=changed)

    if not (person.profile_picture and str(person.profile_picture)):
        image = _download_avatar(sociallogin.account.get_avatar_url())
        if image:
            from django.core.files.base import ContentFile
            person.profile_picture.save(f'{sociallogin.account.provider}.jpg',
                                        ContentFile(image), save=True)


def _download_avatar(url, *, max_bytes=5 * 1024 * 1024):
    """The provider's profile photo as JPEG bytes, or ``None``. HTTPS only, small, verified.

    Google hands out a 96 px thumbnail (``…=s96-c``), so a 512 px one is asked
    for instead. The download must be an ``image/*`` no larger than
    ``max_bytes`` and must pass Pillow's ``verify()`` — a real image, not just a
    label — before it is re-encoded as JPEG.
    """
    if not url or not url.startswith('https://'):
        return None
    if 'googleusercontent.com' in url:
        url = url.split('=')[0] + '=s512-c'
    import io
    import urllib.request

    from PIL import Image
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if not resp.headers.get('Content-Type', '').startswith('image/'):
                return None
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data)).convert('RGB')
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=85)
        return out.getvalue()
    except Exception:
        return None


def user_email(user):
    """The user's e-mail address, stripped, or ``''``."""
    return (getattr(user, 'email', '') or '').strip()
