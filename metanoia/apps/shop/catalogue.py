"""The billing catalogue: tuition items kept in step with the learning spine.

Staff invoice from the same :class:`~apps.shop.models.Product` table the shop
sells from. Three kinds of item are maintained here rather than typed in:

* **Module fees** — one per :class:`ProgrammeModule`, priced at the module's
  ``price_per_month``, unit "month".
* **Programme fees** — one per :class:`Programme`, priced at the sum of its
  active modules' fees, less any bundle promotion the full set earns.
* **Standard items** — the per-hour 1-on-1 session and the Metanoia hoodie.

Tuition items are invoice-only (``shop_listed=False``) and ``unlocks`` their
module(s) when the invoice is paid — see :func:`unlock_paid_lines`. Their price
follows the module fees while ``price_from_modules`` is on; staff untick it to
set their own. Everything here is idempotent: re-running changes nothing that
staff have edited except a followed price, and a tuition item is active exactly
when its module (or programme) is.

Kept current by the ProgrammeModule / Programme ``post_save`` handlers in
:mod:`apps.shop.signals` and by ``manage.py sync_catalogue``.
"""
from decimal import Decimal

from django.db import transaction

from .models import Product, ProductCategory, ProductVariant

CATEGORY_MODULES = 'Module fees'
CATEGORY_PROGRAMMES = 'Programme fees'
CATEGORY_SESSIONS = '1-on-1 sessions'
CATEGORY_MERCH = 'Merchandise'

SKU_SESSION = 'SVC-1ON1-HOUR'
SKU_HOODIE = 'METANOIA-HOODIE'
"""SKU of the standard branded hoodie (a sized, shipped merchandise item)."""
HOODIE_SIZES = ['S', 'M', 'L', 'XL', 'XXL']


def _category(name, description=''):
    """Get or create the :class:`ProductCategory` called ``name``."""
    category, _ = ProductCategory.objects.get_or_create(
        name=name, defaults={'description': description})
    return category


def _programme_price(programme):
    """Every active module's fee, less any bundle the full set earns — the
    "All 4 modules" promotion makes Milpark PGDA R2 200 rather than 4 × R750."""
    from .pricing import quote_modules

    active = [m for m in programme.modules.all() if m.is_active and not m.is_general]
    total = quote_modules([(m, m.price_per_month or Decimal('0')) for m in active])['total']
    return Decimal(str(total)).quantize(Decimal('0.01'))


def programme_price(programme):
    """Public name for the programme fee, for the staff item editor."""
    return _programme_price(programme)


def _status(active):
    """Product ``status`` value for an active / inactive flag."""
    return 'active' if active else 'inactive'


def sync_module(programme_module):
    """Create or refresh the fee item for one module offering.

    The General module is free and never billed, so it has no fee item."""
    if programme_module.is_general:
        return None
    programme = programme_module.programme
    product = (Product.objects.filter(unlocks=Product.UNLOCKS_MODULE, module=programme_module).first()
               or Product.objects.filter(sku=f'MOD-{programme.full_code}-{programme_module.code}'[:40]).first())
    active = programme_module.is_active and programme.is_active
    if product is None:
        if not active:
            return None
        product = Product(
            sku=f'MOD-{programme.full_code}-{programme_module.code}'[:40],
            name=f'{programme_module.display_name} ({programme_module.code})'[:200],
            summary=f'{programme.institution.name} · {programme.short_code} · monthly module fee'[:300],
            category=_category(CATEGORY_MODULES, 'Monthly tuition for one module.'),
            fulfilment=Product.FULFIL_NONE, kind=Product.KIND_PRODUCT,
            institution=programme.institution, programme=programme, module=programme_module,
            unlocks=Product.UNLOCKS_MODULE, price_from_modules=True, unit='month',
            shop_listed=False, price=programme_module.price_per_month or 0,
            status=_status(active))
        product.save()
        return product
    changed = []
    if product.price_from_modules and product.price != (programme_module.price_per_month or 0):
        product.price = programme_module.price_per_month or 0
        changed.append('price')
    if product.status != _status(active):
        product.status = _status(active)
        changed.append('status')
    if changed:
        product.save(update_fields=changed + ['updated_at'])
    return product


def sync_programme(programme):
    """Create or refresh the fee item for a whole programme."""
    product = (Product.objects.filter(unlocks=Product.UNLOCKS_PROGRAMME, programme=programme,
                                      module__isnull=True).first()
               or Product.objects.filter(sku=f'PROG-{programme.full_code}'[:40]).first())
    price = _programme_price(programme)
    active = programme.is_active and any(m.is_active and not m.is_general for m in programme.modules.all())
    if product is None:
        if not active:
            return None
        product = Product(
            sku=f'PROG-{programme.full_code}'[:40],
            name=f'{programme.display_name} — {programme.institution.name}'[:200],
            summary=f'All modules of {programme.institution.code} {programme.short_code} · monthly fee'[:300],
            category=_category(CATEGORY_PROGRAMMES, 'Monthly tuition for every module of a programme.'),
            fulfilment=Product.FULFIL_NONE, kind=Product.KIND_PRODUCT,
            institution=programme.institution, programme=programme,
            unlocks=Product.UNLOCKS_PROGRAMME, price_from_modules=True, unit='month',
            shop_listed=False, price=price, status=_status(active))
        product.save()
        return product
    changed = []
    if product.price_from_modules and product.price != price:
        product.price = price
        changed.append('price')
    if product.status != _status(active):
        product.status = _status(active)
        changed.append('status')
    if changed:
        product.save(update_fields=changed + ['updated_at'])
    return product


def ensure_standard_items():
    """The per-hour 1-on-1 session and the hoodie — created once, never overwritten."""
    session = Product.objects.filter(sku=SKU_SESSION).first()
    if session is None:
        session = Product.objects.create(
            sku=SKU_SESSION, name='Private 1-on-1 session',
            summary='One hour with a Metanoia coach, online.',
            category=_category(CATEGORY_SESSIONS, 'Private coaching with an educator.'),
            fulfilment=Product.FULFIL_NONE, kind=Product.KIND_SERVICE, unit='hour',
            shop_listed=False, price=0)
    hoodie = Product.objects.filter(sku=SKU_HOODIE).first()
    if hoodie is None:
        hoodie = Product.objects.create(
            sku=SKU_HOODIE, name='Metanoia Coach Hoodie',
            summary='Heavyweight hoodie in Metanoia green and blue. Sizes S–XXL.',
            category=_category(CATEGORY_MERCH, 'Metanoia branded items.'),
            fulfilment=Product.FULFIL_SHIPPED, unit='each', shop_listed=True,
            price=Decimal('450'), weight_kg=Decimal('0.70'))
        for order, size in enumerate(HOODIE_SIZES):
            ProductVariant.objects.create(product=hoodie, name=size, order=order,
                                          sku=f'{SKU_HOODIE}-{size}')
    return session, hoodie


@transaction.atomic
def sync_all():
    """Every module, every programme, and the standard items. Returns counts."""
    from apps.learning.models import Programme, ProgrammeModule

    modules = ProgrammeModule.objects.select_related('programme__institution', 'module')
    for programme_module in modules:
        sync_module(programme_module)
    programmes = Programme.objects.select_related('institution').prefetch_related('modules')
    for programme in programmes:
        sync_programme(programme)
    ensure_standard_items()
    return {'modules': modules.count(), 'programmes': programmes.count()}


def _modules_for(product):
    """The active modules a module or programme product unlocks (General modules excluded)."""
    if product.unlocks == Product.UNLOCKS_MODULE and product.module_id:
        return [product.module] if product.module.is_active else []
    if product.unlocks == Product.UNLOCKS_PROGRAMME and product.programme_id:
        return list(product.programme.modules.without_general().filter(is_active=True))
    return []


@transaction.atomic
def unlock_paid_lines(invoice):
    """Activate the module(s) behind each tuition line of a paid ``invoice``,
    for as many months as the line's quantity. Returns the enrolments touched.

    Registration invoices never reach here with work to do: their lines carry
    no product, and ``activate_modules_for_invoice`` unlocks those.
    """
    from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment

    person = getattr(invoice.customer, 'profile', None)
    if person is None:
        return []
    touched = []
    lines = invoice.items.select_related('product__module', 'product__programme').exclude(product=None)
    for line in lines:
        product = line.product
        if not product.unlocks:
            continue
        for programme_module in _modules_for(product):
            ProgrammeEnrolment.objects.get_or_create(
                person=person, programme=programme_module.programme, defaults={'is_active': True})
            enrolment, _ = ModuleEnrolment.objects.get_or_create(
                person=person, programme_module=programme_module,
                defaults={'price_at_enrolment': programme_module.price_per_month or 0})
            enrolment.activate(months=max(int(line.quantity or 1), 1))
            touched.append(enrolment)
    return touched
