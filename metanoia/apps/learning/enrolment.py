"""Registration & billing services for the institution → programme → module spine.

Modules are created LOCKED and unlocked by the 7-day trial or by paying their
invoice (PayFast or EFT). Everything is idempotent so the wizard can be replayed
and the payment webhook can fire more than once.

Import cycle note: finance is imported lazily inside the functions (learning must
not import finance at module load), and the invoice is linked to each enrolment
by its ``public_id`` (:attr:`ModuleEnrolment.invoice_uid`), not an FK.
"""
from decimal import Decimal

from django.db import transaction

from apps.learning.models import ProgrammeModule
from .models import ModuleEnrolment, ProgrammeEnrolment


def _resolve_cohort(programme, cohort_id):
    """The Cohort an enrolment belongs to.

    A numeric ``cohort_id`` selects that intake. Otherwise, when exactly one intake
    is running, that is the student's — leaving them unplaced would show them an
    empty schedule, which is built per intake. With several running intakes the
    cohort is left unset. For a programme with no named intakes, the current
    academic year is get-or-created as its cohort (per-cohort chats/schedule hang
    off it).
    """
    from django.utils import timezone

    from .models import Cohort
    if cohort_id and str(cohort_id).isdigit():
        chosen = Cohort.objects.filter(pk=int(cohort_id), programme=programme).first()
        if chosen is not None:
            return chosen
    active = list(programme.cohorts.filter(is_active=True)[:2])
    if len(active) == 1:
        return active[0]
    if active:
        return None
    year = timezone.now().year
    cohort, _ = Cohort.objects.get_or_create(
        programme=programme, code=str(year), defaults={'name': f'{year} intake'})
    return cohort


@transaction.atomic
def register_modules(person, programme, module_ids, cohort_id=None):
    """Create the :class:`ProgrammeEnrolment` and one LOCKED
    :class:`ModuleEnrolment` per selected module, reusing existing rows.

    A defaulted cohort never moves someone out of the intake they are already in;
    only an explicitly chosen ``cohort_id`` does. The General module is open to
    everyone on the programme without an enrolment row
    (:mod:`apps.learning.general`),
    so it is never registered for. Returns the ModuleEnrolment rows.
    """
    prog_enrolment, _ = ProgrammeEnrolment.objects.get_or_create(
        person=person, programme=programme, defaults={'is_active': True})
    cohort = _resolve_cohort(programme, cohort_id)
    chosen = bool(cohort_id and str(cohort_id).isdigit())
    if cohort is not None and prog_enrolment.cohort_id not in (None, cohort.pk) and not chosen:
        cohort = None
    if cohort is not None and prog_enrolment.cohort_id != cohort.pk:
        prog_enrolment.cohort = cohort
        prog_enrolment.save(update_fields=['cohort', 'updated_at'])
    modules = list(ProgrammeModule.objects.without_general().filter(
        pk__in=module_ids, programme=programme, is_active=True))
    enrolments = []
    for module in modules:
        enrolment, _ = ModuleEnrolment.objects.get_or_create(
            person=person, programme_module=module,
            defaults={'price_at_enrolment': module.price_per_month or 0})
        enrolments.append(enrolment)
    return enrolments


def modules_total(enrolments):
    """Sum of the monthly prices of a set of ModuleEnrolments (Decimal)."""
    return sum((Decimal(str(e.price or 0)) for e in enrolments), Decimal('0'))


@transaction.atomic
def start_trial(enrolments):
    """Begin the 7-day free trial ("week one") on each still-locked module."""
    for enrolment in enrolments:
        if enrolment.status == ModuleEnrolment.STATUS_LOCKED:
            enrolment.start_trial()
    return enrolments


@transaction.atomic
def invoice_for_modules(person, enrolments, *, send_to_student=False):
    """Raise ONE finance invoice with a line per (priced) module and link it to
    each of those ModuleEnrolments so payment unlocks exactly them.

    Priced the way the cart prices them: a bundle ("All 4 modules") is one discount
    on the whole invoice, and each module line is billed gross.

    Returns the invoice, or ``None`` when nothing is chargeable. With
    ``send_to_student`` the PDF is e-mailed (the EFT / pay-later path); e-mail is
    best-effort — a failure is reported as ``MAIL-5001`` and the invoice still
    stands.
    """
    from apps.finance.models import Invoice, InvoiceItem

    priced = [e for e in enrolments if (e.price or 0) > 0]
    if not priced:
        return None

    from apps.shop.pricing import quote_modules

    quote = quote_modules([(e.programme_module, Decimal(str(e.price or 0))) for e in priced])
    invoice = Invoice.objects.create(
        customer=person.user, created_by=person.user, status=Invoice.STATUS_SENT,
        discount_amount=quote['discount'],
        discount_label=' + '.join(bundle.name for bundle in quote['applied'])[:120],
        discount_auto=True)
    for enrolment, line in zip(priced, quote['lines']):
        module = enrolment.programme_module
        InvoiceItem.objects.create(
            invoice=invoice,
            description=f'{module.display_name} ({module.code}) — per month'[:255],
            quantity=1, unit_price=line['gross'],
        )
    invoice.recalc_total()
    invoice.refresh_status()
    ModuleEnrolment.objects.filter(pk__in=[e.pk for e in priced]).update(
        invoice_uid=invoice.public_id)

    if send_to_student:
        try:
            from apps.finance.emails import send_invoice_email
            send_invoice_email(invoice)
        except Exception as exc:
            from core.errors import report
            report('MAIL-5001', exc=exc, context={'invoice': str(invoice.public_id), 'kind': 'module-invoice'})
    return invoice


@transaction.atomic
def activate_modules_for_invoice(invoice):
    """Unlock every module an invoice was raised for — called from the
    ``invoice_paid`` signal when the invoice settles (PayFast or a manual mark).
    Idempotent: activating an already-active module just extends it."""
    rows = list(ModuleEnrolment.objects.filter(invoice_uid=invoice.public_id))
    for enrolment in rows:
        enrolment.activate(months=1)
    return rows


@transaction.atomic
def unlock_single_module(person, programme_module):
    """Ensure a locked module has an enrolment + an invoice to pay (reusing a
    linked unpaid invoice), and start its free week if it never had one.

    Every priced module starts with its 7-day free week, whichever way the student
    pays and whether or not the payment goes through — the same rule registration
    follows. Only a module never trialled or paid gets one.

    Returns (enrolment, invoice); invoice is ``None`` for a free module.
    """
    ProgrammeEnrolment.objects.get_or_create(
        person=person, programme=programme_module.programme, defaults={'is_active': True})
    enrolment, _ = ModuleEnrolment.objects.get_or_create(
        person=person, programme_module=programme_module,
        defaults={'price_at_enrolment': programme_module.price_per_month or 0})
    if (not programme_module.is_free and enrolment.status == ModuleEnrolment.STATUS_LOCKED
            and enrolment.started_at is None and enrolment.trial_ends_at is None):
        enrolment.start_trial()

    invoice = None
    if enrolment.invoice_uid:
        from apps.finance.models import Invoice
        invoice = (Invoice.objects.filter(public_id=enrolment.invoice_uid)
                   .exclude(status=Invoice.STATUS_PAID)
                   .exclude(status=Invoice.STATUS_CANCELLED).first())
    if invoice is None:
        invoice = invoice_for_modules(person, [enrolment])
    return enrolment, invoice
