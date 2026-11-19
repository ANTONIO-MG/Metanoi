"""Place the 2026 ``Metanoia Material`` archive on the platform.

    manage.py seed_2026_materials                     # place, and publish student material
    manage.py seed_2026_materials --draft             # place everything, publish nothing
    manage.py seed_2026_materials --calendar master.md  # take test dates from a master calendar
    manage.py seed_2026_materials --scan              # report what would be placed; no writes
    manage.py seed_2026_materials --remove            # take the seed out again

Safe to re-run: rows are keyed and corrected in place. See
:mod:`core.metanoia_materials.seed` for what goes where.
"""

import collections
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Seed the 2026 teaching material (lessons, quizzes, weeks, calendar) from Metanoia Material/.'

    def add_arguments(self, parser):
        parser.add_argument('--root', default=os.path.join(settings.BASE_DIR, 'Metanoia Material'))
        parser.add_argument('--draft', action='store_true', help='Place everything but publish nothing.')
        parser.add_argument('--calendar', help='A master calendar (.md) whose dates override the documents.')
        parser.add_argument('--scan', action='store_true', help='Only report what the archive holds.')
        parser.add_argument('--remove', action='store_true', help='Remove everything the seed created.')
        parser.add_argument('--as-user', default='learninghub@metanoiagroup.co.za',
                            help='Recorded as the author of what is created.')

    def handle(self, *args, **opts):
        from core.metanoia_materials import dates, seed
        from core.metanoia_materials.catalogue import scan

        if opts['remove']:
            seed.remove(out=self.stdout.write)
            self.stdout.write(self.style.SUCCESS('2026 seed removed.'))
            return

        root = opts['root']
        if not os.path.isdir(root):
            raise CommandError(f'No archive at {root}')

        if opts['calendar']:
            added = dates.load_master_calendar(opts['calendar'])
            self.stdout.write(f'Master calendar: {added} assessment date(s) loaded.')

        if opts['scan']:
            docs, report = scan(root)
            for k, v in sorted(report.items()):
                self.stdout.write(f'  {k:<36} {v}')
            for label, key in (('institution/programme', lambda d: f'{d.institution} {d.programme}'),
                               ('module', lambda d: d.module), ('kind', lambda d: d.kind),
                               ('audience', lambda d: d.audience)):
                counts = collections.Counter(key(d) for d in docs)
                self.stdout.write(f'{label}: ' + ', '.join(f'{k} {v}' for k, v in counts.most_common()))
            return

        actor = get_user_model().objects.filter(email__iexact=opts['as_user']).first()
        seeder = seed.Seeder(root, publish=not opts['draft'], actor=actor, out=self.stdout.write)
        report = seeder.run()
        for line in report.lines():
            self.stdout.write(line)

        self.stdout.write(self.style.SUCCESS(f'2026 seed complete. Manifest: {seed.MANIFEST}'))
