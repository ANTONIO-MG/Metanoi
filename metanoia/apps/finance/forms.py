"""ModelForms for the finance pages (Bootstrap-styled to match the theme).

Finance must not import shop or learning at module level (shop is wired
separately, and learning would create an import cycle), so those models are
imported inside methods or looked up by name.
"""

from decimal import Decimal

from django import forms
from django.apps import apps as django_apps

from core.forms import DateInput, StyledModelForm, style_fields

from . import models


class InvoiceForm(StyledModelForm):
    """Edit an invoice's header fields (customer, order, dates, status, tax and notes)."""
    class Meta:
        model = models.Invoice
        fields = ['customer', 'order', 'issue_date', 'due_date', 'status', 'tax_amount', 'notes']
        widgets = {
            'issue_date': DateInput(), 'due_date': DateInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }


class InvoiceCreateForm(forms.Form):
    """The shared fields of a bulk invoice run (``finance:create-invoice``).

    A plain ``Form``: the view creates one Invoice per recipient. It ensures
    ``due_date`` is a real ``date`` — a raw POST string made ``is_overdue``
    (via the InvoiceItem post-save signal) raise.
    """

    AUDIENCE_USER = 'user'
    AUDIENCE_MODULE = 'module'
    AUDIENCE_CHOICES = [
        (AUDIENCE_USER, 'Individual user(s)'),
        (AUDIENCE_MODULE, 'Everyone enrolled in a module'),
    ]

    audience = forms.ChoiceField(choices=AUDIENCE_CHOICES, initial=AUDIENCE_USER)
    due_date = forms.DateField(required=False, widget=DateInput())
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))
    email_now = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, **kwargs):
        """Fill the module choices, importing the learning models lazily."""
        super().__init__(*args, **kwargs)
        from django.apps import apps as django_apps

        Product = django_apps.get_model('shop', 'Product')
        ProgrammeModule = django_apps.get_model('learning', 'ProgrammeModule')
        self.fields['product'] = forms.ModelChoiceField(
            queryset=Product.objects.filter(status='active').order_by('name'),
            required=False, empty_label='— none (add line items later) —',
            label='Initial item')
        self.fields['programme_module'] = forms.ModelChoiceField(
            queryset=ProgrammeModule.objects.filter(is_active=True)
                                            .select_related('programme')
                                            .order_by('programme__name', 'order', 'name'),
            required=False, empty_label='— choose —', label='Module')
        style_fields(self)

    def clean(self):
        """Require a module when the audience is everyone enrolled in a module."""
        cleaned = super().clean()
        if cleaned.get('audience') == self.AUDIENCE_MODULE and not cleaned.get('programme_module'):
            self.add_error('programme_module', 'Choose the module to invoice.')
        return cleaned


class InvoiceItemForm(StyledModelForm):
    """Add a line item — pick a product/course to auto-fill the
    description + price, or type a free-text line from scratch."""

    class Meta:
        model = models.InvoiceItem
        fields = ['product', 'description', 'quantity', 'unit_price']
        widgets = {
            'description': forms.TextInput(attrs={'placeholder': 'Leave blank to use the selected item'}),
        }

    def __init__(self, *args, **kwargs):
        """Make product and description optional, since either one is enough."""
        super().__init__(*args, **kwargs)
        self.fields['product'].required = False
        self.fields['description'].required = False
        self.fields['product'].empty_label = '— choose a product / course —'

    def clean(self):
        """Require a product or a description."""
        cleaned = super().clean()
        if not cleaned.get('product') and not cleaned.get('description'):
            self.add_error('description', 'Pick an item or enter a description.')
        return cleaned


def _module_choice_label(enrolment):
    """``FR — Financial Reporting · SAICA IAC (free week, 3 days left)``."""
    module = enrolment.programme_module
    if enrolment.status == enrolment.STATUS_ACTIVE and enrolment.is_unlocked:
        state = f'paid until {enrolment.paid_until:%d %b %Y}' if enrolment.paid_until else 'paid'
    elif enrolment.is_trial_valid:
        state = f'free week, {enrolment.trial_days_left} day(s) left'
    elif enrolment.status == enrolment.STATUS_TRIAL:
        state = 'free week ended'
    else:
        state = enrolment.get_status_display().lower()
    return f'{module.code} — {module.display_name} · {module.programme} ({state})'


class GrantAccessForm(forms.Form):
    """Record a payment and open a student's modules, in one action.

    Admin/staff only. Amount and proof are asked for with the access because
    nobody's account should open without a record of what paid for it. The
    document is optional (cash), but then staff are on record as vouching.

    Proof documents (``ALLOWED``) are shown inline and downloaded by staff, so
    the types accepted are the ones a bank actually produces. The module boxes
    offer the chosen student's own enrolments.
    """
    student = forms.ModelChoiceField(
        queryset=None, label='Student',
        help_text='Whose modules are being opened.')
    amount = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, label='Amount received (R)')
    months = forms.IntegerField(
        min_value=1, max_value=24, initial=1, label='Months of access',
        help_text='Modules are priced per month; this is how many are paid for.')
    method = forms.ChoiceField(
        choices=[('bank', 'EFT / bank transfer'), ('cash', 'Cash'), ('card', 'Card'),
                 ('cheque', 'Cheque'), ('online', 'Other online')],
        initial='bank', label='How it was paid')
    paid_on = forms.DateField(required=False, widget=DateInput(), label='Date paid',
                              help_text='The date on the deposit slip. Defaults to today.')
    reference = forms.CharField(max_length=120, required=False, label='Bank reference')
    document = forms.FileField(
        required=False, label='Proof of payment',
        help_text='A photograph or a PDF of the deposit slip. Filed against the student '
                  'and shown in their payment history.')
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}),
                           label='Note', help_text='Anything worth remembering about this payment.')
    modules = forms.ModelMultipleChoiceField(
        queryset=None, required=False, widget=forms.CheckboxSelectMultiple,
        label='Modules to open',
        help_text='Tick the modules this grants. Leave all unticked to open every module '
                  'the student has that is not already paid.')

    ALLOWED = ('.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic')
    MAX_BYTES = 10 * 1024 * 1024

    def __init__(self, *args, **kwargs):
        """Offer the active students and the chosen student's own module enrolments."""
        from apps.accounts.models import Person

        super().__init__(*args, **kwargs)
        from apps.learning.models import ModuleEnrolment

        self.fields['student'].queryset = (
            Person.objects.filter(user_type='student', preview_of__isnull=True)
            .select_related('user').order_by('first_name', 'last_name'))
        student_id = self.data.get('student') if self.is_bound else self.initial.get('student')
        student_id = getattr(student_id, 'pk', student_id)
        modules = ModuleEnrolment.objects.none()
        if str(student_id or '').isdigit():
            modules = (ModuleEnrolment.objects.filter(person_id=int(student_id))
                       .select_related('programme_module__programme')
                       .order_by('programme_module__programme__name', 'programme_module__order'))
        self.fields['modules'].queryset = modules
        self.fields['modules'].label_from_instance = _module_choice_label
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs['class'] = 'form-check-input'
            elif isinstance(widget, forms.Select):
                widget.attrs['class'] = 'form-control form-select'
            else:
                widget.attrs['class'] = 'form-control'

    def clean(self):
        """Refuse modules that belong to a different student."""
        cleaned = super().clean()
        student, modules = cleaned.get('student'), cleaned.get('modules')
        if student and modules and any(m.person_id != student.pk for m in modules):
            self.add_error('modules', 'Those modules belong to a different student.')
        return cleaned

    def clean_document(self):
        """Accept only the document types in ``ALLOWED``."""
        document = self.cleaned_data.get('document')
        if not document:
            return document
        name = (document.name or '').lower()
        if not name.endswith(self.ALLOWED):
            raise forms.ValidationError(
                'Upload a PDF or an image (PNG, JPG, GIF, WEBP or HEIC).')
        if document.size > self.MAX_BYTES:
            raise forms.ValidationError('That file is larger than 10 MB — please compress it.')
        return document


def _customer_field(form):
    """Set up ``form.fields["customer"]`` as a hidden field over active users.

    The customer is chosen through the search picker (``finance-lookup.js``), which
    fills the hidden input in.
    """
    from django.contrib.auth import get_user_model
    field = form.fields['customer']
    field.queryset = get_user_model().objects.filter(is_active=True).order_by('first_name', 'last_name', 'email')
    field.label_from_instance = lambda u: (f'{u.get_full_name()} · {u.email}' if u.get_full_name() else u.email or u.get_username())
    field.widget = forms.HiddenInput()
    field.error_messages['required'] = 'Search for the customer and pick them from the list.'


class InvoiceEditForm(StyledModelForm):
    """The invoice editor's header: customer, dates, notes and the invoice discount.

    ``discount_choice`` says how the invoice discount is chosen: ``auto`` (the
    bundles the lines earn, e.g. "All 4 modules"), ``none``, ``custom`` (typed by
    hand), or ``promo:<id>`` (a promotion from Shop -> Discounts, priced in rand).
    """
    discount_choice = forms.CharField(required=False, initial='auto', widget=forms.HiddenInput())

    class Meta:
        model = models.Invoice
        fields = ['customer', 'issue_date', 'due_date', 'summary', 'po_number', 'notes', 'footer',
                  'discount_amount', 'discount_label']
        labels = {'discount_amount': 'Discount (R)', 'discount_label': 'Discount name'}
        widgets = {'issue_date': DateInput(), 'due_date': DateInput(),
                   'notes': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Notes / terms, e.g. banking details or what the fee covers'}),
                   'summary': forms.TextInput(attrs={'placeholder': 'e.g. Semester 2 tuition — FREP'})}

    def __init__(self, *args, **kwargs):
        """Use the customer picker and make the discount amount an optional, non-negative number.
        """
        super().__init__(*args, **kwargs)
        _customer_field(self)
        self.fields['discount_amount'].required = False
        self.fields['discount_amount'].widget.attrs.update({'step': '0.01', 'min': 0})
        self.fields['discount_label'].widget.attrs['placeholder'] = 'e.g. Bursary, Loyalty'
        if not self.is_bound:
            invoice = self.instance
            if invoice.pk is None or invoice.discount_auto:
                self.initial['discount_choice'] = 'auto'
            elif not invoice.discount_amount:
                self.initial['discount_choice'] = 'none'
            else:
                self.initial['discount_choice'] = 'custom'

    def clean_discount_amount(self):
        """Treat a blank discount as zero and refuse a negative one."""
        amount = self.cleaned_data.get('discount_amount') or Decimal('0')
        if amount < 0:
            raise forms.ValidationError('A discount cannot be negative.')
        return amount

    def clean(self):
        """Validate ``discount_choice`` against the allowed forms."""
        cleaned = super().clean()
        choice = (cleaned.get('discount_choice') or 'auto').strip()
        if choice not in ('auto', 'none', 'custom') and not (
                choice.startswith('promo:') and choice[6:].isdigit()):
            choice = 'custom'
        cleaned['discount_choice'] = choice
        if choice == 'none':
            cleaned['discount_amount'], cleaned['discount_label'] = Decimal('0'), ''
        elif choice != 'auto' and cleaned.get('discount_amount') and not cleaned.get('discount_label'):
            cleaned['discount_label'] = 'Discount'
        return cleaned

    def save(self, commit=True):
        """Save, recording whether the discount is worked out automatically."""
        invoice = super().save(commit=False)
        invoice.discount_auto = self.cleaned_data.get('discount_choice') == 'auto'
        if commit:
            invoice.save()
        return invoice


class EstimateEditForm(StyledModelForm):
    """The estimate editor's header: customer, dates, summary, PO number, notes and footer."""
    class Meta:
        model = models.Estimate
        fields = ['customer', 'issue_date', 'valid_until', 'summary', 'po_number', 'notes', 'footer']
        widgets = {'issue_date': DateInput(), 'valid_until': DateInput(),
                   'notes': forms.Textarea(attrs={'rows': 3}),
                   'summary': forms.TextInput(attrs={'placeholder': 'e.g. APC preparation package, 2027 intake'})}

    def __init__(self, *args, **kwargs):
        """Use the customer picker."""
        super().__init__(*args, **kwargs)
        _customer_field(self)


class _LineForm(StyledModelForm):
    """Shared behaviour of an invoice or estimate line in the document editor.

    The product is picked through the item search in the description box
    (``finance-lookup.js``), so it is a hidden field.
    """
    def __init__(self, *args, **kwargs):
        """Limit products to active ones and hide the product field behind the item search."""
        super().__init__(*args, **kwargs)
        from apps.shop.models import Product
        self.fields['product'].queryset = Product.objects.filter(status='active').order_by('name')
        self.fields['product'].required = False
        self.fields['product'].widget = forms.HiddenInput()
        self.fields['description'].widget.attrs.update({
            'placeholder': 'Search products & services, or type a one-off item',
            'data-item-search': '', 'class': 'form-control tc-pick__input'})
        self.fields['detail'].widget.attrs['placeholder'] = 'Description (optional)'
        self.fields['quantity'].widget.attrs['min'] = 1
        self.fields['unit_price'].widget.attrs['step'] = '0.01'
        if 'discount_percent' in self.fields:
            self.fields['discount_percent'].widget.attrs.update({'step': '0.5', 'min': 0, 'max': 100})

    def has_changed(self):
        """Report a new, completely empty line as unchanged.

        A line the editor added but nobody filled in (no item, no name, no price) is
        not a line, so it is skipped rather than failing the whole invoice.
        """
        if not self.instance.pk:
            prefix = self.prefix + '-'
            filled = [self.data.get(prefix + name, '').strip()
                      for name in ('product', 'description', 'unit_price')]
            if not filled[0] and not filled[1] and filled[2] in ('', '0', '0.00'):
                return False
        return super().has_changed()

    def clean(self):
        """Require a name or a shop item on every line that is not being deleted."""
        data = super().clean()
        if not self.cleaned_data.get('DELETE') and not data.get('description') and not data.get('product'):
            self.add_error('description', 'Name the item or pick one from the shop.')
        return data


class InvoiceLineForm(_LineForm):
    """An invoice line. There is no per-line discount: an invoice carries one
    discount for the whole invoice (``InvoiceEditForm.discount_*``).
    """
    class Meta:
        model = models.InvoiceItem
        fields = ['product', 'description', 'detail', 'quantity', 'unit_price']


class EstimateLineForm(_LineForm):
    """An estimate line, which may carry its own percentage discount."""
    class Meta:
        model = models.EstimateItem
        fields = ['product', 'description', 'detail', 'quantity', 'unit_price', 'discount_percent']


InvoiceLineFormSet = forms.inlineformset_factory(models.Invoice, models.InvoiceItem, form=InvoiceLineForm,
                                                 extra=0, can_delete=True)
EstimateLineFormSet = forms.inlineformset_factory(models.Estimate, models.EstimateItem, form=EstimateLineForm,
                                                  extra=0, can_delete=True)


class ExpenseForm(StyledModelForm):
    """An expense, with an optional repeat schedule and a vendor that can be typed in new."""
    REPEAT_CHOICES = [('', 'Does not repeat'), ('1:weekly', 'Every week'), ('1:monthly', 'Every month'),
                      ('3:monthly', 'Every quarter'), ('1:yearly', 'Every year')]
    repeat = forms.ChoiceField(choices=REPEAT_CHOICES, required=False, label='Repeat')
    repeat_until = forms.DateField(required=False, widget=DateInput(), label='Until (blank = never ends)')
    new_vendor = forms.CharField(required=False, label='…or a new vendor',
                                 widget=forms.TextInput(attrs={'placeholder': 'New vendor name'}))

    class Meta:
        model = models.Expense
        fields = ['date', 'category', 'vendor', 'description', 'amount', 'vat_treatment', 'paid_through',
                  'reference', 'status', 'due_date', 'institution', 'receipt', 'notes']
        widgets = {'date': DateInput(), 'due_date': DateInput(), 'notes': forms.Textarea(attrs={'rows': 2}),
                   'amount': forms.NumberInput(attrs={'step': '0.01', 'min': '0'})}
        labels = {'institution': 'Cost centre', 'vat_treatment': 'VAT'}

    def __init__(self, *args, **kwargs):
        """Offer only active categories and vendors; the vendor is optional."""
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = models.ExpenseCategory.objects.filter(is_active=True)
        self.fields['vendor'].queryset = models.Vendor.objects.filter(is_active=True)
        self.fields['vendor'].required = False
        if self.instance.pk:
            del self.fields['repeat'], self.fields['repeat_until']

    def clean(self):
        """Require an amount above zero."""
        data = super().clean()
        if data.get('amount') is not None and data['amount'] <= 0:
            self.add_error('amount', 'Enter an amount above zero.')
        return data

    def save(self, commit=True):
        """Save, creating the typed-in vendor when one is given."""
        expense = super().save(commit=False)
        name = (self.cleaned_data.get('new_vendor') or '').strip()
        if name and not expense.vendor_id:
            expense.vendor, _ = models.Vendor.objects.get_or_create(name=name[:160])
        if commit:
            expense.save()
        return expense


class RecurringExpenseForm(StyledModelForm):
    """A recurring expense template."""
    class Meta:
        model = models.RecurringExpense
        fields = ['name', 'category', 'vendor', 'description', 'amount', 'vat_treatment', 'paid_through',
                  'institution', 'every', 'frequency', 'start_date', 'end_date', 'next_date', 'is_active', 'notes']
        widgets = {'start_date': DateInput(), 'end_date': DateInput(), 'next_date': DateInput(),
                   'notes': forms.Textarea(attrs={'rows': 2})}
        labels = {'institution': 'Cost centre', 'next_date': 'Next expense on'}


class VendorForm(StyledModelForm):
    """A vendor (supplier) record."""
    class Meta:
        model = models.Vendor
        fields = ['name', 'email', 'phone', 'vat_number', 'notes', 'is_active']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2})}


class ExpenseCategoryForm(StyledModelForm):
    """An expense category."""
    class Meta:
        model = models.ExpenseCategory
        fields = ['name', 'code', 'group', 'description', 'is_active']


class CatalogueItemForm(forms.ModelForm):
    """A product or service as staff bill it: name, price, who it is for, and
    whether paying for it opens a module. The storefront-only details (files,
    sizes, photos, 1-on-1 booking rules) stay on the shop's own editor.

    The model is named rather than imported because finance does not import shop
    at module level. A shop item that is delivered (a file, a parcel, a booking) is
    a product or a 1-on-1 by what it does, so its type is not a choice here.
    """

    new_category = forms.CharField(required=False, label='…or a new category',
                                   widget=forms.TextInput(attrs={'placeholder': 'New category name'}))

    class Meta:
        model = django_apps.get_model('shop', 'Product')
        fields = ['kind', 'name', 'sku', 'category', 'summary', 'price', 'unit',
                  'institution', 'programme', 'module', 'unlocks', 'price_from_modules',
                  'shop_listed', 'status']
        labels = {'kind': 'Type', 'summary': 'Description on the invoice', 'price': 'Price (R)',
                  'institution': 'Institution', 'programme': 'Programme', 'module': 'Module'}
        widgets = {'kind': forms.RadioSelect,
                   'price': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
                   'summary': forms.TextInput(attrs={'placeholder': 'One line printed under the item name'}),
                   'unit': forms.TextInput(attrs={'placeholder': 'month, hour, each…', 'list': 'unitOptions'})}

    def __init__(self, *args, **kwargs):
        """Fill the scope and category choices, and fix the type of delivered shop items."""
        super().__init__(*args, **kwargs)
        from apps.learning.models import Programme, ProgrammeModule
        from apps.shop.models import ProductCategory

        self.fields['category'].queryset = ProductCategory.objects.filter(status='active').order_by('name')
        self.fields['category'].required = False
        self.fields['sku'].required = False
        self.fields['sku'].help_text = 'Leave blank to generate one.'
        self.fields['programme'].queryset = Programme.objects.select_related('institution').order_by(
            'institution__order', 'order', 'name')
        self.fields['programme'].label_from_instance = lambda p: f'{p.institution.code} {p.short_code} — {p.name}'
        self.fields['module'].queryset = ProgrammeModule.objects.select_related(
            'programme__institution', 'module').order_by('programme__institution__order', 'programme__order', 'order')
        self.fields['module'].label_from_instance = lambda m: f'{m.programme.institution.code} {m.programme.short_code} · {m.code} — {m.display_name}'
        self.fields['institution'].empty_label = 'General — everyone'
        self.fields['programme'].empty_label = 'All programmes'
        self.fields['module'].empty_label = 'No particular module'
        if self.instance.pk and self.instance.fulfilment != self.instance.FULFIL_NONE:
            self.fields['kind'].disabled = True
            self.fields['kind'].help_text = 'Set by how the shop delivers it.'
        style_fields(self)
        self.fields['kind'].widget.attrs['class'] = 'btn-check'
        for name in ('shop_listed', 'price_from_modules'):
            self.fields[name].widget.attrs['class'] = 'form-check-input'

    def clean(self):
        """Fill the scope (module, programme, institution) upwards from the most specific choice.
        """
        data = super().clean()
        module, programme, institution = data.get('module'), data.get('programme'), data.get('institution')
        if module:
            if programme and programme != module.programme:
                self.add_error('module', 'That module is not on the programme chosen.')
            programme = data['programme'] = module.programme
        if programme:
            if institution and institution != programme.institution:
                self.add_error('programme', 'That programme is not at the institution chosen.')
            data['institution'] = programme.institution
        unlocks = data.get('unlocks')
        if unlocks == 'module' and not module:
            self.add_error('module', 'Choose the module this fee opens.')
        if unlocks == 'programme' and not programme:
            self.add_error('programme', 'Choose the programme this fee opens.')
        if data.get('price_from_modules') and not unlocks:
            data['price_from_modules'] = False
        if data.get('price') is not None and data['price'] < 0:
            self.add_error('price', 'The price cannot be negative.')
        return data

    def save(self, commit=True):
        """Save, creating the typed-in category when one is given."""
        from apps.shop.models import ProductCategory

        item = super().save(commit=False)
        name = (self.cleaned_data.get('new_category') or '').strip()
        if name and not item.category_id:
            item.category, _ = ProductCategory.objects.get_or_create(name=name[:120])
        if item.pk is None:
            item.fulfilment = item.FULFIL_NONE
        if item.price_from_modules:
            if item.unlocks == item.UNLOCKS_MODULE and item.module_id:
                item.price = item.module.price_per_month or 0
            elif item.unlocks == item.UNLOCKS_PROGRAMME and item.programme_id:
                from apps.shop.catalogue import programme_price
                item.price = programme_price(item.programme)
        if commit:
            item.save()
        return item


class QuickItemForm(forms.Form):
    """The three-field "new item" in the invoice editor's item search."""
    name = forms.CharField(max_length=200)
    kind = forms.ChoiceField(choices=[('product', 'Product'), ('service', 'Service')], initial='service')
    price = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    unit = forms.CharField(max_length=20, required=False)
