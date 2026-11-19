"""The content upload queue: document in → draft → human review → published.

A document plus its ``.json`` sidecar (and optional ``.md`` text) — the
``metanoia-upload/1`` format in ``docs/upload_format/`` — is placed from the
sidecar, drafted (template documents converted exactly; others by Claude into
``metanoia-pack`` JSON) and held here with its validation problems.

Nothing reaches a student without two human clicks: approve (imports as
drafts), then publish.

``UPLOAD_EXTENSIONS`` covers the document itself, its ``.md`` text and its
``.json`` sidecar. It is wider than the general attachment list on purpose — and
only reachable by admins. ``validate_content_upload`` adds a 40 MB size cap.
"""

import os
import uuid

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone

from core import validators as v
from core.storage import UnguessableUploadTo, files_storage

from .models import TimeStampedModel

UPLOAD_EXTENSIONS = sorted(set(v.DOCUMENT_EXTENSIONS + v.IMAGE_EXTENSIONS + ['md', 'json']))
validate_content_upload = [FileExtensionValidator(UPLOAD_EXTENSIONS), v.MaxFileSize(40)]


class ContentUpload(TimeStampedModel):
    """One uploaded document and everything the pipeline did with it.

    ``OPEN_STATUSES`` are the statuses that still need a human. ``result`` records
    what approving created: ``{'lessons': [...], 'assessments': [...], 'materials':
    [...]}``.
    """

    TYPE_STUDY_GUIDE = 'study_guide'
    TYPE_BLUEPRINT = 'blueprint'
    TYPE_QUESTIONS = 'questions'
    TYPE_MOCK_EXAM = 'mock_exam'
    TYPE_PAST_PAPER = 'past_paper'
    TYPE_ANSWERS = 'answers'
    TYPE_PLANNER = 'planner'
    TYPE_RECORDING = 'recording'
    TYPE_REFERENCE = 'reference'
    TYPE_CHOICES = [
        (TYPE_STUDY_GUIDE, 'Study guide'),
        (TYPE_BLUEPRINT, 'Strategic blueprint'),
        (TYPE_QUESTIONS, 'Questions & answers (quiz)'),
        (TYPE_MOCK_EXAM, 'Mock exam & answer sheet'),
        (TYPE_PAST_PAPER, 'Past paper'),
        (TYPE_ANSWERS, 'Answer sheet / solution only'),
        (TYPE_PLANNER, 'Planner / schedule (calendar)'),
        (TYPE_RECORDING, 'Live session / recording'),
        (TYPE_REFERENCE, 'Coach reference (staff only)'),
    ]

    STATUS_UPLOADED = 'uploaded'
    STATUS_DRAFTED = 'drafted'
    STATUS_NEEDS_FIX = 'needs_fix'
    STATUS_APPROVED = 'approved'
    STATUS_PUBLISHED = 'published'
    STATUS_REJECTED = 'rejected'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_UPLOADED, 'Uploaded — not drafted yet'),
        (STATUS_DRAFTED, 'Draft ready for review'),
        (STATUS_NEEDS_FIX, 'Draft has problems to fix'),
        (STATUS_APPROVED, 'Approved — imported as draft'),
        (STATUS_PUBLISHED, 'Published to students'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_FAILED, 'Failed'),
    ]
    OPEN_STATUSES = (STATUS_UPLOADED, STATUS_DRAFTED, STATUS_NEEDS_FIX, STATUS_APPROVED)
    STATUS_TONES = {
        STATUS_UPLOADED: 'secondary', STATUS_DRAFTED: 'info', STATUS_NEEDS_FIX: 'warning',
        STATUS_APPROVED: 'primary', STATUS_PUBLISHED: 'success', STATUS_REJECTED: 'dark',
        STATUS_FAILED: 'danger',
    }

    METHOD_TEMPLATE = 'template'
    METHOD_AI = 'ai'
    METHOD_FILE = 'file'
    METHOD_CHOICES = [
        (METHOD_TEMPLATE, 'Converted from the standard template'),
        (METHOD_AI, 'Drafted by the AI assistant'),
        (METHOD_FILE, 'File only (no lesson or quiz)'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    title = models.CharField(max_length=200)
    doc_type = models.CharField(max_length=12, choices=TYPE_CHOICES, db_index=True)

    programme_module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE,
                                         null=True, blank=True, related_name='content_uploads')
    cohort = models.ForeignKey('learning.Cohort', on_delete=models.SET_NULL,
                               null=True, blank=True, related_name='content_uploads')
    phase_kind = models.CharField(max_length=8, default='test')
    phase_sequence = models.PositiveSmallIntegerField(default=1)
    week_number = models.PositiveSmallIntegerField(null=True, blank=True)
    topic_code = models.CharField(max_length=20, blank=True)
    topic_title = models.CharField(max_length=250, blank=True)
    audience = models.CharField(max_length=8, default='students')
    used_on = models.DateField(null=True, blank=True)

    metadata = models.JSONField(default=dict, blank=True,
                                help_text='The sidecar, normalised to metanoia-upload/1.')
    source_text = models.TextField(blank=True, help_text='The .md text, or text extracted from the document.')

    draft_method = models.CharField(max_length=8, choices=METHOD_CHOICES, blank=True)
    pack = models.JSONField(default=dict, blank=True, help_text='The metanoia-pack draft an admin reviews.')
    problems = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_UPLOADED,
                              db_index=True)
    message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']
        verbose_name = 'Content upload'

    def __str__(self):
        """``<document type> · <title>``."""
        return f'{self.get_doc_type_display()} · {self.title}'

    @property
    def tone(self):
        """The Bootstrap tone of the upload's status badge."""
        return self.STATUS_TONES.get(self.status, 'secondary')

    @property
    def is_open(self):
        """True while the upload still needs a human."""
        return self.status in self.OPEN_STATUSES

    @property
    def source_ref(self):
        """The key every row this upload creates is filed under, so approving
        twice corrects rather than duplicates."""
        return f'upload/{self.public_id}'

    @property
    def placement_label(self):
        """Where the upload is placed — ``MILPARK · PGDA · FREP · P26F · Test 2 · Week
        3`` — or "Not placed".
        """
        if not self.programme_module_id:
            return 'Not placed'
        pm = self.programme_module
        bits = [pm.programme.institution.code, pm.programme.code, pm.code]
        if self.cohort_id:
            bits.append(self.cohort.code)
        noun = {'test': 'Test', 'exam': 'Exam'}.get(self.phase_kind, self.phase_kind.title())
        bits.append(f'{noun} {self.phase_sequence}')
        if self.week_number:
            bits.append(f'Week {self.week_number}')
        return ' · '.join(bits)

    def mark_reviewed(self, user, status):
        """Record the review: the new ``status``, who and when (and the publish time).
        Does not save.
        """
        self.status = status
        self.reviewed_by = user
        self.reviewed_at = timezone.now()
        if status == self.STATUS_PUBLISHED:
            self.published_at = self.reviewed_at


class ContentUploadFile(TimeStampedModel):
    """One file of an upload — the document, its ``.md`` text, its sidecar, a
    separate answer sheet."""

    ROLE_DOCUMENT = 'document'
    ROLE_SOLUTION = 'solution'
    ROLE_TEXT = 'text'
    ROLE_SIDECAR = 'sidecar'
    ROLE_CHOICES = [
        (ROLE_DOCUMENT, 'Document'),
        (ROLE_SOLUTION, 'Answer sheet / solution'),
        (ROLE_TEXT, 'Text (.md)'),
        (ROLE_SIDECAR, 'Sidecar (.json)'),
    ]

    upload = models.ForeignKey(ContentUpload, on_delete=models.CASCADE, related_name='files')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_DOCUMENT)
    file = models.FileField(upload_to=UnguessableUploadTo('uploads/content', dated=True), storage=files_storage,
                            validators=validate_content_upload)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)

    class Meta:
        """Ordered by upload, role, then id."""
        ordering = ['upload', 'role', 'id']

    def __str__(self):
        """The file's original name."""
        return self.original_name

    @property
    def extension(self):
        """The original file's extension, lower-cased, without the dot."""
        return os.path.splitext(self.original_name)[1].lower().lstrip('.')
