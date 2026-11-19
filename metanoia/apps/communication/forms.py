"""Forms for the communication app's HTML pages.

Bootstrap-styled (the MyHub theme expects ``form-control`` / ``form-select``),
mirroring :mod:`apps.myhub.forms`.
"""

from django import forms
from django.db.models import Q

from . import models


class _StyledForm(forms.ModelForm):
    """Apply Bootstrap classes + placeholders to every field."""

    def __init__(self, *args, **kwargs):
        """Add the Bootstrap class (and a placeholder) to every field's widget."""
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
                css = 'form-check-input'
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = 'form-control form-select'
            else:
                css = 'form-control'
            widget.attrs['class'] = (widget.attrs.get('class', '') + ' ' + css).strip()
            if isinstance(widget, (forms.TextInput, forms.Textarea, forms.EmailInput, forms.URLInput)):
                widget.attrs.setdefault('placeholder', field.label)


class _DateTimeInput(forms.DateTimeInput):
    """An HTML ``datetime-local`` input using the format that input expects."""
    input_type = 'datetime-local'

    def __init__(self, attrs=None, format='%Y-%m-%dT%H:%M'):
        """Default the format to ``%Y-%m-%dT%H:%M``."""
        super().__init__(attrs, format)


class MeetingRoomForm(_StyledForm):
    """Create / schedule a video-audio meeting or class session."""

    class Meta:
        """Meeting fields, with datetime-local pickers for the schedule."""
        model = models.MeetingRoom
        fields = ['title', 'description', 'group', 'scheduled_start', 'scheduled_end',
                  'is_recurring', 'recurrence', 'requires_login']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'scheduled_start': _DateTimeInput(),
            'scheduled_end': _DateTimeInput(),
        }


class AnnouncementForm(_StyledForm):
    """Compose a broadcast notification (admin / staff only).

    The audience is either *everyone* or any mix of institutions, programmes,
    cohorts, modules and named people; both can be narrowed by role. ``when``
    decides what the submit does: send now, schedule for later, or save a draft.
    """

    WHEN_NOW, WHEN_LATER, WHEN_DRAFT = 'now', 'later', 'draft'
    when = forms.ChoiceField(choices=[(WHEN_NOW, 'Send now'), (WHEN_LATER, 'Schedule for later'),
                                      (WHEN_DRAFT, 'Save as draft')],
                             initial=WHEN_NOW, widget=forms.RadioSelect)
    roles = forms.MultipleChoiceField(choices=models.Announcement.ROLE_CHOICES, required=False,
                                      widget=forms.CheckboxSelectMultiple,
                                      label='Only these roles')

    class Meta:
        """Composer fields, labels and widgets.

        ``level`` (the tone) is a :class:`RadioSelect`: the page draws the tone as a row
        of buttons, one radio per tone. As the model's default ``<select>``, iterating
        it rendered bare ``<option>`` tags, nothing was posted, and every send failed
        with a "required" error the page did not show.
        """
        model = models.Announcement
        fields = ['title', 'body', 'level', 'url', 'url_label', 'media_url', 'audience', 'roles',
                  'institutions', 'programmes', 'cohorts', 'modules', 'users', 'include_parents',
                  'is_important', 'send_email', 'scheduled_for']
        labels = {
            'title': 'Headline', 'body': 'Message', 'level': 'Tone',
            'url': 'Link', 'url_label': 'Link button text', 'media_url': 'Video link',
            'include_parents': 'Copy parents / guardians',
            'is_important': 'Important — reach people who muted announcements',
            'send_email': 'Also send by e-mail', 'scheduled_for': 'Send at',
        }
        widgets = {
            'body': forms.Textarea(attrs={'rows': 7}),
            'audience': forms.RadioSelect,
            'level': forms.RadioSelect,
            'institutions': forms.SelectMultiple(attrs={'data-pick': 'institution'}),
            'programmes': forms.SelectMultiple(attrs={'data-pick': 'programme'}),
            'cohorts': forms.SelectMultiple(attrs={'data-pick': 'cohort'}),
            'modules': forms.SelectMultiple(attrs={'data-pick': 'module'}),
            'users': forms.SelectMultiple(attrs={'data-pick': 'person'}),
            'scheduled_for': _DateTimeInput(),
        }

    def __init__(self, *args, user=None, **kwargs):
        """Set up the scope pickers for ``user``.

        People are picked through a search box, so only the chosen ones (e.g. from a
        ``?user=`` deep link, or a duplicated announcement) are rendered as options;
        validation still checks every active account.
        """
        super().__init__(*args, **kwargs)
        self.user = user
        from django.contrib.auth import get_user_model
        from apps.learning.models import Cohort, Institution, Programme, ProgrammeModule

        A = models.Announcement
        self.fields['audience'].choices = [(A.AUDIENCE_ALL, 'Everyone'),
                                           (A.AUDIENCE_TARGETED, 'Choose who')]
        if self.instance.pk and self.instance.audience not in (A.AUDIENCE_ALL, A.AUDIENCE_TARGETED):
            self.initial['audience'] = A.AUDIENCE_TARGETED
        self.fields['institutions'].queryset = Institution.objects.filter(is_active=True).order_by('name')
        self.fields['programmes'].queryset = (Programme.objects.filter(is_active=True)
                                              .select_related('institution').order_by('institution__name', 'name'))
        self.fields['cohorts'].queryset = (Cohort.objects.filter(is_active=True)
                                           .select_related('programme').order_by('programme__name', 'code'))
        self.fields['modules'].queryset = (ProgrammeModule.objects.filter(is_active=True)
                                           .select_related('programme', 'module').order_by('programme__name', 'code'))
        User = get_user_model()
        self.fields['users'].queryset = User.objects.filter(is_active=True)
        if self.is_bound:
            chosen = self.data.getlist('users')
        elif self.initial.get('users'):
            chosen = list(self.initial['users'])
        else:
            chosen = list(self.instance.users.values_list('pk', flat=True)) if self.instance.pk else []
        from .broadcast import person_label
        self.fields['users'].widget.choices = [
            (u.pk, person_label(u))
            for u in User.objects.filter(pk__in=[c for c in chosen if str(c).isdigit()]).select_related('profile')]
        for name in ('institutions', 'programmes', 'cohorts', 'modules', 'users'):
            self.fields[name].required = False
        self.fields['level'].choices = [('info', 'Update'), ('success', 'Good news'),
                                        ('warning', 'Reminder'), ('error', 'Urgent')]
        self.fields['title'].widget.attrs['placeholder'] = 'e.g. New blueprint available for FAC188'
        self.fields['body'].widget.attrs['placeholder'] = 'Write your message. Links are made clickable.'
        self.fields['url'].widget.attrs['placeholder'] = '/learning/... or https://...'
        self.fields['url_label'].widget.attrs['placeholder'] = 'Open the blueprint'
        self.fields['media_url'].widget.attrs['placeholder'] = 'https://youtu.be/...'
        if self.instance.pk and self.instance.status == A.STATUS_SCHEDULED:
            self.initial.setdefault('when', self.WHEN_LATER)

    def clean_url(self):
        """Allow only a same-site path (``/…``, not ``//…``) or a full http(s) link."""
        url = (self.cleaned_data.get('url') or '').strip()
        if url and not (url.startswith('/') and not url.startswith('//')) \
                and not url.lower().startswith(('http://', 'https://')):
            raise forms.ValidationError('Use a page on this site (starting with /) or a full https:// link.')
        return url

    def clean(self):
        """Require a target for a targeted audience and a time for a scheduled send."""
        cleaned = super().clean()
        A = models.Announcement
        if cleaned.get('audience') == A.AUDIENCE_TARGETED and not any(
                cleaned.get(n) for n in ('institutions', 'programmes', 'cohorts', 'modules', 'users')):
            self.add_error('audience', 'Pick at least one institution, programme, cohort, module or person.')
        if cleaned.get('when') == self.WHEN_LATER:
            from django.utils import timezone
            when = cleaned.get('scheduled_for')
            if not when:
                self.add_error('scheduled_for', 'Pick when it should go out.')
            elif when <= timezone.now():
                self.add_error('scheduled_for', 'That time has already passed.')
        return cleaned

class DiscussionForm(_StyledForm):
    """Start a discussion / post (Q&A-style)."""

    class Meta:
        """Discussion fields and widgets."""
        model = models.Discussion
        fields = ['title', 'body', 'module', 'tags', 'mentions']
        widgets = {
            'body': forms.Textarea(attrs={'rows': 5, 'placeholder': 'Share your question or topic…'}),
            'mentions': forms.SelectMultiple(attrs={'size': 5}),
        }


class DiscussionReplyForm(_StyledForm):
    """Reply to a discussion."""

    class Meta:
        """Just the reply body."""
        model = models.DiscussionReply
        fields = ['body']
        widgets = {'body': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Write a reply…'})}


class NotificationPreferenceForm(_StyledForm):
    """Per-user notification & reminder settings.

    The channel tick-boxes are independent: take e-mail, WhatsApp, both or
    neither. The in-app bell is always on and is not offered as a choice —
    it is the platform's own record of what it told you.
    """

    class Meta:
        """Every notification switch, the digest, quiet hours and reminder lead time."""
        model = models.NotificationPreference
        fields = ['email_enabled', 'email_reminders', 'email_updates', 'whatsapp_enabled', 'browser_enabled',
                  'notify_mentions', 'notify_messages', 'notify_announcements',
                  'notify_deadlines', 'notify_meetings', 'notify_grades',
                  'notify_content', 'notify_assessments', 'notify_weekly',
                  'digest', 'quiet_hours_start', 'quiet_hours_end', 'reminder_lead_minutes']
        labels = {
            'email_enabled': 'E-mail me messages from the Metanoia team',
            'whatsapp_enabled': 'Send me a copy on WhatsApp',
        }
        widgets = {
            'quiet_hours_start': forms.TimeInput(attrs={'type': 'time'}),
            'quiet_hours_end': forms.TimeInput(attrs={'type': 'time'}),
        }

    def __init__(self, *args, **kwargs):
        """Set the help texts, including where WhatsApp messages will land.

        When there is no number on file the WhatsApp switch says so plainly — a tick-box
        that silently does nothing is worse than one that is not offered.
        """
        super().__init__(*args, **kwargs)
        self.fields['email_enabled'].help_text = (
            'Announcements and notes your coaches and the team send you.')
        person = getattr(getattr(self.instance, 'user', None), 'profile', None)
        number = getattr(getattr(person, 'contact', None), 'primary_phone', '')
        field = self.fields['whatsapp_enabled']
        if number:
            field.help_text = (f'Sent to {number} — the number on your contact details. '
                               'Change it under Contact on your profile.')
        else:
            field.help_text = ('Add a phone number under Contact on your profile first — '
                               'there is nowhere to send it yet.')


class WorkspaceForm(_StyledForm):
    """Make or edit a study group.

    Built with the request so the module and member choices are the user's own
    scope (:mod:`apps.communication.study_groups`). Django only accepts a posted
    id that is in the field's queryset, so an out-of-scope person or module is
    a form error, never a saved row. The page draws its own searchable member
    picker; this form is what checks it.
    """

    class Meta:
        """Study-group fields; members as checkboxes."""
        model = models.Workspace
        fields = ['name', 'description', 'module', 'members']
        labels = {'name': 'Group name', 'module': 'Module'}
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'members': forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, request=None, **kwargs):
        """Limit the module and member choices to what the requester may pick.

        When editing, the people and module already on the group stay valid choices, so
        editing the name never drops someone who has since changed programme.
        """
        super().__init__(*args, **kwargs)
        from . import study_groups
        self.request = request
        modules = study_groups.eligible_modules(request)
        members = study_groups.eligible_members(request)
        if self.instance.pk:
            from django.contrib.auth import get_user_model
            current = list(self.instance.members.values_list('pk', flat=True))
            members = (get_user_model().objects
                       .filter(Q(pk__in=members.values('pk')) | Q(pk__in=current)))
            if self.instance.module_id:
                modules = modules.model.objects.filter(
                    Q(pk__in=modules.values('pk')) | Q(pk=self.instance.module_id)
                ).select_related('programme__institution', 'module')
        self.fields['module'].queryset = modules
        self.fields['module'].empty_label = 'No particular module'
        self.fields['module'].label_from_instance = lambda pm: f'{pm.display_name} ({pm.label})'
        self.fields['members'].queryset = members
        self.fields['members'].required = False
        self.fields['name'].widget.attrs['placeholder'] = 'e.g. Tax revision circle'
        self.fields['description'].widget.attrs['placeholder'] = 'What the group is working on'

    def clean(self):
        """Check every chosen member may join a group on the chosen module."""
        cleaned = super().clean()
        module = cleaned.get('module')
        people = cleaned.get('members') or []
        if module and people:
            from . import study_groups
            allowed = set(study_groups.eligible_members(self.request, programme=module.programme)
                          .values_list('pk', flat=True))
            if self.instance.pk:
                allowed |= set(self.instance.members.values_list('pk', flat=True))
            outside = [u for u in people if u.pk not in allowed]
            if outside:
                self.add_error('members', 'Everyone in the group must be registered on the '
                                          "module's programme.")
        return cleaned


class WorkspaceFileForm(_StyledForm):
    """Upload a file to a study group."""
    class Meta:
        """The file and its title."""
        model = models.WorkspaceFile
        fields = ['file', 'title']


class WorkspaceNoteForm(_StyledForm):
    """Write or edit a study-group note."""
    class Meta:
        """Title, body and pinned flag."""
        model = models.WorkspaceNote
        fields = ['title', 'body', 'pinned']
        widgets = {'body': forms.Textarea(attrs={'rows': 5})}
