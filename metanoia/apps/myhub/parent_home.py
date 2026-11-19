"""A parent's home page (``/myhub/`` for a parent account).

A parent has no studies of their own, so the student dashboard (stats, "Register
now", their own modules) means nothing to them. Their home lists the student(s)
linked to them; they pick the one to follow, and every child page — progress,
results, tasks, fees — then opens on that child (core.scoping.viewing_child).

Only children from :func:`core.scoping.children_of` are ever listed, so a parent
cannot be shown another family's student here.
"""
import logging
from decimal import Decimal

from django.shortcuts import render

from core.scoping import children_of, viewing_child

logger = logging.getLogger('apps')


def _outstanding(user):
    """What is still owed on this child's sent invoices."""
    try:
        from apps.finance.models import Invoice
        open_invoices = (Invoice.objects.filter(customer=user)
                         .exclude(status__in=[Invoice.STATUS_PAID, Invoice.STATUS_CANCELLED,
                                              Invoice.STATUS_DRAFT])
                         .prefetch_related('payments'))
        rows = [inv for inv in open_invoices if inv.balance > 0]
        return sum((inv.balance for inv in rows), Decimal('0')), len(rows)
    except Exception:                                   # pragma: no cover
        logger.exception('parent home: outstanding balance failed')
        return Decimal('0'), 0


def _card(child, selected_id):
    """The card for one child: name, programme, study tiles and what is owed."""
    from apps.accounts.context_processors import _study_tiles
    person = getattr(child, 'profile', None)
    name = (f'{getattr(person, "first_name", "")} {getattr(person, "last_name", "")}'.strip()
            or child.get_full_name() or child.get_username())
    programme = None
    try:
        programme = person.programme if person is not None else None
    except Exception:                                   # pragma: no cover
        programme = None
    owed, owed_count = _outstanding(child)
    return {
        'user': child, 'name': name, 'person': person,
        'avatar_url': getattr(person, 'avatar_url', ''),
        'programme': programme,
        'tiles': _study_tiles(child, person),
        'owed': owed, 'owed_count': owed_count,
        'selected': child.pk == selected_id,
    }


def parent_home(request):
    """List the parent's children; the one being followed is highlighted."""
    children = list(children_of(request.user).select_related('profile')
                    .order_by('first_name', 'last_name', 'pk'))
    selected = viewing_child(request)
    selected_id = selected.pk if selected is not None else None
    cards = [_card(child, selected_id) for child in children]
    current = next((c for c in cards if c['selected']), None)
    return render(request, 'myhub/parent-home.html', {
        'page_title': 'My children',
        'cards': cards,
        'current': current,
    })
