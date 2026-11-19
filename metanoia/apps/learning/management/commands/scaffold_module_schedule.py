"""Give module offerings their standard year: four test blocks then two exam blocks.

    python manage.py scaffold_module_schedule                     # every active offering
    python manage.py scaffold_module_schedule --institution METANOIA
    python manage.py scaffold_module_schedule --module INTRO --weeks-test 5
    python manage.py scaffold_module_schedule --with-weeks-from-topics

Idempotent: a block that already exists is left exactly as it is, so this is
safe to re-run after an offering has been edited by hand.

``--with-weeks-from-topics`` goes one step further and puts a real topic on each
week, walking the offering's own :class:`~apps.learning.models.Topic` list in
order — which is what "each week it will be a topic from the study guide" means
once the study guide has been captured.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.learning import models
from apps.learning.module_build import DEFAULT_WEEKS, scaffold_schedule


class Command(BaseCommand):
    """``manage.py scaffold_module_schedule`` — see the module docstring."""
    help = 'Create the standard 4-test + 2-exam preparation schedule for module offerings.'

    def add_arguments(self, parser):
        """Add the ``--institution``/``--programme``/``--module`` filters, block
        lengths, ``--with-weeks-from-topics`` and ``--dry-run``.
        """
        parser.add_argument('--institution', help='Limit to one institution code, e.g. METANOIA.')
        parser.add_argument('--programme', help="Limit to one programme code, e.g. SAMPLE.")
        parser.add_argument('--module', help="Limit to one offering code, e.g. INTRO.")
        parser.add_argument('--weeks-test', type=int, default=DEFAULT_WEEKS[models.ModulePhase.KIND_TEST],
                            help='Weeks per test block (default 4).')
        parser.add_argument('--weeks-exam', type=int, default=DEFAULT_WEEKS[models.ModulePhase.KIND_EXAM],
                            help='Weeks per exam block (default 6).')
        parser.add_argument('--with-weeks-from-topics', action='store_true',
                            help="Name each week after the offering's next study-guide topic.")
        parser.add_argument('--dry-run', action='store_true', help='Report what would change, change nothing.')

    def handle(self, *args, **options):
        """Scaffold the default blocks on every matching active offering.

        General is left out: it has no test/exam blocks, only programme-wide
        material.
        An offering already built for its intakes (only cohort blocks, no shared ones
        —
        a programme pack such as the sample pack) is skipped, because six empty shared
        "Test n preparation" blocks would only bury it.
        """
        offerings = models.ProgrammeModule.objects.without_general().filter(is_active=True).select_related(
            'programme__institution', 'module')
        if options['institution']:
            offerings = offerings.filter(programme__institution__code__iexact=options['institution'])
        if options['programme']:
            offerings = offerings.filter(programme__code__iexact=options['programme'])
        if options['module']:
            offerings = offerings.filter(code__iexact=options['module'])

        weeks = {
            models.ModulePhase.KIND_TEST: options['weeks_test'],
            models.ModulePhase.KIND_EXAM: options['weeks_exam'],
        }

        if not offerings.exists():
            self.stdout.write(self.style.WARNING('No offerings matched.'))
            return

        total_blocks = 0
        total_topics = 0
        for offering in offerings:
            phases = models.ModulePhase.objects.filter(programme_module=offering)
            if phases.filter(cohort__isnull=False).exists() and not phases.filter(cohort__isnull=True).exists():
                self.stdout.write(f'  {offering.reference}: has its own intake schedule — skipped')
                continue
            if options['dry_run']:
                missing = sum(
                    1 for kind, seq in models.ModulePhase.DEFAULT_PLAN
                    if not models.ModulePhase.objects.filter(
                        programme_module=offering, kind=kind, sequence=seq).exists())
                self.stdout.write(f'  {offering.reference}: would add {missing} block(s)')
                total_blocks += missing
                continue

            with transaction.atomic():
                created = scaffold_schedule(offering, weeks_per_phase=weeks)
                total_blocks += created
                if options['with_weeks_from_topics']:
                    total_topics += self._name_weeks_from_topics(offering)

            self.stdout.write(f'  {offering.reference}: +{created} block(s)')

        verb = 'would create' if options['dry_run'] else 'created'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total_blocks} preparation block(s) across {offerings.count()} offering(s)'
            + (f'; named {total_topics} week(s) from the study guide.' if total_topics else '.')))

    def _name_weeks_from_topics(self, offering):
        """Walk the offering's topics in order and attach one to each empty week."""
        from apps.learning.models import WeekTopic
        topics = list(offering.topics.filter(is_active=True).order_by('order', 'code'))
        if not topics:
            return 0
        index = 0
        named = 0
        for phase in offering.phases.order_by('order', 'id'):
            for week in phase.weeks.filter(week_topics__isnull=True).order_by('number'):
                if index >= len(topics):
                    return named
                WeekTopic.objects.create(week=week, topic=topics[index], order=0)
                index += 1
                named += 1
        return named
