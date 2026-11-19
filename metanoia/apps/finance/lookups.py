"""Search + filters for invoice customers and items, shared by the Products &
services list and the editor pickers (:mod:`apps.finance.views_catalogue`).

Every function takes a plain ``params`` mapping (``request.GET``). Search takes
an optional *field* ("all" = any listed field); every whitespace-separated word
must match, so "milpark frep" finds the Milpark Financial Reporting fee.

On items, "All fields" also searches who the item is for (institution and
programme), so a search such as "unisa tax" works.
"""
from django.contrib.auth import get_user_model
from django.db.models import Q

CUSTOMER_FIELDS = [('all', 'All fields'), ('name', 'Name'), ('email', 'E-mail'), ('phone', 'Phone')]
ITEM_FIELDS = [('all', 'All fields'), ('name', 'Name'), ('sku', 'SKU / code'), ('description', 'Description')]

_CUSTOMER_LOOKUPS = {
    'name': ['first_name__icontains', 'last_name__icontains',
             'profile__first_name__icontains', 'profile__last_name__icontains', 'username__icontains'],
    'email': ['email__icontains'],
    'phone': ['profile__phone__icontains', 'profile__contact__primary_phone__icontains'],
}
_ITEM_LOOKUPS = {
    'name': ['name__icontains'],
    'sku': ['sku__icontains', 'module__code__icontains', 'programme__code__icontains'],
    'description': ['summary__icontains', 'description__icontains'],
}
_ITEM_ALL_EXTRA = ['institution__code__icontains', 'institution__name__icontains',
                   'programme__name__icontains', 'programme__abbreviation__icontains',
                   'category__name__icontains']


def _words(params):
    """The search words in ``params["q"]`` (at most six)."""
    return [w for w in (params.get('q') or '').split() if w][:6]


def _search(qs, params, lookups, extra_all=()):
    """Filter ``qs`` so every search word matches one of the chosen field's lookups."""
    field = params.get('field') or 'all'
    if field in lookups:
        paths = lookups[field]
    else:
        paths = [p for group in lookups.values() for p in group] + list(extra_all)
    for word in _words(params):
        match = Q()
        for path in paths:
            match |= Q(**{path: word})
        qs = qs.filter(match)
    return qs


def _int(params, name):
    """``params[name]`` as an integer, or ``None``."""
    try:
        return int(params.get(name) or '')
    except (TypeError, ValueError):
        return None


def customers(params):
    """Active users matching ``params``: q, field, role, institution, programme."""
    qs = get_user_model().objects.filter(is_active=True, profile__preview_of__isnull=True)
    qs = _search(qs, params, _CUSTOMER_LOOKUPS)
    role = params.get('role')
    if role:
        qs = qs.filter(profile__user_type=role)
    programme = _int(params, 'programme')
    institution = _int(params, 'institution')
    if programme:
        qs = qs.filter(profile__programme_enrolments__programme_id=programme,
                       profile__programme_enrolments__is_active=True)
    elif institution:
        qs = qs.filter(profile__programme_enrolments__programme__institution_id=institution,
                       profile__programme_enrolments__is_active=True)
    return qs.distinct().order_by('first_name', 'last_name', 'email')


def customer_name(user):
    """The account's name, else the profile's — registration fills only the latter."""
    name = user.get_full_name()
    person = getattr(user, 'profile', None)
    if not name and person is not None:
        name = f'{person.first_name} {person.last_name}'.strip()
    return name


def customer_label(user):
    """A customer's name and e-mail for a picker, falling back to whichever exists."""
    name = customer_name(user)
    return f'{name} · {user.email}' if name and user.email else (name or user.email or user.get_username())


def customer_rows(users):
    """``users`` as picker rows: name, e-mail, role and what they study."""
    from apps.learning.models import ProgrammeEnrolment

    users = list(users)
    ids = [u.pk for u in users]
    studies = {}
    for enrolment in (ProgrammeEnrolment.objects.filter(person__user_id__in=ids, is_active=True)
                      .select_related('programme__institution', 'person')):
        studies.setdefault(enrolment.person.user_id, []).append(
            f'{enrolment.programme.institution.code} {enrolment.programme.short_code}')
    rows = []
    for user in users:
        person = getattr(user, 'profile', None)
        name = customer_name(user)
        rows.append({
            'id': user.pk,
            'name': name or user.email or user.get_username(),
            'email': user.email,
            'label': customer_label(user),
            'role': person.get_user_type_display() if person else '',
            'phone': (person.phone if person else '') or '',
            'studies': studies.get(user.pk, []),
        })
    return rows


ITEM_SOURCES = [('module', 'Module fees'), ('programme', 'Programme fees'), ('other', 'Other items')]
ITEM_LISTINGS = [('shop', 'In the shop'), ('invoice', 'Invoice only')]


def items(params, *, active_only=True):
    """Catalogue items matching ``params``: q, field, type, institution
    (an id or ``general``), programme, category, source, listing, status.

    Results put what staff type most first: tuition, then the rest, by name.
    """
    from apps.shop.models import Product

    qs = Product.objects.select_related('category', 'institution', 'programme__institution', 'module')
    status = params.get('status')
    if active_only and not status:
        status = 'active'
    if status in ('active', 'inactive'):
        qs = qs.filter(status=status)
    qs = _search(qs, params, _ITEM_LOOKUPS, _ITEM_ALL_EXTRA)
    kind = params.get('type')
    if kind in (Product.KIND_PRODUCT, Product.KIND_SERVICE):
        qs = qs.filter(kind=kind)
    if params.get('institution') == 'general':
        qs = qs.filter(institution__isnull=True)
    elif _int(params, 'institution'):
        qs = qs.filter(institution_id=_int(params, 'institution'))
    if _int(params, 'programme'):
        qs = qs.filter(programme_id=_int(params, 'programme'))
    if _int(params, 'category'):
        qs = qs.filter(category_id=_int(params, 'category'))
    source = params.get('source')
    if source in (Product.UNLOCKS_MODULE, Product.UNLOCKS_PROGRAMME):
        qs = qs.filter(unlocks=source)
    elif source == 'other':
        qs = qs.filter(unlocks='')
    listing = params.get('listing')
    if listing == 'shop':
        qs = qs.filter(shop_listed=True)
    elif listing == 'invoice':
        qs = qs.filter(shop_listed=False)
    return qs.order_by('unlocks', 'institution__order', 'programme__order', 'name')


def item_for_label(product):
    """Who an item is for, in a few characters: ``MILPARK PGDA``, ``UNISA``, ``General``."""
    if product.programme_id:
        return f'{product.programme.institution.code} {product.programme.short_code}'
    if product.institution_id:
        return product.institution.code
    return 'General'


def item_row(product):
    """The JSON-ready row for one catalogue item in a picker."""
    return {
        'id': product.pk,
        'name': product.name,
        'sku': product.sku,
        'price': str(product.price),
        'unit': product.unit,
        'summary': product.summary,
        'kind': product.get_kind_display(),
        'for': item_for_label(product),
        'category': product.category.name if product.category_id else '',
        'unlocks': product.unlocks,
        'shop': product.shop_listed,
    }


def filter_options():
    """Institutions, programmes, categories and roles for the filter menus."""
    from apps.accounts.models import USER_TYPE_CHOICES
    from apps.learning.models import Institution, Programme
    from apps.shop.models import ProductCategory

    return {
        'institutions': [{'id': i.pk, 'label': f'{i.code} — {i.name}' if i.code != i.name else i.name}
                         for i in Institution.objects.order_by('order', 'name')],
        'programmes': [{'id': p.pk, 'label': f'{p.institution.code} {p.short_code}', 'institution': p.institution_id}
                       for p in Programme.objects.select_related('institution')
                       .order_by('institution__order', 'order', 'name')],
        'categories': [{'id': c.pk, 'label': c.name}
                       for c in ProductCategory.objects.filter(status='active').order_by('name')],
        'roles': [{'id': key, 'label': label} for key, label in USER_TYPE_CHOICES],
        'customer_fields': [{'id': k, 'label': v} for k, v in CUSTOMER_FIELDS],
        'item_fields': [{'id': k, 'label': v} for k, v in ITEM_FIELDS],
        'sources': [{'id': k, 'label': v} for k, v in ITEM_SOURCES],
        'listings': [{'id': k, 'label': v} for k, v in ITEM_LISTINGS],
    }
