"""Forms for managing the academic spine (admin/staff only):
Institution → Programme → ProgrammeModule (offering) → Cohort.

Bootstrap-styled to match the dashboard. The module "add" form hides the
canonical-Module / offering split from the admin — they just add a module with a
code, name and monthly price, and the form get-or-creates the canonical
:class:`~apps.learning.models.Module` behind the scenes.
"""
from django import forms

from . import models


def _style(form):
    """Give every field of ``form`` the Bootstrap classes, and a placeholder taken from
    its label.
    """
    for _name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs['class'] = (widget.attrs.get('class', '') + ' form-check-input').strip()
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs['class'] = (widget.attrs.get('class', '') + ' form-control form-select').strip()
        else:
            widget.attrs['class'] = (widget.attrs.get('class', '') + ' form-control').strip()
        if isinstance(widget, (forms.TextInput, forms.Textarea, forms.NumberInput,
                               forms.EmailInput, forms.URLInput)):
            widget.attrs.setdefault('placeholder', field.label)


class _StyledModelForm(forms.ModelForm):
    """A :class:`~django.forms.ModelForm` whose widgets are Bootstrap-styled on
    creation.
    """
    def __init__(self, *args, **kwargs):
        """Build the form, then apply :func:`_style` to its fields."""
        super().__init__(*args, **kwargs)
        _style(self)


class InstitutionForm(_StyledModelForm):
    """Edit an institution: code, name, accent colour, logo, website and description."""
    class Meta:
        """Model fields shown, with a colour picker for the accent."""
        model = models.Institution
        fields = ['code', 'name', 'accent_colour', 'accent_name', 'logo',
                  'website', 'description', 'order', 'is_active']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'accent_colour': forms.TextInput(attrs={'type': 'color'}),
        }


class InstitutionBlueprintForm(InstitutionForm):
    """Create an institution *and* the structure it needs to be usable.

    An institution with no programmes and no modules is an empty shell: nobody
    can register for it and nothing can be scheduled against it. So the add form
    asks the one extra question that turns it into a working institution — what
    shape is it? — and builds the programmes, their modules (at a monthly price)
    and the year's calendar in the same submit. Everything it creates is ordinary
    editable data; the blueprint is a starting point, not a straitjacket.
    """
    blueprint = forms.ChoiceField(
        choices=[], initial='cta', label='Structure to create',
        help_text='The programmes and modules to build under this institution. '
                  'You can add, rename or remove any of it afterwards.')
    price_per_month = forms.DecimalField(
        max_digits=8, decimal_places=2, initial=500, label='Price per module per month (R)',
        help_text='Applied to every module the blueprint creates. 0 = free (never locked).')
    create_calendar = forms.BooleanField(
        required=False, initial=True, label="Create this year's academic calendar",
        help_text='Tests, exams, deadlines, coaching intensives and public holidays. '
                  'Dates are a template — confirm them against the institution\'s timetable.')

    def __init__(self, *args, **kwargs):
        """Load the blueprint choices from :mod:`core.academic_spine` and style the
        extra fields.
        """
        super().__init__(*args, **kwargs)
        from core import academic_spine

        self.fields['blueprint'].choices = academic_spine.BLUEPRINT_CHOICES
        self.blueprints = academic_spine.BLUEPRINTS
        _style(self)

    def save(self, commit=True):
        """Save the institution, then build the chosen structure under it, and
        make sure every programme has at least one cohort so students can pick an
        intake at registration straight away.

        Blueprints only pre-fill *named* intakes for Milpark-shaped providers; every
        other programme gets the current academic year as its first cohort, so the
        registration intake picker is never empty.
        """
        from django.utils import timezone

        from core import academic_spine

        institution = super().save(commit=commit)
        self.blueprint_result = academic_spine.apply_blueprint(
            institution, self.cleaned_data.get('blueprint'),
            price=self.cleaned_data.get('price_per_month') or 0,
            calendar=self.cleaned_data.get('create_calendar', True))
        year = timezone.now().year
        for programme in institution.programmes.all():
            if not programme.cohorts.exists():
                models.Cohort.objects.get_or_create(
                    programme=programme, code=str(year),
                    defaults={'name': f'{year} intake'})
        return institution


class ProgrammeForm(_StyledModelForm):
    """The full title is what the programme is called; the abbreviation is what
    it is called in a hurry. Both are asked for, and everything downstream shows
    them together — "Postgraduate Diploma in Accounting (PGDA)"."""
    class Meta:
        """Model fields shown, with labels and help that put the full name and
        abbreviation first.
        """
        model = models.Programme
        fields = ['full_name', 'abbreviation', 'code', 'name', 'level', 'depth_default',
                  'is_competency_based', 'description', 'order', 'is_active']
        labels = {
            'full_name': 'Full programme name',
            'abbreviation': 'Abbreviation',
            'code': 'Record code',
            'name': 'Short display name',
        }
        help_texts = {
            'full_name': 'Written out in full, e.g. "Postgraduate Diploma in Accounting".',
            'abbreviation': 'Shown in brackets after the full name and used for search, e.g. "PGDA".',
        }
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, **kwargs):
        """Require the full name; make the derivable fields optional.

        ``code``, ``name`` and ``abbreviation`` are derived in :meth:`clean` when
        left
        blank, so they must not fail field-level validation first — required checks
        run
        before ``clean()`` ever sees them.
        """
        super().__init__(*args, **kwargs)
        self.fields['full_name'].required = True
        for derived in ('code', 'name', 'abbreviation'):
            self.fields[derived].required = False

    def clean(self):
        """Derive the fields left blank from the full name and the abbreviation.

        The abbreviation defaults to the initials of the full name; the record code
        and
        short name default to the abbreviation. The person filling this in only has
        to
        answer the two questions that matter.
        """
        cleaned = super().clean()
        abbreviation = (cleaned.get('abbreviation') or '').strip()
        full_name = (cleaned.get('full_name') or '').strip()
        if not abbreviation and full_name:
            abbreviation = ''.join(word[0] for word in full_name.split() if word[0].isalpha())[:30].upper()
            cleaned['abbreviation'] = abbreviation
        if not (cleaned.get('code') or '').strip():
            cleaned['code'] = abbreviation[:30]
        if not (cleaned.get('name') or '').strip():
            cleaned['name'] = abbreviation or full_name[:160]
        return cleaned


class CohortForm(_StyledModelForm):
    """Edit an intake (cohort) of a programme: code, name and its start and end dates.
    """
    class Meta:
        """Model fields shown, with date pickers for the dates."""
        model = models.Cohort
        fields = ['code', 'name', 'start_date', 'end_date', 'is_active']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }


class ProgrammeModuleForm(_StyledModelForm):
    """Edit an existing offering; the canonical module it points at is fixed."""
    class Meta:
        """Model fields shown for an offering."""
        model = models.ProgrammeModule
        fields = ['code', 'name', 'depth_level', 'price_per_month',
                  'description', 'order', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}


class ProgrammeModuleAddForm(forms.Form):
    """Add a module to a programme. Get-or-creates the canonical Module, then the
    offering, so the admin never sees the two-level split."""
    module_code = forms.CharField(
        max_length=20, label='Module code',
        help_text='Canonical code, e.g. "FR" — shared with the same module on other institutions.')
    module_name = forms.CharField(max_length=160, label='Module name',
                                  help_text='e.g. "Financial Reporting".')
    offering_code = forms.CharField(
        max_length=20, required=False, label="This institution's code",
        help_text='Optional, e.g. "INTRO". Defaults to the module code.')
    price_per_month = forms.DecimalField(
        max_digits=8, decimal_places=2, initial=0, label='Price per month (R)',
        help_text='0 = free (never locked).')
    depth_level = forms.ChoiceField(
        required=False, label='Depth',
        choices=[('', '— inherit the programme depth —')] + list(models.DEPTH_CHOICES))

    def __init__(self, *args, **kwargs):
        """Build the form and apply the Bootstrap styling."""
        super().__init__(*args, **kwargs)
        _style(self)

    def save(self, programme):
        """Create the offering on ``programme`` and return it.

        The canonical :class:`~apps.learning.models.Module` is found by code, then by
        name, else created. The offering code defaults to the module code, and the
        offering keeps its own name only when it differs from the canonical one.
        """
        cd = self.cleaned_data
        code = cd['module_code'].strip().upper()
        name = cd['module_name'].strip() or code
        module = models.Module.objects.filter(code=code).first()
        if module is None:
            module = (models.Module.objects.filter(name=name).first()
                      or models.Module.objects.create(code=code, name=name))
        offering_code = (cd.get('offering_code') or code).strip().upper()
        offering, _ = models.ProgrammeModule.objects.get_or_create(
            programme=programme, code=offering_code,
            defaults={
                'module': module,
                'name': name if name != module.name else '',
                'price_per_month': cd['price_per_month'] or 0,
                'depth_level': cd.get('depth_level') or '',
            })
        return offering
