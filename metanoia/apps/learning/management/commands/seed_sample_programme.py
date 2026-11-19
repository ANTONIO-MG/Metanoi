"""Seed the Sample Programme pack from ``core/academic_spine_sample.py``.

    python manage.py seed_sample_programme             # build or top up — safe on a live database
    python manage.py seed_sample_programme --dry-run   # do it all, report, then roll back
    python manage.py seed_sample_programme --ai        # also let Claude write the unit summaries
    python manage.py seed_sample_programme --refresh   # rebuild lesson text from the pack (resets
                                                       # progress on those lessons — not for a live cohort)
    python manage.py seed_sample_programme --update    # a new version of the pack on a live cohort:
                                                       # re-read the text into the existing lessons
                                                       # (progress kept), replace changed files, hand
                                                       # renumbered files over to their new rows

Idempotent: every row is keyed (``sample/…``), so a second run creates nothing
and the totals printed at the end stay the same. No e-mail, notification, Teams
or YouTube call is made.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = ('Create or top up Metanoia Group → Sample Programme → Sample pack intake 2027: modules, weeks, '
            'lessons, files, sessions, calendar.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Roll everything back at the end.')
        parser.add_argument('--ai', action='store_true',
                            help='Ask Claude for unit summaries (needs ANTHROPIC_API_KEY; skipped if absent).')
        parser.add_argument('--refresh', action='store_true',
                            help='Rebuild lessons that already exist from the pack text.')
        parser.add_argument('--update', action='store_true',
                            help='Apply a new version of the pack in place, keeping student progress.')
        parser.add_argument('--host', help='E-mail of the user who hosts the live sessions '
                                           '(default: the first superuser).')
        parser.add_argument('--pack', help='Read the pack from this folder instead of fixtures/sample_programme.')

    def handle(self, *args, dry_run=False, ai=False, refresh=False, update=False, host=None, pack=None, **options):
        from core import academic_spine_sample as sample
        from core.programme_pack import seed_spine

        spine = dict(sample.SPINE)
        if host:
            spine['host_email'] = host
        say = self.stdout.write
        with transaction.atomic():
            builder, report, totals = seed_spine(spine, pack=pack, use_ai=ai, refresh=refresh, update=update, out=say)
            say(f'{builder.institution.code} → {builder.programme.code} ({builder.programme.full_name}) '
                f'→ cohort {builder.cohort.code} "{builder.cohort.name}" '
                f'{builder.cohort.start_date} – {builder.cohort.end_date}')
            for line in report.lines():
                say(line)
            say('Totals: ' + ', '.join(f'{k} {v}' for k, v in totals.items()))
            if dry_run:
                transaction.set_rollback(True)
                say(self.style.WARNING('Dry run — rolled back. Files copied to the file store are kept.'))
                return
        say(self.style.SUCCESS('The Sample Programme pack is seeded.'))
