"""Django admin for the accounts app.

Deleting an account here never deletes it: :class:`ArchiveOnDeleteMixin` routes
single and bulk deletes through :func:`apps.accounts.archiving.archive_account`,
so the account is hidden at once and purged after the admin grace period unless
restored. Developer accounts can only be changed or deleted by developers, and
the audit log is visible to developers only.
"""

from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as django_delete_selected
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin

from . import archiving, undo
from .developers import DeveloperOnlyAdminMixin
from .models import (
    ARCHIVED_USER_LOOKUP, ActivityLog, Invitation, OneOnOneRequest, ParentLink, Person,
    PersonConsent, PersonContact, PersonStudyProfile, UndoAction, UserSettings,
)
from .resources import PersonResource


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    """Invitations to register as a parent or educator."""
    list_display = ('email', 'role', 'student', 'status', 'created_at', 'accepted_at')
    list_filter = ('role', 'status', 'created_at')
    search_fields = ('email', 'student__user__email')
    autocomplete_fields = ('student', 'invited_by', 'accepted_by')


@admin.register(ParentLink)
class ParentLinkAdmin(admin.ModelAdmin):
    """Parent ↔ student links."""
    list_display = ('parent', 'student', 'created_at')
    search_fields = ('parent__username', 'parent__email', 'student__username', 'student__email')


class PersonContactInline(admin.StackedInline):
    """A person's phone and rough location, edited on the person page."""
    model = PersonContact
    extra = 0
    can_delete = False


class PersonStudyProfileInline(admin.StackedInline):
    """The academic calendar (exam / assignment / practical / vacation dates)."""
    model = PersonStudyProfile
    extra = 0
    can_delete = False


class PersonConsentInline(admin.StackedInline):
    """A person's POPIA consent record, edited on the person page."""
    model = PersonConsent
    extra = 0
    can_delete = False


class ArchivedFilter(admin.SimpleListFilter):
    """List filter splitting archived (closed, awaiting purge) accounts from live ones.

    Works on the user changelist and on any model with a ``user`` relation.
    """
    title = 'archived'
    parameter_name = 'archived'

    def lookups(self, request, model_admin):
        """The two choices: archived and live."""
        return (('yes', 'Archived (deleted, awaiting purge)'), ('no', 'Live'))

    def queryset(self, request, queryset):
        """Filter on :data:`~apps.accounts.models.ARCHIVED_USER_LOOKUP`, through ``user__`` when needed."""
        prefix = '' if queryset.model is get_user_model() else 'user__'
        if self.value() == 'yes':
            return queryset.filter(**{f'{prefix}{ARCHIVED_USER_LOOKUP}': True})
        if self.value() == 'no':
            return queryset.exclude(**{f'{prefix}{ARCHIVED_USER_LOOKUP}': True})
        return queryset


class ArchiveOnDeleteMixin:
    """Deleting an account (single or bulk) archives it instead via
    :func:`apps.accounts.archiving.archive_account`; it is purged after
    ``UserSettings.ADMIN_GRACE_DAYS`` and restorable until then.
    """

    def _account_of(self, obj):
        """The ``auth.User`` behind ``obj`` (a user or a person)."""
        return obj if isinstance(obj, get_user_model()) else obj.user

    def get_deleted_objects(self, objs, request):
        """The confirmation page's list: each account and what archiving means for it."""
        days = UserSettings.ADMIN_GRACE_DAYS
        lines = [format_html(
            '{} — <strong>archived</strong>: sign-in is switched off and the account disappears '
            'from the LMS straight away. It can be restored for {} days; after that it is '
            'permanently deleted together with all of its data.', str(obj), days)
            for obj in objs]
        return lines, {}, set(), []

    def log_deletions(self, request, queryset):
        """Write nothing: nothing is deleted yet, and ``archive_account`` writes the audit row."""
        return None

    def _archive(self, request, objs):
        """Archive each account in ``objs``; return the undo actions.

        Only a developer may delete a developer's account; refused and failing
        accounts are reported as warnings instead.
        """
        actions, skipped = [], []
        from core.roles import is_developer
        actor_is_developer = is_developer(request.user)
        for obj in objs:
            user = self._account_of(obj)
            if not actor_is_developer and getattr(getattr(user, 'profile', None), 'is_developer', False):
                skipped.append(f"{user}: only a developer can delete a developer's account.")
                continue
            try:
                action = archiving.archive_account(
                    user, by=request.user, reason=f'Deleted in the advanced admin by {request.user}')
            except ValueError as exc:
                skipped.append(f'{user}: {exc}')
                continue
            if action is not None:
                actions.append(action)
        for line in skipped:
            messages.warning(request, line)
        return actions

    def delete_model(self, request, obj):
        """Archive the account instead of deleting it."""
        self._archived_now = self._archive(request, [obj])

    def delete_queryset(self, request, queryset):
        """Archive every selected account instead of deleting it."""
        self._archived_now = self._archive(request, list(queryset))

    def response_delete(self, request, obj_display, obj_id):
        """Report the archive with an undo link and return to the changelist."""
        actions = getattr(self, '_archived_now', [])
        if actions:
            undo.success_with_undo(
                request, f'“{obj_display}” was archived. It will be permanently deleted in '
                         f'{UserSettings.ADMIN_GRACE_DAYS} days unless restored.', actions[0])
        return HttpResponseRedirect(reverse(
            f'admin:{self.opts.app_label}_{self.opts.model_name}_changelist'))

    def get_actions(self, request):
        """Replace Django's ``delete_selected`` with :meth:`archive_selected`."""
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            actions['delete_selected'] = (
                ArchiveOnDeleteMixin.archive_selected, 'delete_selected',
                f'Delete selected accounts (archived {UserSettings.ADMIN_GRACE_DAYS} days, then deleted)')
        return actions

    @staticmethod
    def archive_selected(modeladmin, request, queryset):
        """Bulk action: archive the selected accounts once confirmed.

        Before confirmation it shows Django's usual confirmation page.
        """
        if request.POST.get('post'):
            actions = modeladmin._archive(request, list(queryset))
            if actions:
                messages.success(request, format_html(
                    'Archived {} account(s). They are hidden from the LMS and will be permanently '
                    'deleted in {} days — restore them with the “Restore archived accounts” '
                    'action or on <a href="{}">Closed accounts</a>.',
                    len(actions), UserSettings.ADMIN_GRACE_DAYS, reverse('accounts:closed-accounts')))
            return None
        return django_delete_selected(modeladmin, request, queryset)

    @admin.action(description='Restore archived accounts')
    def restore_selected(self, request, queryset):
        """Bulk action: restore the selected archived accounts."""
        restored = 0
        for obj in queryset:
            if archiving.restore_account(self._account_of(obj), by=request.user,
                                         reason='Restored in the advanced admin'):
                restored += 1
        messages.success(request, f'Restored {restored} account(s).')

    @admin.display(description='Archived', boolean=True)
    def archived_flag(self, obj):
        """Changelist column: whether the account is archived."""
        settings_row = getattr(self._account_of(obj), 'account_settings', None)
        return bool(settings_row and settings_row.is_closing)


User = get_user_model()
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:  # pragma: no cover
    pass


@admin.register(User)
class AccountUserAdmin(ArchiveOnDeleteMixin, DjangoUserAdmin):
    """``auth.User`` admin that archives on delete and protects developer logins."""
    list_display = (*DjangoUserAdmin.list_display, 'is_active', 'archived_flag')
    list_filter = (ArchivedFilter, *DjangoUserAdmin.list_filter)
    actions = ('restore_selected',)

    def _developer_locked(self, request, obj):
        """True when ``obj`` is a developer's account and the requester is not a developer.

        A developer's login is for developers to change (:mod:`apps.accounts.developers`);
        otherwise a superuser could reset its e-mail or password and take it over.
        """
        from core.roles import is_developer
        person = getattr(obj, 'profile', None) if obj is not None else None
        return bool(person is not None and person.is_developer and not is_developer(request.user))

    def has_change_permission(self, request, obj=None):
        """Refuse changes to a developer's account by a non-developer."""
        if self._developer_locked(request, obj):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        """Refuse deleting a developer's account by a non-developer."""
        if self._developer_locked(request, obj):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Person)
class PersonAdmin(ArchiveOnDeleteMixin, ImportExportModelAdmin):
    """People, with Excel/CSV import & export (see :mod:`apps.accounts.resources`)."""

    resource_classes = [PersonResource]
    list_display = ('__str__', 'user_email', 'user_type', 'registered', 'profile_status',
                    'archived_flag')
    list_filter = (ArchivedFilter, 'user_type', 'registered', 'profile_status', 'gender')
    actions = ('restore_selected',)

    def get_queryset(self, request):
        """Every person, archived ones included — the advanced admin is where they stay visible."""
        return Person.all_objects.select_related('user', 'user__account_settings')
    search_fields = ('first_name', 'last_name', 'user__email', 'user__username', 'phone', 'ms_upn')
    autocomplete_fields = ('user',)
    list_select_related = ('user',)
    inlines = [PersonContactInline, PersonStudyProfileInline, PersonConsentInline]

    @admin.display(description='Email', ordering='user__email')
    def user_email(self, obj):
        """Changelist column: the account's e-mail."""
        return obj.user.email if obj.user_id else ''

    def get_readonly_fields(self, request, obj=None):
        """Lock ``is_developer`` (and a developer's ``user_type``) for non-developers.

        Only a developer grants developer access or changes a developer's role
        (:mod:`apps.accounts.developers`, :mod:`apps.accounts.roles_admin`).
        """
        fields = list(super().get_readonly_fields(request, obj))
        from core.roles import is_developer
        if not is_developer(request.user):
            fields.append('is_developer')
            if obj is not None and obj.is_developer:
                fields.append('user_type')
        return fields

    def has_delete_permission(self, request, obj=None):
        """Refuse deleting a developer's person row by a non-developer."""
        from core.roles import is_developer
        if obj is not None and obj.is_developer and not is_developer(request.user):
            return False
        return super().has_delete_permission(request, obj)


@admin.register(UndoAction)
class UndoActionAdmin(admin.ModelAdmin):
    """Read-only: every undoable action and, once taken back, who/when/why."""
    list_display = ('created_at', 'kind', 'summary', 'actor', 'undone_at', 'undone_by')
    list_filter = ('kind', 'created_at', 'undone_at')
    search_fields = ('summary', 'undo_reason', 'object_ref')
    readonly_fields = [f.name for f in UndoAction._meta.fields]

    def has_add_permission(self, request):
        """Undo actions are recorded by the code, never added by hand."""
        return False

    def has_change_permission(self, request, obj=None):
        """Undo actions are read-only."""
        return False


@admin.register(ActivityLog)
class ActivityLogAdmin(DeveloperOnlyAdminMixin, admin.ModelAdmin):
    """The audit log — developers only (:class:`~apps.accounts.developers.DeveloperOnlyAdminMixin`)."""
    list_display = ('action', 'actor', 'target_user', 'ip_address', 'timestamp')
    list_filter = ('action', 'timestamp')
    search_fields = ('description',)
    date_hierarchy = 'timestamp'
    readonly_fields = ('timestamp',)


@admin.register(OneOnOneRequest)
class OneOnOneRequestAdmin(admin.ModelAdmin):
    """Requests for one-on-one coaching made at registration (any institution)."""
    list_display = ('person', 'institution', 'programme', 'start_date', 'end_date',
                    'status', 'educator', 'created_at')
    list_filter = ('status', 'created_at', 'start_date', 'educator')
    search_fields = ('person__first_name', 'person__last_name', 'person__user__email',
                     'institution', 'programme', 'modules')
    autocomplete_fields = ('person', 'educator')
    readonly_fields = ('created_at', 'updated_at', 'enrolled_at', 'handled_by')
    date_hierarchy = 'created_at'
    actions = ('mark_in_progress', 'mark_enrolled', 'mark_declined')

    def save_model(self, request, obj, form, change):
        """Stamp ``handled_by`` and, on first enrolment, ``enrolled_at``."""
        from django.utils import timezone
        if obj.status == OneOnOneRequest.STATUS_ENROLLED and not obj.enrolled_at:
            obj.enrolled_at = timezone.now()
        obj.handled_by = request.user
        super().save_model(request, obj, form, change)

    def _mark(self, request, queryset, status):
        """Set ``status`` on every selected request and report how many changed."""
        from django.utils import timezone
        fields = {'status': status, 'handled_by': request.user}
        if status == OneOnOneRequest.STATUS_ENROLLED:
            queryset.filter(enrolled_at__isnull=True).update(enrolled_at=timezone.now())
        n = queryset.update(**fields)
        self.message_user(request, f'{n} request(s) marked {dict(OneOnOneRequest.STATUS_CHOICES)[status].lower()}.')

    @admin.action(description='Mark in progress')
    def mark_in_progress(self, request, queryset):
        """Bulk action: mark the selected requests in progress."""
        self._mark(request, queryset, OneOnOneRequest.STATUS_IN_PROGRESS)

    @admin.action(description='Mark enrolled')
    def mark_enrolled(self, request, queryset):
        """Bulk action: mark the selected requests enrolled."""
        self._mark(request, queryset, OneOnOneRequest.STATUS_ENROLLED)

    @admin.action(description='Mark declined')
    def mark_declined(self, request, queryset):
        """Bulk action: mark the selected requests declined."""
        self._mark(request, queryset, OneOnOneRequest.STATUS_DECLINED)
