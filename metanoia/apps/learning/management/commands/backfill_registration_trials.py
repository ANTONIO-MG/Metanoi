"""Give "pay now" registrants whose payment never cleared their free week.

Repairs rows created before :func:`apps.accounts.views.register_review` started
the trial on the "Pay now" path too (those students were left fully LOCKED if
payment never cleared).

A module enrolment qualifies when **all** of these hold:

* it is still ``locked`` and has never been trialled or started
  (``trial_ends_at`` and ``started_at`` both empty);
* it was created as part of the student's registration — within
  ``REGISTRATION_WINDOW`` of the programme enrolment — not by the later
  "unlock a module" flow;
* the invoice it is linked to is still owed (not paid, not cancelled,
  balance above zero);
* its seven days, counted from registration, have **not** run out yet.

``REGISTRATION_WINDOW`` is ten minutes: registration creates the programme
enrolment and its module enrolments in one request, so anything much later came
from another flow.

The trial is anchored at the enrolment's ``created_at`` (sign-up), so it ends
exactly when a trial started at registration would have. Rows whose week has
already passed are listed but left locked — nobody gets access back after the
window has closed.

Dry run by default; pass ``--apply`` to write.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

REGISTRATION_WINDOW = timedelta(minutes=10)


def candidates(now=None):
    """``(eligible, window_passed)`` lists of ModuleEnrolment rows."""
    from apps.finance.models import Invoice
    from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment

    now = now or timezone.now()
    rows = (ModuleEnrolment.objects
            .filter(status=ModuleEnrolment.STATUS_LOCKED,
                    trial_ends_at__isnull=True, started_at__isnull=True,
                    invoice_uid__isnull=False)
            .select_related('person__user', 'programme_module__programme')
            .order_by('created_at'))
    invoices = {inv.public_id: inv for inv in Invoice.objects.filter(
        public_id__in={r.invoice_uid for r in rows})}

    eligible, passed = [], []
    for row in rows:
        invoice = invoices.get(row.invoice_uid)
        if invoice is None or invoice.status in (Invoice.STATUS_PAID, Invoice.STATUS_CANCELLED):
            continue
        if invoice.balance <= 0:
            continue
        registration = ProgrammeEnrolment.objects.filter(
            person_id=row.person_id, programme=row.programme_module.programme).first()
        if registration is None or abs(row.created_at - registration.created_at) > REGISTRATION_WINDOW:
            continue
        ends = row.created_at + timedelta(days=ModuleEnrolment.TRIAL_DAYS)
        (eligible if ends > now else passed).append(row)
    return eligible, passed


class Command(BaseCommand):
    """``manage.py backfill_registration_trials [--apply]`` — see the module docstring.
    """
    help = ('Start the 7-day free week, counted from registration, for "pay now" '
            'registrants whose payment never cleared. Dry run unless --apply.')

    def add_arguments(self, parser):
        """Add ``--apply`` (write the changes; the default is a dry run)."""
        parser.add_argument('--apply', action='store_true',
                            help='Write the changes (default is a dry run).')

    def _line(self, row, ends):
        """One report line for an enrolment: who, which module, when registered, when
        the free week ends.
        """
        person = row.person
        user = person.user
        name = f'{user.first_name} {user.last_name}'.strip() or user.username
        return (f'  Person {person.pk} {name} <{user.email}> · '
                f'{row.programme_module.programme.full_code} {row.programme_module.code} · '
                f'registered {timezone.localtime(row.created_at):%Y-%m-%d %H:%M} · '
                f'free week ends {timezone.localtime(ends):%Y-%m-%d %H:%M}')

    def handle(self, *args, **options):
        """List the eligible and lapsed enrolments; with ``--apply`` start the free week
        on the eligible ones.
        """
        from apps.learning.models import ModuleEnrolment

        days = timedelta(days=ModuleEnrolment.TRIAL_DAYS)
        eligible, passed = candidates()

        self.stdout.write(f'{len(eligible)} enrolment(s) to start on the free week:')
        for row in eligible:
            self.stdout.write(self._line(row, row.created_at + days))
        self.stdout.write(f'{len(passed)} enrolment(s) whose week has already passed '
                          '(left locked):')
        for row in passed:
            self.stdout.write(self._line(row, row.created_at + days))

        if not options['apply']:
            self.stdout.write('Dry run — nothing written. Re-run with --apply.')
            return
        with transaction.atomic():
            for row in eligible:
                row.start_trial(now=row.created_at)
        self.stdout.write(self.style.SUCCESS(f'Started the free week on {len(eligible)} enrolment(s).'))
