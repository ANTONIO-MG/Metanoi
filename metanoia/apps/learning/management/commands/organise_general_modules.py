"""Give every programme its General module and move programme-wide material onto it.

    python manage.py organise_general_modules                    # dry run: print what would change
    python manage.py organise_general_modules --apply            # do it
    python manage.py organise_general_modules --programme SAMPLE    # one programme (code, full code or pk)

Dry run by default: nothing is written, and the report is exactly what
``--apply`` does. Idempotent: once applied, a second run reports nothing to do.

For every programme it (a) creates the General module, or renames an existing
"overall"/onboarding module to it; (b) finds material that sits on several of
the programme's modules (same lesson, file, session, assessment or link — or a
lookalike file of the same name and size, or a lesson with the same content) or
in an onboarding block, keeps one copy and moves it to General, and deletes the
other copies after re-pointing anything that referred to them. Items that only
share a title are listed and left alone. Live sessions themselves are never
touched. See :mod:`apps.learning.general_organise` for the rules.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q


class Command(BaseCommand):
    help = 'Create each programme\'s General module and move onboarding / programme-wide material onto it ' \
           '(dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: dry run).')
        parser.add_argument('--programme', action='append', default=[],
                            help='Only this programme: its code ("SAMPLE"), full code ("METANOIA-SAMPLE") or pk. '
                                 'Repeatable.')

    def handle(self, *args, apply=False, programme=(), **options):
        from apps.learning import general_organise as organise
        from apps.learning.models import Programme

        programmes = Programme.objects.select_related('institution').order_by('institution__code', 'order', 'code')
        if programme:
            wanted = Q()
            for value in programme:
                if value.isdigit():
                    wanted |= Q(pk=int(value))
                elif '-' in value:
                    inst, _, code = value.partition('-')
                    wanted |= Q(institution__code__iexact=inst, code__iexact=code)
                else:
                    wanted |= Q(code__iexact=value)
            programmes = programmes.filter(wanted)
            if not programmes.exists():
                raise CommandError(f'No programme matches {", ".join(programme)}.')

        say = self.stdout.write
        say(self.style.WARNING('DRY RUN — nothing will be changed. Re-run with --apply to make these changes.')
            if not apply else self.style.WARNING('APPLYING.'))
        totals = {'programmes': 0, 'create': 0, 'reuse': 0, 'moved': 0, 'deleted': 0, 'lookalikes': 0}
        for prog in programmes:
            result = organise.plan(prog)
            totals['programmes'] += 1
            totals['create'] += int(result.create)
            totals['reuse'] += int(result.reuse is not None)
            totals['moved'] += len(result.moves)
            totals['deleted'] += len(result.deletes)
            totals['lookalikes'] += len(result.lookalikes)
            for line in organise.describe(result):
                say(line)
            if apply and result.changes:
                organise.apply(result)
                say(self.style.SUCCESS(f'  applied to {prog.full_code}'))
        say(f'Programmes: {totals["programmes"]} · General created: {totals["create"]} · reused: {totals["reuse"]} '
            f'· items moved: {totals["moved"]} · duplicates deleted: {totals["deleted"]} '
            f'· lookalike groups left alone: {totals["lookalikes"]}'
            + ('' if apply else ' (dry run)'))
