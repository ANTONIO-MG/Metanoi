"""Admin for AI-generated reports and insights."""

from django.contrib import admin

from . import models


@admin.register(models.AiReport)
class AiReportAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'module', 'student', 'used_llm', 'status', 'created_by', 'created_at')
    list_filter = ('kind', 'status', 'used_llm')
    search_fields = ('title', 'content', 'prompt')
    date_hierarchy = 'created_at'
    readonly_fields = ('public_id', 'data', 'linked_ref', 'created_at', 'updated_at')
    autocomplete_fields = ('module', 'student', 'created_by')


@admin.register(models.AiInsight)
class AiInsightAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind', 'severity', 'status', 'module', 'created_at')
    list_filter = ('severity', 'status', 'kind')
    search_fields = ('title', 'body', 'dedupe_key')
    date_hierarchy = 'created_at'
    readonly_fields = ('public_id', 'dedupe_key', 'data', 'created_at', 'updated_at')
    autocomplete_fields = ('module', 'student', 'owner')
