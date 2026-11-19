"""Identity models: Person profile, invitations, settings, archive, undo and audit log.

:class:`Person` is the profile attached to every ``auth.User`` (``user.profile``)
and carries the role (``user_type``), onboarding flags and learning track. Around
it sit contact, study-calendar and POPIA consent records, the parent ↔ student
link, single-use invitations, per-user settings (which also hold the soft-close
bookkeeping that archives an account), the archive snapshot written on purge,
reversible staff actions (:class:`UndoAction`), WhatsApp phone verification and
one-on-one coaching requests.

Archived accounts are hidden from ``Person.objects`` (see
:class:`LivePersonManager`); admin, purge and restore code uses
``Person.all_objects``.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core import validators as v


GENDER_CHOICES = [
    ('male', 'Male'),
    ('female', 'Female'),
    ('other', 'Other'),
]

TITLE_CHOICES = [
    ('mr', 'Mr'), ('mrs', 'Mrs'), ('ms', 'Ms'), ('miss', 'Miss'),
    ('dr', 'Dr'), ('prof', 'Prof'),
]

FUNDING_SOURCE_CHOICES = [
    ('self', 'Self'), ('parent', 'Parent / Guardian'), ('employer', 'Employer'),
    ('nsfas', 'NSFAS'), ('government', 'Government'), ('scholarship', 'Scholarship'),
    ('company', 'Company'), ('other', 'Other'),
]

DEVICE_CHOICES = [
    ('laptop', 'Laptop'), ('desktop', 'Desktop'), ('tablet', 'Tablet'), ('phone', 'Phone'),
]

USER_TYPE_CHOICES = [
    ('student', 'Student'),
    ('admin', 'Admin'),
    ('staff', 'Staff'),
    ('parent', 'Parent'),
    ('educator', 'Educator'),
    ('community', 'Community member'),
]
"""Roles a :class:`Person` can hold. ``community`` is a Metanoia Community member who
joined at ``/join/`` (no e-mail verification, no enrolment); they are confined by
``CommunityAccessMiddleware`` until they become a student."""


ARCHIVED_USER_LOOKUP = 'account_settings__is_closing'
"""Lookup (from ``auth.User``) marking an archived account: closed or admin-deleted,
awaiting purge (see :mod:`apps.accounts.archiving`). Hidden everywhere except the
Django admin and the closed-accounts desk."""


def archived_user_ids():
    """Subquery of archived users' ids. A subquery rather than a join, so the
    filtered querysets still allow ``select_for_update`` / ``update_or_create``."""
    from django.apps import apps as django_apps
    return (django_apps.get_model('accounts', 'UserSettings')._base_manager
            .filter(is_closing=True).values('user_id'))


class PersonQuerySet(models.QuerySet):
    """Person queryset that can split live accounts from archived ones."""

    def live(self):
        """Without archived accounts."""
        return self.exclude(user_id__in=archived_user_ids())

    def archived(self):
        """Only archived accounts."""
        return self.filter(user_id__in=archived_user_ids())


class LivePersonManager(models.Manager.from_queryset(PersonQuerySet)):
    """``Person.objects`` — every person except archived accounts.

    The central switch hiding archived accounts. Admin, purge and restore use
    ``Person.all_objects``; relations use the unfiltered base manager, so
    ``user.profile`` still works for an archived account.
    """

    def get_queryset(self):
        """Every person whose account is not archived."""
        return super().get_queryset().live()


def profile_picture_path(instance, filename):
    """A fresh name per upload, so a replaced picture gets a new URL and
    browsers don't keep showing the cached old one."""
    import os
    ext = os.path.splitext(filename or '')[1].lower()[:8] or '.jpg'
    return f'profile_pics/{uuid.uuid4().hex[:16]}{ext}'


DEFAULT_AVATARS = {
    'male': 'images/avatar/default-male.svg',
    'female': 'images/avatar/default-female.svg',
    'neutral': 'images/avatar/default-neutral.svg',
}
"""Fallback avatars (static paths), chosen by gender, else by title."""
_TITLE_GENDER = {'mr': 'male', 'mrs': 'female', 'ms': 'female', 'miss': 'female'}


def default_avatar_url(gender='', title=''):
    """The static URL of the default avatar for this gender / title."""
    from django.templatetags.static import static
    key = gender if gender in ('male', 'female') else _TITLE_GENDER.get(title or '', 'neutral')
    return static(DEFAULT_AVATARS[key])


class Person(models.Model):
    """Profile for an ``auth.User`` (``user.profile``); auto-created by
    :func:`apps.accounts.signals.create_person_profile`.

    ``registered`` and ``profile_status`` together gate the dashboard
    (``OnboardingMiddleware``); ``pending_invoice_uid`` adds the enrolment
    payment gate. ``objects`` hides archived accounts, ``all_objects`` does not.
    Enrolment rows live in ``apps.learning`` to avoid an accounts ↔ learning
    import cycle, so the enrolment properties import lazily.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile',
    )

    title = models.CharField(max_length=10, choices=TITLE_CHOICES, blank=True)
    first_name = models.CharField(max_length=50, blank=True)
    last_name = models.CharField(max_length=50, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    profile_picture = models.ImageField(upload_to=profile_picture_path, blank=True, null=True,
                                        validators=v.validate_avatar)

    funding_source = models.CharField(
        'Who pays the fees', max_length=20, choices=FUNDING_SOURCE_CHOICES, blank=True)
    primary_device = models.CharField(
        'Primary learning device', max_length=12, choices=DEVICE_CHOICES, blank=True)

    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='student')
    TRACK_CLASS = 'class'
    TRACK_ONE_ON_ONE = 'one_on_one'
    TRACK_CHOICES = [(TRACK_CLASS, 'Group classes'), (TRACK_ONE_ON_ONE, 'One-on-one')]
    learning_track = models.CharField('Learning track', max_length=12, choices=TRACK_CHOICES,
                                      default=TRACK_CLASS, db_index=True)
    """How a student is taught and billed, for every module they take (owner,
    2026-11-10: one track per student, never mixed). ``class`` — group classes,
    per module per month; ``one_on_one`` — private sessions bought by the hour
    (``apps.shop.one_on_one``). One-on-one modules stay open (schedule,
    documents, their own recordings) whatever they owe."""
    is_developer = models.BooleanField('Developer', default=False)
    """An admin who also sees platform tools (error/audit log, jobs, backups). Only
    counts on an admin account (``core.roles.is_developer``); only a developer may
    grant or revoke it. Set automatically for ``settings.DEVELOPER_EMAILS``."""
    profile_status = models.BooleanField(default=False)

    registered = models.BooleanField(default=False)
    """With ``profile_status``, gates the dashboard (``OnboardingMiddleware``)."""
    phone = models.CharField(max_length=30, blank=True)
    whatsapp_number = models.CharField('Verified WhatsApp number', max_length=20, blank=True,
                                       db_index=True, editable=False)
    """Verified by code (:mod:`apps.accounts.phone_verify`), digits only (2782…).
    Used for notifications and inbound matching. Not unique: a parent and child
    may share one."""
    whatsapp_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    parent_invite_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    enrolled_class = models.CharField('Class', max_length=80, blank=True)
    ms_upn = models.CharField('Microsoft Teams UPN', max_length=255, blank=True)
    """When set for an educator, live classes are hosted under this licensed Teams
    account; otherwise the account e-mail or ``MS_GRAPH_DEFAULT_ORGANIZER``
    (``apps.msteams.services.organizer_upn_for``)."""
    child_name = models.CharField('Child / dependant name', max_length=160, blank=True)

    community_institution = models.ForeignKey(
        'learning.Institution', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    community_programme = models.ForeignKey(
        'learning.Programme', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    """``community_institution`` / ``community_programme``: what a community member
    said they study at ``/join/``; places them in their programme's community.
    Blank for students; kept after upgrading."""
    community_joined_at = models.DateTimeField(null=True, blank=True, editable=False)
    community_explore = models.BooleanField('Explore other communities', default=False)
    """Off: only the general community plus their programme's. On: can browse and
    join others (``apps.communication.models.CommunityMembership``)."""

    other_institution = models.CharField('Institution (as typed)', max_length=160, blank=True)
    other_cohort = models.CharField('Cohort or year (as typed)', max_length=60, blank=True)
    """``other_institution`` / ``other_cohort``: free text for someone who picked
    "Other institution" (the OTHER row)."""
    upgrade_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    upgrade_verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    """"Become a student": requested when the verification e-mail goes out,
    verified on click; ``CommunityAccessMiddleware`` then forces a new password,
    which completes the upgrade."""

    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=20, blank=True)
    """``terms_accepted_at`` / ``terms_version`` are captured server-side at signup
    (``MetanoiaSignupForm``); null for admin-provisioned or pre-consent accounts."""

    pending_invoice_uid = models.UUIDField(null=True, blank=True)
    """Enrolment checkout gate: ``public_id`` of the invoice to settle before the
    onboarding gate lifts. A UUID, not a foreign key, to avoid an accounts ↔
    finance migration cycle."""
    invite = models.ForeignKey(
        'accounts.Invitation', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    """The invite this account signed up through; ``None`` = student self-registration."""
    guardian_emails = models.JSONField(default=list, blank=True)
    """Guardian e-mails the student entered; saving step 1 invites each one."""
    preview_of = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name='preview_student', editable=False,
        help_text='The admin/educator account this preview student belongs to.',
    )
    """Super student (:mod:`core.student_view`): the hidden preview account's owner.
    Such a row is never a real student — rosters, counts and broadcasts exclude it."""

    objects = LivePersonManager()
    all_objects = models.Manager.from_queryset(PersonQuerySet)()

    @property
    def is_preview(self):
        """True for a super-student preview account (see :mod:`core.student_view`)."""
        return self.preview_of_id is not None

    @property
    def is_community(self):
        """True for a Metanoia Community member (joined at /join/, not a student)."""
        return self.user_type == 'community'

    @property
    def has_own_picture(self):
        """True when the person uploaded a picture (rather than wearing a default)."""
        return bool(self.profile_picture)

    @property
    def avatar_url(self):
        """The uploaded picture, or the default that matches their gender / title.

        A super-student preview without a picture wears its owner's face. A row
        naming a file the storage no longer has falls back to the default.
        """
        if not self.profile_picture and self.preview_of_id:
            owner = getattr(self.preview_of, 'profile', None)
            if owner is not None:
                return owner.avatar_url
        if self.profile_picture:
            try:
                return self.profile_picture.url
            except ValueError:
                pass
        return default_avatar_url(self.gender or '', self.title or '')

    def __str__(self):
        full_name = f'{self.first_name} {self.last_name}'.strip()
        if full_name:
            return full_name
        from core.utils import display_name
        return display_name(self.user)

    def get_pending_invoice(self):
        """The unsettled enrolment invoice for this person, or ``None``."""
        if not self.pending_invoice_uid:
            return None
        from apps.finance.models import Invoice
        return Invoice.objects.filter(public_id=self.pending_invoice_uid).first()

    @property
    def is_one_on_one(self):
        """A one-on-one student (see ``learning_track``)."""
        return self.learning_track == self.TRACK_ONE_ON_ONE

    @property
    def enrolment_settled(self):
        """True when no unpaid enrolment invoice is blocking access."""
        if not self.pending_invoice_uid:
            return True
        inv = self.get_pending_invoice()
        return inv is None or inv.status == 'paid'

    @property
    def onboarding_complete(self):
        """True once registration, profile AND the enrolment payment are done."""
        return bool(self.registered and self.profile_status and self.enrolment_settled)

    @property
    def enrolled_programmes(self):
        """Programmes this student is registered for."""
        from apps.learning.models import Programme
        return Programme.objects.filter(
            enrolments__person=self, enrolments__is_active=True,
        ).distinct()

    @property
    def enrolled_modules(self):
        """Modules on trial or active. Fast list for menus; exact trial expiry is
        enforced at access time by ``ModuleEnrolment.is_unlocked``."""
        from apps.learning.models import ModuleEnrolment, ProgrammeModule
        return ProgrammeModule.objects.filter(
            enrolments__person=self,
            enrolments__status__in=[ModuleEnrolment.STATUS_TRIAL, ModuleEnrolment.STATUS_ACTIVE],
        ).distinct()

    @property
    def selected_modules(self):
        """Every module the student has an enrolment row for — locked ones included."""
        from apps.learning.models import ProgrammeModule
        return ProgrammeModule.objects.filter(enrolments__person=self).distinct()

    @property
    def programme(self):
        """The most recent active programme registration's programme, or ``None``."""
        enrolment = (self.programme_enrolments
                     .filter(is_active=True)
                     .select_related('programme__institution')
                     .order_by('-created_at')
                     .first())
        return enrolment.programme if enrolment else None


CONSENT_VERSION = '2026-07'
"""Current POPIA consent wording version. Bump it when the wording changes
materially, so a re-consent prompt can tell who has only agreed to a superseded
version."""


class PersonContact(models.Model):
    """Phone and rough location. ``phone_country`` (ISO alpha-2) is stored so the
    picker reopens on the right country without parsing ``primary_phone``."""
    person = models.OneToOneField('accounts.Person', on_delete=models.CASCADE, related_name='contact')
    phone_country = models.CharField('Phone country', max_length=2, blank=True, default='ZA')
    primary_phone = models.CharField('Phone number (WhatsApp)', max_length=30, blank=True)
    suburb = models.CharField('Area / suburb', max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Contact · {self.person}'


class PersonStudyProfile(models.Model):
    """The student's academic-calendar dates as JSON lists (settings → Calendar).

    Each ``*_dates`` field is a list of ``{'label': str, 'date': 'YYYY-MM-DD'}``.
    """
    person = models.OneToOneField('accounts.Person', on_delete=models.CASCADE, related_name='study_profile')
    exam_dates = models.JSONField(default=list, blank=True)
    assignment_dates = models.JSONField(default=list, blank=True)
    practical_dates = models.JSONField(default=list, blank=True)
    vacation_dates = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Study profile · {self.person}'


class PersonConsent(models.Model):
    """POPIA consent record. ``privacy_policy_accepted`` and
    ``data_processing_accepted`` are the mandatory basis for operating the LMS;
    the rest are optional opt-ins. The audit fields (date/version/IP/device) and
    the data-subject-rights timestamps satisfy POPIA accountability.
    ``consent_device`` holds the user-agent string."""
    person = models.OneToOneField('accounts.Person', on_delete=models.CASCADE, related_name='consent')
    privacy_policy_accepted = models.BooleanField(default=False)
    data_processing_accepted = models.BooleanField(default=False)
    marketing_consent = models.BooleanField(default=False)
    research_consent = models.BooleanField(default=False)
    ai_personalisation_consent = models.BooleanField(default=False)
    analytics_consent = models.BooleanField(default=False)
    cookie_consent = models.BooleanField(default=False)
    data_sharing_consent = models.BooleanField(default=False)
    consent_date = models.DateTimeField(null=True, blank=True)
    consent_version = models.CharField(max_length=20, blank=True)
    consent_ip = models.GenericIPAddressField(null=True, blank=True)
    consent_device = models.CharField(max_length=300, blank=True)
    account_deletion_requested = models.DateTimeField(null=True, blank=True)
    data_export_requested = models.DateTimeField(null=True, blank=True)
    data_retention_expiry = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Consent · {self.person}'


class ActivityLog(models.Model):
    """One row per audited event. Written by :mod:`apps.accounts.signals`;
    exposed read-only via the admin and the REST API."""

    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('archive', 'Archive'),
        ('restore', 'Restore'),
        ('undo', 'Undo'),
    ]

    actor = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name='actions_performed',
    )
    target_user = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.SET_NULL, related_name='actions_received',
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    description = models.TextField(blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    request_path = models.CharField(max_length=255, null=True, blank=True)

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        """Newest first, indexed by action and timestamp."""
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['action']),
            models.Index(fields=['timestamp']),
        ]

    def __str__(self):
        return f'{self.action} - {self.timestamp}'


class ParentLink(models.Model):
    """Links a parent/guardian account to the student (regular user) they look
    after. A student may have at most :attr:`MAX_PER_STUDENT` parents."""

    MAX_PER_STUDENT = 2

    parent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='guardian_of',
    )
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='guardians',
    )
    relationship = models.CharField(max_length=60, blank=True, help_text='e.g. Mother, Sponsor')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One link per parent–student pair; newest first."""
        unique_together = ('parent', 'student')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.parent} → {self.student}'

    @classmethod
    def count_for_student(cls, student):
        """How many parents are linked to ``student``."""
        return cls.objects.filter(student=student).count()

    @classmethod
    def can_add_parent(cls, student):
        """True while ``student`` has fewer than :attr:`MAX_PER_STUDENT` parents."""
        return cls.count_for_student(student) < cls.MAX_PER_STUDENT


class Invitation(models.Model):
    """A single-use invite to register with a pre-decided role.

    Parent invites are tied to a ``student``, educator invites to a ``programme``.
    A parent's account is created already verified (the e-mail is the invited
    one). The link stops working once accepted or after :attr:`EXPIRY_DAYS`.
    """
    EXPIRY_DAYS = 30
    """A pending invite older than this many days no longer opens."""

    ROLE_PARENT = 'parent'
    ROLE_EDUCATOR = 'educator'
    ROLE_CHOICES = [(ROLE_PARENT, 'Parent / Guardian / Sponsor'), (ROLE_EDUCATOR, 'Educator')]

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REVOKED = 'revoked'
    STATUS_CHOICES = [(STATUS_PENDING, 'Pending'), (STATUS_ACCEPTED, 'Accepted'), (STATUS_REVOKED, 'Revoked')]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    email = models.EmailField()
    student = models.ForeignKey(
        'accounts.Person', on_delete=models.CASCADE, null=True, blank=True, related_name='parent_invites')
    programme = models.ForeignKey(
        'learning.Programme', on_delete=models.CASCADE, null=True, blank=True, related_name='educator_invites')
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_invites')
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='accepted_invites')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        target = self.student or self.programme or '—'
        return f'{self.get_role_display()} invite for {self.email} ({target})'

    @property
    def is_expired(self):
        """True once a still-pending invite is older than :attr:`EXPIRY_DAYS`."""
        if self.status != self.STATUS_PENDING or self.created_at is None:
            return False
        return self.created_at < timezone.now() - timedelta(days=self.EXPIRY_DAYS)

    @property
    def is_open(self):
        """True while the invite is pending and not expired."""
        return self.status == self.STATUS_PENDING and not self.is_expired

    def accept(self, user):
        """Mark the invite accepted by ``user`` now and save it."""
        from django.utils import timezone as _tz
        self.status = self.STATUS_ACCEPTED
        self.accepted_by = user
        self.accepted_at = _tz.now()
        self.save(update_fields=['status', 'accepted_by', 'accepted_at'])


class UserSettings(models.Model):
    """Per-user preferences/privacy and close-account bookkeeping
    (soft close → grace period → ``purge_closed_accounts``).

    Fields fall into three groups: privacy (profile visibility and what it
    shows), preferences (theme, language, time zone, calendar feed, digest) and
    account-closure bookkeeping (``is_closing`` … ``close_reason``).
    """

    GRACE_DAYS = 30
    """Grace period, in days, when the owner closes their own account."""
    ADMIN_GRACE_DAYS = 5
    """Grace period, in days, for an account an administrator deleted."""

    VISIBILITY_PUBLIC = 'public'
    VISIBILITY_MEMBERS = 'members'
    VISIBILITY_PRIVATE = 'private'
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, 'Public — anyone signed in'),
        (VISIBILITY_MEMBERS, 'Members — people in my groups'),
        (VISIBILITY_PRIVATE, 'Private — only me'),
    ]

    THEME_LIGHT = 'light'
    THEME_DARK = 'dark'
    THEME_AUTO = 'auto'
    THEME_CHOICES = [
        (THEME_LIGHT, 'Light'),
        (THEME_DARK, 'Dark'),
        (THEME_AUTO, 'Match system'),
    ]

    DIGEST_OFF = 'off'
    DIGEST_DAILY = 'daily'
    DIGEST_WEEKLY = 'weekly'
    DIGEST_CHOICES = [
        (DIGEST_OFF, 'Never'),
        (DIGEST_DAILY, 'Daily'),
        (DIGEST_WEEKLY, 'Weekly'),
    ]

    LANGUAGE_CHOICES = [
        ('en', 'English'),
        ('fr', 'Français (French)'),
        ('pt', 'Português (Portuguese)'),
    ]
    """Only languages with a real translation catalogue — offering one without looks broken."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='account_settings',
    )

    profile_visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_MEMBERS,
        help_text='Who may see your member profile page.')
    show_email = models.BooleanField(
        default=False, help_text='Show your e-mail address on your profile.')
    show_activity = models.BooleanField(
        default=True, help_text='Show your recent activity on your profile.')
    allow_messages = models.BooleanField(
        default=True, help_text='Let other members start a direct chat with you.')

    theme = models.CharField(max_length=6, choices=THEME_CHOICES, default=THEME_AUTO)
    language = models.CharField(max_length=8, choices=LANGUAGE_CHOICES, default='en')
    timezone = models.CharField(
        max_length=64, blank=True,
        help_text='Your time zone. Session times, reminders and the calendar are '
                  'shown in it. Blank = the platform default.')
    """Display only — stored datetimes are UTC. Matters for live-session start times
    across countries. Blank = ``settings.TIME_ZONE``."""
    calendar_token = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    """Calendar-feed URL secret. Feed subscribers cannot sign in, so the token *is*
    the authentication; rotating it revokes every old link."""
    email_digest = models.CharField(
        max_length=8, choices=DIGEST_CHOICES, default=DIGEST_WEEKLY,
        help_text='How often to bundle non-urgent e-mail.')

    is_closing = models.BooleanField(default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    purge_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', help_text='The administrator who deleted (archived) this account.')
    """Who archived it — empty when the owner closed their own account."""
    close_reason = models.CharField(max_length=255, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Admin names (the model is per-user, so the plural reads the same)."""
        verbose_name = 'user settings'
        verbose_name_plural = 'user settings'

    def __str__(self):
        return f'Settings · {self.user}'

    @classmethod
    def for_user(cls, user):
        """Get-or-create the settings row for ``user``."""
        obj, _ = cls.objects.get_or_create(user=user)
        return obj

    def mark_closing(self, now=None, *, days=None, by=None, reason=''):
        """Flag the account as closing and stamp the purge deadline.

        ``days`` defaults to :attr:`GRACE_DAYS` (the owner closing their own
        account); an administrator's delete passes :attr:`ADMIN_GRACE_DAYS`.
        """
        now = now or timezone.now()
        self.is_closing = True
        self.closed_at = now
        self.purge_at = now + timedelta(days=self.GRACE_DAYS if days is None else days)
        self.closed_by = by if getattr(by, 'pk', None) else None
        self.close_reason = (reason or '')[:255]
        self.save(update_fields=['is_closing', 'closed_at', 'purge_at', 'closed_by',
                                 'close_reason', 'updated_at'])
        return self

    def cancel_closing(self):
        """Undo a pending closure (an admin can reactivate the account)."""
        self.is_closing = False
        self.closed_at = None
        self.purge_at = None
        self.closed_by = None
        self.close_reason = ''
        self.save(update_fields=['is_closing', 'closed_at', 'purge_at', 'closed_by',
                                 'close_reason', 'updated_at'])
        return self


class AccountArchive(models.Model):
    """JSON snapshot of a purged account, written by ``purge_closed_accounts``
    before the user row is deleted, for restore/audit. A copy also goes to
    ``BASE_DIR/backups/archived_accounts/``."""

    original_user_id = models.IntegerField(null=True, blank=True)
    username = models.CharField(max_length=254, blank=True)
    email = models.EmailField(blank=True)
    data = models.JSONField(default=dict)
    file_path = models.CharField(max_length=500, blank=True)
    archive_dir = models.CharField(max_length=500, blank=True)
    """The account's archive-park directory (what a restore reads); ``file_path``
    only points at the summary inside it."""
    content_rows = models.PositiveIntegerField(
        default=0, help_text='How many database rows were parked with this account.')
    archived_at = models.DateTimeField(default=timezone.now, db_index=True)
    restored_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set when an administrator restored this account back onto the live system.')

    class Meta:
        """Newest archive first."""
        ordering = ['-archived_at']

    def __str__(self):
        return f'Archive · {self.username or self.email} ({self.archived_at:%Y-%m-%d})'

    @property
    def display_name(self):
        """The archived person's name, falling back to username, e-mail or id."""
        profile = (self.data or {}).get('profile') or {}
        name = f"{profile.get('first_name') or ''} {profile.get('last_name') or ''}".strip()
        return name or self.username or self.email or f'Account {self.original_user_id}'

    @property
    def is_restorable(self):
        """True while the park directory this row points at is still on disk."""
        from pathlib import Path
        return bool(self.archive_dir) and (Path(self.archive_dir) / 'content.json').exists()


class UndoAction(models.Model):
    """A reversible destructive or money-moving staff action.

    ``payload`` holds what is needed to reverse it; handlers per ``kind`` live in
    :mod:`apps.accounts.undo`. Undos are audited here and in :class:`ActivityLog`.
    """

    KIND_PAYMENT = 'payment'
    KIND_DELETE = 'delete'
    KIND_INVOICE_VOID = 'invoice_void'
    KIND_ARCHIVE = 'archive'
    KIND_CHOICES = [
        (KIND_PAYMENT, 'Payment recorded'),
        (KIND_DELETE, 'Deleted'),
        (KIND_INVOICE_VOID, 'Invoice voided'),
        (KIND_ARCHIVE, 'Account deleted (archived)'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    summary = models.CharField(max_length=255)
    object_ref = models.CharField(max_length=80, blank=True, db_index=True)
    """``app_label.model:pk`` of the main object, so a page can find the undo for
    the thing it shows (e.g. a payment row on an invoice)."""
    payload = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name='+')
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name='+')
    undo_reason = models.TextField(blank=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()} · {self.summary}'

    @property
    def is_undone(self):
        """True once the action has been reversed."""
        return self.undone_at is not None


class PhoneVerification(models.Model):
    """One WhatsApp verification code sent to one number for one account.

    Only a hash of the code is kept. A code is good for ten minutes and five
    guesses, and is used once; see :mod:`apps.accounts.phone_verify`.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='phone_verifications')
    number = models.CharField(max_length=20, db_index=True, help_text='Digits only (2782…).')
    code_hash = models.CharField(max_length=64)
    purpose = models.CharField(max_length=20, default='register')
    sent = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']
        verbose_name = 'phone verification'

    def __str__(self):
        return f'{self.number} · {self.user_id} · {"verified" if self.verified_at else "pending"}'


class OneOnOneRequest(models.Model):
    """A registration-time request for one-on-one coaching on any programme,
    typed as free text. Staff assign an educator and enrol by hand; billed via
    the ``SVC-1ON1-HOUR`` catalogue item."""
    STATUS_NEW = 'new'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_ENROLLED = 'enrolled'
    STATUS_DECLINED = 'declined'
    STATUS_CHOICES = [
        (STATUS_NEW, 'New'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_ENROLLED, 'Enrolled'),
        (STATUS_DECLINED, 'Declined'),
    ]
    OPEN_STATUSES = (STATUS_NEW, STATUS_IN_PROGRESS)

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name='one_on_one_requests')
    institution = models.CharField(max_length=160, help_text='As the student typed it.')
    programme = models.CharField(max_length=200, help_text='As the student typed it.')
    start_date = models.DateField()
    end_date = models.DateField()
    modules = models.CharField('Modules / subjects', max_length=300, blank=True)
    notes = models.TextField('Student notes', blank=True)
    cohort = models.CharField('Cohort or year', max_length=60, blank=True,
                              help_text='As the student typed it, e.g. "2027 intake" or "3rd year".')
    """Required when the student picked "Other institution"; optional otherwise."""

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    educator = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, blank=True, related_name='one_on_one_assignments',
        limit_choices_to={'user_type__in': ['educator', 'staff', 'admin']},
        help_text='The coach taking the sessions.')
    admin_notes = models.TextField(blank=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    enrolled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']
        verbose_name = 'one-on-one request'

    def __str__(self):
        return f'{self.person} · {self.institution} · {self.programme}'

    @property
    def is_open(self):
        """True while the request is new or in progress."""
        return self.status in self.OPEN_STATUSES
