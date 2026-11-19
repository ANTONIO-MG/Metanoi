"""Accounts forms: sign-up, registration wizard, profile/settings, invites."""

from allauth.account.forms import ResetPasswordKeyForm, SignupForm
from captcha.fields import CaptchaField
from django import forms
from django.utils import timezone

from core import countries
from core.forms import DateInput, StyledModelForm, style_fields

from . import models


class ProfileForm(StyledModelForm):
    """A user editing their own profile: deliberately *only* the picture.

    Name, gender and date of birth appear on results and certificates, so only
    an administrator may change them. They are left out of the form entirely —
    a merely ``disabled`` template field is still POST-able.
    """

    class Meta:
        """Model and fields edited by this form."""
        model = models.Person
        fields = ['profile_picture']
        widgets = {'profile_picture': forms.FileInput(attrs={'accept': 'image/*'})}
        labels = {'profile_picture': 'Choose a new picture'}


class UserSettingsForm(StyledModelForm):
    """Preferences & privacy toggles on the settings page.

    Closure bookkeeping is excluded (driven by the close-account flow), and so is
    ``email_digest`` — it lives on the notification-preferences page only.
    """

    COMMON_ZONES = [
        'Africa/Johannesburg', 'Africa/Lagos', 'Africa/Nairobi', 'Africa/Cairo',
        'Africa/Accra', 'Africa/Harare', 'Africa/Gaborone', 'Africa/Windhoek',
        'Europe/London', 'Europe/Amsterdam', 'Europe/Berlin', 'Europe/Dublin',
        'Asia/Dubai', 'Asia/Kolkata', 'Asia/Shanghai', 'Asia/Singapore',
        'Australia/Perth', 'Australia/Sydney',
        'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
        'America/Toronto', 'America/Sao_Paulo', 'UTC',
    ]
    """Zones offered first in the picker (the full IANA list is about 600 entries)."""

    class Meta:
        """Model and fields edited by this form."""
        model = models.UserSettings
        fields = ['profile_visibility', 'show_email', 'show_activity',
                  'allow_messages', 'theme', 'language', 'timezone']

    def __init__(self, *args, **kwargs):
        """Replace the time-zone field with a grouped dropdown.

        A dropdown, not free text: a typo would silently fall back to the platform
        default and show the wrong session times. The other IANA zones follow in a
        second group so "Use this device's time zone" can always pick the exact one.
        """
        super().__init__(*args, **kwargs)
        from django.conf import settings as django_settings
        default = django_settings.TIME_ZONE
        from zoneinfo import available_timezones
        others = sorted(z for z in available_timezones()
                        if '/' in z and not z.startswith(('Etc/', 'SystemV/', 'US/')) and z not in self.COMMON_ZONES)
        self.fields['timezone'] = forms.ChoiceField(
            label='Time zone', required=False,
            choices=[('', f'Platform default ({default})'),
                     ('Common', [(z, z.replace('_', ' ')) for z in self.COMMON_ZONES]),
                     ('All time zones', [(z, z.replace('_', ' ')) for z in others])],
            help_text='Session times, reminders and the calendar are shown in this zone.',
            widget=forms.Select(attrs={'class': 'form-control form-select'}),
        )


class CloseAccountForm(forms.Form):
    """Confirm closing an account: tick the box and enter the current password,
    the account e-mail or the word ``CLOSE``."""

    confirm = forms.CharField(
        label='Confirm',
        widget=forms.PasswordInput(render_value=False),
        help_text='Type your password, your e-mail address, or the word CLOSE to confirm.',
    )
    understand = forms.BooleanField(
        label="I understand my account will be closed and purged after 30 days.",
        required=True,
    )

    def __init__(self, user, *args, **kwargs):
        """Bind the form to ``user``, whose credentials :meth:`clean_confirm` checks."""
        self.user = user
        super().__init__(*args, **kwargs)
        style_fields(self)

    def clean_confirm(self):
        """Accept the word ``CLOSE``, the account's e-mail or username, or its password."""
        value = (self.cleaned_data.get('confirm') or '').strip()
        email = (self.user.email or '').strip()
        username = (self.user.get_username() or '').strip()
        ok = (
            value == 'CLOSE'
            or (email and value.lower() == email.lower())
            or (username and value.lower() == username.lower())
            or self.user.check_password(value)
        )
        if not ok:
            raise forms.ValidationError(
                'That did not match. Enter your password, your e-mail, or the word CLOSE.')
        return value


TERMS_VERSION = '2026-10b'
"""Current Terms & Conditions version. Bump it when the text changes materially,
so existing consent records stay attributable to the wording actually agreed to."""


class MetanoiaResetPasswordKeyForm(ResetPasswordKeyForm):
    """allauth's "choose a new password" form, plus a must-differ rule — a reset
    to the same password would leave a compromised one in place."""

    def clean_password1(self):
        """Refuse a new password equal to the current one.

        No ``super()`` call: ``ResetPasswordKeyForm`` has no ``clean_password1``, and
        the field's ``clean()`` has already applied ``AUTH_PASSWORD_VALIDATORS``.
        """
        password = self.cleaned_data.get('password1')
        if self.user and password and self.user.check_password(password):
            raise forms.ValidationError(
                'That is already your current password. Please choose a different one.')
        return password


class MetanoiaSignupForm(SignupForm):
    """allauth's ``SignupForm`` plus required, recorded T&C consent.

    Goes through allauth (not ``create_user``) so password validators and e-mail
    rules apply. Acceptance is stamped on the Person, because an un-recorded
    tickbox proves nothing after the fact.
    """

    terms = forms.BooleanField(
        required=True,
        label='I agree to the Terms & Conditions',
        error_messages={
            'required': 'Please accept the Terms & Conditions to create your account.',
        },
    )

    captcha = CaptchaField(
        label='Solve the sum',
        error_messages={'invalid': 'That answer was wrong — solve the sum and try again.'},
    )
    """Self-hosted maths CAPTCHA. In tests set ``CAPTCHA_TEST_MODE=True`` and answer ``PASSED``."""

    def save(self, request):
        """Create the user through allauth, then stamp the T&C acceptance on the Person.

        The Person profile already exists — a ``post_save`` signal on User creates it.
        """
        user = super().save(request)
        profile = getattr(user, 'profile', None)
        if profile is not None:
            profile.terms_accepted_at = timezone.now()
            profile.terms_version = TERMS_VERSION
            profile.save(update_fields=['terms_accepted_at', 'terms_version'])
        return user


class CompleteProfileForm(StyledModelForm):
    """Step 2 of onboarding: finish the personal profile before entering the hub."""

    class Meta:
        """Model and fields edited by this form."""
        model = models.Person
        fields = ['first_name', 'last_name', 'gender', 'date_of_birth',
                  'phone', 'profile_picture']
        widgets = {
            'date_of_birth': DateInput(),
        }

    def __init__(self, *args, **kwargs):
        """Make the name fields required."""
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True


GUARDIAN_RELATIONSHIP_CHOICES = [
    ('parent', 'Parent'), ('guardian', 'Guardian'), ('sponsor', 'Sponsor'),
]
"""How a guardian relates to the student, on registration step 1. Step 1 writes to
the Person and its side-tables (contact, consent)."""


class RegBasicsForm(StyledModelForm):
    """Step 1 · who you are: title, name, picture, guardian and funding."""
    guardian_relationship = forms.ChoiceField(
        required=False, choices=GUARDIAN_RELATIONSHIP_CHOICES, initial='parent',
        widget=forms.RadioSelect, label='They are my')
    guardian_email = forms.EmailField(
        required=False, label='Parent / guardian / sponsor e-mail',
        widget=forms.EmailInput(attrs={'placeholder': 'name@example.com'}),
        help_text="Optional — we'll invite them to create an account linked to you.")

    class Meta:
        """Model and fields edited by this form."""
        model = models.Person
        fields = ['title', 'first_name', 'last_name', 'gender', 'date_of_birth',
                  'profile_picture', 'funding_source']
        widgets = {'date_of_birth': DateInput(),
                   'profile_picture': forms.FileInput(attrs={'accept': 'image/*'})}
        labels = {
            'funding_source': 'Who pays the fees',
        }

    def __init__(self, *args, **kwargs):
        """Require the names, relax title/funding, and prefill the first guardian e-mail."""
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        for optional in ('title', 'funding_source'):
            self.fields[optional].required = False
        if self.instance and self.instance.pk and self.instance.guardian_emails:
            self.fields['guardian_email'].initial = (self.instance.guardian_emails or [None])[0]


def normalise_phone(raw, iso=None):
    """Normalise to ``+<code> <digits>`` so ``tel:`` and WhatsApp links work.
    Raises ``ValidationError`` when there is no number in it.

    Whether or not the country code survived the typing, the result has exactly
    one; the national trunk prefix is dropped (``082`` → ``82``).
    """
    raw = (raw or '').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise forms.ValidationError('Enter a phone number.')
    code = countries.dial_code(iso or countries.DEFAULT_COUNTRY).lstrip('+')

    if code and digits.startswith(code):
        national = digits[len(code):]
    elif raw.startswith('+'):
        return f'+{digits}'
    else:
        national = digits
    national = national.lstrip('0')
    if not national:
        raise forms.ValidationError('Enter the number after the country code.')
    return f'+{code} {national}' if code else f'+{national}'


class _VerifiedPhoneMixin:
    """For the single-page registrations that keep the number on ``Person.phone``."""

    def clean_phone(self):
        """Normalise the number and insist it was verified (:mod:`apps.accounts.phone_verify`)."""
        number = (self.cleaned_data.get('phone') or '').strip()
        if number:
            number = normalise_phone(number)
        from .phone_verify import verification_error
        error = verification_error(self.instance, number)
        if error:
            raise forms.ValidationError(error)
        return number


class ParentRegistrationForm(_VerifiedPhoneMixin, StyledModelForm):
    """Single-page registration for an invited parent/guardian/sponsor."""
    relationship = forms.CharField(max_length=60, label='Relationship to the student',
                                   help_text='e.g. Mother, Father, Guardian, Sponsor')
    confirm_student = forms.BooleanField(label='Yes, this is the student I am registering for')

    class Meta:
        """Model and fields edited by this form."""
        model = models.Person
        fields = ['first_name', 'last_name', 'phone']
        labels = {'phone': 'Phone number (WhatsApp)'}
        widgets = {'phone': forms.TextInput(attrs={'type': 'tel', 'placeholder': '+27 82 123 4567',
                                                   'autocomplete': 'tel', 'data-wa-phone': ''})}

    def __init__(self, *args, **kwargs):
        """Make names and phone required."""
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['phone'].required = True


class EducatorRegistrationForm(_VerifiedPhoneMixin, StyledModelForm):
    """Single-page registration for an invited educator."""
    confirm_programme = forms.BooleanField(label='Yes, this is the programme I will be teaching')

    class Meta:
        """Model and fields edited by this form."""
        model = models.Person
        fields = ['first_name', 'last_name', 'phone']
        labels = {'phone': 'Phone number (WhatsApp)'}
        widgets = {'phone': forms.TextInput(attrs={'type': 'tel', 'placeholder': '+27 82 123 4567',
                                                   'autocomplete': 'tel', 'data-wa-phone': ''})}

    def __init__(self, *args, **kwargs):
        """Make names and phone required."""
        super().__init__(*args, **kwargs)
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        self.fields['phone'].required = True


class AddEducatorForm(forms.Form):
    """Admin/staff: invite an educator to a programme by e-mail."""
    email = forms.EmailField(label="Educator's e-mail address")
    programme = forms.ModelChoiceField(queryset=None, label='Programme they will teach')

    def __init__(self, *args, **kwargs):
        """Offer only active programmes, ordered by institution."""
        super().__init__(*args, **kwargs)
        from apps.learning.models import Programme
        self.fields['programme'].queryset = (
            Programme.objects.filter(is_active=True)
            .select_related('institution').order_by('institution__name', 'name'))
        style_fields(self)


class RegContactForm(StyledModelForm):
    """Step 1 · one phone number (also the WhatsApp number) and area/city/province."""

    class Meta:
        """Model and fields edited by this form."""
        model = models.PersonContact
        fields = ['phone_country', 'primary_phone', 'suburb', 'city', 'province']
        widgets = {
            'phone_country': forms.HiddenInput(),
            'primary_phone': forms.TextInput(attrs={
                'type': 'tel', 'inputmode': 'tel', 'autocomplete': 'tel',
                'placeholder': '+27 82 123 4567', 'data-wa-phone': '',
            }),
        }

    def __init__(self, *args, **kwargs):
        """Require phone and city; default the phone country."""
        super().__init__(*args, **kwargs)
        for f in ('primary_phone', 'city'):
            self.fields[f].required = True
        self.fields['phone_country'].required = False
        if not (self.initial.get('phone_country') or getattr(self.instance, 'phone_country', '')):
            self.initial['phone_country'] = countries.DEFAULT_COUNTRY

    def clean_primary_phone(self):
        """Normalise to ``+<code> <digits>`` (see :func:`normalise_phone`), then
        insist it was verified on WhatsApp when verification is switched on."""
        raw = (self.cleaned_data.get('primary_phone') or '').strip()
        if not raw:
            return raw
        iso = (self.data.get(self.add_prefix('phone_country'))
               or self.initial.get('phone_country') or countries.DEFAULT_COUNTRY)
        number = normalise_phone(raw, iso)
        from .phone_verify import verification_error
        error = verification_error(getattr(self, 'person', None) or getattr(self.instance, 'person', None),
                                   number)
        if error:
            raise forms.ValidationError(error)
        return number


class RegConsentForm(StyledModelForm):
    """Step 1 · POPIA consent. The first two are mandatory; audit fields are
    stamped by the view.

    AI personalisation, cookies and data sharing are deliberately NOT asked here:
    too many questions up front get skimmed, which is not informed consent. They
    default to ``False`` (no assumed consent) and are editable in
    :class:`PrivacyConsentForm`; cookies are also asked in the banner.
    """
    class Meta:
        """Model and fields edited by this form."""
        model = models.PersonConsent
        fields = ['privacy_policy_accepted', 'data_processing_accepted', 'marketing_consent',
                  'research_consent', 'analytics_consent']
        labels = {
            'privacy_policy_accepted': 'Privacy policy',
            'data_processing_accepted': 'Data processing',
            'marketing_consent': 'Marketing e-mails',
            'research_consent': 'Research use',
            'analytics_consent': 'Product analytics',
        }
        help_texts = {
            'privacy_policy_accepted': "You've read and accept our Privacy Policy. Required.",
            'data_processing_accepted': 'We may process your personal information to run your learning account. Required.',
            'marketing_consent': 'Send me news, course offers and platform updates by e-mail.',
            'research_consent': 'Use my anonymised data to improve teaching and for research.',
            'analytics_consent': 'Allow usage analytics that help us improve the platform.',
        }

    def __init__(self, *args, **kwargs):
        """Make the two mandatory consents required."""
        super().__init__(*args, **kwargs)
        self.fields['privacy_policy_accepted'].required = True
        self.fields['data_processing_accepted'].required = True


class PrivacyConsentForm(StyledModelForm):
    """Manage the optional POPIA opt-ins on the settings → Privacy & consent tab.
    The two mandatory consents (privacy policy + data processing) are captured at
    registration and shown read-only in the UI; the view re-stamps the audit
    fields (date/version/IP/device) on every save."""
    class Meta:
        """Model and fields edited by this form."""
        model = models.PersonConsent
        fields = ['marketing_consent', 'research_consent', 'ai_personalisation_consent',
                  'analytics_consent', 'cookie_consent', 'data_sharing_consent']
        labels = {
            'marketing_consent': 'Marketing e-mails & updates',
            'research_consent': 'Use my (anonymised) data for research',
            'ai_personalisation_consent': 'Personalise my learning with AI',
            'analytics_consent': 'Product analytics to improve the platform',
            'cookie_consent': 'Non-essential cookies',
            'data_sharing_consent': 'Share my data with partner institutions',
        }


WIZARD_STEP1_FORMS = [
    ('basics', RegBasicsForm, None, 'About you', 'about.png', True),
    ('contact', RegContactForm, 'contact', 'Contact & address', 'contact.png', True),
    ('consent', RegConsentForm, 'consent', 'Consent (POPIA)', 'consent.png', True),
]
"""The step-1 sections, in render order:
``(key, form_class, person_attr | None, section title, icon file, open?)``.
``person_attr`` names the reverse one-to-one side-table; ``None`` means the
Person itself. ``icon`` is a PNG under ``static/images/reg/``."""


STAFF_ROLE_CHOICES = [
    ('admin', 'Administrator'),
    ('staff', 'Staff'),
    ('educator', 'Educator'),
]


class StaffAccountForm(forms.Form):
    """A superuser creates a team account (verified e-mail, random password)."""
    first_name = forms.CharField(max_length=50, label='First name')
    last_name = forms.CharField(max_length=50, label='Last name')
    email = forms.EmailField(label='E-mail address')
    user_type = forms.ChoiceField(choices=STAFF_ROLE_CHOICES, label='Role', initial='staff')

    def __init__(self, *args, **kwargs):
        """Apply the shared field styling."""
        super().__init__(*args, **kwargs)
        style_fields(self)

    def clean_email(self):
        """Lower-case the address and refuse one that already has an account."""
        from django.contrib.auth import get_user_model
        email = (self.cleaned_data['email'] or '').strip().lower()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with that e-mail address already exists.')
        return email


OTHER_REQUIRED = 'Required when you choose "Other institution".'


def _other_institution_fields(form, *, institution_field):
    """Add the two "Other institution" boxes to ``form``: where you study and
    your cohort or year. Both start optional; :func:`_clean_other_institution`
    makes them required when the institution picked is the "Other" row."""
    form.fields['other_institution'] = forms.CharField(
        label='Your institution', max_length=160, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'e.g. University of Johannesburg',
                                      'autocomplete': 'organization',
                                      'data-other-for': institution_field}))
    form.fields['other_cohort'] = forms.CharField(
        label='Cohort or year', max_length=60, required=False,
        widget=forms.TextInput(attrs={'placeholder': 'e.g. 2027 intake, or 3rd year',
                                      'data-other-for': institution_field}))


def _clean_other_institution(form, institution):
    """Require both boxes when ``institution`` is "Other"; clear them otherwise."""
    data = form.cleaned_data
    if institution is not None and institution.is_other:
        for name in ('other_institution', 'other_cohort'):
            data[name] = (data.get(name) or '').strip()
            if not data[name]:
                form.add_error(name, OTHER_REQUIRED)
    else:
        data['other_institution'] = data['other_cohort'] = ''
    return data


class OneOnOneRequestForm(forms.ModelForm):
    """One-on-one coaching for any institution and programme. The institution is
    stored as text either way, so staff read one field."""

    institution_choice = forms.ModelChoiceField(
        queryset=None, required=False, label='Institution', empty_label='Select your institution…')

    class Meta:
        """Model and fields edited by this form."""
        model = models.OneOnOneRequest
        fields = ['programme', 'start_date', 'end_date', 'modules', 'notes']
        labels = {
            'programme': 'Programme you are doing',
            'start_date': 'Start date',
            'end_date': 'End date',
            'modules': 'Modules or subjects (optional)',
            'notes': 'Anything we should know (optional)',
        }
        widgets = {
            'start_date': DateInput(),
            'end_date': DateInput(),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    field_order = ['institution_choice', 'other_institution', 'other_cohort', 'programme',
                   'start_date', 'end_date', 'modules', 'notes']

    def __init__(self, *args, **kwargs):
        """Fill the institution choices and add the "Other institution" boxes.

        A hidden free-text ``institution`` field is still accepted from older pages
        that posted it.
        """
        super().__init__(*args, **kwargs)
        from apps.learning.models import Institution
        self.fields['institution_choice'].queryset = (Institution.objects.filter(is_active=True)
                                                      .order_by('order', 'name'))
        _other_institution_fields(self, institution_field=self['institution_choice'].auto_id)
        self.fields['institution'] = forms.CharField(max_length=160, required=False,
                                                     widget=forms.HiddenInput())
        self.order_fields(self.field_order)
        style_fields(self)
        self.fields['programme'].widget.attrs['placeholder'] = 'e.g. BCom Accounting, 3rd year'
        self.fields['modules'].widget.attrs['placeholder'] = 'e.g. Financial Accounting 3B, Tax'

    def clean(self):
        """Check the dates and resolve the institution to the text that is stored.

        Either a chosen institution (or, for "Other", what the student typed) or the
        legacy free-text value.
        """
        data = super().clean()
        start, end = data.get('start_date'), data.get('end_date')
        if start and end and end < start:
            self.add_error('end_date', 'The end date cannot be before the start date.')
        choice = data.get('institution_choice')
        legacy = (data.get('institution') or '').strip()
        if choice is None and not legacy:
            self.add_error('institution_choice', 'Please choose your institution.')
        _clean_other_institution(self, choice)
        if choice is not None:
            data['institution'] = data.get('other_institution', '') if choice.is_other else choice.name
        else:
            data['institution'] = legacy
        return data

    def save(self, commit=True):
        """Store the resolved institution text and the typed cohort on the request."""
        req = super().save(commit=False)
        req.institution = self.cleaned_data['institution'][:160]
        req.cohort = self.cleaned_data.get('other_cohort', '')
        if commit:
            req.save()
        return req


class JoinCommunityForm(forms.Form):
    """Join the Metanoia Community: who you are, where you study, a password.

    Deliberately short. No e-mail check and no password rules — a member can
    only reach the community, Message Metanoia and the shop. Becoming a student
    later verifies the e-mail and sets a proper password
    (apps.accounts.community_views).

    ``programme`` is not required when the institution is "Other institution":
    that person types where they study and their cohort or year instead.
    ``password`` has no validators on purpose — the owner asked for "a simple
    password" here — but it is still hashed like every other password.
    """

    first_name = forms.CharField(max_length=50, label='Name')
    last_name = forms.CharField(max_length=50, label='Surname')
    email = forms.EmailField(label='Email address')
    phone = forms.CharField(max_length=30, label='Cell number')
    institution = forms.ModelChoiceField(queryset=None, label='Institution you study at',
                                         empty_label='Choose your institution')
    programme = forms.ModelChoiceField(queryset=None, label='CA programme', required=False,
                                       empty_label='Choose your programme')
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), strip=False,
                               label='Password', max_length=128)
    terms = forms.BooleanField(
        required=True, label='I agree to the Terms & Conditions',
        error_messages={'required': 'Please accept the Terms & Conditions to join.'})
    captcha = CaptchaField(
        label='Solve the sum',
        error_messages={'invalid': 'That answer was wrong — solve the sum and try again.'})

    def __init__(self, *args, **kwargs):
        """Fill the institution and programme choices and add the "Other institution" boxes."""
        super().__init__(*args, **kwargs)
        from apps.learning.models import Institution, Programme
        self.fields['institution'].queryset = Institution.objects.order_by('order', 'name')
        self.fields['programme'].queryset = (Programme.objects.filter(is_active=True)
                                             .select_related('institution')
                                             .order_by('institution__order', 'order', 'name'))
        _other_institution_fields(self, institution_field='id_institution')

    def clean_first_name(self):
        """Strip surrounding spaces."""
        return self.cleaned_data['first_name'].strip()

    def clean_last_name(self):
        """Strip surrounding spaces."""
        return self.cleaned_data['last_name'].strip()

    def clean_email(self):
        """Strip and lower-case the address."""
        return self.cleaned_data['email'].strip().lower()

    def clean_phone(self):
        """Normalise to ``+<code> <digits>``."""
        return normalise_phone(self.cleaned_data.get('phone', ''))

    def clean(self):
        """Check the programme belongs to the institution and is given unless "Other"."""
        data = super().clean()
        institution, programme = data.get('institution'), data.get('programme')
        if institution and programme and programme.institution_id != institution.pk:
            self.add_error('programme', 'Choose a programme offered by your institution.')
        if institution is not None and not institution.is_other and programme is None \
                and 'programme' not in self.errors:
            self.add_error('programme', 'This field is required.')
        _clean_other_institution(self, institution)
        return data


PARENT_RELATIONSHIP_CHOICES = [
    ('Mother', 'Mother'), ('Father', 'Father'), ('Guardian', 'Guardian'),
    ('Sponsor', 'Sponsor'), ('Other', 'Other family member'),
]


class ParentSignupForm(forms.Form):
    """Sign-up for an invited parent/guardian (apps.accounts.parent_views).

    The invited e-mail is never posted. The password follows the site rules (a
    parent sees results and fees). ``with_email=True`` adds an e-mail field for
    the shareable student link, which has none behind it.
    """

    first_name = forms.CharField(max_length=50, label='First name')
    last_name = forms.CharField(max_length=50, label='Surname')
    phone = forms.CharField(max_length=30, label='Cell number')
    relationship = forms.ChoiceField(choices=PARENT_RELATIONSHIP_CHOICES, initial='Mother',
                                     label='Relationship to the student')
    password1 = forms.CharField(widget=forms.PasswordInput(render_value=False), strip=False,
                                label='Password', max_length=128)
    password2 = forms.CharField(widget=forms.PasswordInput(render_value=False), strip=False,
                                label='Confirm password', max_length=128)
    terms = forms.BooleanField(
        required=True, label='I agree to the Terms & Conditions',
        error_messages={'required': 'Please accept the Terms & Conditions to continue.'})

    def __init__(self, *args, with_email=False, email='', **kwargs):
        """``with_email`` adds an e-mail field; ``email`` is the invited address, if known."""
        super().__init__(*args, **kwargs)
        self.known_email = (email or '').strip().lower()
        if with_email:
            self.fields['email'] = forms.EmailField(label='Email address')

    def clean_first_name(self):
        """Strip surrounding spaces."""
        return self.cleaned_data['first_name'].strip()

    def clean_last_name(self):
        """Strip surrounding spaces."""
        return self.cleaned_data['last_name'].strip()

    def clean_email(self):
        """Strip and lower-case the address."""
        return self.cleaned_data['email'].strip().lower()

    def clean_phone(self):
        """Normalise to ``+<code> <digits>``."""
        return normalise_phone(self.cleaned_data.get('phone', ''))

    def clean(self):
        """Check the passwords match and pass the site's password validators."""
        data = super().clean()
        p1, p2 = data.get('password1'), data.get('password2')
        if p1 and p2 and p1 != p2:
            self.add_error('password2', 'The two passwords do not match.')
        elif p1:
            from django.contrib.auth import get_user_model, password_validation
            email = self.known_email or data.get('email') or ''
            probe = get_user_model()(username=email, email=email,
                                     first_name=data.get('first_name') or '',
                                     last_name=data.get('last_name') or '')
            try:
                password_validation.validate_password(p1, probe)
            except forms.ValidationError as exc:
                self.add_error('password1', exc)
        return data
