"""Create/refresh the billing catalogue: a fee item per module and per programme,
the per-hour 1-on-1 session and the hoodie. Idempotent — see shop.catalogue."""
from django.core.management.base import BaseCommand

from apps.shop.catalogue import sync_all


class Command(BaseCommand):
    help = 'Create/refresh module, programme and standard items in Products & services.'

    def handle(self, *args, **options):
        counts = sync_all()
        self.stdout.write(self.style.SUCCESS(
            f"Catalogue in step: {counts['modules']} module fees, {counts['programmes']} programme fees, "
            'plus the 1-on-1 session and the hoodie.'))
