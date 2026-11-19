"""What a cart actually costs once discounts are applied.

One entry point, :func:`price_cart`, so the storefront, the cart page, checkout,
the invoice and the PayFast amount all quote the same number. Checkout freezes
this quote onto the order and the invoice (see :mod:`apps.shop.checkout`); it
used to copy the undiscounted line prices instead, so a buyer shown 20% off was
charged in full.

Rules — "best deal plus one coupon":

* Per line, the **best automatic saving** wins: the product's own discount % or
  the best live promotion covering it, whichever takes more off. They never
  compound.
* At most one **coupon** then applies on top, to what is left of each line it
  covers — otherwise a buyer can chain codes and pay nothing.
* A **percentage** coupon bites per line. A **fixed-amount** coupon is a cart
  saving: "R100 off" takes R100 off the order once, spread across the lines it
  covers — not R100 off every line.
* A minimum spend is judged on the cart subtotal, before any discount, and a
  discount that misses it is simply not a candidate. It does not knock out the
  others.
* A **bundle** is an automatic, fixed-amount promotion that names specific
  items ("All 4 modules" — R800 off when MACF, FREP, TAXA and CGAU are bought
  together). It applies **once**, only when every named item is in the cart,
  spread pro rata over those lines, and it replaces their per-line automatic
  saving when it takes more off. Its minimum spend is judged on the bundled
  lines. :func:`quote_modules` prices registration and module invoices with the
  same rule, so the cart, the registration summary and the invoice agree.
* Prices include VAT; ``vat`` is the VAT contained in the total.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from .models import Discount

CENT = Decimal('0.01')
ZERO = Decimal('0')


def _money(value):
    """``value`` as a :class:`~decimal.Decimal` rounded half-up to the cent."""
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def _pct_label(percent):
    """A percentage discount's label, e.g. ``12.5% off``."""
    percent = Decimal(str(percent)).normalize()
    return f'{percent:f}% off'


def _live_promotions():
    """Active automatic (code-less) discounts that are live right now."""
    return [d for d in Discount.objects.filter(is_active=True, code__isnull=True)
            .prefetch_related('products') if d.is_live]


def is_bundle(discount):
    """True for an automatic fixed-amount promotion that names specific items."""
    return (not discount.code and discount.discount_type == Discount.TYPE_AMOUNT
            and bool(_bundle_ids(discount)))


def _bundle_ids(discount):
    """Product ids a bundle discount names."""
    return {product.pk for product in discount.products.all()}


def live_bundles():
    """The live automatic promotions that are bundles."""
    return [d for d in _live_promotions() if is_bundle(d)]


def bundle_savings(gross_by_product, bundles=None):
    """Which bundles a set of items earns, and what each takes off which item.

    ``gross_by_product`` maps product id → gross amount. Returns a list of
    ``(bundle, {product_id: off})``. Biggest bundle first; an item counts towards
    one bundle at most.

    Within a bundle, the last item takes the rounding, so the parts add up exactly.
    """
    if bundles is None:
        bundles = live_bundles()
    present = {pk: _money(gross) for pk, gross in gross_by_product.items()}
    taken, out = set(), []
    for bundle in sorted(bundles, key=lambda d: d.value, reverse=True):
        ids = _bundle_ids(bundle)
        if not ids or not ids <= set(present) or ids & taken:
            continue
        pool = sum((present[pk] for pk in ids), ZERO)
        if pool <= 0 or (bundle.min_spend and pool < bundle.min_spend):
            continue
        budget = min(_money(bundle.value), pool)
        remaining, offs = budget, {}
        ordered = sorted(ids)
        for index, pk in enumerate(ordered):
            off = remaining if index == len(ordered) - 1 else min(
                _money(budget * present[pk] / pool), remaining)
            offs[pk] = off
            remaining -= off
        taken |= ids
        out.append((bundle, offs))
    return out


def quote_modules(pairs):
    """Price module fees the way the cart would: ``pairs`` is a list of
    ``(programme_module, monthly_gross)``.

    Each module is matched to its module-fee item in the catalogue (see
    :mod:`apps.shop.catalogue`) so a bundle promotion naming those items applies
    here exactly as it would in a cart. Returns ``{'lines', 'subtotal',
    'discount', 'total', 'applied'}``; each line is ``{'module', 'gross',
    'discount', 'label', 'payable'}``.
    """
    from .models import Product

    lines = [{'module': pm, 'gross': _money(gross), 'discount': ZERO, 'label': '',
              'payable': _money(gross)} for pm, gross in pairs]
    fee_ids = dict(Product.objects.filter(
        unlocks=Product.UNLOCKS_MODULE, module__in=[pm.pk for pm, _ in pairs])
        .values_list('module_id', 'pk'))
    by_product = {}
    for line in lines:
        pk = fee_ids.get(line['module'].pk)
        if pk is not None and pk not in by_product and line['gross'] > 0:
            by_product[pk] = line
    applied = []
    for bundle, offs in bundle_savings({pk: line['gross'] for pk, line in by_product.items()}):
        applied.append(bundle)
        for pk, off in offs.items():
            line = by_product[pk]
            line['discount'], line['label'] = off, bundle.name
            line['payable'] = line['gross'] - off
    subtotal = sum((line['gross'] for line in lines), ZERO)
    discount = sum((line['discount'] for line in lines), ZERO)
    return {'lines': lines, 'subtotal': subtotal, 'discount': discount,
            'total': subtotal - discount, 'applied': applied}


def find_coupon(code):
    """A live coupon for ``code``, or ``None``."""
    if not code:
        return None
    discount = (Discount.objects.filter(code=(code or '').strip().upper())
                .prefetch_related('products').first())
    return discount if discount and discount.is_live else None


def vat_in(amount):
    """VAT contained in a VAT-inclusive ``amount`` (0 when not VAT-registered)."""
    if not getattr(settings, 'VAT_REGISTERED', False):
        return ZERO
    rate = Decimal(str(getattr(settings, 'VAT_RATE', 15)))
    amount = Decimal(str(amount or 0))
    if amount <= 0 or rate <= 0:
        return ZERO
    return _money(amount * rate / (100 + rate))


def price_cart(order, coupon_code=None, *, shipping=ZERO):
    """Return a quote for ``order``.

    ``{'subtotal', 'discount', 'shipping', 'total', 'vat', 'lines', 'applied',
    'coupon', 'coupon_code', 'coupon_error'}``. ``lines`` carries the per-item
    breakdown (``item``, ``gross``, ``discount``, ``payable``, ``label``) so the
    cart shows what came off where, rather than one unexplained number.

    Bundles apply once per cart, and only where they beat the per-line savings.
    """
    items = list(order.items.select_related('product', 'product__category', 'variant'))
    subtotal = sum((_money(item.gross_total) for item in items), ZERO)
    coupon_code = (coupon_code or '').strip().upper()

    live = _live_promotions()
    bundles = [p for p in live if is_bundle(p)]
    promotions = [p for p in live if not is_bundle(p)
                  and (not p.min_spend or subtotal >= p.min_spend)]

    coupon, coupon_error = None, ''
    if coupon_code:
        coupon = find_coupon(coupon_code)
        if coupon is None:
            coupon_error = 'That code is not valid or has expired.'
        elif coupon.min_spend and subtotal < coupon.min_spend:
            coupon_error = f'Spend at least R{coupon.min_spend:,.2f} to use {coupon_code}.'
            coupon = None

    lines = []
    for item in items:
        gross = _money(item.gross_total)
        product = item.product

        best, best_off, label = None, ZERO, ''
        if product.has_discount:
            best_off = gross - _money(product.sale_price_for(item.unit_price) * item.quantity)
            label = _pct_label(product.discount_percent)
        for promotion in promotions:
            if not promotion.covers(product):
                continue
            off = promotion.amount_for(gross)
            if off > best_off:
                best, best_off, label = promotion, off, promotion.name
        lines.append({'item': item, 'gross': gross, 'discount': _money(best_off),
                      'label': label, 'payable': gross - _money(best_off), 'promotion': best})

    first_line = {}
    for line in lines:
        first_line.setdefault(line['item'].product_id, line)
    for bundle, offs in bundle_savings(
            {pk: line['gross'] for pk, line in first_line.items()}, bundles):
        covered = [first_line[pk] for pk in offs]
        if sum(offs.values(), ZERO) <= sum((line['discount'] for line in covered), ZERO):
            continue
        for pk, off in offs.items():
            line = first_line[pk]
            line['discount'], line['label'], line['promotion'] = off, bundle.name, bundle
            line['payable'] = line['gross'] - off
    applied = []
    for line in lines:
        if line['promotion'] is not None and line['promotion'] not in applied:
            applied.append(line['promotion'])

    if coupon is not None:
        covered = [line for line in lines if coupon.covers(line['item'].product) and line['payable'] > 0]
        if coupon.discount_type == Discount.TYPE_PERCENT:
            for line in covered:
                off = coupon.amount_for(line['payable'])
                _apply_coupon(line, off, coupon)
        else:
            _spread_fixed(covered, coupon)
        if any(line.get('coupon_off') for line in covered):
            applied.append(coupon)
        else:
            coupon_error = coupon_error or f'{coupon_code} does not apply to anything in your cart.'
            coupon = None

    discount = sum((line['discount'] for line in lines), ZERO)
    shipping = _money(shipping)
    total = subtotal - discount + shipping
    return {'subtotal': subtotal, 'discount': discount, 'shipping': shipping,
            'total': total, 'vat': vat_in(total), 'lines': lines, 'applied': applied,
            'coupon': coupon, 'coupon_code': coupon_code if coupon else '',
            'coupon_error': coupon_error}


def _apply_coupon(line, off, coupon):
    """Take ``off`` (capped at what is still payable) off one quote line for ``coupon``."""
    off = min(_money(off), line['payable'])
    if off <= 0:
        return
    line['coupon_off'] = off
    line['discount'] += off
    line['payable'] -= off
    line['label'] = f"{line['label']} + {coupon.code}" if line['label'] else coupon.code


def _spread_fixed(covered, coupon):
    """Take a fixed-amount coupon off the covered lines once, pro rata.

    The last line takes whatever rounding left over, so the lines add up to the coupon exactly.
    """
    pool = sum((line['payable'] for line in covered), ZERO)
    if pool <= 0:
        return
    budget = min(_money(coupon.value), pool)
    remaining = budget
    for index, line in enumerate(covered):
        if index == len(covered) - 1:
            off = remaining
        else:
            off = min(_money(budget * line['payable'] / pool), remaining)
        remaining -= off
        _apply_coupon(line, off, coupon)
