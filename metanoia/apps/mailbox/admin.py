from django.contrib import admin

from .models import MailAccount, MailDraft, MailFolder, MailItem, MailServer


@admin.register(MailServer)
class MailServerAdmin(admin.ModelAdmin):
    list_display = ('domain', 'imap_host', 'smtp_host', 'updated_at')


@admin.register(MailAccount)
class MailAccountAdmin(admin.ModelAdmin):
    list_display = ('email_address', 'user', 'is_active', 'last_sync_at', 'last_error')
    search_fields = ('email_address', 'user__email', 'user__first_name', 'user__last_name')
    list_filter = ('is_active',)
    exclude = ('password_encrypted',)


@admin.register(MailFolder)
class MailFolderAdmin(admin.ModelAdmin):
    list_display = ('name', 'account', 'role', 'unread', 'total', 'synced_at')
    list_filter = ('role',)


@admin.register(MailItem)
class MailItemAdmin(admin.ModelAdmin):
    list_display = ('subject', 'from_address', 'folder', 'date', 'is_read')
    search_fields = ('subject', 'from_address')
    raw_id_fields = ('account', 'folder')
    exclude = ('body_html',)


@admin.register(MailDraft)
class MailDraftAdmin(admin.ModelAdmin):
    list_display = ('subject', 'account', 'mode', 'updated_at')
