"""Set up the hourly 1-on-1 offer on the shop. Idempotent.

* The shop item **One-on-one session** (SKU ``SHOP-1ON1-HOUR``): a bookable
  service, one-hour sessions starting on the hour, no gap between them, listed
  for everyone at R300 per hour (VAT inclusive). Created if missing; an existing
  one keeps whatever staff changed, except that it is put back on the hourly
  grid (60-minute length only, which makes starts fall on the hour) the week calendar needs.
* Each coach named with ``--email`` (none by default, so a fresh install only
  gets the shop item): an active rate of R300 per hour on it, created if
  missing (an existing rate is left as staff set it unless ``--reset-rate``),
  and weekly hours of Mon–Fri 09:00–17:00 *only if the coach has no hours at
  all* — existing hours are never overwritten.

This is separate from the invoice-only ``SVC-1ON1-HOUR`` line in
:mod:`apps.shop.catalogue`, which staff bill on invoices and which has no
calendar behind it.

    manage.py ensure_one_on_one_rates [--email someone@...] [--rate 300] [--dry-run]
"""
from datetime import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.shop.models import EducatorAvailability, EducatorRate, Product, ProductCategory

SKU = 'SHOP-1ON1-HOUR'
DEFAULT_RATE = Decimal('300')
DEFAULT_HOURS = [(day, time(9), time(17)) for day in range(5)]
"""Weekly hours given to a coach with none: Mon–Fri 09:00–17:00."""


class Command(BaseCommand):
    """``manage.py ensure_one_on_one_rates`` — see the module docstring."""

    help = 'Create the hourly 1-on-1 shop item and give each --email coach a rate and weekly hours.'

    def add_arguments(self, parser):
        """``--email`` (repeatable), ``--rate``, ``--reset-rate`` and ``--dry-run``."""
        parser.add_argument('--email', action='append',
                            help='Coach to set up (repeatable). Default: none, only the shop item.')
        parser.add_argument('--rate', default=str(DEFAULT_RATE), help='Rand per hour, VAT inclusive.')
        parser.add_argument('--reset-rate', action='store_true',
                            help='Also set an existing rate back to --rate and active.')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        """Create or repair the shop item, then each coach's rate and hours.

        The invoice-only catalogue line for 1-on-1 hours (``SVC-1ON1-HOUR``, whose
        price registration's "One-on-one coaching" card shows) is given the same
        rate when it has none.
        """
        rate_value = Decimal(opts['rate'])
        emails = opts['email'] or []
        User = get_user_model()
        report = []
        with transaction.atomic():
            product = Product.objects.filter(sku=SKU).first()
            if product is None:
                category, _ = ProductCategory.objects.get_or_create(
                    name='1-on-1 sessions', defaults={'description': 'Private coaching with an educator.'})
                product = Product.objects.create(
                    sku=SKU, name='One-on-one session', category=category,
                    summary='One hour with a Metanoia coach, online.',
                    description=('Book an hour with a Metanoia coach on Microsoft Teams. Pick a free hour on '
                                 'their weekly calendar; the joining link arrives as soon as you have paid.'),
                    fulfilment=Product.FULFIL_BOOKING, price=rate_value, unit='hour',
                    session_lengths='60', buffer_minutes=0,
                    min_notice_hours=24, reschedule_cutoff_hours=24, booking_window_days=56,
                    shop_listed=True, status='active')
                report.append(f'created product {product.pk} “{product.name}” at R{rate_value}/h')
            else:
                changed = []
                for field, value in (('fulfilment', Product.FULFIL_BOOKING), ('session_lengths', '60')):
                    if getattr(product, field) != value:
                        setattr(product, field, value)
                        changed.append(field)
                if changed:
                    product.save()
                report.append(f'product {product.pk} “{product.name}” exists'
                              + (f' (fixed {", ".join(changed)})' if changed else ''))

            for email in emails:
                user = User.objects.filter(email__iexact=email).select_related('profile').first()
                person = getattr(user, 'profile', None) if user else None
                if person is None:
                    raise CommandError(f'No user with a profile for {email}.')
                name = user.get_full_name() or email
                rate, created = EducatorRate.objects.get_or_create(
                    product=product, educator=person,
                    defaults={'hourly_rate': rate_value, 'is_active': True})
                if created:
                    report.append(f'{name}: rate R{rate_value}/h created')
                elif opts['reset_rate'] and (rate.hourly_rate != rate_value or not rate.is_active):
                    rate.hourly_rate, rate.is_active = rate_value, True
                    rate.save(update_fields=['hourly_rate', 'is_active', 'updated_at'])
                    report.append(f'{name}: rate reset to R{rate_value}/h')
                else:
                    report.append(f'{name}: rate exists (R{rate.hourly_rate}/h, '
                                  f'{"active" if rate.is_active else "inactive"})')

                hours = EducatorAvailability.objects.filter(educator=person)
                if hours.exists():
                    report.append(f'{name}: {hours.count()} weekly hour row(s) exist, left as they are')
                else:
                    EducatorAvailability.objects.bulk_create(
                        EducatorAvailability(educator=person, weekday=d, start_time=s, end_time=e)
                        for d, s, e in DEFAULT_HOURS)
                    report.append(f'{name}: weekly hours set to Mon–Fri 09:00–17:00')

            line = Product.objects.filter(sku='SVC-1ON1-HOUR').first()
            if line is not None and not line.price:
                line.price = rate_value
                line.save(update_fields=['price', 'updated_at'])
                report.append(f'catalogue line {line.sku} priced at R{rate_value}/h')

            if opts['dry_run']:
                transaction.set_rollback(True)
                report.append('dry run: nothing saved')

        for line in report:
            self.stdout.write(line)
