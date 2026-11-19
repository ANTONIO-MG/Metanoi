"""Django admin for the error log (read-mostly: events are written by code)."""

from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html

from apps.accounts.developers import DeveloperOnlyAdminMixin

from .catalog import get as spec_for
from .models import ErrorEvent


@admin.register(ErrorEvent)
class ErrorEventAdmin(DeveloperOnlyAdminMixin, admin.ModelAdmin):
    """Admin for :class:`~apps.diagnostics.models.ErrorEvent`, visible to developers only.

    Every technical field is read-only; staff triage an event by changing its
    ``status`` and ``note`` or through the bulk actions. The ``remedy`` panel shows
    the catalogue entry (:mod:`apps.diagnostics.catalog`) for the event's code.
    """
    list_display = ('code', 'severity', 'short_title', 'count', 'status',
                    'module', 'last_seen')
    list_filter = ('severity', 'status', 'code', 'last_seen')
    search_fields = ('code', 'message', 'module', 'function', 'path',
                     'reference', 'exception_type')
    date_hierarchy = 'last_seen'
    readonly_fields = ('code', 'fingerprint', 'severity', 'message', 'exception_type',
                       'traceback', 'context', 'module', 'function', 'line', 'path',
                       'method', 'view_name', 'user', 'user_label', 'ip_address',
                       'count', 'first_seen', 'last_seen', 'reference', 'remedy')
    fields = ('code', 'remedy', 'severity', 'status', 'note', 'count',
              'first_seen', 'last_seen', 'reference',
              'message', 'exception_type', 'module', 'function', 'line',
              'path', 'method', 'view_name', 'user_label', 'ip_address',
              'context', 'traceback', 'fingerprint')
    actions = ['mark_resolved', 'mark_ignored', 'reopen']

    @admin.display(description='Title')
    def short_title(self, obj):
        """Return the catalogue title for the event's error code."""
        return spec_for(obj.code).title

    @admin.display(description='What the dictionary says')
    def remedy(self, obj):
        """Render the catalogue's title, cause, developer fix and user action for the event's code."""
        spec = spec_for(obj.code)
        return format_html(
            '<strong>{}</strong><br><br>'
            '<em>Why:</em> {}<br><br>'
            '<em>Developer fix:</em> {}<br><br>'
            '<em>User action:</em> {}',
            spec.title, spec.why, spec.fix, spec.action)

    def has_add_permission(self, request):
        """Refuse manual creation: events are recorded by the platform, never typed in."""
        return False

    @admin.action(description='Mark selected as resolved')
    def mark_resolved(self, request, queryset):
        """Bulk action: mark the selected events resolved, stamping who and when."""
        updated = queryset.update(status=ErrorEvent.STATUS_RESOLVED,
                                  resolved_at=timezone.now(), resolved_by=request.user)
        self.message_user(request, f'{updated} error(s) marked resolved.')

    @admin.action(description='Mark selected as ignored')
    def mark_ignored(self, request, queryset):
        """Bulk action: mark the selected events ignored."""
        updated = queryset.update(status=ErrorEvent.STATUS_IGNORED)
        self.message_user(request, f'{updated} error(s) ignored.')

    @admin.action(description='Reopen selected')
    def reopen(self, request, queryset):
        """Bulk action: reopen the selected events and clear their resolution time."""
        updated = queryset.update(status=ErrorEvent.STATUS_OPEN, resolved_at=None)
        self.message_user(request, f'{updated} error(s) reopened.')
