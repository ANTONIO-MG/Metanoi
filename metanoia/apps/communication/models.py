"""Data models for the communication app.

Three feature areas:

* **Chat** — :class:`ChatGroup` (direct / module / custom) with
  :class:`ChatMembership`, :class:`Message`, :class:`MessageAttachment`
  (images / videos / files), edit history (``is_edited`` / ``edited_at``) and
  @mentions via :class:`MessageMention`. Module-scoped groups have
  their membership kept in sync with enrolment by :mod:`apps.communication.signals`.

* **Meetings** — :class:`MeetingRoom`: a class session or a group's video call
  with a shareable join link and scheduling fields. The call itself is a
  Microsoft Teams meeting (:mod:`apps.msteams`); :class:`MeetingParticipant`
  records the invite list / RSVPs.

* **Notifications** — :class:`Notification` (one per recipient) and
  :class:`Announcement` (a staff/admin broadcast targeting everyone, a user
  category, modules or individuals — optionally also e-mailed).

Uploads exchanged or generated here use ``core.storage.files_storage`` (the file
server). ``_FORBIDDEN_IN_FOLDER`` lists the characters OneDrive / SharePoint refuse in
an item name (see :func:`_safe_folder_name`).
"""

import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core import validators as v

from core.storage import files_storage


def _new_token():
    """Short URL-safe token used for meeting-room slugs / join links."""
    return uuid.uuid4().hex[:12]


_FORBIDDEN_IN_FOLDER = '"*:<>?/\\|#%'


def _safe_folder_name(name, limit=120):
    """A folder/file name OneDrive will accept, trimmed to ``limit``.

    OneDrive rejects a handful of characters outright and silently mangles
    leading/trailing dots and spaces, so a session titled "Q&A: IAS 12" has to
    be cleaned before it can become a folder.
    """
    cleaned = ''.join(' ' if ch in _FORBIDDEN_IN_FOLDER else ch for ch in str(name or ''))
    cleaned = ' '.join(cleaned.split()).strip(' .')
    return (cleaned[:limit].strip(' .')) or 'Untitled'


class ChatGroup(models.Model):
    """A conversation: a 1:1 direct chat, a module group or a custom group.

    ``KIND_SUPPORT`` is "Message Metanoia": one private thread per community member,
    shared by every admin and staff member so whoever is free can answer (see
    :func:`apps.communication.community.support_chat_for`). An auto-managed group is
    linked from its ProgrammeModule (``ProgrammeModule.chat_group``), so its membership
    tracks enrolment. ``coaching_for`` marks a one-on-one student's private space: them,
    the educators of their modules and the Metanoia team
    (:func:`apps.shop.one_on_one.ensure_coaching_group`). Video calls are for groups
    only; see :mod:`apps.communication.group_calls`.
    """

    KIND_DIRECT = 'direct'
    KIND_SUBJECT = 'module'
    KIND_CUSTOM = 'custom'
    KIND_SUPPORT = 'support'
    KIND_CHOICES = [
        (KIND_DIRECT, 'Direct (1:1)'),
        (KIND_SUBJECT, 'Module group'),
        (KIND_CUSTOM, 'Custom group'),
        (KIND_SUPPORT, 'Message Metanoia'),
    ]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_CUSTOM)
    name = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    coaching_for = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name='coaching_group')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Most recently active first."""
        ordering = ['-updated_at']

    def __str__(self):
        """Return :attr:`display_name`."""
        return self.display_name

    @property
    def display_name(self):
        """The group's name, or one derived from its module offering or members."""
        if self.name:
            return self.name
        offering = getattr(self, 'programme_module', None)
        if offering is not None:
            return f'Module · {offering}'
        if self.kind == self.KIND_DIRECT:
            from core.utils import display_name
            people = ', '.join(display_name(membership.user) for membership in self.memberships.all()[:2])
            return people or 'Direct chat'
        return f'Chat #{self.pk}'

    @property
    def is_auto_managed(self):
        """True for module groups whose membership tracks enrolment."""
        return self.kind == self.KIND_SUBJECT

    def add_member(self, user, *, role='member', auto=False):
        """Add ``user`` (or upgrade an existing manual membership to ``auto``); return it.
        """
        membership, created = self.memberships.get_or_create(
            user=user, defaults={'role': role, 'is_auto': auto},
        )
        if not created and auto and not membership.is_auto:
            membership.is_auto = True
            membership.save(update_fields=['is_auto'])
        return membership

    @property
    def can_video_call(self):
        """True when this group may start a video call (see :func:`group_calls.can_call`).
        """
        from .group_calls import can_call
        return can_call(self)

    def live_call(self):
        """The Teams call running in this group right now, or None."""
        from .group_calls import live_call
        return live_call(self)

    @property
    def linked_study_group(self):
        """The study group this conversation belongs to, if any."""
        try:
            return self.study_group
        except Exception:
            return None


class ChatMembership(models.Model):
    """A user's membership in a :class:`ChatGroup`.

    ``is_auto`` is True when the user was added automatically because they are enrolled
    in the module.
    """

    ROLE_CHOICES = [('owner', 'Owner'), ('admin', 'Admin'), ('member', 'Member')]

    group = models.ForeignKey(ChatGroup, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='member')

    is_auto = models.BooleanField(default=False)
    muted = models.BooleanField(default=False)
    last_read_at = models.DateTimeField(null=True, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One membership per user per group."""
        unique_together = ('group', 'user')
        ordering = ['group', 'user']

    def __str__(self):
        """Return ``user in group``."""
        return f'{self.user} in {self.group}'

    @property
    def unread_count(self):
        """Unread messages in this one conversation.

        One query per membership — fine for a single conversation, wrong for a
        list of them. Use :meth:`unread_total_for` when you want the badge.
        """
        qs = self.group.messages.exclude(sender=self.user)
        if self.last_read_at:
            qs = qs.filter(created_at__gt=self.last_read_at)
        return qs.count()

    @classmethod
    def unread_total_for(cls, user):
        """Total unread across every conversation ``user`` is in — in ONE query.

        The obvious loop (``sum(m.unread_count for m in memberships)``) issues a
        COUNT per membership, and this number is rendered in the navbar on every
        single page: it was the platform's most-repeated query. Each membership
        has its own ``last_read_at`` watermark, so the whole thing collapses into
        one aggregate with a per-row condition rather than N round trips.
        """
        from django.db.models import Count, F, Q

        row = (cls.objects
               .filter(user=user)
               .aggregate(total=Count(
                   'group__messages',
                   filter=(~Q(group__messages__sender=user)
                           & (Q(last_read_at__isnull=True)
                              | Q(group__messages__created_at__gt=F('last_read_at')))))))
        return row['total'] or 0


class Message(models.Model):
    """A chat message. Supports text + attachments, replies, editing and @mentions."""

    group = models.ForeignKey(ChatGroup, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='sent_messages')
    body = models.TextField(blank=True)
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')

    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through='MessageMention', related_name='mentioned_in_messages', blank=True,
    )

    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        """Chronological; indexed by group and time."""
        ordering = ['created_at']
        indexes = [models.Index(fields=['group', 'created_at'])]

    def __str__(self):
        """Return ``sender: body`` (truncated)."""
        return f'{self.sender}: {self.body[:40]}'

    def mark_edited(self):
        """Flag the message as edited now (the caller saves it)."""
        self.is_edited = True
        self.edited_at = timezone.now()


class MessageAttachment(models.Model):
    """A file attached to a :class:`Message` — an image, a video or a generic file.

    ``IMAGE_EXTS`` / ``VIDEO_EXTS`` are the extensions that auto-classify an upload as
    an image or a video.
    """

    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_CHOICES = [(KIND_IMAGE, 'Image'), (KIND_VIDEO, 'Video'), (KIND_FILE, 'File')]

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='chat/%Y/%m/', storage=files_storage,
                            validators=v.validate_attachment)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_FILE)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
    VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v', '.avi', '.mkv', '.ogg'}

    def __str__(self):
        """Return the original file name."""
        return self.original_name or self.file.name

    @classmethod
    def classify(cls, filename):
        """The attachment kind (image / video / file) for ``filename``'s extension."""
        import os
        ext = os.path.splitext(filename or '')[1].lower()
        if ext in cls.IMAGE_EXTS:
            return cls.KIND_IMAGE
        if ext in cls.VIDEO_EXTS:
            return cls.KIND_VIDEO
        return cls.KIND_FILE

    def save(self, *args, **kwargs):
        """Fill in the original name and size from the file, then save."""
        if self.file and not self.original_name:
            self.original_name = getattr(self.file, 'name', '')
        if self.file and not self.size:
            try:
                self.size = self.file.size
            except (OSError, ValueError):
                self.size = 0
        if (not self.kind or self.kind == self.KIND_FILE) and self.original_name:
            self.kind = self.classify(self.original_name)
        super().save(*args, **kwargs)


class MessageMention(models.Model):
    """Through-table linking a :class:`Message` to a tagged (``@``) user.

    Creating one also creates a :class:`Notification` for that user (see
    :mod:`apps.communication.signals`), so they're alerted they were tagged.
    """

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='mention_links')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='message_mentions')
    seen = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One mention row per user per message."""
        unique_together = ('message', 'user')

    def __str__(self):
        """Return ``@user in message #id``."""
        return f'@{self.user} in message #{self.message_id}'


class ConnectionRequest(models.Model):
    """A request from one user to open 1:1 direct messaging with another.

    Used to gate **student ↔ student** direct chats: two students can only
    exchange direct messages once a request between them is ``accepted``. Chats
    that involve staff/admin/educator/parent — and every course/module/custom
    group chat — are not gated (see
    :func:`apps.communication.services.students_need_connection`).
    """

    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_DECLINED = 'declined'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_ACCEPTED, 'Accepted'),
        (STATUS_DECLINED, 'Declined'),
    ]

    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='connection_requests_sent')
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='connection_requests_received')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """At most one request row per ordered (from, to) pair; newest first."""
        unique_together = ('from_user', 'to_user')
        ordering = ['-created_at']
        indexes = [models.Index(fields=['to_user', 'status'])]

    def __str__(self):
        """Return ``from → to (status)``."""
        return f'{self.from_user} → {self.to_user} ({self.status})'

    def accept(self):
        """Mark the request accepted now and save."""
        self.status = self.STATUS_ACCEPTED
        self.responded_at = timezone.now()
        self.save(update_fields=['status', 'responded_at'])

    def decline(self):
        """Mark the request declined now and save."""
        self.status = self.STATUS_DECLINED
        self.responded_at = timezone.now()
        self.save(update_fields=['status', 'responded_at'])


class MeetingRoom(models.Model):
    """A live class session / video call.

    The live engine is **Microsoft Teams** (``provider='teams'``), created from
    the LMS via Microsoft Graph — see :mod:`apps.msteams` and
    docs/TEAMS_INTEGRATION.md. Students join anonymously via ``teams_join_url``.
    Teams is the only engine; rows with ``provider='jitsi'`` are history from
    the retired in-app video room and are kept so old sessions still display.

    :meth:`get_join_url` is the in-app launch page (stable link used by the
    calendar, lessons and notifications); the page then sends the attendee to
    :meth:`live_url` (the Teams meeting).

    ``PROVIDER_LEGACY`` is historical only — sessions from the retired in-app video
    room; it is never set.

    Who the session is for (``audience``): the audience decides two different things at
    once — who is *notified* when the session is created, and who may *see it on the
    calendar*. They are the same set on purpose: a student seeing a session they were
    never told about is the bug this field exists to prevent. The audiences are
    ``everyone`` (the whole platform), ``institution`` (everyone registered with one
    institution), ``programme`` (everyone on one programme), ``cohort`` (one intake of
    one programme), ``module`` (everyone studying one module offering) and ``private``
    (named invitees only, including 1:1). ``DERIVED_AUDIENCES`` are those whose
    membership is derived from enrolment rather than from the invite list; both the
    notifier and the calendar filter use it. ``KIND_*`` says what kind of contact time
    it is (``consult`` is one-on-one).

    ``group`` is an optional link to a chat group; ``module`` is the direct link to the
    module offering the session belongs to (auto-assigned on create) and stays the
    primary link. The placement fields narrow or widen it: a programme-wide briefing has
    no module, a module class has no cohort, and a one-on-one has neither. ``week`` and
    ``calendar_event`` are what put the session on the module's schedule next to the
    right topic or test.

    Live engine: Microsoft Teams via Graph. ``external_event_id`` is the Outlook/Teams
    calendar occurrence the session was imported from, when it was scheduled in Teams
    rather than here (:mod:`apps.msteams.calendar_import`). The live-session settings
    are per session, with defaults from ``LiveSessionSettings``.
    ``teams_meeting_external`` means the Teams meeting was made by hand in Outlook/Teams
    (often one link shared by a whole timetable), not by the platform: the platform
    never edits or deletes such a meeting, and moving the session gives it a meeting of
    its own. The thumbnail (background image + session details) is drawn by
    ``livesessions.thumbnails``; the after-class artifacts are filled by
    :mod:`apps.msteams.services`. ``live_url`` is an alias of :attr:`external_url` used
    by the launch page and templates.
    """

    PROVIDER_TEAMS = 'teams'
    PROVIDER_LEGACY = 'jitsi'
    PROVIDER_CHOICES = [(PROVIDER_TEAMS, 'Microsoft Teams'), (PROVIDER_LEGACY, 'Retired video room')]

    AUDIENCE_EVERYONE = 'everyone'
    AUDIENCE_INSTITUTION = 'institution'
    AUDIENCE_PROGRAMME = 'programme'
    AUDIENCE_COHORT = 'cohort'
    AUDIENCE_MODULE = 'module'
    AUDIENCE_PRIVATE = 'private'
    AUDIENCE_CHOICES = [
        (AUDIENCE_EVERYONE, 'Everyone on the platform'),
        (AUDIENCE_INSTITUTION, 'One institution'),
        (AUDIENCE_PROGRAMME, 'One programme'),
        (AUDIENCE_COHORT, 'One cohort'),
        (AUDIENCE_MODULE, 'One module'),
        (AUDIENCE_PRIVATE, 'Invited people only (incl. one-on-one)'),
    ]

    DERIVED_AUDIENCES = (AUDIENCE_EVERYONE, AUDIENCE_INSTITUTION,
                         AUDIENCE_PROGRAMME, AUDIENCE_COHORT, AUDIENCE_MODULE)

    KIND_CLASS = 'class'
    KIND_REVISION = 'revision'
    KIND_TEST_REVIEW = 'test_review'
    KIND_CONSULT = 'consult'
    KIND_WORKSHOP = 'workshop'
    KIND_BRIEFING = 'briefing'
    KIND_CHOICES = [
        (KIND_CLASS, 'Class'),
        (KIND_REVISION, 'Revision'),
        (KIND_TEST_REVIEW, 'Test / exam review'),
        (KIND_CONSULT, 'One-on-one consultation'),
        (KIND_WORKSHOP, 'Workshop'),
        (KIND_BRIEFING, 'Briefing'),
    ]

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=24, unique=True, default=_new_token, editable=False)
    description = models.TextField(blank=True)

    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='hosted_meetings')
    group = models.ForeignKey(ChatGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='meetings')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True, related_name='meetings')

    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    is_recurring = models.BooleanField(default=False)
    RECURRENCE_CHOICES = [('', 'None'), ('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')]
    recurrence = models.CharField(max_length=10, choices=RECURRENCE_CHOICES, blank=True)

    requires_login = models.BooleanField(default=True, help_text='If off, anyone with the link can join.')
    is_active = models.BooleanField(default=True)

    provider = models.CharField(max_length=10, choices=PROVIDER_CHOICES, default=PROVIDER_TEAMS)
    teams_meeting_id = models.CharField(max_length=512, blank=True)
    teams_join_url = models.URLField(max_length=1000, blank=True)
    external_event_id = models.CharField(max_length=512, blank=True, db_default='', db_index=True)
    organizer_upn = models.CharField(max_length=255, blank=True,
                                     help_text='Microsoft UPN of the meeting organiser (the licensed educator).')

    audience = models.CharField(max_length=12, choices=AUDIENCE_CHOICES, default=AUDIENCE_PRIVATE,
                                db_index=True,
                                help_text='Who this session is for — decides both who is notified '
                                          'and who can see it on the calendar.')
    session_kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_CLASS)
    institution = models.ForeignKey('learning.Institution', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='meetings')
    programme = models.ForeignKey('learning.Programme', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='meetings')
    cohort = models.ForeignKey('learning.Cohort', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='meetings')
    week = models.ForeignKey('learning.ModuleWeek', on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='meetings',
                             help_text='The week / topic this session covers, if any.')
    calendar_event = models.ForeignKey('learning.CalendarEvent', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='meetings',
                                       help_text='The test / exam this session prepares for or reviews.')

    record_automatically = models.BooleanField(
        default=True, help_text='Start recording the moment the host joins.')
    publish_recording = models.BooleanField(
        default=True, help_text='Make the recording available as a past session afterwards.')

    teams_calendar_event_id = models.CharField(max_length=512, blank=True)
    teams_meeting_external = models.BooleanField(default=False)

    thumbnail = models.ImageField(upload_to='sessions/thumbnails/', blank=True, null=True,
                                  storage=files_storage, validators=v.validate_image)

    recording_url = models.URLField(max_length=1000, blank=True)
    artifacts_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Newest first.

        The ``(audience, scheduled_start)`` index answers the calendar's only question:
        "sessions in this window, for this audience". Both the staff view (no audience
        filter) and every student view (audience + one scope id) are served by walking
        this index over the requested month.
        """
        ordering = ['-scheduled_start', '-created_at']
        indexes = [
            models.Index(fields=['audience', 'scheduled_start'], name='comm_meet_aud_start'),
            models.Index(fields=['module', 'scheduled_start'], name='comm_meet_mod_start'),
            models.Index(fields=['scheduled_start'], name='comm_meet_start'),
        ]

    def __str__(self):
        """Return the session title."""
        return self.title

    @property
    def is_teams(self):
        """True for a Microsoft Teams session."""
        return self.provider == self.PROVIDER_TEAMS

    @property
    def external_url(self):
        """The real A/V room URL — the Teams join link ('' when there is none yet)."""
        return self.teams_join_url if self.is_teams else ''

    live_url = external_url

    def get_join_url(self):
        """The launch page — the button that actually puts you in the call."""
        return reverse('communication:meeting-room', args=[self.slug])

    def get_absolute_url(self):
        """The session's own page: details, join button, and afterwards the
        recording, transcript, summary and your attendance.

        This is what the calendar, reminders and e-mails link to. The launch page
        above is one button on it, because a link sent 24 hours in advance should
        open something you can read, not something that tries to start a call.
        """
        return reverse('livesessions:session-detail', args=[self.pk])

    @property
    def is_upcoming(self):
        """True when the session starts now or later."""
        return bool(self.scheduled_start and self.scheduled_start >= timezone.now())

    @property
    def is_live_now(self):
        """True while now falls between the session's start and end."""
        now = timezone.now()
        start, end = self.scheduled_start, self.scheduled_end
        if not start:
            return False
        return start <= now and (end is None or now <= end)

    @property
    def has_ended(self):
        """True once the scheduled end has passed."""
        end = self.scheduled_end
        return bool(end and timezone.now() > end)

    @property
    def duration_minutes(self):
        """The scheduled length in minutes (60 when start/end are missing or inverted).
        """
        start, end = self.scheduled_start, self.scheduled_end
        if start and end and end > start:
            return int((end - start).total_seconds() // 60)
        return 60

    @property
    def is_one_on_one(self):
        """A private session with exactly one invitee besides the host.

        Used to decide whether the attendance report goes back to the attendee
        personally rather than into a class register.
        """
        if self.audience != self.AUDIENCE_PRIVATE:
            return False
        return self.participants.exclude(role='host').count() == 1

    @property
    def resolved_institution(self):
        """The institution this session belongs to, however it was scoped."""
        if self.institution_id:
            return self.institution
        programme = self.resolved_programme
        return getattr(programme, 'institution', None)

    @property
    def resolved_programme(self):
        """The programme this session belongs to, however it was scoped (programme, cohort
        or module).
        """
        if self.programme_id:
            return self.programme
        if self.cohort_id:
            return self.cohort.programme
        if self.module_id:
            return self.module.programme
        return None

    @property
    def storage_path_parts(self):
        """Institution → programme → module → (intake) → (Test N → Week n) → session, as folders.

        This mirrors the MetanoiaLMS OneDrive tree that the ``provision_onedrive``
        command builds, so a recap files itself into the same place. When the
        session is pinned to a week (:attr:`week`), it routes into that week's
        folder — the recording and summary then land in the week's Recordings /
        Session summaries buckets. A session with no week (a general briefing)
        keeps its own dated folder under the module. Levels that do not apply are
        left out, so a platform-wide briefing lands in ``General/`` rather than a
        chain of "None" folders. Segment names match ``provision_onedrive``:
        institution.code, programme.code, module.code, phase.short_label.

        For example an intake folder is the cohort code ("B26S") and a test folder the
        phase's short label ("Test 1").
        """
        parts = []
        institution = self.resolved_institution
        programme = self.resolved_programme
        parts.append(getattr(institution, 'code', '') or 'General')
        if programme is not None:
            parts.append(getattr(programme, 'code', '') or str(programme))
        if self.module_id:
            parts.append(self.module.code)
        if self.cohort_id:
            parts.append(self.cohort.code)
        if self.week_id:
            parts.append(self.week.phase.short_label)
            parts.append(f'Week {self.week.number}')
        else:
            when = self.scheduled_start or self.created_at or timezone.now()
            parts.append(f'{timezone.localtime(when):%Y-%m-%d} {self.title}'.strip())
        return [_safe_folder_name(part) for part in parts if part]

    @property
    def audience_label(self):
        """A human sentence naming who the session is for."""
        if self.audience == self.AUDIENCE_EVERYONE:
            return 'Everyone'
        if self.audience == self.AUDIENCE_INSTITUTION and self.institution_id:
            return self.institution.label
        if self.audience == self.AUDIENCE_PROGRAMME and self.programme_id:
            return self.programme.label
        if self.audience == self.AUDIENCE_COHORT and self.cohort_id:
            return str(self.cohort)
        if self.audience == self.AUDIENCE_MODULE and self.module_id:
            return self.module.label
        if self.audience == self.AUDIENCE_PRIVATE:
            return 'One-on-one' if self.is_one_on_one else 'Invited people only'
        return self.get_audience_display()

    @property
    def thumbnail_caption_lines(self):
        """The lines drawn onto the thumbnail, top to bottom.

        Programme / module, then the week or test it covers, then the date and
        time — the four things a student scanning a schedule actually needs.
        """
        lines = []
        programme = self.resolved_programme
        if self.module_id:
            lines.append(f'{programme.label} | {self.module.code}' if programme else self.module.label)
        elif programme is not None:
            lines.append(programme.label)
        elif self.institution_id:
            lines.append(self.institution.display_name)
        if self.week_id:
            lines.append(self.week.display_title)
        elif self.calendar_event_id:
            lines.append(self.calendar_event.title)
        if self.scheduled_start:
            from core import clock
            lines.append(f'{clock.local(self.scheduled_start):%a %d %b %Y · %H:%M} SAST')
        return lines


class MeetingParticipant(models.Model):
    """An invitee / attendee of a :class:`MeetingRoom` (used for RSVPs & roles)."""

    ROLE_CHOICES = [('host', 'Host'), ('cohost', 'Co-host'), ('attendee', 'Attendee')]
    RSVP_CHOICES = [('pending', 'Pending'), ('yes', 'Going'), ('no', 'Not going'), ('maybe', 'Maybe')]

    meeting = models.ForeignKey(MeetingRoom, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='meeting_invites')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='attendee')
    rsvp = models.CharField(max_length=10, choices=RSVP_CHOICES, default='pending')
    invited_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One row per user per meeting."""
        unique_together = ('meeting', 'user')

    def __str__(self):
        """Return ``user → meeting``."""
        return f'{self.user} → {self.meeting}'


class Notification(models.Model):
    """A single notification delivered to one user (also shown in the bell menu).

    ``body_format``: plain text is escaped and line-broken; ``html`` is rendered as-is
    on the detail page. Only ever set ``html`` for bodies the system itself composes
    (welcome guides, digests) — never for anything a user typed.

    ``summary`` is an optional one-line standfirst shown under the headline on the
    reading page; ``message`` / ``meeting`` are optional links back to the thing the
    notification is about. ``category`` is the :class:`NotificationPreference` category
    it was sent under (``notify(category=)``), blank when none was given; it decides
    which Metanoia mailbox the notice is "from" when no person sent it
    (:func:`senders.notification_sender`). ``is_favourite`` is set when the recipient
    stars it, so they can find it again from the favourites rail on the inbox,
    independent of whether it has been read.
    """

    LEVEL_CHOICES = [('info', 'Info'), ('success', 'Success'), ('warning', 'Warning'), ('error', 'Error')]

    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    verb = models.CharField(max_length=120, blank=True, help_text='e.g. "mentioned you", "sent an announcement"')
    title = models.CharField(max_length=200, blank=True)
    body = models.TextField(blank=True)
    FORMAT_TEXT = 'text'
    FORMAT_HTML = 'html'
    FORMAT_CHOICES = [(FORMAT_TEXT, 'Plain text'), (FORMAT_HTML, 'Rich text (system-authored)')]
    body_format = models.CharField(max_length=4, choices=FORMAT_CHOICES, default=FORMAT_TEXT)
    summary = models.CharField(max_length=300, blank=True)
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='info')
    url = models.CharField(max_length=500, blank=True, help_text='Where clicking the notification goes.')

    message = models.ForeignKey(Message, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    meeting = models.ForeignKey(MeetingRoom, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    announcement = models.ForeignKey('Announcement', on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')

    category = models.CharField(max_length=20, blank=True, default='')

    is_read = models.BooleanField(default=False, db_index=True)
    is_favourite = models.BooleanField(default=False, db_index=True)
    emailed = models.BooleanField(default=False)
    whatsapped = models.BooleanField(default=False,
                                     help_text='A copy reached the recipient on WhatsApp.')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        """Newest first; indexed by recipient and read state for the bell."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['recipient', 'is_read'])]

    def __str__(self):
        """Return ``recipient: title``."""
        return f'{self.recipient}: {self.title or self.verb}'

    def mark_read(self):
        """Mark the notification read (saves only when it changes)."""
        if not self.is_read:
            self.is_read = True
            self.save(update_fields=['is_read'])

    @property
    def is_html(self):
        """True when the body is system-composed HTML."""
        return self.body_format == self.FORMAT_HTML

    @property
    def reading_minutes(self):
        """Rough reading time for the detail page, at 200 words a minute."""
        import re
        text = re.sub(r'<[^>]+>', ' ', self.body or '')
        words = len([w for w in text.split() if w])
        return max(1, round(words / 200))


class Announcement(models.Model):
    """A broadcast composed by a staff member / admin.

    :attr:`audience` picks who receives it; :func:`apps.communication.services.send_announcement`
    resolves that to a set of users, creates one :class:`Notification` each and
    (if :attr:`send_email`) e-mails them via :mod:`apps.communication.emails`.

    The ``user_type``, ``modules`` and ``users`` audiences predate the composer's
    "targeted" audience. They are kept so old rows and the REST API still resolve, but
    the composer no longer offers them: "Everyone" + a role filter is a user category,
    and "targeted" covers modules and named people. The ``user_type`` field is used when
    ``audience == user_type`` and matches ``accounts.Person.user_type``.

    ``ROLE_CHOICES`` are the roles a broadcast can be narrowed to — the values of
    ``Person.user_type``. ``roles`` narrows the audience to those values (empty = every
    role); people picked by name are always included, whatever their role.
    """

    AUDIENCE_ALL = 'all'
    AUDIENCE_TARGETED = 'targeted'
    AUDIENCE_USER_TYPE = 'user_type'
    AUDIENCE_MODULES = 'modules'
    AUDIENCE_USERS = 'users'
    AUDIENCE_CHOICES = [
        (AUDIENCE_ALL, 'Everyone'),
        (AUDIENCE_TARGETED, 'Selected institutions, programmes, cohorts, modules or people'),
        (AUDIENCE_USER_TYPE, 'A user category'),
        (AUDIENCE_MODULES, 'Members of selected modules'),
        (AUDIENCE_USERS, 'Specific individuals'),
    ]
    LEVEL_CHOICES = Notification.LEVEL_CHOICES

    STATUS_DRAFT = 'draft'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_SENDING = 'sending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'), (STATUS_SCHEDULED, 'Scheduled'), (STATUS_SENDING, 'Sending'),
        (STATUS_SENT, 'Sent'), (STATUS_FAILED, 'Failed'), (STATUS_CANCELLED, 'Cancelled'),
    ]

    ROLE_CHOICES = [('student', 'Students'), ('educator', 'Educators'), ('parent', 'Parents'),
                    ('staff', 'Staff'), ('admin', 'Admins'), ('community', 'Community members')]

    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='announcements_sent')
    title = models.CharField(max_length=200)
    body = models.TextField()
    level = models.CharField(max_length=10, choices=LEVEL_CHOICES, default='info')
    url = models.CharField(max_length=500, blank=True)
    url_label = models.CharField(max_length=60, blank=True,
                                 help_text='Button text for the link, e.g. "Open the blueprint".')
    media_url = models.URLField(max_length=500, blank=True,
                                help_text='A YouTube, Vimeo or other video link to play inside the notification.')

    audience = models.CharField(max_length=12, choices=AUDIENCE_CHOICES, default=AUDIENCE_ALL)
    user_type = models.CharField(max_length=20, blank=True)
    institutions = models.ManyToManyField('learning.Institution', blank=True, related_name='announcements')
    programmes = models.ManyToManyField('learning.Programme', blank=True, related_name='announcements')
    cohorts = models.ManyToManyField('learning.Cohort', blank=True, related_name='announcements')
    modules = models.ManyToManyField('learning.ProgrammeModule', blank=True, related_name='announcements')
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='+')
    roles = models.JSONField(default=list, blank=True)
    include_parents = models.BooleanField(
        default=False, help_text="Also send a copy to the parents/guardians of every student reached.")
    is_important = models.BooleanField(
        default=False, help_text='Deliver even to people who have switched announcements off.')

    send_email = models.BooleanField(default=True, help_text='Also deliver a copy by e-mail.')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    scheduled_for = models.DateTimeField(null=True, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    recipient_count = models.PositiveIntegerField(default=0)
    failure = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        """Return the announcement title."""
        return self.title

    @property
    def is_sent(self):
        """True once the announcement has been sent."""
        return self.sent_at is not None

    @property
    def is_editable(self):
        """True while it is a draft or still scheduled."""
        return self.status in (self.STATUS_DRAFT, self.STATUS_SCHEDULED)

    @property
    def role_labels(self):
        """Display labels of the roles the announcement is narrowed to."""
        names = dict(self.ROLE_CHOICES)
        return [names.get(r, r) for r in (self.roles or [])]

    @property
    def embed_url(self):
        """An iframe-able player URL for :attr:`media_url`, or '' if it isn't one."""
        from .broadcast import embed_url_for
        return embed_url_for(self.media_url)


class AnnouncementAttachment(models.Model):
    """A picture, video or document sent with an :class:`Announcement`.

    Every recipient's notification points at the same announcement, so the file
    is stored once and shown on each person's notification page.
    """

    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_CHOICES = [(KIND_IMAGE, 'Image'), (KIND_VIDEO, 'Video'), (KIND_FILE, 'File')]

    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'}
    VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v'}

    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='announcements/%Y/%m/', storage=files_storage,
                            validators=v.validate_broadcast)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_FILE)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """In upload order."""
        ordering = ['created_at', 'pk']

    def __str__(self):
        """Return the original file name."""
        return self.original_name or self.file.name

    @classmethod
    def classify(cls, filename):
        """The attachment kind (image / video / file) for ``filename``'s extension."""
        import os
        ext = os.path.splitext(filename or '')[1].lower()
        if ext in cls.IMAGE_EXTS:
            return cls.KIND_IMAGE
        if ext in cls.VIDEO_EXTS:
            return cls.KIND_VIDEO
        return cls.KIND_FILE

    def save(self, *args, **kwargs):
        """Fill in the original name and size from the file, then save."""
        if self.file and not self.original_name:
            self.original_name = getattr(self.file, 'name', '')
        if self.file and not self.size:
            try:
                self.size = self.file.size
            except (OSError, ValueError):
                self.size = 0
        if self.original_name:
            self.kind = self.classify(self.original_name)
        super().save(*args, **kwargs)


class AutoNotice(models.Model):
    """One automatic notification waiting to go out (or already sent).

    Things that happen in the platform — a blueprint published, a test opening,
    a lesson released — queue a row here keyed by what happened (``key``), so the
    same event can never notify twice however often it is saved. A scheduler
    job (:func:`apps.communication.auto.run`) sends the rows whose ``due_at``
    has passed, checks each is still true at that moment (a material unpublished
    in the meantime is dropped) and folds several new items on one module into a
    single notification.

    ``ref`` is the object the event is about, as ``app_label.model:pk``; ``group``
    groups items that can be folded into one notification (e.g. ``module:12``);
    ``audience`` says who it is for, as the keyword arguments of
    :func:`broadcast.audience_users`.
    """

    key = models.CharField(max_length=120, unique=True)
    kind = models.CharField(max_length=30, db_index=True)
    ref = models.CharField(max_length=80, blank=True)
    group = models.CharField(max_length=60, blank=True, db_index=True)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    url = models.CharField(max_length=500, blank=True)
    level = models.CharField(max_length=10, default='info')
    category = models.CharField(max_length=20, blank=True)
    audience = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                              blank=True, related_name='+')
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    recipient_count = models.PositiveIntegerField(default=0)
    dropped = models.CharField(max_length=200, blank=True,
                               help_text='Why it was not sent, when it was not.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Due first."""
        ordering = ['due_at', 'pk']

    def __str__(self):
        """Return the notice key."""
        return self.key


class Discussion(models.Model):
    """A discussion thread / post a user starts.

    Can be scoped to a :class:`learning.ProgrammeModule` so
    members of that course/module see it; tags and @mentions surface it to
    specific people. Supports media attachments and threaded replies (see
    :class:`DiscussionReply`).

    Part of the Q&A-style forum (posts, replies, media, tagging, likes). ``module`` is
    an optional scope — a course or a module.
    """

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='discussions',
    )
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)

    module = models.ForeignKey(
        'learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='discussions',
    )

    tags = models.CharField(max_length=255, blank=True, help_text='Comma separated')
    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='mentioned_in_discussions',
    )
    likes = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='liked_discussions',
    )

    is_pinned = models.BooleanField(default=False)
    is_closed = models.BooleanField(default=False)
    view_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Pinned first, then newest."""
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        """Return the discussion title."""
        return self.title

    def get_absolute_url(self):
        """The discussion's detail page."""
        return reverse('communication:discussion-detail', args=[self.pk])

    @property
    def tag_list(self):
        """The comma-separated ``tags`` as a list."""
        return [t.strip() for t in self.tags.split(',') if t.strip()]

    @property
    def reply_count(self):
        """Number of replies."""
        return self.replies.count()

    @property
    def like_count(self):
        """Number of likes."""
        return self.likes.count()


class DiscussionReply(models.Model):
    """A reply to a :class:`Discussion` (optionally nested via ``reply_to``)."""

    discussion = models.ForeignKey(Discussion, on_delete=models.CASCADE, related_name='replies')
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='discussion_replies',
    )
    body = models.TextField()
    reply_to = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='child_replies',
    )
    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='mentioned_in_replies',
    )
    likes = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='liked_replies',
    )
    is_answer = models.BooleanField(default=False, help_text='Marked as the accepted answer.')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        """Chronological."""
        ordering = ['created_at']

    def __str__(self):
        """Return ``Reply by author on discussion``."""
        return f'Reply by {self.author} on {self.discussion}'

    @property
    def like_count(self):
        """Number of likes."""
        return self.likes.count()


class DiscussionAttachment(models.Model):
    """Media attached to a discussion or a reply (image / video / file)."""

    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_CHOICES = [(KIND_IMAGE, 'Image'), (KIND_VIDEO, 'Video'), (KIND_FILE, 'File')]

    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
    VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v', '.avi', '.mkv', '.ogg'}

    discussion = models.ForeignKey(
        Discussion, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments',
    )
    reply = models.ForeignKey(
        DiscussionReply, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments',
    )
    file = models.FileField(upload_to='discussions/%Y/%m/', storage=files_storage,
                            validators=v.validate_attachment)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_FILE)
    original_name = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        """Return the original file name."""
        return self.original_name or self.file.name

    @classmethod
    def classify(cls, filename):
        """The attachment kind (image / video / file) for ``filename``'s extension."""
        import os
        ext = os.path.splitext(filename or '')[1].lower()
        if ext in cls.IMAGE_EXTS:
            return cls.KIND_IMAGE
        if ext in cls.VIDEO_EXTS:
            return cls.KIND_VIDEO
        return cls.KIND_FILE

    def save(self, *args, **kwargs):
        """Fill in the original name and classify the kind from it, then save."""
        if self.file and not self.original_name:
            self.original_name = getattr(self.file, 'name', '')
        if (not self.kind or self.kind == self.KIND_FILE) and self.original_name:
            self.kind = self.classify(self.original_name)
        super().save(*args, **kwargs)


class Violation(models.Model):
    """A flagged piece of content with a risk score and category.

    Part of Community Safety & Moderation (My Learning Hub plan, section 12). ``score``:
    0 Safe · 1 Warning · 2 Moderate · 3 High · 4 Severe.
    """

    SOURCE_CHOICES = [
        ('chat', 'Chat'), ('dm', 'Direct message'), ('forum', 'Forum'),
        ('comment', 'Comment'), ('assignment', 'Assignment'), ('upload', 'Upload'),
        ('announcement', 'Announcement'), ('profile', 'Profile'),
    ]
    DETECTED_RULE = 'rule'
    DETECTED_AI = 'ai'
    DETECTED_REPORT = 'report'
    DETECTED_CHOICES = [(DETECTED_RULE, 'Rule'), (DETECTED_AI, 'AI'), (DETECTED_REPORT, 'User report')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='violations')
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default='chat')
    text = models.TextField(blank=True, help_text='Snapshot of the offending content (evidence).')
    category = models.CharField(max_length=40, blank=True)
    score = models.PositiveSmallIntegerField(default=0)
    message = models.ForeignKey(Message, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='violations')
    detected_by = models.CharField(max_length=8, choices=DETECTED_CHOICES, default=DETECTED_RULE)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='reported_violations')
    evidence = models.JSONField(default=dict, blank=True)
    handled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first; indexed by user and handled state."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'handled'], name='comm_violation_user_idx')]

    def __str__(self):
        """Return ``user · category (score)``."""
        return f'{self.user} · {self.category or "violation"} ({self.score})'


class Penalty(models.Model):
    """A penalty applied to a user (warning → mute → suspension → escalation).

    ``SILENCING`` lists the penalty kinds that stop a user from sending messages while
    active.
    """

    KIND_WARNING = 'warning'
    KIND_DELETE = 'delete'
    KIND_MUTE = 'mute'
    KIND_CHAT_SUSPEND = 'chat_suspend'
    KIND_CLASS_SUSPEND = 'class_suspend'
    KIND_PLATFORM_SUSPEND = 'platform_suspend'
    KIND_ESCALATE = 'escalate'
    KIND_CHOICES = [
        (KIND_WARNING, 'Warning'), (KIND_DELETE, 'Message deletion'), (KIND_MUTE, 'Temporary mute'),
        (KIND_CHAT_SUSPEND, 'Chat suspension'), (KIND_CLASS_SUSPEND, 'Class suspension'),
        (KIND_PLATFORM_SUSPEND, 'Platform suspension'), (KIND_ESCALATE, 'Escalated to staff'),
    ]
    SILENCING = (KIND_MUTE, KIND_CHAT_SUSPEND, KIND_PLATFORM_SUSPEND)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='penalties')
    kind = models.CharField(max_length=18, choices=KIND_CHOICES, default=KIND_WARNING)
    reason = models.CharField(max_length=255, blank=True)
    violation = models.ForeignKey(Violation, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='penalties')
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='penalties_issued')
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, help_text='Blank = indefinite.')
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first; indexed by user and active state."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'active'], name='comm_penalty_user_idx')]

    def __str__(self):
        """Return ``kind · user``."""
        return f'{self.get_kind_display()} · {self.user}'

    def is_current(self):
        """True while the penalty is active and has not reached its end time."""
        if not self.active:
            return False
        if self.ends_at and self.ends_at <= timezone.now():
            return False
        return True


class Appeal(models.Model):
    """A user's appeal against a :class:`Penalty`, reviewed by staff."""
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [(STATUS_PENDING, 'Pending'), (STATUS_ACCEPTED, 'Accepted'), (STATUS_REJECTED, 'Rejected')]

    penalty = models.ForeignKey(Penalty, on_delete=models.CASCADE, related_name='appeals')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='appeals')
    message = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='appeals_reviewed')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        """Return ``Appeal by user (status)``."""
        return f'Appeal by {self.user} ({self.get_status_display()})'


class ReputationScore(models.Model):
    """A community reputation score (100 = excellent). Violations lower it."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reputation')
    score = models.PositiveSmallIntegerField(default=100)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        """Return ``user: score``."""
        return f'{self.user}: {self.score}'


class ClassSession(models.Model):
    """A single class session that attendance is recorded against.

    A session belongs to a :class:`accounts.ProgrammeModule` (the class) on a given
    date. It may optionally be backed by a live :class:`MeetingRoom` (a video
    class).

    **Attendance is automatic and time-based.** While a student has the session
    open their browser sends a heartbeat; each one accrues real seconds onto
    their :class:`Attendance` row (see
    :func:`apps.communication.services.record_presence_ping`). Once they have
    been present for :attr:`min_attendance_pct` of the session they are marked
    present automatically — merely opening the page is not enough. Educators,
    staff and admins can always override the result by hand, and a manual mark
    is never undone by a later heartbeat.

    Automated class-session attendance (Learning Hub feature 2).
    ``DEFAULT_MIN_ATTENDANCE_PCT``: attendance below this share of the session does not
    earn a "present". ``FALLBACK_DURATION_MINUTES`` is used when a session has neither
    its own times nor a scheduled meeting. ``finalised_at`` is set by
    ``services.finalise_session_attendance`` once the roll is closed out — everyone
    below the threshold becomes absent and non-attendees get a row.
    """

    DEFAULT_MIN_ATTENDANCE_PCT = 50
    FALLBACK_DURATION_MINUTES = 60

    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE,
                               null=True, blank=True, related_name='class_sessions')
    title = models.CharField(max_length=200, blank=True)
    session_date = models.DateField(default=timezone.localdate, db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    meeting = models.OneToOneField(
        MeetingRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='class_session',
    )
    late_after_minutes = models.PositiveIntegerField(
        default=10, help_text='A check-in this many minutes after the start counts as "late".')
    min_attendance_pct = models.PositiveIntegerField(
        default=DEFAULT_MIN_ATTENDANCE_PCT,
        help_text='Share of the session a student must actually attend to be auto-marked present.')
    is_open = models.BooleanField(
        default=True, help_text='While open, joining or checking-in records attendance.')
    finalised_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='class_sessions_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first; indexed by module and date."""
        ordering = ['-session_date', '-starts_at']
        indexes = [models.Index(fields=['module', 'session_date'], name='comm_session_subj_idx')]

    def __str__(self):
        """Return ``title · date``."""
        return f'{self.title or self.module} · {self.session_date}'

    @property
    def scheduled_start(self):
        """The session's start — its own, else the backing meeting's."""
        return self.starts_at or getattr(self.meeting, 'scheduled_start', None)

    @property
    def scheduled_end(self):
        """The session's end — its own, else the backing meeting's."""
        return self.ends_at or getattr(self.meeting, 'scheduled_end', None)

    @property
    def planned_seconds(self):
        """How long the session is meant to run, in seconds.

        Falls back to :attr:`FALLBACK_DURATION_MINUTES` when neither the session
        nor its meeting carries both times, so an ad-hoc class still produces a
        sane percentage rather than dividing by zero.
        """
        start, end = self.scheduled_start, self.scheduled_end
        if start and end and end > start:
            return int((end - start).total_seconds())
        return self.FALLBACK_DURATION_MINUTES * 60

    @property
    def has_ended(self):
        """True once the end has passed; with no end time, once the day has passed."""
        end = self.scheduled_end
        if end:
            return timezone.now() > end
        return self.session_date < timezone.localdate()

    @property
    def is_live_now(self):
        """True while the session is running (an open session today when it has no times).
        """
        start, end = self.scheduled_start, self.scheduled_end
        if not start:
            return self.is_open and self.session_date == timezone.localdate()
        now = timezone.now()
        return start <= now and (end is None or now <= end)

    @property
    def present_count(self):
        """Number of present or late attendance rows."""
        return self.attendance.filter(
            status__in=[Attendance.STATUS_PRESENT, Attendance.STATUS_LATE]).count()

    @property
    def absent_count(self):
        """Number of absent attendance rows."""
        return self.attendance.filter(status=Attendance.STATUS_ABSENT).count()


class Attendance(models.Model):
    """One student's attendance for one :class:`ClassSession`.

    Presence is measured, not assumed: :attr:`seconds_attended` accumulates from
    heartbeats sent while the student actually has the session open, and the
    status is derived from that against the session's
    :attr:`ClassSession.min_attendance_pct`. An educator/staff/admin mark
    (``source='manual'``) overrides the measurement and is never undone by a
    later heartbeat.

    ``source`` records how the status was established: ``auto`` (measured by the in-page
    presence heartbeat), ``teams`` (the Microsoft Graph attendance report), ``login``
    (self check-in), ``lesson`` (studied the module's lesson content on the day of the
    session), ``active`` (signed in and used the platform that day, but did not touch
    this module) and ``manual`` (set by an educator / admin — wins over the rest). The
    measured presence fields are written by ``services.record_presence_ping``.
    """

    STATUS_PRESENT = 'present'
    STATUS_LATE = 'late'
    STATUS_ABSENT = 'absent'
    STATUS_EXCUSED = 'excused'
    STATUS_CHOICES = [
        (STATUS_PRESENT, 'Present'), (STATUS_LATE, 'Late'),
        (STATUS_ABSENT, 'Absent'), (STATUS_EXCUSED, 'Excused'),
    ]

    SOURCE_AUTO = 'auto'
    SOURCE_TEAMS = 'teams'
    SOURCE_LOGIN = 'login'
    SOURCE_LESSON = 'lesson'
    SOURCE_ACTIVE = 'active'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = [(SOURCE_AUTO, 'Auto (page time)'), (SOURCE_TEAMS, 'Teams report'),
                      (SOURCE_LOGIN, 'Check-in'), (SOURCE_LESSON, 'Lesson studied'),
                      (SOURCE_ACTIVE, 'On the platform'), (SOURCE_MANUAL, 'Manual')]

    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='attendance')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='attendance_records')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PRESENT, db_index=True)
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    check_in_at = models.DateTimeField(null=True, blank=True)

    seconds_attended = models.PositiveIntegerField(
        default=0, help_text='Seconds the student was actually present, from heartbeats.')
    first_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='attendance_marked')
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """One row per student per session."""
        unique_together = ('session', 'student')
        ordering = ['session', 'student']

    def __str__(self):
        """Return ``student · session (status)``."""
        return f'{self.student} · {self.session} ({self.get_status_display()})'

    @property
    def minutes_attended(self):
        """Measured presence in whole minutes."""
        return round(self.seconds_attended / 60)

    @property
    def attended_pct(self):
        """Share of the session the student was actually present for (0–100)."""
        planned = self.session.planned_seconds
        if not planned:
            return 0
        return min(100, round(self.seconds_attended / planned * 100))

    @property
    def meets_threshold(self):
        """True when the attended share reaches the session's minimum."""
        return self.attended_pct >= (self.session.min_attendance_pct or 0)

    @property
    def is_manual(self):
        """True when an educator set this by hand — heartbeats must not touch it."""
        return self.source == self.SOURCE_MANUAL


class WebPushSubscription(models.Model):
    """One browser that asked for desktop notifications (Web Push).

    A browser may be subscribed for several accounts (someone who signs in to
    more than one Metanoia mailbox account on the same computer gets all of their
    alerts). The push service drops dead subscriptions, and
    :mod:`apps.communication.webpush` deletes them when it says so.

    Part of the customisable notifications (Learning Hub feature 3).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=1000)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """One subscription per user per push endpoint."""
        constraints = [models.UniqueConstraint(fields=['user', 'endpoint'], name='webpush_unique_user_endpoint')]

    def __str__(self):
        """Return ``user · browser``."""
        return f'{self.user} · {self.user_agent[:40] or self.endpoint[:40]}'


class NotificationPreference(models.Model):
    """Per-user delivery preferences for notifications & reminders.

    Channels (in-app is always on) and per-category switches are honoured by
    :func:`apps.communication.services.notify` and the reminder scan; a digest
    setting and quiet hours let users throttle e-mail.

    ``CATEGORIES`` are the values used as the ``category`` argument to ``notify(...)``;
    the ``notify_*`` switches decide which kinds of notification to receive at all.

    Channels: in-app is always on — the bell is the platform's own record. The other two
    are copies of it, and a person can take both, either or neither: e-mail for the
    paper trail, WhatsApp for the one they will actually see today.

    Automated e-mail: reminders, the weekly summary and "new material" notices are
    system notifications. Reminders are e-mailed by default; updates only to someone who
    switches them on. Account mail (invoices, receipts, sign-in, welcome) is not covered
    — it always goes. ``digest`` and quiet hours throttle delivery.
    """

    DIGEST_OFF = 'off'
    DIGEST_DAILY = 'daily'
    DIGEST_WEEKLY = 'weekly'
    DIGEST_CHOICES = [(DIGEST_OFF, 'Send immediately'), (DIGEST_DAILY, 'Daily digest'),
                      (DIGEST_WEEKLY, 'Weekly digest')]

    CATEGORIES = ['mentions', 'messages', 'announcements', 'deadlines', 'meetings', 'grades',
                  'content', 'assessments', 'weekly']

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preference')

    email_enabled = models.BooleanField(default=True, help_text='Also deliver a copy by e-mail.')
    whatsapp_enabled = models.BooleanField(
        default=False,
        help_text='Also deliver a copy by WhatsApp, to the number on your contact details.')
    browser_enabled = models.BooleanField(default=True, help_text='Show desktop / browser notifications.')

    EMAIL_REMINDERS = 'reminders'
    EMAIL_UPDATES = 'updates'
    email_reminders = models.BooleanField(
        default=True, verbose_name='Reminders by e-mail',
        help_text='Upcoming live sessions, due dates and "where you left off" nudges.')
    email_updates = models.BooleanField(
        default=False, verbose_name='Updates by e-mail',
        help_text='New material on your modules, the weekly summary, class summaries and recordings.')
    email_unsubscribed_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the person last unsubscribed from automated e-mail (from an e-mail link).')

    notify_mentions = models.BooleanField(default=True)
    notify_messages = models.BooleanField(default=True)
    notify_announcements = models.BooleanField(default=True)
    notify_deadlines = models.BooleanField(default=True, help_text='Upcoming assessment deadlines.')
    notify_meetings = models.BooleanField(default=True, help_text='Live sessions & meeting invites.')
    notify_grades = models.BooleanField(default=True, help_text='New grades & certificates.')
    notify_content = models.BooleanField(
        default=True, help_text='New material on your modules: blueprints, mock exams, solutions, lessons, videos.')
    notify_assessments = models.BooleanField(
        default=True, help_text='A test, quiz or mock exam opening, and solutions being released.')
    notify_weekly = models.BooleanField(
        default=True, help_text='A Monday-morning summary of the week ahead.')

    digest = models.CharField(max_length=8, choices=DIGEST_CHOICES, default=DIGEST_OFF)
    quiet_hours_start = models.TimeField(null=True, blank=True)
    quiet_hours_end = models.TimeField(null=True, blank=True)
    reminder_lead_minutes = models.PositiveIntegerField(
        default=60, help_text='How long before a deadline / session to remind you.')

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        """Return ``Notification preferences · user``."""
        return f'Notification preferences · {self.user}'

    @classmethod
    def for_user(cls, user):
        """The user's preferences row, created with the defaults on first use."""
        pref, _ = cls.objects.get_or_create(user=user)
        return pref

    def allows(self, category):
        """True if the user wants notifications of this ``category`` at all."""
        return bool(getattr(self, f'notify_{category}', True)) if category else True

    def allows_email(self, category=None):
        """True if e-mail is on and ``category`` is wanted."""
        return self.email_enabled and self.allows(category)

    def allows_automated_email(self, kind, category=None):
        """True if this person asked for e-mail copies of ``kind`` (reminders / updates)."""
        return bool(getattr(self, f'email_{kind}', False)) and self.allows(category)

    def unsubscribe(self):
        """The e-mail footer's Unsubscribe: every optional e-mail stops.

        Reminders, updates and the e-mail copies of team messages all go off;
        the bell keeps every one of them. Account mail is not optional and is
        not touched — there is no switch for it.
        """
        self.email_enabled = False
        self.email_reminders = False
        self.email_updates = False
        self.email_unsubscribed_at = timezone.now()
        self.save(update_fields=['email_enabled', 'email_reminders', 'email_updates',
                                 'email_unsubscribed_at', 'updated_at'])

    def allows_whatsapp(self, category=None):
        """True if WhatsApp is on and ``category`` is wanted."""
        return self.whatsapp_enabled and self.allows(category)

    @property
    def channels(self):
        """The channels this person has switched on, for showing back to them."""
        picked = ['in-app']
        if self.email_enabled or self.email_reminders or self.email_updates:
            picked.append('e-mail')
        if self.whatsapp_enabled:
            picked.append('WhatsApp')
        return picked

    def in_quiet_hours(self, now=None):
        """True if the current local time falls inside the user's quiet hours.

        An overnight window (e.g. 22:00 → 07:00) wraps past midnight.
        """
        if not self.quiet_hours_start or not self.quiet_hours_end:
            return False
        now = now or timezone.localtime()
        t_now = now.time()
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start <= end:
            return start <= t_now <= end
        return t_now >= start or t_now <= end


class Workspace(models.Model):
    """A **study group**: a shared space for a few candidates, optionally tied
    to a module. (Called a "workspace" until 2026-11; the model and table kept
    the name. Who may be added is scoped in :mod:`apps.communication.study_groups`.)

    Complements the existing chat + discussion forum with a place to keep
    shared **files** and collaborative **notes / project areas**. Access is open
    to admins/staff, the workspace's creator, explicit ``members`` and anyone
    enrolled in the linked course or module.

    Study groups share files and notes (Learning Hub feature 4). ``chat_group`` is the
    group's own conversation — made with the group, kept in step with its members, and
    shown on the group's page (:mod:`apps.communication.study_groups`).
    """

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='workspaces')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='workspaces_created')
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='workspaces')
    chat_group = models.OneToOneField(ChatGroup, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='study_group')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Most recently updated first."""
        ordering = ['-updated_at']
        verbose_name = 'Study group'
        verbose_name_plural = 'Study groups'

    def __str__(self):
        """Return the group name."""
        return self.name

    def get_absolute_url(self):
        """The study group's page."""
        return reverse('communication:workspace-detail', args=[self.pk])

    def can_access(self, user):
        """True for staff, the creator, members, and people on the group's module."""
        if not user or not user.is_authenticated:
            return False
        if user.is_staff or user.is_superuser:
            return True
        if self.created_by_id == user.id or self.members.filter(pk=user.pk).exists():
            return True
        person = getattr(user, 'profile', None)
        if person:
            if self.module_id and self.module.member_people.filter(pk=person.pk).exists():
                return True
        return False


class WorkspaceFile(models.Model):
    """A shared file in a study group (:class:`Workspace`; simple re-upload versioning)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='files')
    file = models.FileField(upload_to='workspaces/%Y/%m/', storage=files_storage,
                            validators=v.validate_attachment)
    title = models.CharField(max_length=200, blank=True)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='workspace_files')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Admin naming and ordering."""
        ordering = ['-created_at']
        verbose_name = 'Study group file'

    def __str__(self):
        """Return the title or file name."""
        return self.title or self.original_name or self.file.name

    def save(self, *args, **kwargs):
        """Fill in the original name and size from the file, then save."""
        if self.file and not self.original_name:
            self.original_name = getattr(self.file, 'name', '')[:255]
        if self.file and not self.size:
            try:
                self.size = self.file.size
            except (OSError, ValueError):
                self.size = 0
        super().save(*args, **kwargs)


class MailMessage(models.Model):
    """An in-system e-mail composed by an admin/staff/educator. On send it's
    stored here (an internal mailbox) *and* delivered as a real branded e-mail
    to each recipient's registered address, with the sender's address as
    reply-to (so replies go straight back to them)."""

    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_mail')
    sender_email = models.CharField(max_length=254, blank=True, help_text='Sender registered e-mail (used as reply-to).')
    subject = models.CharField(max_length=255, help_text='The e-mail subject line.')
    body = models.TextField(blank=True)
    external_to = models.CharField(max_length=500, blank=True, help_text='Comma-separated external addresses (no in-system inbox).')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        """Return the subject."""
        return self.subject or f'Mail #{self.pk}'

    @property
    def recipient_names(self):
        """The recipients' names (and any external address), comma-separated."""
        names = [r.recipient.get_full_name() or r.recipient.get_username() for r in self.recipients.all()]
        if self.external_to:
            names.append(self.external_to)
        return ', '.join(names)


class MailRecipient(models.Model):
    """One recipient's copy of a :class:`MailMessage` (their inbox row)."""

    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name='recipients')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='received_mail')
    is_read = models.BooleanField(default=False, db_index=True)
    starred = models.BooleanField(default=False)
    trashed = models.BooleanField(default=False)

    class Meta:
        """Ordering and uniqueness of inbox rows."""
        unique_together = ('message', 'recipient')
        ordering = ['-message__created_at']

    def __str__(self):
        """Return a short label for the admin."""
        return f'{self.message} → {self.recipient}'


class WorkspaceNote(models.Model):
    """A collaborative note within a study group (:class:`Workspace`)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name='notes')
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='workspace_notes')
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Ordering of notes."""
        ordering = ['-pinned', '-updated_at']
        verbose_name = 'Study group note'

    def __str__(self):
        """Return the note title."""
        return self.title


class Community(models.Model):
    """A space in the Metanoia Community the feed is filed under.

    There is one **default** community everybody belongs to, and one per
    :class:`~apps.learning.models.Programme`, created automatically (see
    ``apps.communication.signals``). Who belongs to a programme's community is
    worked out, never stored — its students, the members who said they study
    it when they joined, its educators, its students' parents, and all staff —
    so membership can never drift from enrolment. See
    :mod:`apps.communication.community`.
    """

    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True)
    description = models.TextField(blank=True)
    programme = models.OneToOneField(
        'learning.Programme', on_delete=models.CASCADE, null=True, blank=True,
        related_name='community')
    is_default = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """The default community first, then by name; at most one default community."""
        ordering = ['-is_default', 'name']
        verbose_name_plural = 'communities'
        constraints = [
            models.UniqueConstraint(fields=['is_default'], condition=models.Q(is_default=True),
                                    name='one_default_community'),
        ]

    def __str__(self):
        """Return the community name."""
        return self.name

    @classmethod
    def default(cls):
        """The community every account belongs to (created on first use)."""
        community = cls.objects.filter(is_default=True).first()
        if community is None:
            community, _ = cls.objects.get_or_create(
                slug='metanoia', defaults={'name': 'Metanoia Community', 'is_default': True,
                                         'description': 'Everyone on the Metanoia Learning Hub.'})
            if not community.is_default:
                community.is_default = True
                community.save(update_fields=['is_default'])
        return community

    @classmethod
    def for_programme(cls, programme):
        """The community of ``programme`` (created if it does not exist yet)."""
        from django.utils.text import slugify
        existing = cls.objects.filter(programme=programme).first()
        if existing:
            return existing
        institution = getattr(programme, 'institution', None)
        name = ' '.join(filter(None, [getattr(institution, 'code', ''), programme.name])).strip()
        base = slugify(name) or f'programme-{programme.pk}'
        slug, n = base, 2
        while cls.objects.filter(slug=slug).exists():
            slug, n = f'{base}-{n}', n + 1
        return cls.objects.create(name=name or str(programme), slug=slug, programme=programme,
                                  description=programme.full_name or '')


class CommunityMembership(models.Model):
    """A community somebody *chose* to join while exploring.

    Nobody needs a row for the general Metanoia Community or their own
    programme's community: those are worked out from enrolment (see
    :mod:`apps.communication.community`). A row here only counts while the
    person has "Explore other communities" switched on
    (``Person.community_explore``); switching it off takes them back to their
    two communities without forgetting what they had joined.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='community_memberships')
    community = models.ForeignKey(Community, on_delete=models.CASCADE, related_name='memberships')
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One membership per user per community."""
        ordering = ['community__name']
        constraints = [
            models.UniqueConstraint(fields=['user', 'community'], name='one_membership_per_community'),
        ]

    def __str__(self):
        """Return ``user in community``."""
        return f'{self.user} in {self.community}'


class Feed(models.Model):
    """A user-authored post in the unified activity feed — a status update with
    optional image / video / file, or an event share. Aggregated together with
    announcements, notifications, messages, tasks, calendar events and reminders
    by :func:`apps.communication.feed.build_feed` for the dashboard / social page.

    ``media_kind`` is image / video / file. ``module`` is the module the post belongs
    to, so the feed's module filter can pick it up; blank means a general post, only
    visible under "General". ``likes`` is authoritative (one row per person, so a like
    can be undone and cannot be spammed); ``like_count`` is the denormalised total the
    feed reads.

    Metanoia Community: ``community`` is where the post was made — blank means the default
    community (posts made before communities existed). ``shared_from`` is set on a
    share: the post being passed on to another community; the share carries the sharer's
    own words in ``body`` and the original is shown under it. Moderation: a removed post
    disappears for everyone but staff, who can put it back. ``removed_by`` is blank when
    it was hidden automatically after enough reports (see
    ``apps.communication.community.AUTO_HIDE_REPORTS``).
    """

    VIS_PUBLIC = 'public'
    VIS_GROUPS = 'groups'
    VIS_STAFF = 'staff'
    VISIBILITY_CHOICES = [
        (VIS_PUBLIC, 'Everyone'),
        (VIS_GROUPS, 'My courses & modules'),
        (VIS_STAFF, 'Staff & admins'),
    ]
    KIND_TEXT = 'text'
    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_EVENT = 'event'

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='feed_posts')
    body = models.TextField(blank=True)
    media = models.FileField(upload_to='feed/%Y/%m/', blank=True, null=True, storage=files_storage,
                             validators=v.validate_media)
    media_kind = models.CharField(max_length=10, blank=True)
    kind = models.CharField(max_length=10, default=KIND_TEXT)
    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default=VIS_PUBLIC)
    group = models.ForeignKey('ChatGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='feed_posts')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='feed_posts')
    likes = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='liked_feed_posts', blank=True)
    like_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    community = models.ForeignKey('Community', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='posts')
    shared_from = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='shares')
    is_removed = models.BooleanField(default=False, db_index=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        """Return a short label for the post."""
        return f'{self.author} · {(self.body or self.kind)[:40]}'

    def toggle_like(self, user):
        """Add or remove ``user``'s like and resync the counter. Returns
        ``(liked, count)``."""
        if self.likes.filter(pk=user.pk).exists():
            self.likes.remove(user)
            liked = False
        else:
            self.likes.add(user)
            liked = True
        self.like_count = self.likes.count()
        self.save(update_fields=['like_count'])
        return liked, self.like_count

    @classmethod
    def visible_to(cls, user):
        """Posts the given user is allowed to see (their groups / class, plus
        public posts and — for staff/admins — staff-only posts).

        Staff see removed posts too (the feed labels them). Everyone else sees only the
        communities they belong to (see :mod:`apps.communication.community`).
        """
        from django.db.models import Q

        from core.roles import role_of_user
        qs = cls.objects.select_related('author', 'author__profile', 'group', 'module', 'community')
        if not user.is_authenticated:
            return qs.none()
        if user.is_staff or user.is_superuser or role_of_user(user) in ('admin', 'staff'):
            return qs
        from .community import community_ids_for
        in_my_communities = (Q(community__isnull=True) | Q(community__is_default=True)
                             | Q(community_id__in=community_ids_for(user)))
        group_ids = list(ChatGroup.objects.filter(memberships__user=user).values_list('id', flat=True))
        return qs.filter(in_my_communities, is_removed=False).filter(
            Q(author=user)
            | Q(visibility=cls.VIS_PUBLIC)
            | Q(visibility=cls.VIS_GROUPS, group__isnull=True)
            | Q(visibility=cls.VIS_GROUPS, group_id__in=group_ids)
        ).distinct()


class FeedAttachment(models.Model):
    """A file attached to a :class:`Feed` post — an image, a video or a generic
    file. A post can carry several; the feed tiles them into a gallery. Mirrors
    :class:`MessageAttachment` so uploads classify and label themselves the same
    way. The legacy single ``Feed.media`` field is kept for old posts; new posts
    store every file here instead.
    """

    KIND_IMAGE = 'image'
    KIND_VIDEO = 'video'
    KIND_FILE = 'file'
    KIND_CHOICES = [(KIND_IMAGE, 'Image'), (KIND_VIDEO, 'Video'), (KIND_FILE, 'File')]

    feed = models.ForeignKey('Feed', on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='feed/%Y/%m/', storage=files_storage,
                            validators=v.validate_media)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_FILE)
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
    VIDEO_EXTS = {'.mp4', '.webm', '.mov', '.m4v', '.avi', '.mkv', '.ogg'}

    class Meta:
        """In upload order."""
        ordering = ['created_at', 'pk']

    def __str__(self):
        """Return the original file name."""
        return self.original_name or self.file.name

    @classmethod
    def classify(cls, filename):
        """The attachment kind (image / video / file) for ``filename``'s extension."""
        import os
        ext = os.path.splitext(filename or '')[1].lower()
        if ext in cls.IMAGE_EXTS:
            return cls.KIND_IMAGE
        if ext in cls.VIDEO_EXTS:
            return cls.KIND_VIDEO
        return cls.KIND_FILE

    def save(self, *args, **kwargs):
        """Fill in the original name, kind and size from the file, then save."""
        if self.file and not self.original_name:
            self.original_name = getattr(self.file, 'name', '')
        if self.file and not self.size:
            try:
                self.size = self.file.size
            except (OSError, ValueError):
                self.size = 0
        if (not self.kind or self.kind == self.KIND_FILE) and self.original_name:
            self.kind = self.classify(self.original_name)
        super().save(*args, **kwargs)


class FeedComment(models.Model):
    """A comment on a :class:`Feed` post (the feed's own comment thread)."""

    post = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='feed_comments')
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Chronological."""
        ordering = ['created_at']

    def __str__(self):
        """Return ``author: body`` (truncated)."""
        return f'{self.author}: {self.body[:40]}'


class FeedReport(models.Model):
    """Somebody flagging a community post for staff to look at.

    ``status`` ends as ``removed`` (staff removed the post) or ``dismissed`` (staff kept
    it).
    """

    REASON_SPAM = 'spam'
    REASON_HARASSMENT = 'harassment'
    REASON_INAPPROPRIATE = 'inappropriate'
    REASON_MISLEADING = 'misleading'
    REASON_OTHER = 'other'
    REASON_CHOICES = [
        (REASON_SPAM, 'Spam or advertising'),
        (REASON_HARASSMENT, 'Harassment or bullying'),
        (REASON_INAPPROPRIATE, 'Inappropriate or offensive'),
        (REASON_MISLEADING, 'False or misleading'),
        (REASON_OTHER, 'Something else'),
    ]
    STATUS_OPEN = 'open'
    STATUS_REMOVED = 'removed'
    STATUS_DISMISSED = 'dismissed'
    STATUS_CHOICES = [(STATUS_OPEN, 'Open'), (STATUS_REMOVED, 'Post removed'),
                      (STATUS_DISMISSED, 'Dismissed')]

    post = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name='reports')
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=REASON_OTHER)
    note = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    handled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first; one report per person per post."""
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['post', 'reporter'], name='one_report_per_person')]

    def __str__(self):
        """Return ``Report on #post · reason``."""
        return f'Report on #{self.post_id} · {self.get_reason_display()}'


class FeedMention(models.Model):
    """A person tagged (@First Last) in a community post or comment."""

    post = models.ForeignKey(Feed, on_delete=models.CASCADE, related_name='mentions')
    comment = models.ForeignKey(FeedComment, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='mentions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One mention per person per post or comment."""
        constraints = [models.UniqueConstraint(fields=['post', 'comment', 'user'],
                                               name='one_mention_per_person')]

    def __str__(self):
        """Return ``@user in #post``."""
        return f'@{self.user} in #{self.post_id}'


class WhatsAppThread(models.Model):
    """One WhatsApp conversation: everything to and from one phone number.

    A number, not a person, because that is what WhatsApp is. Several accounts
    can share one number (a parent and a child), so the people on a thread are
    looked up rather than stored (:meth:`people`), and ``active_person`` records
    which of them the bot is currently answering for.

    ``status`` is the staff side. A conversation is *open* when somebody wrote
    something the bot could not answer, asked for a person, or sent a file, and
    stays open until a staff member closes it. While it is open the bot keeps
    answering its commands but stays quiet otherwise, so it never talks over
    a staff member.

    WhatsApp is the two-way channel.
    """

    OPEN = 'open'
    CLOSED = 'closed'
    STATUS_CHOICES = [(OPEN, 'Open'), (CLOSED, 'Closed')]

    number = models.CharField(max_length=20, unique=True, help_text='Digits only (2782…).')
    contact_name = models.CharField(max_length=120, blank=True,
                                    help_text='The name on their WhatsApp profile.')
    status = models.CharField(max_length=6, choices=STATUS_CHOICES, default=CLOSED, db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='whatsapp_threads')
    active_person = models.ForeignKey('accounts.Person', on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='+')
    awaiting_choice = models.BooleanField(default=False,
                                          help_text='The bot asked which account is writing.')
    unread = models.PositiveIntegerField(default=0, help_text='Inbound messages no staff member has seen.')
    last_inbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_preview = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        """Most recently active first."""
        ordering = ['-last_message_at']
        verbose_name = 'WhatsApp conversation'

    def __str__(self):
        """Return ``WhatsApp number``."""
        return f'WhatsApp {self.number}'

    def people(self):
        """Every account on this number, verified ones first."""
        from apps.communication.whatsapp_inbox import people_for_number
        return people_for_number(self.number)

    @property
    def window_open(self):
        """True while a free-form reply is allowed (24 hours from their last message)."""
        from datetime import timedelta
        return bool(self.last_inbound_at
                    and self.last_inbound_at >= timezone.now() - timedelta(hours=24))

    @property
    def window_closes_at(self):
        """When the 24-hour free-form reply window closes, or None if they never wrote.
        """
        from datetime import timedelta
        return self.last_inbound_at + timedelta(hours=24) if self.last_inbound_at else None


class WhatsAppMessage(models.Model):
    """One message in or out on WhatsApp — the bot's transcript.

    Everything is kept: what the student asked, what the bot answered, and the
    notification copies pushed out to them. Three reasons, all practical.
    ``wa_id`` makes inbound handling idempotent (Meta re-delivers a webhook until
    it gets a 200, so the same message arrives more than once). The transcript
    lets staff see what a student was told when they say "the bot said my test
    was Friday". And the *inbound* timestamps are what decide whether a free-form
    reply is allowed at all — Meta only permits one within 24 hours of the
    person's last message (see :func:`core.whatsapp.send_text`).

    Delivery ``status`` is for messages we sent, from Meta's status webhooks; it only
    moves forward (``STATUS_RANK``): a late "delivered" never overwrites "read".
    ``person`` is null when the number belongs to nobody on the platform — those are
    kept too, because "somebody messaged us from an unknown number" is a thing staff
    need to be able to see. ``command`` is the command the bot matched, when it
    recognised one, which makes it obvious from the transcript why it answered the way
    it did. ``sent_by`` is who wrote it, for staff replies; null for the bot and for
    automatic copies. Inbound media arrives as a Meta id and is fetched once into
    ``media`` (served to admin and staff only, see :mod:`core.media_access`).
    """

    IN = 'in'
    OUT = 'out'
    DIRECTION_CHOICES = [(IN, 'Received'), (OUT, 'Sent')]

    KIND_TEXT = 'text'
    KIND_DOCUMENT = 'document'
    KIND_IMAGE = 'image'
    KIND_AUDIO = 'audio'
    KIND_VIDEO = 'video'
    KIND_NOTIFICATION = 'notification'
    KIND_TEMPLATE = 'template'
    KIND_CODE = 'code'
    KIND_OTHER = 'other'
    KIND_CHOICES = [
        (KIND_TEXT, 'Text'),
        (KIND_DOCUMENT, 'Document'),
        (KIND_IMAGE, 'Photo'),
        (KIND_AUDIO, 'Voice note / audio'),
        (KIND_VIDEO, 'Video'),
        (KIND_NOTIFICATION, 'Notification copy'),
        (KIND_TEMPLATE, 'Template'),
        (KIND_CODE, 'Verification code'),
        (KIND_OTHER, 'Other'),
    ]
    MEDIA_KINDS = (KIND_DOCUMENT, KIND_IMAGE, KIND_AUDIO, KIND_VIDEO)

    STATUS_QUEUED = 'queued'
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_QUEUED, 'Waiting for them to reply'), (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'), (STATUS_READ, 'Read'), (STATUS_FAILED, 'Not delivered'),
    ]
    STATUS_RANK = {STATUS_QUEUED: 0, STATUS_SENT: 1, STATUS_DELIVERED: 2, STATUS_READ: 3,
                   STATUS_FAILED: 4}

    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, db_index=True)
    number = models.CharField(max_length=20, db_index=True,
                              help_text='The person\'s number, digits only (2782…).')
    person = models.ForeignKey('accounts.Person', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='whatsapp_messages')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_TEXT)
    body = models.TextField(blank=True)
    command = models.CharField(max_length=30, blank=True, db_index=True)
    wa_id = models.CharField(max_length=128, blank=True, db_index=True,
                             help_text="Meta's message id: inbound de-duplication, delivery receipts.")
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    thread = models.ForeignKey(WhatsAppThread, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='messages')
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='+')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, blank=True, db_index=True)
    error = models.CharField(max_length=255, blank=True)
    template = models.CharField(max_length=60, blank=True)
    media = models.FileField(upload_to='whatsapp/%Y/%m/', blank=True, max_length=255,
                             validators=v.validate_whatsapp_media)
    media_id = models.CharField(max_length=128, blank=True)
    media_mime = models.CharField(max_length=100, blank=True)
    media_filename = models.CharField(max_length=200, blank=True)

    class Meta:
        """Newest first; indexed by number; inbound Meta message ids are unique."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['number', '-created_at'])]
        constraints = [
            models.UniqueConstraint(fields=['wa_id'], condition=models.Q(direction='in'),
                                    name='uniq_inbound_whatsapp_message'),
        ]
        verbose_name = 'WhatsApp message'

    def __str__(self):
        """Return ``← / → number: body``."""
        arrow = '←' if self.direction == self.IN else '→'
        return f'{arrow} {self.number}: {(self.body or self.kind)[:50]}'

    @classmethod
    def within_service_window(cls, number, hours=24):
        """True when this number messaged us recently enough for a free-form reply.

        Meta's rule, not ours: outside 24 hours from the person's last inbound
        message only an approved template may be sent.
        """
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(hours=hours)
        return cls.objects.filter(direction=cls.IN, number=number,
                                  created_at__gte=cutoff).exists()
