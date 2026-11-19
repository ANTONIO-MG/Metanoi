"""The session scheduler form, and the settings form behind it.

:meth:`clean` enforces the audience/scope pairing server-side (the page hiding
pickers is a convenience, not the control). An educator's form is narrowed to
their modules on the field querysets, so a hand-edited POST cannot schedule a
class on somebody else's module.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.communication.models import MeetingRoom
from core.forms import DateTimeInput as _DateTimeInput

from .models import CalendarSubscription, LiveSessionSettings


class _Styled(forms.ModelForm):
    """Bootstrap classes, matching apps.communication.forms."""

    def __init__(self, *args, **kwargs):
        """Add the Bootstrap class for each field's widget type to its existing classes."""
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                css = 'form-check-input'
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = 'form-control form-select'
            else:
                css = 'form-control'
            widget.attrs['class'] = (widget.attrs.get('class', '') + ' ' + css).strip()


class _LinkedSelect(forms.Select):
    """A ``<select>`` whose options carry ``data-*`` attributes from their object.

    Lets the page narrow each picker to the one above it (programmes of the
    chosen institution, modules of the chosen programme, …) and build the title
    prefix from the codes without another request. ``data`` maps an attribute
    name to a function of the option's model instance.
    """

    def __init__(self, data=None, attrs=None):
        """Keep ``data``: a mapping of ``data-*`` attribute name to a function of the option's instance."""
        super().__init__(attrs)
        self.data = data or {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        """Add the ``data-*`` attributes to each option; an attribute that cannot be read is left empty."""
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, 'instance', None)
        if instance is not None:
            for key, read in self.data.items():
                try:
                    option['attrs'][f'data-{key}'] = read(instance) or ''
                except Exception:
                    option['attrs'][f'data-{key}'] = ''
        return option


class SessionForm(_Styled):
    """Schedule (or edit) a live session.

    * **A class** — the most specific scope chosen (module, narrowed to intake
      if given; else intake, programme, institution) is the audience.
    * **Individual people** — only they are invited; scope fields still decide
      where the recording is filed.

    The title is the generated codes + date prefix, then `` | `` and
    :attr:`title_suffix`.
    """

    TARGET_CLASS, TARGET_PEOPLE = 'class', 'people'
    target = forms.ChoiceField(
        label='Who is it for?', initial=TARGET_CLASS, widget=forms.RadioSelect,
        choices=[(TARGET_CLASS, 'A class (institution, programme, module)'),
                 (TARGET_PEOPLE, 'Individual people')])
    title_suffix = forms.CharField(
        label='Session title', max_length=150,
        help_text='What the session is about. The institution, programme, module, intake, '
                  'date and time are put in front of it automatically.')

    invitees = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.none(), required=False,
        widget=forms.MultipleHiddenInput,
        help_text='Search by name or e-mail and add the people to invite.')

    class Meta:
        """The meeting fields the scheduler edits; title and audience are hidden because they are worked out."""
        model = MeetingRoom
        fields = ['title', 'description', 'session_kind', 'scheduled_start', 'scheduled_end',
                  'audience', 'institution', 'programme', 'cohort', 'module', 'week',
                  'calendar_event', 'record_automatically', 'publish_recording',
                  'is_recurring', 'recurrence']
        widgets = {
            'title': forms.HiddenInput,
            'audience': forms.HiddenInput,
            'description': forms.Textarea(attrs={'rows': 3}),
            'scheduled_start': _DateTimeInput(),
            'scheduled_end': _DateTimeInput(),
            'institution': _LinkedSelect({'code': lambda o: o.code}),
            'programme': _LinkedSelect({'code': lambda o: o.code, 'parent': lambda o: o.institution_id}),
            'module': _LinkedSelect({'code': lambda o: o.code, 'parent': lambda o: o.programme_id}),
            'cohort': _LinkedSelect({'code': lambda o: o.code, 'parent': lambda o: o.programme_id}),
            'week': _LinkedSelect({'parent': lambda o: o.phase.programme_module_id}),
        }

    def __init__(self, *args, user=None, **kwargs):
        """Set defaults from the settings and narrow the pickers to what ``user`` may schedule on.

        An educator (a scoped user) schedules on their own modules only: the module,
        programme, institution, intake, week and invitee querysets are all narrowed to
        what they teach, so a hand-edited POST cannot reach another module. When
        editing, the form starts from what the session already is (its title suffix
        and whether it is for a class or for individual people).
        """
        super().__init__(*args, **kwargs)
        self.user = user
        User = get_user_model()
        from . import services

        self.fields['target'].widget.attrs['class'] = 'btn-check'
        conf = LiveSessionSettings.load()
        self.fields['record_automatically'].initial = conf.auto_record
        self.fields['publish_recording'].initial = conf.publish_recordings

        for name in ('title', 'audience', 'institution', 'programme', 'cohort', 'module', 'week',
                     'calendar_event'):
            self.fields[name].required = False
        self.fields['cohort'].label = 'Intake (cohort)'
        self.fields['cohort'].help_text = 'Optional — leave empty for every intake on the module.'
        self.fields['week'].help_text = 'Optional — puts the session on the module schedule ' \
                                        'next to the week or topic it covers.'
        self.fields['calendar_event'].help_text = 'Optional — the test or exam this session ' \
                                                  'prepares for or reviews.'

        from apps.learning.models import Cohort, Institution, ModuleWeek, Programme, ProgrammeModule
        self.fields['institution'].queryset = Institution.objects.order_by('code')
        self.fields['programme'].queryset = Programme.objects.select_related('institution').order_by('code')
        self.fields['module'].queryset = ProgrammeModule.objects.select_related('programme').order_by('code')
        self.fields['cohort'].queryset = Cohort.objects.select_related('programme').order_by('code')
        self.fields['week'].queryset = (ModuleWeek.objects.select_related('phase')
                                        .order_by('phase__programme_module', 'number'))

        from core.scoping import is_scoped, taught_module_qs, taught_programmes
        self.is_scoped = bool(user is not None and is_scoped(user))

        if self.is_scoped:
            programmes = taught_programmes(user)
            self.fields['module'].queryset = taught_module_qs(user).select_related('programme')
            self.fields['programme'].queryset = programmes.select_related('institution')
            self.fields['institution'].queryset = Institution.objects.filter(
                pk__in=programmes.values('institution')).order_by('code')
            self.fields['cohort'].queryset = Cohort.objects.filter(programme__in=programmes)
            self.fields['week'].queryset = self.fields['week'].queryset.filter(
                phase__programme_module__in=self.fields['module'].queryset)
            from core.scoping import taught_students
            self.fields['invitees'].queryset = taught_students(user).order_by('first_name', 'username')
        else:
            self.fields['invitees'].queryset = User.objects.filter(is_active=True) \
                                                   .order_by('first_name', 'username')

        if self.instance.pk and not self.is_bound:
            self.initial['title_suffix'] = services.split_session_title(self.instance.title)
            self.initial['target'] = (self.TARGET_PEOPLE
                                      if self.instance.audience == MeetingRoom.AUDIENCE_PRIVATE
                                      else self.TARGET_CLASS)

    def selected_people(self):
        """The invitees to show as chips: the posted ones, or the saved ones."""
        if self.is_bound:
            ids = [i for i in self.data.getlist(self.add_prefix('invitees')) if str(i).isdigit()]
        else:
            ids = list(self.initial.get('invitees') or [])
            ids = [getattr(i, 'pk', i) for i in ids]
        if not ids:
            return []
        return list(self.fields['invitees'].queryset.filter(pk__in=ids))

    def clean(self):
        """Check the start/end times and the scope chain, then work out the audience and title.

        Each choice has to sit under the one above it (programme under institution,
        module and intake under programme, week under module), or the recording would
        be filed, and the audience drawn, from two different places.

        For individual people the audience is private. A new private session may not
        invite one-on-one students: they are booked through their hours and invoice
        (``apps.shop.one_on_one``), never as a free private session. For a class the
        audience is the most specific scope chosen and the invitees are cleared — a
        class's people come from its scope (:func:`.audience.recipients_for`), and
        hand-picked names on top would only make the audience wrong.
        """
        from . import services
        cleaned = super().clean()
        target = cleaned.get('target') or self.TARGET_CLASS
        start, end = cleaned.get('scheduled_start'), cleaned.get('scheduled_end')
        institution, programme = cleaned.get('institution'), cleaned.get('programme')
        module, cohort, week = cleaned.get('module'), cleaned.get('cohort'), cleaned.get('week')

        if start and end and end <= start:
            self.add_error('scheduled_end', 'The session has to end after it starts.')
        if start and not self.instance.pk and start < timezone.now() - timezone.timedelta(minutes=5):
            self.add_error('scheduled_start', 'That start time is in the past.')

        if programme and institution and programme.institution_id != institution.pk:
            self.add_error('programme', 'That programme is not at the chosen institution.')
        if module and programme and module.programme_id != programme.pk:
            self.add_error('module', 'That module is not on the chosen programme.')
        if cohort and programme and cohort.programme_id != programme.pk:
            self.add_error('cohort', 'That intake is not on the chosen programme.')
        if module and not programme:
            self.add_error('programme', 'Choose the programme first.')
        if programme and not institution:
            self.add_error('institution', 'Choose the institution first.')
        if week and (not module or week.phase.programme_module_id != module.pk):
            self.add_error('week', 'That week belongs to a different module.')

        if target == self.TARGET_PEOPLE:
            cleaned['audience'] = MeetingRoom.AUDIENCE_PRIVATE
            if not cleaned.get('invitees'):
                self.add_error('invitees', 'Add at least one person to invite.')
            elif not self.instance.pk:
                o2o = [u for u in cleaned['invitees']
                       if getattr(getattr(u, 'profile', None), 'is_one_on_one', False)]
                if o2o:
                    from django.urls import reverse
                    names = ', '.join(u.get_full_name() or u.get_username() for u in o2o)
                    self.add_error('invitees', forms.ValidationError(
                        f'{names} {"is a" if len(o2o) == 1 else "are"} one-on-one student'
                        f'{"" if len(o2o) == 1 else "s"}: schedule their sessions from the '
                        f'One-on-one page ({reverse("staffdesk:ops-one-on-one")}) so their hours '
                        'and invoice are handled.'))
        else:
            if not institution:
                self.add_error('institution', 'Choose the institution this class is for.')
            cleaned['audience'] = (MeetingRoom.AUDIENCE_MODULE if module
                                   else MeetingRoom.AUDIENCE_COHORT if cohort
                                   else MeetingRoom.AUDIENCE_PROGRAMME if programme
                                   else MeetingRoom.AUDIENCE_INSTITUTION)
            cleaned['invitees'] = self.fields['invitees'].queryset.none()

        suffix = (cleaned.get('title_suffix') or '').strip()
        cleaned['title'] = services.compose_session_title(
            institution=institution, programme=programme, module=module, cohort=cohort,
            start=start, suffix=suffix)
        return cleaned

    @property
    def changed_data(self):
        """Title and audience are worked out, not typed: compare their results."""
        changed = [name for name in super().changed_data
                   if name not in ('title', 'audience', 'title_suffix', 'target')]
        if self.instance.pk and hasattr(self, 'cleaned_data'):
            for name in ('title', 'audience'):
                if self.cleaned_data.get(name) != self.initial.get(name):
                    changed.append(name)
        return changed


class LiveSessionSettingsForm(_Styled):
    """The admin-facing behaviour switches (credentials stay in the environment)."""

    class Meta:
        """Every setting except the audit fields."""
        model = LiveSessionSettings
        exclude = ['updated_at', 'updated_by']

    def clean_reminder_leads(self):
        """Normalise the comma-separated lead times to positive whole minutes, defaulting to ``1440,30``."""
        raw = (self.cleaned_data.get('reminder_leads') or '').strip()
        leads = []
        for chunk in raw.split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not chunk.isdigit() or int(chunk) <= 0:
                raise forms.ValidationError(
                    'Use whole numbers of minutes, separated by commas — e.g. "1440, 30".')
            leads.append(str(int(chunk)))
        return ','.join(leads) or '1440,30'


class CalendarSubscriptionForm(_Styled):
    """Connect an outside calendar by its iCalendar feed URL.

    Validated against the *live* feed, so a bad URL fails now rather than
    silently in a background job later.
    """

    class Meta:
        """The fields a user fills in to connect a calendar."""
        model = CalendarSubscription
        fields = ['name', 'url', 'provider', 'colour', 'blocks_time']
        widgets = {
            'url': forms.URLInput(attrs={
                'placeholder': 'https://calendar.google.com/calendar/ical/…/basic.ics'}),
            'colour': forms.TextInput(attrs={'type': 'color'}),
        }

    def __init__(self, *args, user=None, **kwargs):
        """Keep the user the subscription is for and set friendlier labels."""
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['name'].help_text = 'What to call it on your calendar — "My Outlook diary".'
        self.fields['url'].label = 'Calendar feed address (.ics)'

    def clean_url(self):
        """Refuse a calendar the user has already connected, or a feed that cannot be fetched.

        A feed with no events is legitimate: it is accepted and ``_empty_feed`` is set
        so the page can say so rather than refusing it.
        """
        url = (self.cleaned_data.get('url') or '').strip()
        if self.user is not None:
            clash = CalendarSubscription.objects.filter(user=self.user, url=url)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError('You have already connected that calendar.')

        from . import ics
        text, error = ics.fetch(url)
        if error:
            raise forms.ValidationError(error)
        if 'BEGIN:VEVENT' not in text:
            self._empty_feed = True
        return url
