"""Granting module access, and the payment that justified it.

A student's modules unlock in exactly three ways, and every one of them leaves a
money trail:

1. **The 7-day trial** — free, automatic, expires on its own
   (:meth:`apps.learning.models.ModuleEnrolment.start_trial`).
2. **PayFast settles the invoice** — the gateway callback activates the modules
   and :func:`record_gateway_proof` files the proof automatically.
3. **A staff member grants access manually** — :func:`grant_access`, which is
   the only manual route, and which *requires* a payment to be recorded in the
   same call.

Granting access and recording the money are one operation, so no path opens a
student's account without a record of why. Staff-only: the views are behind an
admin/staff check, and nothing student-facing reaches these functions.

Free extensions ("pay later") are the one manual route that opens access
*without* a payment: staff give a student 7 or 30 days (``EXTENSION_DAYS``) so
they can carry on while the money is sorted out. Nothing is booked -- no proof,
no payment -- and an invoice paid later unlocks the modules exactly as it would
have anyway. Every extension is undoable.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import models


def _person_of(invoice):
    """The learner profile of the invoice's customer, or ``None``."""
    return getattr(getattr(invoice, 'customer', None), 'profile', None)


@transaction.atomic
def grant_access(person, *, amount, months=1, modules=None, granted_by=None,
                 document=None, method='bank', reference='', paid_on=None, note='',
                 invoice=None, source=None):
    """Open ``person``'s modules for ``months``, recording the payment behind it.

    ``modules`` is an iterable of :class:`~apps.learning.models.ModuleEnrolment`;
    omit it and every locked or expired module the person has is granted. Pass
    ``document`` (an uploaded image or PDF) to file the deposit slip with it.

    Returns ``(proof, payment, [module_enrolments])``. ``payment`` is ``None``
    when there was no invoice to book the money against — the proof still records
    it, so the amount is never lost. ``proof.undo_action`` is the
    :class:`~apps.accounts.models.UndoAction` that takes the whole grant back.
    """
    from . import undo

    with undo.track_effects(invoice, person) as effects:
        proof, payment, modules = _grant_access(
            person, amount=amount, months=months, modules=modules, granted_by=granted_by,
            document=document, method=method, reference=reference, paid_on=paid_on,
            note=note, invoice=invoice, source=source)
    proof.undo_action = undo.record_payment(payment, proof, effects=effects, by=granted_by)
    return proof, payment, modules


def _grant_access(person, *, amount, months, modules, granted_by, document, method,
                  reference, paid_on, note, invoice, source):
    """The unguarded core of :func:`grant_access`.

    The payment is booked against the invoice named, else the person's most recent
    unpaid one, so a manual EFT lands on the bill it was meant for.
    """
    from apps.learning.models import ModuleEnrolment

    amount = Decimal(str(amount or 0))
    months = max(1, int(months or 1))
    if modules is None:
        modules = list(ModuleEnrolment.objects.filter(person=person).exclude(
            status=ModuleEnrolment.STATUS_ACTIVE))
    else:
        modules = list(modules)

    if invoice is None and person is not None:
        invoice = (models.Invoice.objects
                   .filter(customer=person.user)
                   .exclude(status__in=[models.Invoice.STATUS_PAID, models.Invoice.STATUS_CANCELLED])
                   .order_by('-created_at').first())

    payment = None
    if invoice is not None and amount > 0:
        payment = models.InvoicePayment.objects.create(
            invoice=invoice, amount=amount, method=method,
            gateway=models.InvoicePayment.GATEWAY_MANUAL,
            status=models.InvoicePayment.STATUS_COMPLETED,
            reference=reference, paid_at=timezone.now(),
            recorded_by=granted_by if getattr(granted_by, 'pk', None) else None)
        invoice.refresh_status()

    proof = models.ProofOfPayment(
        person=person, invoice=invoice, payment=payment,
        source=source or (models.ProofOfPayment.SOURCE_UPLOAD if document
                          else models.ProofOfPayment.SOURCE_MANUAL),
        status=models.ProofOfPayment.STATUS_VERIFIED,
        amount=amount, paid_on=paid_on or date.today(), reference=reference, note=note,
        recorded_by=granted_by, verified_by=granted_by, verified_at=timezone.now(),
        granted_months=months)
    if document is not None:
        proof.original_name = getattr(document, 'name', '')[:255]
        proof.content_type = getattr(document, 'content_type', '')[:100]
        proof.document = document
    proof.save()

    for enrolment in modules:
        enrolment.activate(months=months)
    return proof, payment, modules


def record_gateway_proof(invoice, payment):
    """File the proof for a payment the gateway settled.

    Called from the PayFast fulfilment path. There is no document to attach —
    the gateway reference is the evidence — and no staff member to credit,
    because nobody keyed it in.
    """
    person = _person_of(invoice)
    if person is None:
        return None
    reference = (getattr(payment, 'gateway_ref', '') or getattr(payment, 'reference', '')
                 or str(invoice.public_id))
    online = getattr(payment, 'gateway', '') == models.InvoicePayment.GATEWAY_PAYFAST
    staff = getattr(payment, 'recorded_by', None)
    proof, _ = models.ProofOfPayment.objects.get_or_create(
        person=person, payment=payment,
        defaults={
            'invoice': invoice,
            'source': (models.ProofOfPayment.SOURCE_GATEWAY if online or staff is None
                       else models.ProofOfPayment.SOURCE_MANUAL),
            'status': models.ProofOfPayment.STATUS_VERIFIED,
            'amount': getattr(payment, 'amount', 0) or 0,
            'paid_on': timezone.localdate(getattr(payment, 'paid_at', None) or timezone.now()),
            'reference': reference,
            'note': ('Settled online — recorded automatically by the payment gateway.'
                     if online or staff is None else 'Recorded by staff against the invoice.'),
            'recorded_by': staff, 'verified_by': staff,
            'verified_at': timezone.now(),
        })
    return proof


def payment_history(person):
    """Everything on record for this student, newest first — for their profile
    page and for the staff view of the account."""
    return (models.ProofOfPayment.objects
            .filter(person=person)
            .select_related('invoice', 'payment', 'recorded_by')
            .order_by('-paid_on', '-created_at'))


def access_state(person):
    """A one-glance summary of where this student stands.

    ``{'status': 'active'|'trial'|'locked'|'none', 'until': date|None,
    'days_left': int|None, 'modules': int, 'locked_modules': int}``.
    """
    from apps.learning.models import ModuleEnrolment

    enrolments = list(ModuleEnrolment.objects.filter(person=person))
    if not enrolments:
        return {'status': 'none', 'until': None, 'days_left': None,
                'modules': 0, 'locked_modules': 0}

    unlocked = [e for e in enrolments if e.is_unlocked]
    locked = [e for e in enrolments if not e.is_unlocked]
    active = [e for e in unlocked if e.status == ModuleEnrolment.STATUS_ACTIVE]
    trial = [e for e in unlocked if e.status == ModuleEnrolment.STATUS_TRIAL]

    if active:
        until = max((e.paid_until for e in active if e.paid_until), default=None)
        status = 'active'
    elif trial:
        ends = max((e.trial_ends_at for e in trial if e.trial_ends_at), default=None)
        until = timezone.localdate(ends) if ends else None
        status = 'trial'
    else:
        until, status = None, 'locked'

    days_left = (until - timezone.localdate()).days if until else None
    return {'status': status, 'until': until, 'days_left': days_left,
            'modules': len(enrolments), 'locked_modules': len(locked)}


EXTENSION_DAYS = (7, 30)


def can_extend(enrolment):
    """True when a free extension means something for ``enrolment``: not a
    free or General module (always open), and not an open-ended grant (an
    ACTIVE module with no end date, e.g. the one-on-one track)."""
    module = enrolment.programme_module
    if module.is_free or module.is_general:
        return False
    return not (enrolment.status == enrolment.STATUS_ACTIVE and enrolment.paid_until is None)


def _extend_one(enrolment, days, now):
    """Extend one enrolment by ``days`` and return it.

    A paid, running enrolment has its end date pushed out, with no
    :class:`PaidPeriod`: these days were not paid for, so they do not stay open once
    access lapses. A locked, expired or lapsed enrolment, or one on a trial, is
    opened as free access, added to whatever is left of a live trial.
    """
    from datetime import timedelta

    today = timezone.localdate(now)
    if (enrolment.status == enrolment.STATUS_ACTIVE and enrolment.paid_until
            and enrolment.paid_until >= today):
        enrolment.paid_until = enrolment.paid_until + timedelta(days=days)
        enrolment.save(update_fields=['paid_until', 'updated_at'])
        return enrolment
    running = (enrolment.status == enrolment.STATUS_TRIAL and enrolment.trial_ends_at
               and enrolment.trial_ends_at > now)
    start = enrolment.trial_ends_at if running else now
    enrolment.status = enrolment.STATUS_TRIAL
    enrolment.started_at = enrolment.started_at or now
    enrolment.trial_ends_at = start + timedelta(days=days)
    enrolment.save(update_fields=['status', 'started_at', 'trial_ends_at', 'updated_at'])
    return enrolment


def extend_access(person, days, *, enrolments=None, by=None):
    """Give ``person`` ``days`` (7 or 30) of free access to their modules.

    ``enrolments`` limits it to those :class:`ModuleEnrolment` rows (of this
    person); omit it for every module that can be extended. Returns
    ``(extended, undo_action)``.
    """
    from apps.learning.models import ModuleEnrolment

    from . import undo

    if days not in EXTENSION_DAYS:
        raise ValueError(f'Access can be extended by {" or ".join(map(str, EXTENSION_DAYS))} days.')
    qs = ModuleEnrolment.objects.filter(person=person).select_related('programme_module__module')
    if enrolments is not None:
        qs = qs.filter(pk__in=[getattr(e, 'pk', e) for e in enrolments])
    rows = [e for e in qs if can_extend(e)]
    if not rows:
        return [], None
    now = timezone.now()
    with transaction.atomic():
        with undo.track_effects(None, person) as effects:
            for enrolment in rows:
                _extend_one(enrolment, days, now)
    action = undo.record_extension(person, rows, days, effects=effects, by=by)
    return rows, action
