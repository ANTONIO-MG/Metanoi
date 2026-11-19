"""Backfill the completion records behind the Reports page.

Records are normally written as work happens — the assessment post-save signal
and every visit to /reports/ — so this is for history that predates the feature,
or after a bulk import that bypassed the signal. Safe to re-run: records are
keyed per student, so a second run only adds what is new.

    python manage.py record_completions                 # every student
    python manage.py record_completions --student 64    # one student (user id)
    python manage.py record_completions --dry-run       # count, write nothing
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Record what every student has completed (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--student', type=int, default=None, help='Only this user id.')
        parser.add_argument('--dry-run', action='store_true', help='Report only, write nothing.')

    def handle(self, *args, **options):
        from apps.reports import completions
        from apps.reports.progress_board import load

        students = (get_user_model().objects
                    .filter(profile__user_type='student', profile__preview_of__isnull=True,
                            is_active=True)
                    .order_by('pk'))
        if options['student']:
            students = students.filter(pk=options['student'])

        total_new = total_updated = people = 0
        for user in students:
            if options['dry_run']:
                data = load(user)
                existing = set(completions.Record.objects.filter(student=user).values_list('key', flat=True))
                new = sum(1 for c in completions._candidates(data) if c['key'] not in existing)
                updated = 0
            else:
                new, updated = completions.record_completions(user)
            if new or updated:
                people += 1
                self.stdout.write(f'  {user.get_full_name() or user.pk}: {new} new, {updated} updated')
            total_new += new
            total_updated += updated

        verb = 'Would record' if options['dry_run'] else 'Recorded'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_new} completion(s), refreshed {total_updated}, '
            f'across {people} of {students.count()} student(s).'))
