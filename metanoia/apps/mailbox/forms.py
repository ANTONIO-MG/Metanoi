"""Metanoia Mail forms: the domain's mail server, and connecting a mailbox.

Every IMAP/SMTP host a form accepts is vetted by
:func:`apps.mailbox.hosts.vet_host`, because the LMS opens sockets to it.

``SERVER_OVERRIDE_FIELDS`` lists the per-mailbox server overrides. Only an
administrator may set them; everyone else uses the domain's
:class:`~apps.mailbox.models.MailServer`.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q

from .hosts import UnsafeHost, vet_host
from .models import SECURITY_CHOICES, MailAccount, MailServer

SERVER_OVERRIDE_FIELDS = ('imap_host', 'imap_port', 'imap_security', 'smtp_host', 'smtp_port', 'smtp_security')


class _VettedHostsMixin:
    """``clean_imap_host`` / ``clean_smtp_host``: public mail servers only."""

    def _vet(self, name):
        """Return the cleaned host in field ``name``, raising a validation error if it is not public."""
        host = (self.cleaned_data.get(name) or '').strip()
        if not host:
            return host
        try:
            return vet_host(host)
        except UnsafeHost as exc:
            raise forms.ValidationError(str(exc))

    def clean_imap_host(self):
        """Vet the IMAP host."""
        return self._vet('imap_host')

    def clean_smtp_host(self):
        """Vet the SMTP host."""
        return self._vet('smtp_host')


class MailServerForm(_VettedHostsMixin, forms.ModelForm):
    """An administrator editing a domain's :class:`~apps.mailbox.models.MailServer` settings."""

    class Meta:
        """Model and fields edited by :class:`MailServerForm`."""

        model = MailServer
        fields = ['domain', 'imap_host', 'imap_port', 'imap_security', 'smtp_host', 'smtp_port', 'smtp_security',
                  'username_is_email', 'server_saves_sent', 'history_days', 'max_message_mb']
        widgets = {
            'domain': forms.TextInput(attrs={'placeholder': 'metanoiagroup.co.za'}),
            'imap_host': forms.TextInput(attrs={'placeholder': 'mail.metanoiagroup.co.za'}),
            'smtp_host': forms.TextInput(attrs={'placeholder': 'mail.metanoiagroup.co.za'}),
        }


class MailAccountForm(_VettedHostsMixin, forms.ModelForm):
    """Connect or edit one person's :class:`~apps.mailbox.models.MailAccount`.

    The password is write-only: left blank on an edit, the saved one is kept.
    It is required when connecting a new mailbox.
    """

    password = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
        help_text='Leave blank to keep the saved password.')
    imap_security = forms.ChoiceField(choices=[('', 'Use the domain setting')] + SECURITY_CHOICES, required=False)
    smtp_security = forms.ChoiceField(choices=[('', 'Use the domain setting')] + SECURITY_CHOICES, required=False)

    class Meta:
        """Model and fields edited by :class:`MailAccountForm`."""

        model = MailAccount
        fields = ['email_address', 'display_name', 'password', 'signature_html', 'notify_new_mail', 'is_default',
                  'username', 'imap_host', 'imap_port', 'imap_security', 'smtp_host', 'smtp_port', 'smtp_security']
        widgets = {'signature_html': forms.Textarea(attrs={'rows': 4})}
        labels = {'signature_html': 'Signature', 'email_address': 'E-mail address'}

    def __init__(self, *args, owner=None, can_override_servers=False, **kwargs):
        """Build the form for mailbox owner ``owner``.

        Without ``can_override_servers`` the server override fields are removed
        from the form, so a non-administrator cannot point the mailbox at a
        host of their choosing: a POSTed value is ignored and the saved
        override (if any) is left as it is.
        """
        super().__init__(*args, **kwargs)
        self.owner = owner
        if not can_override_servers:
            for name in SERVER_OVERRIDE_FIELDS:
                self.fields.pop(name, None)
        if self.instance.pk is None:
            self.fields['password'].required = True
            self.fields['password'].help_text = ''

    def clean_email_address(self):
        """Normalise the address and refuse one the owner has already connected."""
        address = self.cleaned_data['email_address'].strip().lower()
        owner = self.owner or self.instance.user
        clash = MailAccount.objects.filter(user=owner, email_address__iexact=address).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('This mailbox is already connected.')
        return address

    def save(self, commit=True):
        """Save the mailbox, storing a new password and keeping one default mailbox per person."""
        account = super().save(commit=False)
        if self.owner is not None and account.user_id is None:
            account.user = self.owner
        if self.cleaned_data.get('password'):
            account.set_password(self.cleaned_data['password'])
        if commit:
            account.save()
            if account.is_default:
                MailAccount.objects.filter(user=account.user).exclude(pk=account.pk).update(is_default=False)
        return account


class AssignMailboxForm(MailAccountForm):
    """An administrator connecting a mailbox on someone's behalf."""

    person = forms.ModelChoiceField(queryset=get_user_model().objects.none(), label='Person')

    class Meta(MailAccountForm.Meta):
        """The person to connect for, plus the basic mailbox fields."""

        fields = ['person', 'email_address', 'display_name', 'password', 'notify_new_mail', 'is_default']

    def __init__(self, *args, **kwargs):
        """Offer active staff, administrators and educators as the person."""
        super().__init__(*args, **kwargs)
        User = get_user_model()
        self.fields['person'].queryset = (User.objects.filter(is_active=True).filter(
            Q(is_staff=True) | Q(is_superuser=True) | Q(profile__user_type__in=['admin', 'staff', 'educator']))
            .order_by('first_name', 'last_name', 'username').distinct())
        self.fields['person'].label_from_instance = lambda u: f'{u.get_full_name() or u.username} ({u.email})'
        self.order_fields(['person'])

    def clean(self):
        """Use the chosen person as the mailbox owner."""
        self.owner = self.cleaned_data.get('person')
        return super().clean()

    def clean_email_address(self):
        """Normalise the address; the duplicate check happens in :meth:`_post_clean`."""
        return self.cleaned_data['email_address'].strip().lower()

    def _post_clean(self):
        """Refuse a mailbox the chosen person already has."""
        super()._post_clean()
        owner = self.cleaned_data.get('person')
        address = self.cleaned_data.get('email_address')
        if owner and address and MailAccount.objects.filter(user=owner, email_address__iexact=address).exists():
            self.add_error('email_address', 'That person already has this mailbox.')
