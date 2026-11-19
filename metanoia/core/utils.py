"""Small project-wide helper functions shared across the apps.

Module constants
----------------
``LOCAL_SITE_URL``
    What ``SITE_URL`` falls back to when nothing is configured. Only ever right
    on a developer's own machine.
``DISCONTINUED_USER``
    What a closed-and-purged account is called wherever it still appears. A
    candidate who closes their account is erased, but the conversations they
    took part in belong to everyone who was in them: deleting the other side of
    a thread would rewrite other people's history. So the rows stay, the author
    link is dropped (every author FK worth keeping is ``SET_NULL``), and the gap
    reads as this rather than as a blank or a stray "User 41".
"""

import logging

logger = logging.getLogger(__name__)

LOCAL_SITE_URL = 'http://127.0.0.1:8000'


def site_base():
    """The public base URL of this installation, with no trailing slash.

    Every outbound link (invoice pay-links, PayFast return/ITN callbacks, e-mail
    buttons, WhatsApp) is built on this; PayFast signs the return URL, so a
    mismatch is a payment that never confirms. Outside DEBUG a still-local
    ``SITE_URL`` is logged as an error, since ``127.0.0.1`` links fail silently.
    """
    from django.conf import settings

    raw = (getattr(settings, 'SITE_URL', '') or '').strip().rstrip('/')
    if not raw:
        raw = LOCAL_SITE_URL
    if not getattr(settings, 'DEBUG', False) and raw == LOCAL_SITE_URL:
        logger.error(
            'SITE_URL is still %s with DEBUG off — every e-mailed link and every '
            'PayFast callback will point at localhost. Set SITE_URL in .env.',
            LOCAL_SITE_URL)
    return raw


def absolute_url(path=''):
    """``site_base()`` joined to ``path`` exactly once (no double slashes)."""
    base = site_base()
    if not path:
        return base
    return f'{base}/{str(path).lstrip("/")}'


def _is_internal_host(host):
    """A host only this server answers to: the bare IP, the Oracle VCN names,
    localhost — anything in ``ALLOWED_HOSTS`` that is not the public site."""
    import ipaddress
    from urllib.parse import urlsplit

    from django.conf import settings

    host = (host or '').lower().rstrip('.')
    if not host:
        return False
    public = (urlsplit(site_base()).hostname or '').lower()
    if host == public:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == 'localhost' or host.endswith('.oraclevcn.com'):
        return True
    allowed = {h.lower().lstrip('.') for h in getattr(settings, 'ALLOWED_HOSTS', []) if h and h != '*'}
    return host in allowed


def email_link(url):
    """``url`` made safe to click from an e-mail or a WhatsApp message.

    Notifications store their link as a site path (``/learning/…``), and a
    relative ``href`` in an e-mail has no host to resolve against — the button
    did nothing, or opened something else. Links built from a request that came
    in on the bare IP or an internal name get moved onto the public site too.
    Every other absolute link (YouTube, Payhip, wa.me) and ``mailto:``/``tel:``
    pass through untouched.
    """
    from urllib.parse import urlsplit

    url = str(url or '').strip()
    if not url or url.startswith(('mailto:', 'tel:', 'cid:', 'data:', '#')):
        return url
    if url.startswith('//'):
        url = 'https:' + url
    parts = urlsplit(url)
    if not parts.scheme:
        return absolute_url(url)
    if parts.scheme in ('http', 'https') and _is_internal_host(parts.hostname):
        rest = parts.path + (f'?{parts.query}' if parts.query else '') + (f'#{parts.fragment}' if parts.fragment else '')
        return absolute_url(rest)
    return url


DISCONTINUED_USER = 'Discontinued user'


def display_name(user):
    """Return a human-friendly name for ``user``.

    The platform is e-mail-only — there are no usernames — so this prefers the
    person's full name and falls back to the local-part of their e-mail address
    (e.g. ``jane`` for ``jane@example.com``), never the internal username column.

    ``None`` means the account was closed and purged; see
    :data:`DISCONTINUED_USER`.

    The name is read from the profile first, because that is where people type
    it (registration, settings); the login's own first/last name is only
    sometimes copied across.
    """
    if user is None:
        return DISCONTINUED_USER
    person = getattr(user, 'profile', None)
    full = ' '.join(filter(None, [getattr(person, 'first_name', '').strip() if person else '',
                                  getattr(person, 'last_name', '').strip() if person else ''])).strip()
    if not full:
        full = (user.get_full_name() or '').strip()
    if full:
        return full
    email = getattr(user, 'email', '') or ''
    return email.split('@')[0] or f'User {user.pk}'


def avatar_url(user):
    """URL of ``user``'s profile picture, or the default avatar for them.

    Never empty: someone who has not uploaded a picture gets the default that
    matches their gender (or title), and an account with no profile at all
    gets the neutral one. Storage-safe — see ``Person.avatar_url``.
    """
    from apps.accounts.models import default_avatar_url
    profile = getattr(user, 'profile', None) if user is not None else None
    if profile is None:
        return default_avatar_url()
    return profile.avatar_url


def initials(user):
    """One- or two-letter initials for ``user`` — the avatar fallback."""
    name = (display_name(user) or '?').strip()
    parts = [p for p in name.replace('.', ' ').split() if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[-1][:1]).upper()
    return (parts[0][:2] if parts else '?').upper()
