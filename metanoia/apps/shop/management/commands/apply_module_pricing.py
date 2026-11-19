"""Module fees from the 2026 timetable: R750 a module a month, R2 200 for all four.

    manage.py apply_module_pricing            # apply
    manage.py apply_module_pricing --dry-run  # show what would change

The owner's rule (2026-11-03): every module, at every institution and in every
programme, is R750 a month; only 1-on-1 coaching is priced by the hour. A
programme of exactly four modules also gets the automatic **"All 4 modules"**
bundle: R800 off when all four are taken together, so 4 × R750 = R3 000
becomes R2 200 (see :mod:`apps.shop.pricing`).

Idempotent — re-running changes nothing once applied. For every programme
with active modules it:

1. sets ``price_per_month`` to R750 on each active module (the fee lives on the
   programme's module, so every cohort of that programme registers at the new
   fee; existing enrolments keep the price they captured at enrolment);
2. keeps the module-fee items in the billing catalogue in step;
3. for a four-module programme, creates (or corrects) the bundle promotion;
4. re-prices the programme-fee item, and checks the four quote R2 200.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

MODULE_FEE = Decimal('750.00')
BUNDLE_SIZE = 4
BUNDLE_TOTAL = Decimal('2200.00')
BUNDLE_NAME = 'All 4 modules'


class Command(BaseCommand):
    """``manage.py apply_module_pricing`` — see the module docstring."""

    help = 'Every module R750 a month; four-module programmes R2 200 for all four (automatic bundle).'

    def add_arguments(self, parser):
        """``--dry-run`` reports only and changes nothing."""
        parser.add_argument('--dry-run', action='store_true', help='Report only; change nothing.')

    def handle(self, *args, dry_run=False, **options):
        """Apply the module fee and bundle to every programme, inside one transaction.

        The programme's General module is free and outside every bundle ("all 4
        modules"), so it is never priced or counted.
        """
        from apps.learning.models import ModuleEnrolment, Programme
        from apps.shop import catalogue, pricing
        from apps.shop.models import Discount, Product

        programmes = Programme.objects.select_related('institution').order_by('institution__code', 'code')
        with transaction.atomic():
            for programme in programmes:
                offerings = list(programme.modules.without_general().filter(is_active=True)
                                 .order_by('order', 'pk'))
                if not offerings:
                    continue
                kept = (ModuleEnrolment.objects.filter(programme_module__in=offerings)
                        .exclude(status=ModuleEnrolment.STATUS_EXPIRED).count())
                self.stdout.write(f'{programme.full_code} — {len(offerings)} module(s); '
                                  f'{kept} current enrolment(s) keep their captured price')
                for pm in offerings:
                    before = pm.price_per_month
                    if before != MODULE_FEE:
                        pm.price_per_month = MODULE_FEE
                        if not dry_run:
                            pm.save(update_fields=['price_per_month', 'updated_at'])
                    if not dry_run:
                        catalogue.sync_module(pm)
                    self.stdout.write(f'  {pm.code}: R{before} -> R{MODULE_FEE}'
                                      + ('' if before != MODULE_FEE else ' (unchanged)'))

                if len(offerings) == BUNDLE_SIZE:
                    fee_items = list(Product.objects.filter(
                        unlocks=Product.UNLOCKS_MODULE, module__in=offerings))
                    if not dry_run and len(fee_items) != BUNDLE_SIZE:
                        raise CommandError(f'{programme.full_code}: module-fee items are missing — '
                                           'run manage.py sync_catalogue.')
                    saving = MODULE_FEE * BUNDLE_SIZE - BUNDLE_TOTAL
                    bundle = Discount.objects.filter(name=BUNDLE_NAME, programme=programme,
                                                     code__isnull=True).first()
                    state = 'created' if bundle is None else 'updated'
                    if bundle is None:
                        bundle = Discount(name=BUNDLE_NAME, programme=programme)
                    bundle.institution = programme.institution
                    bundle.discount_type = Discount.TYPE_AMOUNT
                    bundle.value = saving
                    bundle.min_spend = Decimal('0')
                    bundle.starts_at = bundle.ends_at = bundle.max_uses = None
                    bundle.is_active = True
                    if not dry_run:
                        bundle.save()
                        bundle.products.set(fee_items)
                    self.stdout.write(f'  Bundle "{BUNDLE_NAME}": R{saving} off all {BUNDLE_SIZE} '
                                      f'({state}{", dry run" if dry_run else ""})')

                if not dry_run:
                    product = catalogue.sync_programme(programme)
                    if product is not None:
                        self.stdout.write(f'  Programme fee item {product.sku}: R{product.price}')
                    quote = pricing.quote_modules([(pm, pm.price_per_month) for pm in offerings])
                    expected = BUNDLE_TOTAL if len(offerings) == BUNDLE_SIZE else MODULE_FEE * len(offerings)
                    if quote['total'] != expected:
                        raise CommandError(f'{programme.full_code}: all modules quote R{quote["total"]}, '
                                           f'expected R{expected}.')
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(self.style.SUCCESS(
            f'Done: R{MODULE_FEE} a module; four-module programmes R{BUNDLE_TOTAL} for all four.'
            + (' (dry run)' if dry_run else '')))
