"""List module materials whose file is truncated, so they can be re-uploaded.

    python manage.py check_material_files                 # summary + table
    python manage.py check_material_files --csv out.csv   # the full list

A truncated file is one that ends before a PDF's ``%%EOF`` or an Office file's
zip directory — see :mod:`apps.learning.file_check`. Re-upload the original on
the module builder (Build schedule → the material → replace file); the listing
picks the new file up straight away.
"""

import csv

from django.core.management.base import BaseCommand

from apps.learning.file_check import file_is_complete
from apps.learning.models import ModuleMaterial


class Command(BaseCommand):
    help = 'Report module materials whose file is truncated (needs re-upload).'

    def add_arguments(self, parser):
        parser.add_argument('--csv', help='Write the full list to this CSV path.')

    def handle(self, *args, **opts):
        qs = (ModuleMaterial.objects.exclude(file='')
              .select_related('phase__programme_module__programme', 'week')
              .order_by('phase__programme_module_id', 'phase__sequence', 'pk'))
        rows = []
        for m in qs:
            if file_is_complete(m.file):
                continue
            try:
                size = m.file.size
            except Exception:
                size = None
            offering = m.phase.programme_module
            rows.append({
                'material_id': m.pk,
                'module': offering.code or offering.display_name,
                'block': m.phase.display_title,
                'week': m.week.display_title if m.week_id else '',
                'title': m.title,
                'published': 'yes' if m.is_published else 'no',
                'file': m.file.name,
                'size_on_disk': size,
            })

        total = qs.count()
        files = {r['file'] for r in rows}
        self.stdout.write(f'{len(rows)} of {total} materials point at a truncated file '
                          f'({len(files)} distinct files); '
                          f'{sum(r["published"] == "yes" for r in rows)} are published.')
        for r in rows[:40]:
            self.stdout.write(f'  #{r["material_id"]:<5} {r["module"]:<8} {r["published"]:<3} {r["title"][:70]}')
        if len(rows) > 40:
            self.stdout.write(f'  … and {len(rows) - 40} more (use --csv for the full list)')

        if opts.get('csv') and rows:
            with open(opts['csv'], 'w', newline='') as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.stdout.write(self.style.SUCCESS(f'Wrote {opts["csv"]}'))
