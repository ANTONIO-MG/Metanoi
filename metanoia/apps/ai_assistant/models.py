"""AI-generated documents (:class:`AiReport` — Teams class recaps, session
follow-up packs) and proactive observations (:class:`AiInsight`).

The admin AI chat and the retired CrewAI widget (page cache, chat sessions,
uploads, proposed actions) were removed; their tables are dropped by
migration 0008.

These models back the AI Teaching Assistant (local CrewAI + Ollama) in the My Learning
Hub. Everything works offline: the database is always read locally; the LLM narrative is
added when Ollama is configured and falls back to a deterministic template otherwise.
"""

import uuid

from django.conf import settings
from django.db import models


class AiReport(models.Model):
    """A document the AI produced for review — a report card, a generated
    assignment/rubric, a parent letter, meeting minutes, an attendance analysis,
    an 'ask-the-data' answer or a secretary briefing. Kept as Markdown in
    ``content`` (+ structured ``data``) so it renders in the theme and can be
    finalised, downloaded or turned into real records.

    ``KIND_MEETING_RECAP`` is a Teams class recap built from the meeting transcript.
    ``student`` is set for per-student documents (report cards, parent letters) and
    names the module student. ``linked_ref`` is a loose link to a record this report
    produced (e.g. ``assessments.Assessment:42``).
    """

    KIND_MINUTES = 'meeting_minutes'
    KIND_ASSIGNMENT = 'assignment'
    KIND_REPORT_CARD = 'report_card'
    KIND_ATTENDANCE = 'attendance_analysis'
    KIND_PARENT = 'parent_letter'
    KIND_NL = 'nl_answer'
    KIND_BRIEFING = 'secretary_briefing'
    KIND_MEETING_RECAP = 'meeting_recap'
    KIND_CHOICES = [
        (KIND_MINUTES, 'Meeting minutes'),
        (KIND_ASSIGNMENT, 'Generated assignment'),
        (KIND_REPORT_CARD, 'Report card'),
        (KIND_ATTENDANCE, 'Attendance analysis'),
        (KIND_PARENT, 'Parent message'),
        (KIND_NL, 'Ask-the-data answer'),
        (KIND_BRIEFING, 'Secretary briefing'),
        (KIND_MEETING_RECAP, 'Class recap'),
    ]

    STATUS_DRAFT = 'draft'
    STATUS_FINAL = 'final'
    STATUS_CHOICES = [(STATUS_DRAFT, 'Draft'), (STATUS_FINAL, 'Final')]

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, db_index=True)
    title = models.CharField(max_length=255)
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='ai_reports')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='ai_reports_about')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='ai_reports_created')

    prompt = models.TextField(blank=True, help_text='The instruction / inputs used to generate this.')
    content = models.TextField(blank=True, help_text='The generated document (Markdown).')
    data = models.JSONField(default=dict, blank=True)
    used_llm = models.BooleanField(default=False)
    linked_ref = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Newest first; indexed by kind and status for the review lists."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['kind', 'status'], name='ai_report_kind_status_idx')]

    def __str__(self):
        """Return ``[kind] title`` for the admin."""
        return f'[{self.get_kind_display()}] {self.title[:50]}'


class AiInsight(models.Model):
    """A proactive observation the *AI Secretary* surfaced from scanning the
    database — nobody asked, the AI noticed. Shown on dashboards; can be
    acknowledged, dismissed, or turned into an action.

    ``kind`` names the observation (e.g. ``overdue_assessment``,
    ``missing_attendance``). ``owner`` is the educator/admin it is for — null means
    institution-wide (any admin/staff). ``dedupe_key`` is stable so re-running the scan
    updates an insight rather than duplicating it.
    """

    SEVERITY_INFO = 'info'
    SEVERITY_WARNING = 'warning'
    SEVERITY_CRITICAL = 'critical'
    SEVERITY_CHOICES = [(SEVERITY_INFO, 'Info'), (SEVERITY_WARNING, 'Warning'), (SEVERITY_CRITICAL, 'Critical')]

    STATUS_NEW = 'new'
    STATUS_ACK = 'acknowledged'
    STATUS_DISMISSED = 'dismissed'
    STATUS_ACTIONED = 'actioned'
    STATUS_CHOICES = [
        (STATUS_NEW, 'New'), (STATUS_ACK, 'Acknowledged'),
        (STATUS_DISMISSED, 'Dismissed'), (STATUS_ACTIONED, 'Actioned'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    kind = models.CharField(max_length=40, db_index=True)
    severity = models.CharField(max_length=8, choices=SEVERITY_CHOICES, default=SEVERITY_WARNING, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                              related_name='ai_insights')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='ai_insights')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='ai_insights_about')
    data = models.JSONField(default=dict, blank=True)
    dedupe_key = models.CharField(max_length=200, db_index=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Newest first; indexed by status and severity for the dashboard."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'severity'], name='ai_insight_status_sev_idx')]

    def __str__(self):
        """Return ``[severity] title`` for the admin."""
        return f'[{self.severity}] {self.title[:60]}'
