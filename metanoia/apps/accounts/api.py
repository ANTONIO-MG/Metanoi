"""REST API viewsets for the accounts app (mounted at ``/api/accounts/``).

Exposes the universal user/profile and organisation-structure models so the
web, mobile and desktop clients share one API surface.  All endpoints require
an authenticated user; the activity log is read-only.
"""

from django.contrib.auth import get_user_model
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from core.roles import is_developer, role_flags

from . import roles_admin
from . import serializers as s
from .models import ActivityLog, Person

User = get_user_model()


class IsManager(permissions.BasePermission):
    """Admin/staff only — the API twin of ``ManagementAccessMiddleware``.

    Uses ``role_flags(...)['is_admin_staff']`` rather than DRF's ``IsAdminUser``
    so it recognises the same people the HTML management pages do: Django
    staff/superusers *and* accounts whose ``Person.user_type`` is admin or staff.
    """

    message = 'Only administrators and staff may perform this action.'

    def has_permission(self, request, view):
        """True for a signed-in admin/staff account."""
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        return role_flags(request)['is_admin_staff']


class IsAdministrator(permissions.BasePermission):
    """Administrators only, by the same test as the member-management page
    (:func:`apps.accounts.roles_admin.is_admin_user`) — on top of DRF's
    ``IsAdminUser`` (``User.is_staff``), so a Django-staff flag alone on a
    non-admin role is not enough to rewrite other people's logins."""

    message = 'Only administrators may perform this action.'

    def has_permission(self, request, view):
        """True for an administrator (:func:`apps.accounts.roles_admin.is_admin_user`)."""
        return roles_admin.is_admin_user(getattr(request, 'user', None))


class IsDeveloper(permissions.BasePermission):
    """Developers only (:func:`core.roles.is_developer`): the API twin of the
    gate on the error log, audit log, background jobs and backups."""

    message = 'Only developers may see this.'

    def has_permission(self, request, view):
        """True for a developer account."""
        return is_developer(getattr(request, 'user', None))


class UserViewSet(viewsets.ModelViewSet):
    """Login accounts (``auth.User``) — Django staff who are administrators only.

    A developer's login (e-mail, active flag) is for developers and its owner
    to change: an administrator could otherwise take the account over.
    """
    queryset = User.objects.all().order_by('id')
    serializer_class = s.UserSerializer
    permission_classes = [permissions.IsAdminUser, IsAdministrator]
    search_fields = ['email', 'first_name', 'last_name']
    ordering_fields = ['id', 'email', 'date_joined']
    filterset_fields = ['is_active', 'is_staff']

    def _check_may_edit(self, user):
        """Raise ``PermissionDenied`` unless the requester may edit ``user``'s person."""
        person = getattr(user, 'profile', None)
        if not roles_admin.may_edit_person(self.request.user, person):
            raise PermissionDenied("Only a developer can change a developer's account.")

    def perform_update(self, serializer):
        """Save an update the requester is allowed to make."""
        self._check_may_edit(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        """Delete an account the requester is allowed to delete."""
        self._check_may_edit(instance)
        instance.delete()


class PersonViewSet(viewsets.ModelViewSet):
    """Profiles. Admin/staff see everyone; everybody else sees only themselves.

    The HTML side is gated by ``ManagementAccessMiddleware``, but ``/api/`` is on
    that middleware's exempt list — so the scoping has to be repeated here or the
    API is a way round it. Three things are enforced:

    * ``get_queryset`` narrows non-admins to their own row, so another person's
      profile cannot be read, rewritten or deleted;
    * ``get_serializer_class`` gives non-managers a serializer where
      :data:`~.serializers.PERSON_PRIVILEGED_FIELDS` (``user_type`` above all)
      and the identity fields the website locks
      (:data:`~.serializers.PERSON_SELF_LOCKED_FIELDS`) are read-only, and
      staff who are not administrators one where ``user_type`` is read-only —
      only an administrator changes roles, exactly as on the website
      (:mod:`apps.accounts.roles_admin`);
    * an administrator's record cannot be changed or deleted by anybody who
      is not an administrator (or its owner);
    * creating and deleting people is admin-only regardless of scope — a
      profile is created by the signup flow, not by a client.
    """

    queryset = Person.objects.select_related('user').order_by('first_name', 'last_name', 'id')
    serializer_class = s.PersonSerializer
    permission_classes = [permissions.IsAuthenticated]
    search_fields = ['first_name', 'last_name', 'user__email']
    ordering_fields = ['first_name', 'last_name']
    filterset_fields = ['user_type', 'gender', 'profile_status']
    admin_only_actions = ('create', 'destroy')

    def _is_manager(self):
        """True for admin/staff (by profile type as well as Django flags)."""
        return role_flags(self.request)['is_admin_staff']

    def get_permissions(self):
        """Creating and deleting people (:attr:`admin_only_actions`) needs :class:`IsManager`."""
        if getattr(self, 'action', None) in self.admin_only_actions:
            return [IsManager()]
        return super().get_permissions()

    def get_queryset(self):
        """Everyone for managers; only the requester's own row otherwise."""
        qs = super().get_queryset()
        if self._is_manager():
            return qs
        return qs.filter(user=self.request.user)

    def get_serializer_class(self):
        """Pick the serializer for the requester's role.

        Non-managers get the locked self-service serializer; administrators may
        change roles; other staff get ``user_type`` read-only — only an
        administrator changes roles, the website's rule (:mod:`apps.accounts.roles_admin`).
        """
        if not self._is_manager():
            return s.PersonSerializer
        if roles_admin.is_admin_user(self.request.user):
            return s.PersonAdminSerializer
        return s.PersonStaffSerializer

    def _check_may_edit(self, person):
        """Raise ``PermissionDenied`` unless the requester may edit ``person``.

        An administrator's record is for administrators (and its owner); a
        developer's for developers. Staff cannot rewrite or delete either.
        """
        if not roles_admin.may_edit_person(self.request.user, person):
            if person.is_developer:
                raise PermissionDenied("Only a developer can change a developer's account.")
            raise PermissionDenied("Only an administrator can change an administrator's account.")

    def perform_update(self, serializer):
        """Save an update the requester is allowed to make."""
        self._check_may_edit(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        """Delete a person the requester may delete — never the last administrator."""
        self._check_may_edit(instance)
        if instance.user_type == 'admin' and not Person.objects.filter(
                user_type='admin').exclude(pk=instance.pk).exists():
            raise PermissionDenied('That is the only administrator left.')
        instance.delete()


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    """The audit log, read-only and for developers only (:mod:`apps.accounts.developers`)."""
    queryset = ActivityLog.objects.select_related('actor', 'target_user').all()
    serializer_class = s.ActivityLogSerializer
    permission_classes = [permissions.IsAdminUser, IsDeveloper]
    search_fields = ['description', 'request_path']
    ordering_fields = ['timestamp', 'action']
    filterset_fields = ['action', 'actor', 'target_user']
