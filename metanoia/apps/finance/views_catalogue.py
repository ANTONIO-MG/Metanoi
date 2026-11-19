"""Products & services — the catalogue invoices bill from — and the two search
endpoints behind the invoice editor's customer and item pickers.

Admin and staff only. The items are shop :class:`~apps.shop.models.Product`
rows; tuition items (module and programme fees) are kept in step by
:mod:`apps.shop.catalogue`, everything else is added here or in the shop.

``PICKER_LIMIT`` is how many rows a picker shows; the filters are there to
narrow past it.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.roles import staff_required

from . import forms, lookups


PICKER_LIMIT = 25


def _product_model():
    """The shop ``Product`` model, imported lazily."""
    from apps.shop.models import Product
    return Product


@login_required
@staff_required
def items(request):
    """The catalogue list, searchable and paginated."""
    Product = _product_model()
    params = request.GET
    qs = lookups.items(params, active_only=False)
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    keep = request.GET.copy()
    keep.pop('page', None)
    return render(request, 'finance/items.html', {
        'page_title': 'Products & services', 'page': page, 'total': page.paginator.count,
        'sel': {k: params.get(k, '') for k in ('q', 'field', 'type', 'institution', 'programme',
                                               'category', 'source', 'listing', 'status')},
        'options': lookups.filter_options(), 'querystring': keep.urlencode(),
        'counts': {
            'all': Product.objects.count(),
            'products': Product.objects.filter(kind=Product.KIND_PRODUCT, status='active').count(),
            'services': Product.objects.filter(kind=Product.KIND_SERVICE, status='active').count(),
            'tuition': Product.objects.exclude(unlocks='').filter(status='active').count(),
        },
    })


def _item_editor(request, item):
    """Show and save the catalogue item editor for ``item`` (a new item when ``None``)."""
    Product = _product_model()
    creating = item is None
    if creating:
        item = Product(kind=request.GET.get('type') if request.GET.get('type') in ('product', 'service')
                       else Product.KIND_SERVICE, fulfilment=Product.FULFIL_NONE, shop_listed=False)
    if request.method == 'POST':
        form = forms.CatalogueItemForm(request.POST, instance=item)
        if form.is_valid():
            obj = form.save(commit=False)
            if creating:
                obj.created_by = request.user
            obj.save()
            messages.success(request, f'“{obj.name}” saved.')
            return redirect('finance:items')
        messages.error(request, 'Please correct the highlighted fields.')
    else:
        form = forms.CatalogueItemForm(instance=item, initial={'name': request.GET.get('name', '')} if creating else None)
    return render(request, 'finance/item-form.html', {
        'page_title': 'New product or service' if creating else f'Edit {item.name}',
        'form': form, 'item': item, 'creating': creating,
        'vat_registered': getattr(settings, 'VAT_REGISTERED', False),
        'module_prices': {str(m.pk): str(m.price_per_month or 0)
                          for m in form.fields['module'].queryset},
    })


@login_required
@staff_required
def item_add(request):
    """Add a catalogue item."""
    return _item_editor(request, None)


@login_required
@staff_required
def item_edit(request, pk):
    """Edit a catalogue item."""
    return _item_editor(request, get_object_or_404(_product_model(), pk=pk))


@login_required
@staff_required
def lookup_customers(request):
    """JSON search results for the invoice editor's customer picker."""
    qs = lookups.customers(request.GET).select_related('profile')
    total = qs.count()
    return JsonResponse({'total': total, 'results': lookups.customer_rows(qs[:PICKER_LIMIT])})


@login_required
@staff_required
def lookup_items(request):
    """JSON search results for the invoice editor's item picker."""
    qs = lookups.items(request.GET)
    total = qs.count()
    return JsonResponse({'total': total, 'results': [lookups.item_row(p) for p in qs[:PICKER_LIMIT]]})


@login_required
@staff_required
@require_POST
def item_quick(request):
    """Create a plain item from the invoice editor and hand it straight back."""
    Product = _product_model()
    form = forms.QuickItemForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'errors': {k: ' '.join(v) for k, v in form.errors.items()}}, status=400)
    data = form.cleaned_data
    item = Product.objects.create(
        name=data['name'].strip(), kind=data['kind'], price=data['price'], unit=(data['unit'] or '').strip(),
        fulfilment=Product.FULFIL_NONE, shop_listed=False, created_by=request.user)
    row = lookups.item_row(item)
    row['edit_url'] = reverse('finance:item-edit', args=[item.pk])
    return JsonResponse({'item': row}, status=201)
