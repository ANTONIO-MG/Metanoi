"""ModelForms for the shop CRUD / checkout pages (MyHub Bootstrap styling)."""

from django import forms

from core import validators as v
from core.forms import DateTimeInput, StyledModelForm

from . import models


class ProductCategoryForm(StyledModelForm):
    """Add / edit a product category."""

    class Meta:
        model = models.ProductCategory
        fields = ['name', 'slug', 'description', 'status']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}


class MultipleFileInput(forms.ClearableFileInput):
    """A file widget that lets the browser pick several files at once."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A file input that takes several files and cleans each one.

    ``validators`` run per file, so one oversized PDF in a batch of five names
    that file rather than rejecting the lot with no clue why.
    """

    def __init__(self, *args, file_validators=(), **kwargs):
        """Default to :class:`MultipleFileInput`, optional, with per-file ``file_validators``."""
        kwargs.setdefault('widget', MultipleFileInput())
        kwargs.setdefault('required', False)
        self.file_validators = list(file_validators)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        """Clean every upload, collecting each file's validation errors under its name."""
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        cleaned, errors = [], []
        for upload in files:
            upload = super().clean(upload, initial)
            for validator in self.file_validators:
                try:
                    validator(upload)
                except forms.ValidationError as exc:
                    errors.extend(f'{upload.name}: {message}' for message in exc.messages)
            cleaned.append(upload)
        if errors:
            raise forms.ValidationError(errors)
        return cleaned


class ProductForm(StyledModelForm):
    """Add / edit a product or service.

    The fulfilment type decides which sections the template shows
    (``data-for``); fields for other types are still saved, so switching type
    back loses nothing.
    """

    new_files = MultipleFileField(
        label='Add files', file_validators=v.validate_download,
        help_text='PDF, Word, Excel, PowerPoint or ZIP — up to 200 MB each. Select several at once.')
    new_images = MultipleFileField(
        label='Add gallery images', file_validators=v.validate_image,
        help_text='JPG, PNG or WebP, up to 5 MB each.')

    LENGTH_CHOICES = [(30, '30 min'), (45, '45 min'), (60, '1 hour'), (90, '1½ hours'),
                      (120, '2 hours'), (180, '3 hours')]
    BOOKING_SETTINGS = ('buffer_minutes', 'min_notice_hours', 'reschedule_cutoff_hours',
                        'booking_window_days')
    lengths = forms.TypedMultipleChoiceField(
        label='Session lengths students can book', choices=LENGTH_CHOICES, coerce=int,
        required=False, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = models.Product
        fields = ['fulfilment', 'name', 'category', 'summary', 'description',
                  'price', 'discount_percent', 'image', 'featured', 'status', 'sku',
                  'educator', 'duration', 'level', 'validity_days',
                  'buffer_minutes', 'min_notice_hours', 'reschedule_cutoff_hours', 'booking_window_days',
                  'track_stock', 'stock', 'weight_kg', 'length_cm', 'width_cm', 'height_cm',
                  'institution', 'programme', 'module',
                  'card_tag', 'card_headline', 'card_price_label', 'card_points', 'card_cta',
                  'card_use_image']
        labels = {
            'fulfilment': 'What are you selling?',
            'discount_percent': 'Discount %',
            'image': 'Cover image',
            'validity_days': 'Access lasts (days)',
            'track_stock': 'Track stock',
            'weight_kg': 'Weight (kg)', 'length_cm': 'Length (cm)',
            'width_cm': 'Width (cm)', 'height_cm': 'Height (cm)',
            'educator': 'Educator / author',
            'buffer_minutes': 'Gap between sessions (min)',
            'min_notice_hours': 'Book at least (hours ahead)',
            'reschedule_cutoff_hours': 'Reschedule until (hours before)',
            'booking_window_days': 'Bookable up to (days ahead)',
        }
        widgets = {
            'fulfilment': forms.RadioSelect,
            'description': forms.Textarea(attrs={'rows': 6}),
            'summary': forms.TextInput(attrs={'placeholder': 'One line shown on the product card'}),
            'card_tag': forms.TextInput(attrs={'placeholder': 'Test 3 preparation bundle'}),
            'card_headline': forms.TextInput(attrs={'placeholder': 'INTRO Test 1. Start strong'}),
            'card_price_label': forms.TextInput(attrs={'placeholder': 'Full bundle'}),
            'card_points': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Test 3: Wednesday, 13 January 2027\n21-page Blueprint, five topic packs\n37 resources, 410 marks of practice'}),
            'card_cta': forms.TextInput(attrs={'placeholder': 'Get the bundle'}),
            'price': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'discount_percent': forms.NumberInput(attrs={'step': '0.5', 'min': '0', 'max': '100'}),
        }

    def __init__(self, *args, **kwargs):
        """Style the fulfilment toggle and session-length checkboxes, make the SKU
        optional (one is generated) and relax the booking settings: they have
        sensible model defaults, and only a 1-on-1 needs them.
        """
        super().__init__(*args, **kwargs)
        self.fields['fulfilment'].widget.attrs['class'] = 'btn-check'
        self.fields['sku'].required = False
        self.fields['sku'].help_text = 'Leave blank to generate one.'
        self.fields['lengths'].initial = self.instance.lengths
        for name in self.BOOKING_SETTINGS:
            self.fields[name].required = False
        self.fields['lengths'].widget.attrs['class'] = 'form-check-input'

    def save(self, commit=True):
        """Store the chosen session lengths as a sorted comma-separated list, then save."""
        product = super().save(commit=False)
        lengths = self.cleaned_data.get('lengths')
        if lengths:
            product.session_lengths = ','.join(str(m) for m in sorted(lengths))
        if commit:
            product.save()
            self.save_m2m()
        return product

    def clean(self):
        """Fill blank booking settings from their model defaults, require a session
        length for a booked product and at least one file for a digital one.
        """
        data = super().clean()
        for name in self.BOOKING_SETTINGS:
            if data.get(name) is None:
                data[name] = models.Product._meta.get_field(name).default
        if data.get('fulfilment') == models.Product.FULFIL_BOOKING and not data.get('lengths'):
            self.add_error('lengths', 'Choose at least one session length.')
        if data.get('fulfilment') == models.Product.FULFIL_DIGITAL:
            has_files = bool(self.instance.pk and self.instance.deliverables())
            if not has_files and not data.get('new_files'):
                self.add_error('new_files', 'A digital product needs at least one file to deliver.')
        return data


class ProductVariantForm(StyledModelForm):
    """Add / edit one variant (size, colour…) of a product."""

    class Meta:
        model = models.ProductVariant
        fields = ['name', 'sku', 'price_adjustment', 'stock', 'is_active']
        widgets = {'price_adjustment': forms.NumberInput(attrs={'step': '0.01'})}


ProductVariantFormSet = forms.inlineformset_factory(
    models.Product, models.ProductVariant, form=ProductVariantForm,
    extra=0, can_delete=True)


def educator_choices():
    """People who may run 1-on-1s: educators, and staff/admin who also teach."""
    from apps.accounts.models import Person
    return (Person.objects.filter(user_type__in=('educator', 'staff', 'admin'), user__is_active=True)
            .select_related('user').order_by('user__first_name', 'user__last_name'))


class EducatorRateForm(StyledModelForm):
    """An educator's hourly 1-on-1 rate."""

    class Meta:
        model = models.EducatorRate
        fields = ['educator', 'hourly_rate', 'is_active']
        widgets = {'hourly_rate': forms.NumberInput(attrs={'step': '0.01', 'min': '0'})}

    def __init__(self, *args, **kwargs):
        """Limit the educator choice to :func:`educator_choices`."""
        super().__init__(*args, **kwargs)
        self.fields['educator'].queryset = educator_choices()


EducatorRateFormSet = forms.inlineformset_factory(
    models.Product, models.EducatorRate, form=EducatorRateForm, extra=0, can_delete=True)


class TimeInput(forms.TimeInput):
    """An HTML ``<input type="time">`` rendered as ``HH:MM``."""

    input_type = 'time'

    def __init__(self, attrs=None, format='%H:%M'):
        """Use the ``%H:%M`` format by default."""
        super().__init__(attrs, format)


class AvailabilityForm(StyledModelForm):
    """One weekly availability window (weekday, start, end) for an educator."""

    class Meta:
        model = models.EducatorAvailability
        fields = ['weekday', 'start_time', 'end_time']
        widgets = {'start_time': TimeInput(), 'end_time': TimeInput()}

    def clean(self):
        """The window must end after it starts."""
        data = super().clean()
        if data.get('start_time') and data.get('end_time') and data['end_time'] <= data['start_time']:
            self.add_error('end_time', 'Must be after the start.')
        return data


class TimeOffForm(StyledModelForm):
    """A period an educator is away and cannot be booked."""

    class Meta:
        model = models.EducatorTimeOff
        fields = ['start', 'end', 'reason']
        widgets = {'start': DateTimeInput(), 'end': DateTimeInput()}

    def clean(self):
        """The time off must end after it starts."""
        data = super().clean()
        if data.get('start') and data.get('end') and data['end'] <= data['start']:
            self.add_error('end', 'Must be after the start.')
        return data


def availability_formsets(person, data=None):
    """The ``(hours, away)`` inline formsets for ``person``'s availability and time off."""
    from apps.accounts.models import Person
    hours = forms.inlineformset_factory(Person, models.EducatorAvailability, form=AvailabilityForm,
                                        extra=0, can_delete=True)
    away = forms.inlineformset_factory(Person, models.EducatorTimeOff, form=TimeOffForm,
                                       extra=0, can_delete=True)
    return (hours(data, instance=person, prefix='hours'),
            away(data, instance=person, prefix='away'))


class ProductReviewForm(StyledModelForm):
    """A buyer's star rating and comment on a product."""

    class Meta:
        model = models.ProductReview
        fields = ['rating', 'comment']
        widgets = {
            'rating': forms.Select(choices=[(i, f'{i} ★') for i in range(5, 0, -1)]),
            'comment': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Share your experience…'}),
        }


class DiscountForm(StyledModelForm):
    """Admin/staff only — see the staff_required gate on the discount views."""

    class Meta:
        model = models.Discount
        fields = ['name', 'code', 'discount_type', 'value', 'institution', 'programme',
                  'category', 'products', 'starts_at', 'ends_at', 'min_spend',
                  'max_uses', 'is_active']
        widgets = {
            'starts_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'ends_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'code': forms.TextInput(attrs={'placeholder': 'Blank = automatic promotion'}),
            'products': forms.SelectMultiple(attrs={'size': 8}),
        }
        help_texts = {
            'products': 'Specific items. Blank = anything else in scope. An automatic '
                        'fixed-amount discount that names items is a bundle: it comes off '
                        'once, only when every named item is bought together.',
        }


class AddressForm(StyledModelForm):
    """A South African delivery address for courier orders."""

    class Meta:
        model = models.Address
        fields = ['recipient', 'phone', 'street_address', 'complex', 'suburb', 'city',
                  'province', 'postal_code', 'address_type', 'company']
        widgets = {'postal_code': forms.TextInput(attrs={'inputmode': 'numeric', 'maxlength': 4})}

    def clean_postal_code(self):
        """South African postal codes are four digits."""
        code = (self.cleaned_data.get('postal_code') or '').strip()
        if not (code.isdigit() and len(code) == 4):
            raise forms.ValidationError('South African postal codes are 4 digits.')
        return code

    def clean_phone(self):
        """Keep digits and ``+`` only; the courier needs a number it can SMS."""
        phone = ''.join(ch for ch in (self.cleaned_data.get('phone') or '') if ch.isdigit() or ch == '+')
        if len(phone.lstrip('+')) < 9:
            raise forms.ValidationError('Enter a mobile number the courier can SMS.')
        return phone
