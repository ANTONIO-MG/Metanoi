"""PayFast payment-gateway integration (South Africa).

PayFast works by **redirecting** the buyer to PayFast with a signed set of form
fields; PayFast then notifies us server-to-server via an **ITN** (Instant
Transaction Notification) POST to our ``notify_url``, which we validate and use
to mark the invoice paid.

Everything is driven from settings / ``.env``.

    PAYFAST_SANDBOX = True
    PAYFAST_MERCHANT_ID = "..."      # from your PayFast (sandbox or live) account
    PAYFAST_MERCHANT_KEY = "..."     # from your PayFast (sandbox or live) account
    PAYFAST_PASSPHRASE = "..."       # must match the passphrase set in the PayFast dashboard

**Zero-config sandbox:** with no merchant id/key configured (and DEBUG on), we
**simulate** a successful payment locally (see :func:`simulate_in_sandbox`) —
PayFast's shared sandbox merchant rejects unauthenticated signatures and its ITN
can't reach ``localhost``. For the real redirect, use your own sandbox account
at https://sandbox.payfast.co.za and set the three settings above.

Docs: https://developers.payfast.co.za/
"""

import hashlib
import logging
import urllib.parse

from django.conf import settings
from core.utils import site_base
from django.urls import reverse

logger = logging.getLogger('apps')

def is_sandbox():
    """True while PayFast runs against its sandbox (the default)."""
    return bool(getattr(settings, 'PAYFAST_SANDBOX', True))


def process_url():
    """The PayFast checkout URL for the current mode."""
    host = 'sandbox.payfast.co.za' if is_sandbox() else 'www.payfast.co.za'
    return f'https://{host}/eng/process'


def _validate_url():
    """The PayFast ITN server-validation URL for the current mode."""
    host = 'sandbox.payfast.co.za' if is_sandbox() else 'www.payfast.co.za'
    return f'https://{host}/eng/query/validate'


def _has_real_credentials():
    """True when a real PayFast merchant id + key are configured (sandbox or live)."""
    return bool((getattr(settings, 'PAYFAST_MERCHANT_ID', '') or '').strip()
                and (getattr(settings, 'PAYFAST_MERCHANT_KEY', '') or '').strip())


def simulate_in_sandbox():
    """True when checkout should be simulated locally instead of redirecting to
    PayFast — the zero-config sandbox case (no merchant credentials set).

    ``DEBUG`` is part of the condition on purpose: settling an invoice on a bare
    GET would otherwise let anyone with their own invoice's ``public_id`` (it is
    in their pay link) enrol for free while ``PAYFAST_SANDBOX`` is left true.
    :mod:`apps.finance.checks` also refuses to start in that combination.
    """
    return is_sandbox() and not _has_real_credentials() and bool(settings.DEBUG)


def _merchant_id():
    """The configured merchant id."""
    return (getattr(settings, 'PAYFAST_MERCHANT_ID', '') or '').strip()


def _merchant_key():
    """The configured merchant key."""
    return (getattr(settings, 'PAYFAST_MERCHANT_KEY', '') or '').strip()


def _passphrase():
    """The configured signature passphrase."""
    return (getattr(settings, 'PAYFAST_PASSPHRASE', '') or '').strip()


def _signature(fields, with_passphrase=True):
    """MD5 signature over non-empty fields in the given order (PayFast spec)."""
    parts = []
    for key, value in fields.items():
        if key == 'signature' or value is None:
            continue
        value = str(value).strip()
        if value == '':
            continue
        parts.append(f'{key}={urllib.parse.quote_plus(value)}')
    payload = '&'.join(parts)
    passphrase = _passphrase()
    if with_passphrase and passphrase:
        payload += f'&passphrase={urllib.parse.quote_plus(passphrase)}'
    return hashlib.md5(payload.encode('utf-8')).hexdigest()


def _customer_names(customer):
    """Best-effort (first, last) for PayFast — it rejects an e-mail as name_first.

    Falls back to the part of the username/e-mail before ``@`` so we never send an
    address (with its ``@``) into a name field.
    """
    first = (getattr(customer, 'first_name', '') or '').strip()
    last = (getattr(customer, 'last_name', '') or '').strip()
    if not first:
        local = (customer.get_username() or '').split('@')[0].replace('.', ' ').strip()
        first = (local or 'Customer').title()
    return first[:100], last[:100]


def build_checkout(invoice):
    """Return ``{'process_url', 'fields'}`` to render an auto-submitting form
    that redirects the buyer to PayFast to pay ``invoice``'s outstanding balance."""
    base = site_base()
    customer = invoice.customer
    name_first, name_last = _customer_names(customer)
    fields = {
        'merchant_id': _merchant_id(),
        'merchant_key': _merchant_key(),
        'return_url': base + reverse('finance:payfast-return') + f'?inv={invoice.public_id}',
        'cancel_url': base + reverse('finance:payfast-cancel') + f'?inv={invoice.public_id}',
        'notify_url': base + reverse('finance:payfast-notify'),
        'name_first': name_first,
        'name_last': name_last,
        'email_address': (getattr(customer, 'email', '') or '')[:255],
        'm_payment_id': str(invoice.public_id),
        'amount': f'{invoice.balance:.2f}',
        'item_name': f'Invoice {invoice.number}'[:100],
    }
    fields['signature'] = _signature(fields)
    return {'process_url': process_url(), 'fields': fields}


def verify_itn(post_data):
    """Validate an ITN POST: signature + server confirmation + COMPLETE status.

    ``post_data`` is the request's POST QueryDict (order preserved). Returns
    ``True`` only when PayFast confirms the notification is genuine.

    The checks run in order:

    1. The signature is recomputed over the received fields, in order. A
       *missing* signature is a forgery, not a pass -- never guard this with
       ``if received and ...``.
    2. The payment must be ``COMPLETE``.
    3. Server confirmation: the data is posted back to PayFast, which must answer
       ``VALID``. This is an *exact* match: the rejection reply ``INVALID``
       contains ``VALID``, so a substring test would accept it.
    """
    data = {key: post_data.get(key) for key in post_data.keys()}

    received = (data.get('signature') or '').strip()
    if not received:
        logger.warning('PayFast ITN carried no signature — rejected')
        return False
    if _signature(data) != received:
        logger.warning('PayFast ITN signature mismatch')
        return False

    if (data.get('payment_status') or '').upper() != 'COMPLETE':
        return False

    try:
        import requests
        resp = requests.post(_validate_url(), data=post_data.dict() if hasattr(post_data, 'dict') else dict(post_data),
                             timeout=10, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        if resp.text.strip().upper() != 'VALID':
            logger.warning('PayFast ITN server confirmation failed: %s', resp.text[:200])
            return False
    except Exception:  # pragma: no cover - network error
        logger.exception('PayFast ITN server confirmation error')
        return False

    return True
