"""Mailbox passwords at rest.

A mailbox password has to be recoverable (IMAP and SMTP both want the
plaintext), so it is encrypted rather than hashed: Fernet with a key derived
from ``MAILBOX_SECRET_KEY`` — or, when that is unset, from ``SECRET_KEY``.

Rotating whichever key is in use makes every stored password unreadable; the
mailboxes then report "sign-in failed" and each one needs its password typed
in again. Set ``MAILBOX_SECRET_KEY`` once and leave it alone to decouple the
two.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    secret = getattr(settings, 'MAILBOX_SECRET_KEY', '') or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(('metanoia-mail:' + secret).encode()).digest())
    return Fernet(key)


def encrypt(plaintext):
    if not plaintext:
        return ''
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token):
    """The plaintext, or ``''`` when there is none or the key has changed."""
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ''
