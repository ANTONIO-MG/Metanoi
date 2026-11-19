"""The one discount an invoice carries: a rand amount off the whole invoice.

Invoices used to discount line by line (a % per line, and bundles spread over
each module line). Now the discount sits on the invoice itself — see
``Invoice.discount_amount`` / ``discount_label`` — and lines are billed gross.

While ``Invoice.discount_auto`` is on, :func:`apply_auto` keeps the discount in
step with the bundles the lines earn: an invoice holding all four IAC modules
gets "All 4 modules" (R800) without anyone typing it. A line counts towards a
bundle when it is the bundled product itself, or — for lines raised before
products were linked — when its description starts with that product's name
("Financial Reporting (FREP) — per month").
"""
from decimal import Decimal

ZERO = Decimal('0')
CENT = Decimal('0.01')


def _money(value):
    return Decimal(str(value or 0)).quantize(CENT)


def _bundle_products(bundles):
    from apps.shop.models import Product
    ids = {p.pk for bundle in bundles for p in bundle.products.all()}
    return list(Product.objects.filter(pk__in=ids).only('pk', 'name'))


def _product_for_line(item, products):
    if item.product_id:
        return item.product_id
    description = (item.description or '').strip().lower()
    for product in products:
        name = (product.name or '').strip().lower()
        if name and description.startswith(name):
            return product.pk
    return None


def bundle_discount(items, bundles=None):
    """``(amount, label)`` the bundles earn for ``items`` (invoice lines, saved
    or not). ``(0, '')`` when none applies."""
    from apps.shop.pricing import bundle_savings, live_bundles

    if bundles is None:
        bundles = live_bundles()
    if not bundles:
        return ZERO, ''
    products = _bundle_products(bundles)
    gross = {}
    for item in items:
        pk = _product_for_line(item, products)
        if pk is not None:
            gross[pk] = gross.get(pk, ZERO) + _money(item.gross_amount)
    earned = bundle_savings(gross, bundles)
    amount = sum((sum(offs.values(), ZERO) for _bundle, offs in earned), ZERO)
    label = ' + '.join(bundle.name for bundle, _offs in earned)
    return _money(amount), label


def apply_auto(invoice):
    """Reset an auto-discounting invoice's discount to what its lines earn and
    recalculate it. A hand-set discount (``discount_auto`` off) is left alone."""
    if invoice.discount_auto:
        amount, label = bundle_discount(list(invoice.items.all()))
        invoice.discount_amount, invoice.discount_label = amount, label
        invoice.save(update_fields=['discount_amount', 'discount_label', 'updated_at'])
    invoice.recalc_total()
    return invoice


def choices():
    """The discounts staff can pick in the invoice editor: every live
    promotion and coupon, as ``{id, name, kind, value, bundle}`` for the page."""
    from apps.shop.models import Discount
    from apps.shop.pricing import is_bundle

    rows = []
    for discount in Discount.objects.filter(is_active=True).prefetch_related('products'):
        if not discount.is_live:
            continue
        rows.append({
            'id': discount.pk, 'name': discount.name, 'kind': discount.discount_type,
            'value': str(discount.value), 'bundle': is_bundle(discount),
            'products': [p.pk for p in discount.products.all()] if is_bundle(discount) else [],
            'product_names': [p.name for p in discount.products.all()] if is_bundle(discount) else [],
            'min_spend': str(discount.min_spend or 0),
        })
    return rows


def apply_choice(invoice, choice):
    """Apply the editor's discount choice (see ``InvoiceEditForm``) to a saved
    invoice and recalculate it. A picked promotion is priced here, on the
    lines as saved: a fixed one is its rand value, a percentage one that share
    of the lines."""
    if choice and choice.startswith('promo:') and choice[6:].isdigit():
        from apps.shop.models import Discount
        promotion = Discount.objects.filter(pk=int(choice[6:])).first()
        if promotion is not None:
            lines = sum((_money(item.amount) for item in invoice.items.all()), ZERO)
            invoice.discount_amount = promotion.amount_for(lines)
            invoice.discount_label = promotion.name[:120]
            invoice.discount_auto = False
            invoice.save(update_fields=['discount_amount', 'discount_label', 'discount_auto',
                                        'updated_at'])
    return apply_auto(invoice)
