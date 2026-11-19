"""Proving a WhatsApp number belongs to the person typing it.

A six-digit code sent via the approved WhatsApp authentication template proves
the number is on WhatsApp and held by the registrant; it is then stored on
``Person.whatsapp_number``. Several accounts may verify the same number (parent
and child).

Rules:

* a code lasts :data:`CODE_MINUTES` minutes and allows :data:`MAX_ATTEMPTS`
  guesses, and only a hash of it is stored;
* sending is rate-limited per account and per number (:data:`RESEND_SECONDS`,
  :data:`PER_HOUR`), so the form cannot be used to flood someone's phone;
* while WhatsApp is not configured (:func:`required` is False) nothing is
  demanded, so registration still works before Meta approval.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from core import whatsapp

CODE_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
PER_HOUR = 5


def required():
    """True when registration must verify the number."""
    return whatsapp.enabled() and getattr(settings, 'WHATSAPP_VERIFY_AT_REGISTRATION', True)


def _hash(verification_id, number, code):
    """HMAC-SHA256 of the code, bound to this verification row and number."""
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f'{verification_id}:{number}:{code}'.encode(), hashlib.sha256).hexdigest()


def is_verified(person, number):
    """True when ``person`` has verified exactly this number."""
    digits = whatsapp.msisdn(number)
    return bool(person is not None and digits and person.whatsapp_number == digits)


def send_code(user, number, *, purpose='register'):
    """Send a code to ``number`` for ``user``. Returns ``(ok, message)``.

    Throttled to one code per :data:`RESEND_SECONDS` and :data:`PER_HOUR` per
    user and per number. Only the code's hash is stored, and the WhatsApp
    transcript records that a code went out — never the code itself.
    """
    from apps.communication.whatsapp_inbox import log_outbound

    from .models import PhoneVerification

    digits = whatsapp.msisdn(number)
    if not digits:
        return False, 'Enter your full WhatsApp number first.'
    if not whatsapp.enabled():
        return False, 'WhatsApp verification is not switched on yet.'

    now = timezone.now()
    recent = PhoneVerification.objects.filter(created_at__gte=now - timedelta(hours=1))
    last = recent.filter(user=user).order_by('-created_at').first()
    if last and last.created_at > now - timedelta(seconds=RESEND_SECONDS):
        wait = RESEND_SECONDS - int((now - last.created_at).total_seconds())
        return False, f'A code was just sent. You can ask for another in {max(wait, 1)} seconds.'
    if recent.filter(user=user).count() >= PER_HOUR or recent.filter(number=digits).count() >= PER_HOUR:
        return False, 'Too many codes were requested. Please try again in an hour.'

    code = f'{secrets.randbelow(1_000_000):06d}'
    verification = PhoneVerification.objects.create(
        user=user, number=digits, purpose=purpose, code_hash='-',
        expires_at=now + timedelta(minutes=CODE_MINUTES))
    verification.code_hash = _hash(verification.pk, digits, code)

    sent, reason, wamid = whatsapp.send_code(digits, code)
    verification.sent = sent
    verification.save(update_fields=['code_hash', 'sent'])
    log_outbound(digits, 'Verification code sent', kind='code', person=getattr(user, 'profile', None),
                 sent=sent, reason=reason, wamid=wamid)
    if not sent:
        return False, ("We could not send a WhatsApp message to that number. Check it is "
                       "your WhatsApp number, with the right country code.")
    return True, f'We sent a 6-digit code to {whatsapp.display(digits)} on WhatsApp.'


def check_code(user, number, code):
    """Check a code. On success the number is stored as verified. Returns ``(ok, message)``."""
    from .models import Person, PhoneVerification

    digits = whatsapp.msisdn(number)
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if not digits or len(code) != 6:
        return False, 'Enter the 6-digit code from WhatsApp.'

    verification = (PhoneVerification.objects
                    .filter(user=user, number=digits, verified_at__isnull=True, sent=True)
                    .order_by('-created_at').first())
    if verification is None:
        return False, 'Ask for a code first.'
    if verification.expires_at < timezone.now():
        return False, 'That code has expired. Ask for a new one.'
    if verification.attempts >= MAX_ATTEMPTS:
        return False, 'Too many wrong codes. Ask for a new one.'

    PhoneVerification.objects.filter(pk=verification.pk).update(attempts=F('attempts') + 1)
    if not hmac.compare_digest(verification.code_hash, _hash(verification.pk, digits, code)):
        left = MAX_ATTEMPTS - verification.attempts - 1
        return False, (f'That code is not right. {left} tr{"y" if left == 1 else "ies"} left.'
                       if left > 0 else 'Too many wrong codes. Ask for a new one.')

    now = timezone.now()
    PhoneVerification.objects.filter(pk=verification.pk).update(verified_at=now)
    person = Person.objects.filter(user=user).first()
    if person is not None:
        person.whatsapp_number = digits
        person.whatsapp_verified_at = now
        person.save(update_fields=['whatsapp_number', 'whatsapp_verified_at'])
    return True, f'{whatsapp.display(digits)} is verified.'


def verification_error(person, number):
    """The form error for an unverified number, or ``None`` when all is well."""
    if not required() or not number:
        return None
    if is_verified(person, number):
        return None
    return ('Verify this number: tap "Send code", then enter the code we send you on WhatsApp.')
