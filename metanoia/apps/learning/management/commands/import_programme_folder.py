"""Load a pack folder that is already on the server into an existing programme and cohort.

The upload screen without the upload — for big packs, or packs copied over from
Drive / OneDrive. Same scanner, same schedule sheet, same builder, same result.

    manage.py import_programme_folder fixtures/sample_programme --cohort METANOIA-SAMPLE/PACK2027  # dry run
    manage.py import_programme_folder fixtures/sample_programme --cohort METANOIA-SAMPLE/PACK2027 --apply
    manage.py import_programme_folder ~/packs/Sample_Test1 --cohort METANOIA-SAMPLE/SAMPLE1 --sheet t1.csv --apply
    manage.py import_programme_folder fixtures/sample_programme --cohort METANOIA-SAMPLE/PACK2027 --ai --write-sheet

Nothing is created at the programme level: the cohort must exist and every
module folder must match one of its programme's modules. ``--ai`` drafts the
schedule sheet when the pack has none (and writes unit summaries); with
``--write-sheet`` the sheet in use is saved to ``<folder>/0_Start_Here/schedule.csv``
so it can be checked and kept with the pack. A dry run builds inside a
transaction and rolls it back, so its report is exactly what ``--apply`` does.
"""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = 'Load a pack folder on the server into an existing programme cohort (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('folder', help='The unpacked pack: the student pack and the tutor pack beside it.')
        parser.add_argument('--cohort', required=True,
                            help='INSTITUTION-PROGRAMME/COHORT, e.g. METANOIA-SAMPLE/PACK2027, or the cohort id.')
        parser.add_argument('--sheet', help='A schedule sheet (.csv / .xlsx) to use instead of the one in the pack.')
        parser.add_argument('--year', type=int, help='The year of the first week, for folder dates like 7-8_Nov.')
        parser.add_argument('--ai', action='store_true',
                            help='Let Claude draft a missing sheet and write unit summaries (needs the API key).')
        parser.add_argument('--write-sheet', action='store_true',
                            help='Save the sheet in use to <folder>/0_Start_Here/schedule.csv.')
        parser.add_argument('--apply', action='store_true', help='Write it (default: dry run, rolled back).')

    def cohort(self, value):
        from apps.learning.models import Cohort

        qs = Cohort.objects.select_related('programme__institution')
        if str(value).isdigit():
            found = qs.filter(pk=int(value)).first()
        else:
            programme, _, code = str(value).partition('/')
            institution, _, programme_code = programme.partition('-')
            found = qs.filter(programme__institution__code__iexact=institution,
                              programme__code__iexact=programme_code, code__iexact=code).first()
        if found is None:
            raise CommandError(f'No cohort "{value}". Cohorts are never created here — add it on the programme '
                               'first. Use INSTITUTION-PROGRAMME/COHORT, e.g. METANOIA-SAMPLE/PACK2027.')
        return found

    def handle(self, *args, folder, cohort, sheet=None, year=None, ai=False, write_sheet=False, apply=False,
               **options):
        from core.programme_pack import ai as pack_ai
        from core.programme_pack import intake, scanner
        from core.programme_pack import sheet as schedule_sheet

        folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            raise CommandError(f'No folder at {folder}')
        target = self.cohort(cohort)
        self.stdout.write(f'Into {target.programme.full_code} · {target.code} (existing — nothing is created).')

        plan = scanner.scan(folder, year=year)
        self.stdout.write('Read: ' + ', '.join(f'{k} {v}' for k, v in scanner.summary(plan).items()))
        for note in plan['notes']:
            self.stdout.write(self.style.WARNING(note))

        if sheet:
            rows, problems = schedule_sheet.read_file(os.path.abspath(os.path.expanduser(sheet)))
            source = sheet
        else:
            rows, problems, _text = intake.sheet_for(plan)
            source = plan.get('schedule_sheet') or ''
        enrichment = {}
        if ai:
            if not pack_ai.is_enabled():
                raise CommandError('--ai needs ANTHROPIC_API_KEY and ADMIN_AI_ENABLED on this server.')
            if not rows:
                rows, problems = pack_ai.draft_sheet(plan, out=self.stdout.write)
                source = 'drafted by the AI — check it'
            enrichment = pack_ai.enrich(plan, out=self.stdout.write)
        self.stdout.write(f'Schedule sheet: {len(rows)} row(s)' + (f' ({source})' if source else ' (none)'))
        for problem in problems:
            self.stdout.write(self.style.WARNING(f'  {problem}'))
        if write_sheet and rows:
            start = next((os.path.join(dirpath, d) for dirpath, dirs, _f in os.walk(folder) for d in dirs
                          if 'start' in d.lower()), folder)
            path = os.path.join(start, 'schedule.csv')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(schedule_sheet.to_csv(rows))
            self.stdout.write(f'Sheet saved to {path}')

        found = intake.check(plan, target, rows)
        for code, offering, name in found['mapping']:
            self.stdout.write(f'  {code} → {offering} ({name})')
        if found['problems']:
            raise CommandError(' '.join(found['problems']))
        self.stdout.write(f'Key {found["key"]}')

        with transaction.atomic():
            made, report = intake.build_into(plan, target, rows, found['key'], ai=enrichment,
                                             out=self.stdout.write)
            for line in report.lines():
                self.stdout.write(line)
            self.stdout.write('Totals: ' + ', '.join(f'{k} {v}' for k, v in made.totals().items()))
            if not apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING('Dry run — rolled back. Add --apply to write it.'))
                return
        self.stdout.write(self.style.SUCCESS(f'Loaded into {target.programme.full_code} · {target.code}.'))
