"""Seed a small SAMPLE shop catalogue: general items plus per-programme material.

Idempotent — matched on SKU, so running it again updates rather than duplicates.
Every name, description and price here is placeholder sample data so the
storefront can be demonstrated; replace it with Metanoia Group's real catalogue
in the staff shop screens before going live.

Scoping is the point of the exercise: an item with no institution is general
and everybody sees it, while a programme pack carries its institution and
programme so only that cohort does. See :mod:`apps.shop.scoping`.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.learning.models import Institution, Programme, ProgrammeModule
from apps.shop.models import Product, ProductCategory

CATEGORIES = [
    ('Programme Packs', 'Workbooks and resources for a specific programme.'),
    ('Workshops', 'Recorded workshops and short courses.'),
    ('Merchandise', 'Metanoia Group branded items.'),
]
"""The sample categories, in storefront order."""

GENERAL = [
    ('METANOIA-NOTEBOOK', 'Metanoia Notebook & Pen Set (sample)', 'Merchandise', Decimal('180'), 'product',
     'Sample merchandise item — replace with the real range.'),
    ('METANOIA-WORKSHOP', 'Introduction to Transformation Workshop (sample recording)', 'Workshops',
     Decimal('250'), 'service', 'Sample recorded workshop — replace with a real offering.'),
]
"""Sold to everyone, whatever they study."""


class Command(BaseCommand):
    """``manage.py seed_shop`` — create or refresh the sample shop catalogue. Idempotent (upserts by SKU)."""

    help = 'Create/refresh the SAMPLE shop catalogue (general items + one pack per programme module).'

    def add_arguments(self, parser):
        """``--quiet`` suppresses the per-item output."""
        parser.add_argument('--quiet', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        """Ensure the categories, the general items and every active institution's sample packs."""
        self.quiet = options['quiet']
        categories = {}
        for name, description in CATEGORIES:
            category, _ = ProductCategory.objects.get_or_create(
                name=name, defaults={'description': description})
            categories[name] = category

        made = 0
        for sku, name, category, price, kind, summary in GENERAL:
            made += self._upsert(sku, name=name, category=categories[category], price=price,
                                 kind=kind, summary=summary, institution=None, programme=None)

        for institution in Institution.objects.filter(is_active=True).order_by('order'):
            made += self._seed_institution(institution, categories)

        total = Product.objects.count()
        self._say(f'Shop catalogue seeded — {made} created/updated, {total} products in total.')

    def _seed_institution(self, institution, categories):
        """One sample programme pack per module of the institution's primary programme."""
        programme = (Programme.objects.filter(institution=institution, is_active=True)
                     .order_by('order').first())
        if programme is None:
            return 0
        made = 0
        for offering in ProgrammeModule.objects.filter(programme=programme).order_by('pk'):
            label = getattr(offering, 'name', '') or offering.code
            made += self._upsert(
                f'{institution.code}-{programme.code}-{offering.code}-PACK',
                name=f'{label} — Programme Pack (sample)',
                category=categories['Programme Packs'],
                price=Decimal('320'), kind='product',
                summary=f'{institution.code} {programme.code}: sample workbook and resources for {label}.',
                description='Sample product — replace with the real pack before going live.',
                institution=institution, programme=programme, module=offering)
        return made

    def _upsert(self, sku, **fields):
        """Create or update the active product with ``sku``; returns 1 for the tally."""
        product, created = Product.objects.update_or_create(sku=sku, defaults=dict(
            status='active', stock=999, **fields))
        self._say(f'  {"+" if created else "~"} {sku:26s} {product.name[:52]}')
        return 1

    def _say(self, message):
        """Write ``message`` unless ``--quiet``."""
        if not self.quiet:
            self.stdout.write(message)
