"""Seed any programme from its spine file (written by hand, or by the zip importer).

    python manage.py seed_programme_pack core/academic_spine_sample.py
    python manage.py seed_programme_pack core/academic_spine_acme.py --pack fixtures/acme --dry-run

Same builder, same keys, same idempotency as ``seed_sample_programme``.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Build (or top up) a programme from a spine seed file and its pack folder.'

    def add_arguments(self, parser):
        parser.add_argument('spine', help='Path to the spine .py file (defines SPINE).')
        parser.add_argument('--pack', help="The pack folder, if not the spine's own 'pack'.")
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--ai', action='store_true', help='Let Claude write unit summaries if configured.')
        parser.add_argument('--refresh', action='store_true', help='Rebuild existing lessons from the pack.')

    def handle(self, *args, spine, pack=None, dry_run=False, ai=False, refresh=False, **options):
        from core.programme_pack import codegen, seed_spine

        path = spine if os.path.isabs(spine) else os.path.join(settings.BASE_DIR, spine)
        if not os.path.exists(path):
            raise CommandError(f'No spine file at {path}')
        data = codegen.load(path)
        with transaction.atomic():
            builder, report, totals = seed_spine(data, pack=pack, use_ai=ai, refresh=refresh,
                                                 out=self.stdout.write)
            for line in report.lines():
                self.stdout.write(line)
            self.stdout.write('Totals: ' + ', '.join(f'{k} {v}' for k, v in totals.items()))
            if dry_run:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING('Dry run — rolled back.'))
                return
        self.stdout.write(self.style.SUCCESS(f'{builder.programme.full_code} · {builder.cohort.code} seeded.'))
