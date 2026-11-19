"""Install the 2027 cohorts and their dated calendars (core.academic_spine.COHORT_CALENDAR).

Dry run by default: everything is written inside a transaction that is rolled
back, so the output is exactly what ``--apply`` would do.

    manage.py seed_cohort_calendar            # dry run
    manage.py seed_cohort_calendar --apply    # write
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = 'Create the 2027 cohorts and pre-populate the institution calendars with their dates.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes (default: dry run).')
        parser.add_argument('--update', action='store_true',
                            help='Also overwrite rows an admin has edited.')

    def handle(self, *args, apply=False, update=False, **options):
        from core.academic_spine import seed_cohort_calendar

        with transaction.atomic():
            stats = seed_cohort_calendar(update=update, out=self.stdout.write)
            if not apply:
                transaction.set_rollback(True)
        self.stdout.write(
            f"{'APPLIED' if apply else 'DRY RUN (nothing saved)'}: "
            f"{stats['cohorts']} cohort(s) created/filled, {stats['calendars']} calendar(s) created, "
            f"{stats['created']} event(s) created, {stats['updated']} updated, "
            f"{stats['unchanged']} unchanged")
