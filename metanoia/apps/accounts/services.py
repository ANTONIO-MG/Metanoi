"""Admin/staff enrolment overrides: put a person into or out of modules, and
attach educators to a programme. Self-registration lives in
:mod:`apps.learning.enrolment`."""

from django.db import transaction

from apps.learning.models import ProgrammeModule


def modules_for_programme(programme):
    """Active module offerings under ``programme``."""
    if programme is None:
        return ProgrammeModule.objects.none()
    return ProgrammeModule.objects.filter(
        programme=programme, is_active=True).order_by('order', 'name')


def enrol_educator(person, programme):
    """Add ``person`` to the teaching roster of every active module offering of
    ``programme``. Chat-group membership follows via the ProgrammeModule
    signals. Returns the offerings they now teach."""
    offerings = list(modules_for_programme(programme))
    for offering in offerings:
        offering.educators.add(person)
    return offerings


class MembershipChange:
    """What :func:`set_module_membership` did, for the admin's message.

    * ``added`` — ``ModuleEnrolment`` rows for the modules added;
    * ``trialled`` — those of them that started their 7-day free week now;
    * ``removed`` — ``ProgrammeModule`` offerings the person came off;
    * ``invoice`` — the invoice raised for the new free week, if any;
    * ``cancelled_invoices`` — unpaid invoices cancelled by the removal.
    """

    def __init__(self):
        """Start empty; :func:`set_module_membership` fills the lists in."""
        self.added = []
        self.trialled = []
        self.removed = []
        self.invoice = None
        self.cancelled_invoices = []

    @property
    def trial_ends(self):
        """The earliest end of the free weeks started by this change, or ``None``."""
        ends = [e.trial_ends_at for e in self.trialled if e.trial_ends_at]
        return min(ends) if ends else None

    def __iter__(self):
        """Iterate over the added rows (older callers treated the result as that list)."""
        return iter(self.added)

    def __len__(self):
        """The number of added rows."""
        return len(self.added)


def _never_started(enrolment):
    """True for a row that has never been trialled, paid or opened."""
    return (enrolment.status == enrolment.STATUS_LOCKED
            and enrolment.started_at is None and enrolment.trial_ends_at is None)


@transaction.atomic
def set_module_membership(person, *, add_ids=None, remove_ids=None, notify_person=True, actor=None,
                          undo_sink=None):
    """Admin/staff override: move ``person`` into (or out of) module offerings.

    Removals run first, so a programme both left and re-entered in the same
    switch ends up active.

    * **Closed:** removed enrolments are deleted (via
      :func:`apps.accounts.undo.delete_with_undo`, appended to ``undo_sink``) —
      removing one throws away its paid periods and access dates, so a copy is
      kept for undo; an emptied programme enrolment goes inactive; a wholly
      unpaid invoice for only those modules is cancelled. An invoice with any
      money already taken is left alone: a person decides, not a switch.
    * **Opened:** added priced modules never trialled or paid start the 7-day
      free week, with one (un-e-mailed) invoice so the day-before reminder still
      goes out. Previously trialled/paid modules get no second free week. A
      programme enrolment without a cohort is given the intake the schedule,
      chats and materials hang off — the same choice registration makes when
      the programme runs one intake.

    Returns a :class:`MembershipChange`.
    """
    change = MembershipChange()
    if person is None:
        return change

    from apps.finance.models import Invoice
    from apps.learning import enrolment as enrol
    from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment

    if remove_ids:
        closing = list(ModuleEnrolment.objects.filter(
            person=person, programme_module_id__in=list(remove_ids))
            .select_related('programme_module'))
        change.removed = [row.programme_module for row in closing]
        invoice_uids = {row.invoice_uid for row in closing if row.invoice_uid}
        programme_ids = {row.programme_module.programme_id for row in closing}
        if closing:
            from . import undo
            names = ', '.join(str(row.programme_module) for row in closing)
            action = undo.delete_with_undo(
                closing, summary=f'Removed {person} from {names}'[:255], actor=actor,
                target_user=person.user)
            if undo_sink is not None and action is not None:
                undo_sink.append(action)

        still_billed = set(ModuleEnrolment.objects.filter(
            invoice_uid__in=invoice_uids).values_list('invoice_uid', flat=True))
        for invoice in Invoice.objects.filter(public_id__in=invoice_uids - still_billed):
            if invoice.status in (Invoice.STATUS_PAID, Invoice.STATUS_CANCELLED):
                continue
            if invoice.amount_paid > 0:
                continue
            invoice.status = Invoice.STATUS_CANCELLED
            invoice.save(update_fields=['status'])
            change.cancelled_invoices.append(invoice)

        held = set(ModuleEnrolment.objects.filter(person=person)
                   .values_list('programme_module__programme_id', flat=True))
        ProgrammeEnrolment.objects.filter(
            person=person, programme_id__in=programme_ids - held, is_active=True,
        ).update(is_active=False)

    for offering in ProgrammeModule.objects.filter(
            pk__in=list(add_ids or [])).select_related('programme'):
        programme_enrolment, created = ProgrammeEnrolment.objects.get_or_create(
            person=person, programme=offering.programme, defaults={'is_active': True})
        fields = []
        if not created and not programme_enrolment.is_active:
            programme_enrolment.is_active = True
            fields.append('is_active')
        if programme_enrolment.cohort_id is None:
            cohort = enrol._resolve_cohort(offering.programme, None)
            if cohort is not None:
                programme_enrolment.cohort = cohort
                fields.append('cohort')
        if fields:
            programme_enrolment.save(update_fields=fields + ['updated_at'])

        enrolment, _ = ModuleEnrolment.objects.get_or_create(
            person=person, programme_module=offering,
            defaults={'price_at_enrolment': offering.price_per_month or 0})
        change.added.append(enrolment)
        if not offering.is_free and _never_started(enrolment):
            enrolment.start_trial()
            change.trialled.append(enrolment)

    unbilled = [e for e in change.trialled if not e.invoice_uid]
    if unbilled:
        change.invoice = enrol.invoice_for_modules(person, unbilled, send_to_student=False)

    if notify_person:
        transaction.on_commit(lambda: _tell_person(person, change))
    return change


def _tell_person(person, change):
    """One in-app notice about the switch. Best effort: never breaks enrolment."""
    user = getattr(person, 'user', None)
    if user is None or not (change.trialled or change.removed):
        return
    try:
        from django.urls import reverse
        from django.utils import timezone

        from apps.communication.services import notify
        lines = []
        if change.trialled:
            names = ', '.join(e.programme_module.display_name for e in change.trialled)
            ends = timezone.localtime(change.trial_ends)
            lines.append(f'Your free week has started for {names}. Everything is open until '
                         f'{ends:%A %-d %B, %H:%M}; pay the invoice before then to keep it open.')
        if change.removed:
            names = ', '.join(m.display_name for m in change.removed)
            lines.append(f'You have been moved off {names}.')
        notify(user, title='Your enrolment has changed', body=' '.join(lines),
               verb='enrolment-changed', level='info', url=reverse('learning:my-modules'))
    except Exception:  # pragma: no cover
        import logging
        logging.getLogger('apps').exception('enrolment change notice failed for %s', person)
