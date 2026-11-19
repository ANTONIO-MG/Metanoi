"""Shared Bootstrap form helpers: native date pickers and theme field classes."""

from django import forms


class DateInput(forms.DateInput):
    input_type = 'date'

    def __init__(self, attrs=None, format='%Y-%m-%d'):
        super().__init__(attrs, format)


class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

    def __init__(self, attrs=None, format='%Y-%m-%dT%H:%M'):
        super().__init__(attrs, format)


def style_fields(form):
    """Add the theme's Bootstrap classes (and a label placeholder) to every widget."""
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect, forms.CheckboxSelectMultiple)):
            css = 'form-check-input'
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            css = 'form-control form-select'
        else:
            css = 'form-control'
        existing = widget.attrs.get('class', '')
        widget.attrs['class'] = (existing + ' ' + css).strip()
        if isinstance(widget, (forms.TextInput, forms.Textarea, forms.EmailInput,
                               forms.NumberInput, forms.URLInput)):
            widget.attrs.setdefault('placeholder', field.label)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_fields(self)
