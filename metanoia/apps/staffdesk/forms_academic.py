"""Forms for the academic half of the staff desk: grades, certificates and
at-risk triage.

* Grades and certificates — :class:`GradeOverrideForm`,
  :class:`ModuleWeightingForm`, :class:`CertificateIssueForm` and
  :class:`ReasonForm`.
* At-risk triage — :class:`TriageForm` and :class:`NotifyForm`.
"""

import json

from django import forms
from django.contrib.auth import get_user_model

from apps.learning.models import Programme, ProgrammeModule
from apps.reports.models import Certificate, ModuleWeighting

User = get_user_model()


def _offerings():
    """Active programme modules (offerings), by programme code and module code."""
    return (ProgrammeModule.objects.filter(is_active=True)
            .select_related('programme__institution').order_by('programme__code', 'code'))


class OfferingChoiceField(forms.ModelChoiceField):
    """Offering picker labelled "reference — name"."""

    def label_from_instance(self, obj):
        return f'{obj.reference} — {obj.name or obj.module}'


class StudentChoiceField(forms.ModelChoiceField):
    """Student picker labelled "Full Name <e-mail>" (or just the e-mail)."""

    def label_from_instance(self, obj):
        name = (obj.get_full_name() or '').strip()
        return f'{name} <{obj.email}>' if name else obj.email


class GradeOverrideForm(forms.Form):
    """Override a final mark; the reason is required and kept on the grade."""

    value = forms.DecimalField(label='Final mark (%)', min_value=0, max_value=100,
                               max_digits=5, decimal_places=2)
    reason = forms.CharField(label='Reason', widget=forms.Textarea(attrs={'rows': 3}),
                             help_text='Kept on the grade and shown to staff. Required.')


class ModuleWeightingForm(forms.ModelForm):
    """ModelForm over ModuleWeighting — the model's own ``clean`` enforces the
    100% total, and the grade scale is edited as JSON."""

    grade_scale = forms.JSONField(
        required=False, widget=forms.Textarea(attrs={'rows': 4, 'class': 'font-monospace'}),
        help_text='Optional, e.g. [{"min": 75, "letter": "A"}, {"min": 0, "letter": "F"}]. '
                  'Leave blank for the default A–F scale.')

    class Meta:
        model = ModuleWeighting
        fields = ['assignments_pct', 'quizzes_pct', 'tests_pct', 'exams_pct', 'tasks_pct',
                  'pass_mark_pct', 'extra_credit_pct', 'grade_scale']
        labels = {
            'assignments_pct': 'Assignments %', 'quizzes_pct': 'Quizzes %', 'tests_pct': 'Tests %',
            'exams_pct': 'Exams %', 'tasks_pct': 'Study tasks %', 'pass_mark_pct': 'Pass mark %',
            'extra_credit_pct': 'Extra-credit cap %',
        }

    def clean_grade_scale(self):
        """Check the scale is a list of ``{"min": number, "letter": …}`` bands."""
        scale = self.cleaned_data.get('grade_scale') or []
        if not isinstance(scale, list):
            raise forms.ValidationError('The scale must be a list of {"min", "letter"} bands.')
        for band in scale:
            if not isinstance(band, dict) or 'min' not in band or 'letter' not in band:
                raise forms.ValidationError('Each band needs a "min" and a "letter".')
            try:
                float(band['min'])
            except (TypeError, ValueError):
                raise forms.ValidationError(f'“{band["min"]}” is not a number.')
        return scale

    def clean(self):
        """Cap the pass mark and extra-credit percentages at 100."""
        cleaned = super().clean()
        for name in ('pass_mark_pct', 'extra_credit_pct'):
            if (cleaned.get(name) or 0) > 100:
                self.add_error(name, 'At most 100.')
        return cleaned


class CertificateIssueForm(forms.Form):
    """Issue a module or programme certificate by hand.

    ``kind`` decides whether ``module`` or ``programme`` is required; a blank
    ``final_mark`` uses the student's current grade (for a programme, the average
    across its modules).
    """

    KIND_MODULE, KIND_PROGRAMME = 'module', 'programme'

    student = StudentChoiceField(queryset=User.objects.none())
    kind = forms.ChoiceField(choices=[(KIND_MODULE, 'Module'), (KIND_PROGRAMME, 'Programme')],
                             initial=KIND_MODULE)
    module = OfferingChoiceField(queryset=ProgrammeModule.objects.none(), required=False,
                                 help_text='For a module certificate.')
    programme = forms.ModelChoiceField(queryset=Programme.objects.none(), required=False,
                                       help_text='For a programme certificate.')
    title = forms.CharField(max_length=200, required=False,
                            help_text='Leave blank for the standard wording.')
    final_mark = forms.DecimalField(min_value=0, max_value=100, max_digits=5, decimal_places=2,
                                    required=False,
                                    help_text="Blank = the student's current grade (programme: the "
                                              'average across its modules).')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['student'].queryset = (User.objects.filter(profile__user_type='student', is_active=True,
                                                               profile__preview_of__isnull=True)
                                           .order_by('first_name', 'last_name', 'email'))
        self.fields['module'].queryset = _offerings()
        self.fields['programme'].queryset = (Programme.objects.filter(is_active=True)
                                             .select_related('institution').order_by('code'))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('kind') == self.KIND_MODULE and not cleaned.get('module'):
            self.add_error('module', 'Choose the module this certificate is for.')
        if cleaned.get('kind') == self.KIND_PROGRAMME and not cleaned.get('programme'):
            self.add_error('programme', 'Choose the programme this certificate is for.')
        return cleaned


class ReasonForm(forms.Form):
    """A single required reason, e.g. for revoking a certificate."""

    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))


class TriageForm(forms.Form):
    """One triage step on a risk flag; ``note`` needs the note text."""

    ACTIONS = ['ack', 'resolve', 'reopen', 'note']

    action = forms.ChoiceField(choices=[(a, a) for a in ACTIONS])
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('action') == 'note' and not (cleaned.get('note') or '').strip():
            self.add_error('note', 'Write the note first.')
        return cleaned


class NotifyForm(forms.Form):
    """Title and body of a notification sent from the at-risk page."""

    title = forms.CharField(max_length=200)
    body = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))


def pretty_json(value):
    """Indented, key-sorted JSON for display (non-JSON values via ``str``)."""
    return json.dumps(value, indent=2, sort_keys=True, default=str)
