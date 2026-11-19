"""Who may see which shop item.

An item is visible when either:

* it is **general** — ``institution`` is blank; or
* it belongs to **the viewer's own institution**, and either carries no
  ``programme`` or carries one the viewer is enrolled on (or, for a community
  member or anyone not enrolled yet, the institution and programme they picked
  when they joined — see :func:`spine_for`).

``module`` is deliberately *not* a gate, only a filter facet.

Everything that lists products goes through :func:`visible_products`, and every
page reached by a product id (product page, add to cart, the 1-on-1 calendar)
re-checks it. Admin and staff see the whole catalogue.
"""
from django.db.models import Q

from .models import Product


def _is_team(user):
    """Admin and staff (by flag or by role) see the whole catalogue."""
    from core.roles import role_of_user
    return bool(user.is_staff or user.is_superuser or role_of_user(user) in ('admin', 'staff'))


def spine_for(user):
    """``(institution_ids, programme_ids)`` whose items ``user`` may see.

    * a student — the programmes they are enrolled on (and their institutions);
    * an educator — the programmes they teach;
    * a community member, or anybody not enrolled yet — the institution and
      programme they picked when they joined (``Person.community_institution``
      / ``community_programme``).

    Both sets empty means "general items only".

    A person who is not enrolled (a community member, or a student
    mid-registration) gets what they told us they study.
    """
    from core.roles import role_of_user
    person = getattr(user, 'profile', None)
    if person is None:
        return set(), set()
    programme_ids, institution_ids = set(), set()
    try:
        if role_of_user(user) == 'educator':
            from core.scoping import _spine_ids
            institution_ids, programme_ids, _c, _o = _spine_ids(user)
        else:
            for pid, iid in person.enrolled_programmes.values_list('id', 'institution_id'):
                programme_ids.add(pid)
                institution_ids.add(iid)
    except Exception:          # pragma: no cover — a shop page must not 500
        return set(), set()
    if not programme_ids and not institution_ids:
        if person.community_programme_id:
            programme_ids.add(person.community_programme_id)
            institution_ids.add(person.community_programme.institution_id)
        if person.community_institution_id:
            institution_ids.add(person.community_institution_id)
    programme_ids.discard(None)
    institution_ids.discard(None)
    return institution_ids, programme_ids


def audience_for(request):
    """Whose catalogue this request should show.

    A parent is shopping for their child, so they see what the child sees — not
    the general-only catalogue their own (module-less) account would otherwise
    get, and not everything.
    """
    from core.roles import role_flags

    flags = role_flags(request)
    if flags.get('is_parent'):
        try:
            from core.scoping import viewing_child
            return viewing_child(request)
        except Exception:      # pragma: no cover
            return None
    return getattr(request, 'user', None)


def visible_products(user, queryset=None):
    """Narrow ``queryset`` (default: all active products) to what ``user`` may see.

    Invoice-only items (tuition fees) are billed by staff and never sold off the
    shelf, so they are always excluded. The team — the people who load the
    catalogue — sees all of the rest.
    """
    qs = queryset if queryset is not None else Product.objects.filter(status='active')
    qs = qs.filter(shop_listed=True)

    if user is None or not getattr(user, 'is_authenticated', False):
        return qs.filter(institution__isnull=True)

    if _is_team(user):
        return qs

    institution_ids, programme_ids = spine_for(user)
    if not institution_ids:
        return qs.filter(institution__isnull=True)

    return qs.filter(
        Q(institution__isnull=True)
        | (Q(institution_id__in=institution_ids)
           & (Q(programme__isnull=True) | Q(programme_id__in=programme_ids)))
    ).distinct()


def can_see(request, product):
    """True when ``product`` is in the catalogue this request shops from —
    the check every page reached by a product's id has to make, because an id
    in a URL is trivially guessable."""
    return visible_for_request(request, Product.objects.filter(pk=product.pk)).exists()


def visible_for_request(request, queryset=None):
    """:func:`visible_products` for whoever this request is shopping as."""
    return visible_products(audience_for(request), queryset)
