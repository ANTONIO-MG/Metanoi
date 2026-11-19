"""DRF serializers for the accounts app.

These power the ``/api/accounts/`` endpoints (see :mod:`apps.accounts.api`)
plus the custom registration serializer wired into dj-rest-auth via
``REST_AUTH_REGISTER_SERIALIZERS`` in settings. dj-rest-auth may not be
importable in some tooling contexts, in which case the registration serializer
falls back to a plain ``Serializer`` base.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from core.utils import display_name

from .models import ActivityLog, Person

try:
    from dj_rest_auth.registration.serializers import RegisterSerializer
except Exception:  # pragma: no cover
    RegisterSerializer = serializers.Serializer

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """A login account. The platform is e-mail-only, so the internal ``username`` column is not exposed."""
    display_name = serializers.SerializerMethodField()

    class Meta:
        """Model and fields exposed by this serializer."""
        model = User
        fields = ['id', 'email', 'first_name', 'last_name', 'display_name', 'is_active', 'date_joined']
        read_only_fields = ['date_joined']

    def get_display_name(self, obj):
        """The person's display name."""
        return display_name(obj)


PERSON_PRIVILEGED_FIELDS = (
    'user_type',
    'is_developer',
    'profile_status',
    'registered',
    'pending_invoice_uid',
    'invite',
    'terms_accepted_at',
    'terms_version',
    'guardian_emails',
)
"""Person fields that decide what an account is *allowed to do*, or that the
enrolment/consent machinery owns. A user editing their own profile must never be
able to set these — ``user_type`` in particular is what :mod:`core.roles` reads to
grant ``can_manage``, so a writable ``user_type`` is a self-service route to
administrator. Admins still edit them through the management UI (and
:class:`PersonAdminSerializer`).

* ``user_type`` — the role itself; grants ``can_manage`` / ``can_teach``.
* ``is_developer`` — developer tools; only a developer grants it.
* ``profile_status``, ``registered`` — the onboarding gates.
* ``pending_invoice_uid`` — the enrolment payment gate; ``invite``.
* ``terms_accepted_at``, ``terms_version`` — consent is captured server-side at signup.
* ``guardian_emails`` — drives who gets a parent invite.
"""


PERSON_SELF_LOCKED_FIELDS = (
    'title', 'first_name', 'last_name', 'gender', 'date_of_birth',
    'ms_upn',
    'community_institution', 'community_programme',
    'other_institution', 'other_cohort',
)
"""Fields the website locks once set, which a self-service client must therefore
not be able to change either.

* Legal identity — ``ProfileForm`` (the settings page) deliberately offers only
  the picture: name, gender and date of birth appear on results and
  certificates, and changing them needs an administrator.
* ``ms_upn`` — the Microsoft account the Teams integration hosts classes under,
  and the identity ``apps.msteams.calendar_import`` matches an imported
  meeting's organiser on. Writable by a student, it would make them the host of
  somebody else's classes. Staff/admin only (Django admin, or
  :class:`PersonStaffSerializer` / :class:`PersonAdminSerializer`).
* The Metanoia Community placement chosen at ``/join/`` — it decides which
  programme community a member is in.
"""


class PersonSerializer(serializers.ModelSerializer):
    """A person as they may edit *themselves*.

    Everything in :data:`PERSON_PRIVILEGED_FIELDS` and
    :data:`PERSON_SELF_LOCKED_FIELDS` is exposed but read-only, so a client can
    display its own role, identity and enrolment state without being able to
    change them.
    """

    user = UserSerializer(read_only=True)

    class Meta:
        """Model and fields exposed by this serializer."""
        model = Person
        fields = '__all__'
        read_only_fields = PERSON_PRIVILEGED_FIELDS + PERSON_SELF_LOCKED_FIELDS


class PersonStaffSerializer(serializers.ModelSerializer):
    """The full record for a staff member who is not an administrator.

    Writable — staff manage students — except ``user_type``: on the website
    only an administrator changes what kind of account somebody has
    (:mod:`apps.accounts.roles_admin`), and the API follows the same rule.
    """

    user = UserSerializer(read_only=True)

    class Meta:
        """Model and fields exposed by this serializer."""
        model = Person
        fields = '__all__'
        read_only_fields = ('user_type', 'is_developer')


class PersonAdminSerializer(serializers.ModelSerializer):
    """The full record, writable. Only ever handed to administrators.

    A role change goes through the same checks as the member-management page
    (:func:`apps.accounts.roles_admin.role_change_error`): only grantable
    roles, never demoting the last administrator — and Django-admin access is
    kept in step with the role afterwards.
    """

    user = UserSerializer(read_only=True)

    class Meta:
        """Model and fields exposed by this serializer."""
        model = Person
        fields = '__all__'

    def validate_user_type(self, value):
        """Allow a role change only when :func:`~apps.accounts.roles_admin.role_change_error` permits it."""
        from .roles_admin import role_change_error
        instance = self.instance
        if instance is not None and value == instance.user_type:
            return value
        request = self.context.get('request')
        target = instance if instance is not None else Person(user_type='student')
        error = role_change_error(getattr(request, 'user', None), target, value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_is_developer(self, value):
        """Allow granting or revoking developer access only when
        :func:`~apps.accounts.developers.developer_change_error` permits it.
        """
        instance = self.instance
        current = bool(instance.is_developer) if instance is not None else False
        if bool(value) == current:
            return value
        from .developers import developer_change_error
        request = self.context.get('request')
        target = instance if instance is not None else Person(user_type='student')
        error = developer_change_error(getattr(request, 'user', None), target, bool(value))
        if error:
            raise serializers.ValidationError(error)
        return value

    def save(self, **kwargs):
        """Save, then keep Django-admin access in step with the role.

        A developer is an administrator, so granting developer access promotes them
        (:func:`~apps.accounts.developers.make_developer`).
        """
        grant = self.validated_data.get('is_developer') is True
        person = super().save(**kwargs)
        if grant:
            from .developers import make_developer
            make_developer(person)
        else:
            from .roles_admin import sync_django_admin_flag
            sync_django_admin_flag(person)
        return person


class ActivityLogSerializer(serializers.ModelSerializer):
    """The audit log, read-only via :class:`~apps.accounts.api.ActivityLogViewSet`."""
    class Meta:
        """Model and fields exposed by this serializer."""
        model = ActivityLog
        fields = '__all__'


class CustomRegisterSerializer(RegisterSerializer):
    """Registration serializer that also captures the new user's name and
    creates the matching :class:`~apps.accounts.models.Person` profile."""

    first_name = serializers.CharField(required=False, allow_blank=True, max_length=50)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=50)

    def get_cleaned_data(self):
        """allauth's cleaned data plus the optional first and last name."""
        data = super().get_cleaned_data()
        data['first_name'] = self.validated_data.get('first_name', '')
        data['last_name'] = self.validated_data.get('last_name', '')
        return data

    def save(self, request):
        """Create the user through dj-rest-auth, then ensure their Person carries the name."""
        user = super().save(request)
        Person.objects.get_or_create(
            user=user,
            defaults={
                'first_name': self.cleaned_data.get('first_name', ''),
                'last_name': self.cleaned_data.get('last_name', ''),
            },
        )
        return user
