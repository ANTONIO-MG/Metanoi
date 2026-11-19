"""The Metanoia signature every Metanoia Mail mailbox signs with.

The same block the automated e-mails end with (base_email.html). A personal
mailbox shows the owner's picture, name and role (``senders.PERSONAL_ROLES``);
a shared one shows the Metanoia tick and the mailbox's name.

Pictures are absolute ``SITE_URL`` links so the HTML also works pasted into
other mail apps; when Metanoia Mail sends, :func:`embed_images` swaps them for
inline (``cid:``) parts so recipients see them without "show pictures".

``TICK``, ``LOGO`` and ``BANNER`` are the static images the signature uses
(also used by ``base_email.html``); they are the only static files
:func:`embed_images` will inline. The avatar link carries a signed token
(salt ``_SALT``) so it can be fetched without signing in.
"""

import os
import re
from html import unescape

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core import signing
from django.template.loader import render_to_string
from django.urls import reverse

_SALT = 'mailbox.signature-avatar'

TICK = 'email/sig-avatar.png'
LOGO = 'email/sig-logo.png'
BANNER = 'email/banner.jpg'


def _site():
    """``SITE_URL`` without a trailing slash."""
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/')


def _static_url(path):
    """Absolute URL of static file ``path``."""
    return f'{_site()}{settings.STATIC_URL}{path}'


def avatar_token(account):
    """A signed token naming ``account``, used in the signature avatar URL."""
    return signing.dumps(account.pk, salt=_SALT)


def account_from_token(token):
    """The :class:`~apps.mailbox.models.MailAccount` an :func:`avatar_token` names, or ``None`` if it is forged."""
    from .models import MailAccount
    try:
        pk = signing.loads(token, salt=_SALT)
    except signing.BadSignature:
        return None
    return MailAccount.objects.select_related('user__profile').filter(pk=pk).first()


def _shared_mailbox(account):
    """``'accounts'`` / ``'info'`` / ``'learninghub'`` for a shared mailbox, else ``None``."""
    from apps.communication import senders
    local, _, domain = account.email_address.lower().partition('@')
    server_domain = (getattr(settings, 'MAILBOX_DOMAIN', '') or 'metanoiagroup.co.za').lower()
    if local in senders.SIGNATURES and domain in (server_domain, 'metanoiagroup.co.za'):
        return local
    return None


def _has_photo(user):
    """Whether ``user`` has a profile picture."""
    person = getattr(user, 'profile', None)
    picture = getattr(person, 'profile_picture', None)
    return bool(picture and str(picture))


def context(account):
    """Everything the signature template needs for ``account``."""
    from apps.communication import senders
    from core.branding import strings

    shared = _shared_mailbox(account)
    if shared:
        sig = senders.signature(shared)
        avatar = _static_url(TICK)
    else:
        sig = senders.signature(None, author=account.user)
        sig['email'] = account.email_address
        avatar = (_site() + reverse('mailbox:signature-avatar', args=[avatar_token(account)])
                  if _has_photo(account.user) else _static_url(TICK))
    brand = (strings().get('brand') or {})
    return {'signature': sig, 'avatar_url': avatar, 'logo_url': _static_url(LOGO),
            'banner_url': _static_url(BANNER),
            'website': getattr(settings, 'PUBLIC_SITE_URL', '') or brand.get('website', '')}


def build(account):
    """The signature HTML for ``account``."""
    return render_to_string('mailbox/signature.html', context(account)).strip()


def apply_default(account, *, force=False):
    """Give ``account`` the Metanoia signature if it has none (or always, with ``force``)."""
    if account.signature_html.strip() and not force:
        return False
    account.signature_html = build(account)
    account.save(update_fields=['signature_html', 'updated_at'])
    return True


_SRC = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.I)


_DATA_URI = re.compile(r'data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=\s]+)$', re.I)


def _local_image(url):
    """``(bytes, subtype, filename)`` for a signature picture we host, else ``None``.

    A ``data:`` picture (pasted, or carried over from a copied Gmail draft) is
    sent the same way, since Gmail and Outlook do not show ``data:`` images.
    """
    import base64
    import binascii
    url = unescape(url)
    data_uri = _DATA_URI.match(url.strip())
    if data_uri:
        try:
            blob = base64.b64decode(re.sub(r'\s', '', data_uri.group(2)))
        except (binascii.Error, ValueError):
            return None
        subtype = data_uri.group(1).lower().replace('jpg', 'jpeg')
        return blob, subtype, f'image.{subtype.replace("jpeg", "jpg")}'
    site = _site()
    if not site or not url.startswith(site + '/'):
        return None
    path = url[len(site):].split('?')[0]
    static = settings.STATIC_URL
    if path.startswith(static):
        rel = path[len(static):]
        if rel not in (TICK, LOGO, BANNER):
            return None
        found = finders.find(rel) or os.path.join(settings.STATIC_ROOT, rel)
        if not found or not os.path.exists(found):
            return None
        with open(found, 'rb') as fh:
            data = fh.read()
        subtype = 'jpeg' if rel.endswith('.jpg') else 'png'
        return data, subtype, os.path.basename(rel)
    match = re.fullmatch(r'/mail/sig/([^/]+)\.png', path)
    if match:
        account = account_from_token(match.group(1))
        if account is None:
            return None
        from apps.communication.emails import ringed_avatar
        data = ringed_avatar(account.user)
        return (data, 'png', 'signature.png') if data else None
    return None


def embed_images(html):
    """``(html, parts)``: our signature (and ``data:``) pictures as ``cid:`` links plus the parts to attach.

    ``parts`` is a list of ``(cid, bytes, subtype, filename)``. Anything else
    in the message — a remote image someone pasted — is left alone.
    """
    parts, seen = [], {}

    def swap(match):
        """Replace one ``<img src>`` with a ``cid:`` link when it is one of our pictures."""
        url = match.group(2)
        if url in seen:
            return match.group(1) + 'cid:' + seen[url] + match.group(3)
        found = _local_image(url)
        if not found:
            return match.group(0)
        cid = f'metanoia-sig-{len(parts) + 1}'
        seen[url] = cid
        parts.append((cid, *found))
        return match.group(1) + 'cid:' + cid + match.group(3)

    return _SRC.sub(swap, html or ''), parts
