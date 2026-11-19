"""Undoing a recorded payment (and a voided invoice).

A payment is never deleted. Undoing one **voids** it: the row stays on the books
with who voided it, when and why, but every total, balance and status counts
completed payments only, so the money is simply no longer there. On top of
that, everything the payment *did* is rolled back:

* the invoice status (paid → partial / sent / overdue, from what is now paid);
* module access it opened — each :class:`~apps.learning.models.ModuleEnrolment`
  goes back to the status, trial, ``paid_from`` and ``paid_until`` it had, and
  the :class:`~apps.learning.models.PaidPeriod` rows (which keep weeks open) are
  removed; enrolments the payment created are removed;
* the onboarding gate (``Person.pending_invoice_uid``) is put back;
* the shop order behind the invoice goes back to unpaid, which also withdraws
  its downloads;
* the proof of payment is marked voided (it stays on the record, struck out).

How: the payment paths (:func:`apps.finance.services.settle_payment` and
:func:`apps.finance.access.grant_access`) run inside :func:`track_effects`,
which notes the state of all of the above before and after, and keeps only what
changed in the :class:`~apps.accounts.models.UndoAction`. A field that has been
changed again *since* (say a later payment extended the module) is left alone
and reported, rather than clobbered.

E-mails already sent (receipt, registration summary) cannot be unsent.

A PayFast payment is different: the money really moved through the gateway.
It can still be reversed (for a refund done in the PayFast dashboard, or a
payment booked to the wrong invoice), but only behind an explicit warning.

``ENROLMENT_FIELDS`` and ``ORDER_FIELDS`` are the fields snapshotted before and
after a payment.
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime

from django.db.models import Max
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.accounts import undo as undo_core

from . import models

logger = logging.getLogger('apps')

ENROLMENT_FIELDS = ('status', 'started_at', 'trial_ends_at', 'paid_from', 'paid_until')
ORDER_FIELDS = ('status', 'checked_out', 'paid_processed_at')


def _plain(value):
    """A JSON-safe value: dates and datetimes become ISO strings."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _person_for(invoice=None, person=None):
    """The learner profile to track: ``person``, else the invoice customer's."""
    if person is not None:
        return person
    customer_id = getattr(invoice, 'customer_id', None)
    if not customer_id:
        return None
    from apps.accounts.models import Person
    return Person.all_objects.filter(user_id=customer_id).first()


def _snapshot(invoice, person):
    """The state a payment can change, for comparing before and after."""
    from apps.learning.models import ModuleEnrolment, PaidPeriod, ProgrammeEnrolment

    state = {'enrolments': {}, 'programme_enrolments': [], 'paid_period_max': 0,
             'pending_invoice_uid': None, 'order': None, 'invoice_status': None}
    if person is not None:
        for row in ModuleEnrolment._base_manager.filter(person=person).values('pk', *ENROLMENT_FIELDS):
            pk = row.pop('pk')
            state['enrolments'][pk] = {k: _plain(v) for k, v in row.items()}
        state['programme_enrolments'] = list(
            ProgrammeEnrolment._base_manager.filter(person=person).values_list('pk', flat=True))
        state['pending_invoice_uid'] = (str(person.pending_invoice_uid)
                                        if person.pending_invoice_uid else None)
    state['paid_period_max'] = PaidPeriod._base_manager.aggregate(m=Max('pk'))['m'] or 0
    if invoice is not None:
        state['invoice_status'] = invoice.status
        if invoice.order_id:
            from apps.shop.models import Order
            row = Order._base_manager.filter(pk=invoice.order_id).values(*ORDER_FIELDS).first()
            if row:
                state['order'] = {'id': invoice.order_id, **{k: _plain(v) for k, v in row.items()}}
    return state


@contextmanager
def track_effects(invoice=None, person=None):
    """Note what a payment changes. Yields a dict filled in on exit."""
    from apps.learning.models import ModuleEnrolment, PaidPeriod

    person = _person_for(invoice, person)
    journal = {}
    try:
        before = _snapshot(invoice, person)
    except Exception:  # pragma: no cover - never block a payment over the journal
        logger.exception('finance.undo: could not snapshot before payment')
        before = None
    yield journal
    if before is None:
        return
    try:
        if person is not None:
            person.refresh_from_db(fields=['pending_invoice_uid'])
        if invoice is not None:
            invoice.refresh_from_db(fields=['status', 'order'])
        after = _snapshot(invoice, person)
        changed = []
        for pk, state in after['enrolments'].items():
            if pk in before['enrolments'] and before['enrolments'][pk] != state:
                changed.append({'id': pk, 'before': before['enrolments'][pk], 'after': state})
        journal.update({
            'person_id': getattr(person, 'pk', None),
            'invoice_id': getattr(invoice, 'pk', None),
            'invoice_status_before': before['invoice_status'],
            'enrolments': changed,
            'new_enrolments': [pk for pk in after['enrolments'] if pk not in before['enrolments']],
            'new_programme_enrolments': [pk for pk in after['programme_enrolments']
                                         if pk not in before['programme_enrolments']],
            'paid_periods': list(PaidPeriod._base_manager.filter(
                pk__gt=before['paid_period_max'],
                enrolment__in=ModuleEnrolment._base_manager.filter(person=person) if person else [])
                .values_list('pk', flat=True)),
        })
        if before['pending_invoice_uid'] != after['pending_invoice_uid']:
            journal['pending_invoice_uid'] = {'before': before['pending_invoice_uid'],
                                              'after': after['pending_invoice_uid']}
        if before['order'] and before['order'] != after['order']:
            journal['order'] = {'id': before['order']['id'],
                                'before': {k: before['order'][k] for k in ORDER_FIELDS},
                                'after': {k: (after['order'] or {}).get(k) for k in ORDER_FIELDS}}
    except Exception:  # pragma: no cover
        logger.exception('finance.undo: could not journal payment effects')


def record_payment(payment=None, proof=None, *, effects=None, by=None):
    """Write the UndoAction for a payment that was just recorded."""
    if payment is None and proof is None:
        return None
    invoice = getattr(payment, 'invoice', None) or getattr(proof, 'invoice', None)
    amount = getattr(payment, 'amount', None) or getattr(proof, 'amount', 0)
    customer = getattr(invoice, 'customer', None) or getattr(getattr(proof, 'person', None), 'user', None)
    who = (customer.get_full_name() or customer.email) if customer is not None else ''
    summary = f'R{amount} payment' + (f' on {invoice.number}' if invoice else '') + (f' for {who}' if who else '')
    payload = {'payment_id': getattr(payment, 'pk', None), 'proof_id': getattr(proof, 'pk', None),
               'effects': effects or {}}
    try:
        return undo_core.record(
            'payment', summary, object_ref=undo_core.ref(payment or proof), payload=payload,
            actor=by, target_user=customer)
    except Exception:  # pragma: no cover - a journal problem must not undo the payment
        logger.exception('finance.undo: could not record the undo for %s', summary)
        return None


def undo_for_payment(payment, *, by=None):
    """The open UndoAction for ``payment``; one is created for a payment
    recorded before undo existed (it can still be voided, but has no journal of
    the access it opened)."""
    action = undo_core.latest_for(payment, 'payment')
    if action is None and payment.status == models.InvoicePayment.STATUS_COMPLETED:
        action = record_payment(payment, payment.proofs.first(), effects={'legacy': True}, by=None)
    return action


def undo_for_proof(proof):
    """Undo the payment behind a proof of payment, or a verified proof's own grant."""
    if proof.payment_id:
        return undo_for_payment(proof.payment)
    action = undo_core.latest_for(proof, 'payment')
    if action is None and proof.status == models.ProofOfPayment.STATUS_VERIFIED:
        action = record_payment(None, proof, effects={'legacy': True}, by=None)
    return action


def _coerce(field, value):
    """Parse a journalled value back into the type of ``field``."""
    if value is None:
        return None
    if field in ('paid_from', 'paid_until'):
        return parse_date(value) if isinstance(value, str) else value
    if field in ('started_at', 'trial_ends_at', 'paid_processed_at'):
        return parse_datetime(value) if isinstance(value, str) else value
    return value


def _objects(action):
    """The payment and proof an undo action refers to (either may be ``None``)."""
    payload = action.payload or {}
    payment = (models.InvoicePayment.objects.filter(pk=payload.get('payment_id')).first()
               if payload.get('payment_id') else None)
    proof = (models.ProofOfPayment._base_manager.filter(pk=payload.get('proof_id')).first()
             if payload.get('proof_id') else None)
    return payment, proof


def _blocked(action):
    """Why the payment cannot be undone, or ``None`` when it can."""
    payment, proof = _objects(action)
    if payment is None and proof is None:
        return 'The payment no longer exists.'
    if payment is not None and payment.status == models.InvoicePayment.STATUS_VOIDED:
        return 'This payment has already been voided.'
    return ''


def _warning(action):
    """The warning shown before undoing a PayFast payment (the money really moved)."""
    payment, _ = _objects(action)
    if payment is not None and payment.is_gateway:
        return ('This payment was made online through PayFast — the money really moved. '
                'Reversing it here does NOT refund the customer: do the refund in the '
                'PayFast dashboard first. Only reverse a gateway payment that has been '
                'refunded, charged back, or booked to the wrong invoice.')
    return ''


def _describe(action):
    """The lines describing what undoing this payment will do."""
    payment, proof = _objects(action)
    effects = (action.payload or {}).get('effects') or {}
    lines = []
    if payment is not None:
        lines.append(f'Void the R{payment.amount} {payment.get_method_display().lower()} payment on '
                     f'{payment.invoice.number}; the invoice balance goes back up by R{payment.amount}.')
    elif proof is not None:
        lines.append(f'Void the R{proof.amount} proof of payment (no invoice was charged).')
    if effects.get('legacy'):
        lines.append('This payment was recorded before undo existed, so the module access it '
                     'opened is not known — check the student\'s modules afterwards.')
    else:
        if effects.get('enrolments') or effects.get('new_enrolments'):
            n = len(effects.get('enrolments') or []) + len(effects.get('new_enrolments') or [])
            lines.append(f'Take back the module access it opened ({n} module'
                         f'{"s" if n != 1 else ""}): status, trial and paid-until dates are '
                         f'restored, and the weeks it unlocked close again.')
        if effects.get('pending_invoice_uid'):
            lines.append('Put the "pay your registration invoice" step back for the student.')
        if effects.get('order'):
            lines.append('Set the shop order back to unpaid (its downloads are withdrawn).')
    lines.append('Mark the proof of payment / receipt as voided. The record stays, struck '
                 'out, with your reason. E-mails already sent cannot be recalled.')
    return lines


def _reverse_effects(effects):
    """Roll back journalled effects; return notes on what was left alone.

    A programme enrolment the payment created is dropped only if no module is
    left under it.
    """
    from apps.accounts.models import Person
    from apps.learning.models import ModuleEnrolment, PaidPeriod, ProgrammeEnrolment

    notes = []
    PaidPeriod._base_manager.filter(pk__in=effects.get('paid_periods') or []).delete()

    for change in effects.get('enrolments') or []:
        enrolment = ModuleEnrolment._base_manager.filter(pk=change['id']).first()
        if enrolment is None:
            continue
        current = {f: _plain(getattr(enrolment, f)) for f in ENROLMENT_FIELDS}
        if current != change['after']:
            notes.append(f'{enrolment.programme_module} changed again since — left as it is')
            continue
        for f in ENROLMENT_FIELDS:
            setattr(enrolment, f, _coerce(f, change['before'].get(f)))
        enrolment.save(update_fields=[*ENROLMENT_FIELDS, 'updated_at'])

    new = ModuleEnrolment._base_manager.filter(pk__in=effects.get('new_enrolments') or [])
    new.delete()
    new_programmes = ProgrammeEnrolment._base_manager.filter(
        pk__in=effects.get('new_programme_enrolments') or [])
    for pe in new_programmes:
        if not ModuleEnrolment._base_manager.filter(
                person_id=pe.person_id, programme_module__programme_id=pe.programme_id).exists():
            pe.delete()

    pending = effects.get('pending_invoice_uid')
    if pending and effects.get('person_id'):
        Person.all_objects.filter(pk=effects['person_id'], pending_invoice_uid__isnull=True).update(
            pending_invoice_uid=pending['before'])

    order = effects.get('order')
    if order:
        from apps.shop.models import Order
        row = Order._base_manager.filter(pk=order['id']).first()
        if row is not None:
            current = {f: _plain(getattr(row, f)) for f in ORDER_FIELDS}
            if current == order['after']:
                for f in ORDER_FIELDS:
                    setattr(row, f, _coerce(f, order['before'].get(f)))
                row.save(update_fields=[*ORDER_FIELDS, 'updated_at'])
            else:
                notes.append(f'order {row} changed again since — left as it is')
    return notes


def _perform(action, *, by, reason):
    """Void the payment and its proof, and roll back what it unlocked."""
    payment, proof = _objects(action)
    effects = (action.payload or {}).get('effects') or {}
    now = timezone.now()
    stamp = f'Voided {timezone.localtime(now):%Y-%m-%d %H:%M} by {by.get_full_name() or by.email}: {reason}'

    if payment is not None:
        payment.status = models.InvoicePayment.STATUS_VOIDED
        payment.voided_at = now
        payment.voided_by = by
        payment.void_reason = reason
        payment.save(update_fields=['status', 'voided_at', 'voided_by', 'void_reason', 'updated_at'])
    proofs = list(models.ProofOfPayment._base_manager.filter(payment=payment)) if payment else []
    if proof is not None and proof not in proofs:
        proofs.append(proof)
    for p in proofs:
        p.status = models.ProofOfPayment.STATUS_VOIDED
        p.note = (f'{p.note}\n\n{stamp}' if p.note else stamp)
        p.save(update_fields=['status', 'note', 'updated_at'])

    notes = _reverse_effects(effects) if not effects.get('legacy') else []

    if payment is not None:
        invoice = models.Invoice.all_objects.get(pk=payment.invoice_id)
        invoice.refresh_status()
        invoice.refresh_from_db()
        result = (f'Payment of R{payment.amount} on {invoice.number} voided — the invoice is '
                  f'{invoice.get_status_display().lower()} with R{invoice.balance} outstanding.')
    else:
        result = f'Proof of payment of R{proof.amount} voided.'
    if notes:
        result += ' Not reversed: ' + '; '.join(notes) + '.'
    return result


undo_core.register('payment', perform=_perform, describe=_describe, warning=_warning,
                   blocked=_blocked)


def record_invoice_void(invoice, *, status_before, notes_before, by):
    """Journal an invoice void so it can be undone."""
    return undo_core.record(
        'invoice_void', f'{invoice.number} voided', object_ref=undo_core.ref(invoice),
        payload={'invoice_id': invoice.pk, 'status': status_before, 'notes': notes_before},
        actor=by, target_user=invoice.customer)


def _perform_unvoid(action, *, by, reason):
    """Restore a voided invoice to its previous status and notes."""
    payload = action.payload or {}
    invoice = models.Invoice.all_objects.filter(pk=payload.get('invoice_id')).first()
    if invoice is None:
        raise undo_core.UndoError('The invoice no longer exists.')
    if invoice.status != models.Invoice.STATUS_CANCELLED:
        raise undo_core.UndoError(f'{invoice.number} is no longer voided.')
    invoice.status = payload.get('status') or models.Invoice.STATUS_SENT
    invoice.notes = payload.get('notes', invoice.notes)
    invoice.save(update_fields=['status', 'notes', 'updated_at'])
    invoice.refresh_status()
    return f'{invoice.number} is back — {invoice.get_status_display().lower()}.'


undo_core.register(
    'invoice_void', perform=_perform_unvoid,
    describe=lambda a: [f'Restore the invoice to “{(a.payload or {}).get("status", "sent")}” '
                        f'so it is payable again.'])


def record_extension(person, enrolments, days, *, effects=None, by=None):
    """Journal a free access extension (:func:`apps.finance.access.extend_access`) so it can be undone.
    """
    codes = ', '.join(e.programme_module.code for e in enrolments)
    who = person.user.get_full_name() or person.user.email
    try:
        return undo_core.record(
            'access_extension', f'{days}-day free access for {who} ({codes})',
            object_ref=undo_core.ref(person),
            payload={'days': days, 'effects': effects or {}},
            actor=by, target_user=person.user)
    except Exception:  # pragma: no cover - a journal problem must not undo the grant
        logger.exception('finance.undo: could not record the extension for %s', who)
        return None


def _perform_extension(action, *, by, reason):
    """Take back a free access extension."""
    notes = _reverse_effects((action.payload or {}).get('effects') or {})
    result = 'The free access was taken back — the modules are as they were before.'
    if notes:
        result += ' Not reversed: ' + '; '.join(notes) + '.'
    return result


def _describe_extension(action):
    """The lines describing what undoing an extension will do."""
    payload = action.payload or {}
    n = len((payload.get('effects') or {}).get('enrolments') or [])
    return [f'Take back the {payload.get("days")} days of free access on {n} module'
            f'{"s" if n != 1 else ""}: status, trial and paid-until dates go back to what '
            f'they were. A module changed again since is left as it is.']


undo_core.register('access_extension', perform=_perform_extension, describe=_describe_extension)
