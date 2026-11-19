"""Live-session settings, after-class pipeline state, identity, calendars and reminders.

Credentials stay in the environment; :class:`LiveSessionSettings` holds only
behaviour, so changing it never needs a deploy.

The models fall into six groups:

* :class:`LiveSessionSettings` — the platform-wide singleton.
* :class:`SessionArtifact` — the stored after-class pipeline state of one
  finished Teams session (OneDrive filing, summary, YouTube backup, AI follow-up).
* :class:`SessionJoin` and :class:`TeamsIdentityAlias` — who actually attended,
  matched against the Graph attendance report.
* :class:`CalendarSubscription` and :class:`ExternalEvent` — read-only outside
  calendars imported as busy time.
* :class:`SessionReminder` — de-duplication of reminders already sent.
* :class:`YouTubePlaylist` and :class:`YouTubeVideo` — imported YouTube content.
  Mapping is per *playlist*: staff point it at a module + cohort once. Scanning
  (:mod:`apps.livesessions.youtube`) only upserts raw metadata; it never touches
  a mapping a human has set.
"""

from django.conf import settings as django_settings
from django.db import models
from django.utils import timezone

from core import validators as v
from core.storage import files_storage


class LiveSessionSettings(models.Model):
    """Platform-wide behaviour for live sessions. Exactly one row.

    Read only through :meth:`load`, which creates the row on first use and never
    raises — a missing settings row must not take the calendar down.

    Fields are grouped by concern: recording (``auto_record``,
    ``publish_recordings``), OneDrive filing (``onedrive_root``,
    ``onedrive_owner_upn``, ``keep_teams_copy``), YouTube (``youtube_*``),
    thumbnails (``thumbnail_*``), reminders (``reminder_leads``,
    ``reminder_email``), the AI follow-up (``ai_followup_enabled``), one-on-ones
    booked in Teams (``coach_emails``) and the Microsoft calendar
    (``create_calendar_events``).
    """

    auto_record = models.BooleanField(
        default=True,
        help_text='Start recording automatically when the host joins a session.')
    publish_recordings = models.BooleanField(
        default=True,
        help_text='Publish finished recordings as past sessions students can re-watch.')

    onedrive_root = models.CharField(
        max_length=120, default='Metanoia Sessions',
        help_text='Top folder in the organiser’s OneDrive. Sessions are filed under '
                  '<root>/<institution>/<programme>/<module>/<date + title>/.')
    onedrive_owner_upn = models.CharField(
        max_length=255, blank=True,
        help_text='UPN whose OneDrive holds the whole tree. Blank = each session is '
                  'filed in its own organiser’s drive.')
    keep_teams_copy = models.BooleanField(
        default=True,
        help_text='Leave the original recording where Teams put it as well as filing a copy. '
                  'Off = the recording is moved, so there is one copy, in one place.')

    PRIVACY_UNLISTED = 'unlisted'
    PRIVACY_PRIVATE = 'private'
    PRIVACY_PUBLIC = 'public'
    PRIVACY_CHOICES = [(PRIVACY_UNLISTED, 'Unlisted'), (PRIVACY_PRIVATE, 'Private'),
                       (PRIVACY_PUBLIC, 'Public')]

    youtube_enabled = models.BooleanField(
        default=False,
        help_text='Upload finished recordings to YouTube and stream them in the platform. '
                  'Needs YOUTUBE_* credentials in the environment.')
    youtube_privacy = models.CharField(max_length=8, choices=PRIVACY_CHOICES, default=PRIVACY_UNLISTED)
    youtube_daily_quota = models.PositiveIntegerField(
        default=10000,
        help_text='Your project’s daily YouTube Data API quota. An upload costs 1600 units, '
                  'so the default cap allows about six a day before uploads must wait.')

    thumbnail_background = models.ImageField(
        upload_to='sessions/backgrounds/', blank=True, null=True, storage=files_storage,
        validators=v.validate_image,
        help_text='Background image the session details are drawn on top of. '
                  'Blank = a plain brand-coloured panel. Best at 1280×720.')
    thumbnail_text_colour = models.CharField(max_length=9, default='#FFFFFF')
    thumbnail_accent_colour = models.CharField(max_length=9, default='#C9A84C')

    reminder_leads = models.CharField(
        max_length=100, default='1440,30',
        help_text='Minutes before a session to remind people, comma-separated. '
                  'Default 1440,30 = 24 hours and 30 minutes.')
    reminder_email = models.BooleanField(default=True, help_text='Also send reminders by e-mail.')

    ai_followup_enabled = models.BooleanField(
        default=True,
        help_text='After each session, have the Claude admin layer write the full summary, '
                  'the next session’s agenda and the action items. Runs as a background '
                  'job on the admin account only.')

    coach_emails = models.TextField(
        blank=True, default='', db_default='',
        help_text='Personal addresses coaches and staff use on Teams invites (one per line). '
                  'They are never taken for the student when a one-on-one is imported from the '
                  'Teams calendar. Admin, staff and educator accounts are excluded anyway.')

    create_calendar_events = models.BooleanField(
        default=True,
        help_text='Also create a real Outlook/Teams calendar event for the organiser and '
                  'any staff invitees. Needs the Calendars.ReadWrite application permission.')

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        """Admin labels for the singleton row."""
        verbose_name = 'Live session settings'
        verbose_name_plural = 'Live session settings'

    def __str__(self):
        """Fixed label: there is only ever one row."""
        return 'Live session settings'

    def save(self, *args, **kwargs):
        """Save as the singleton row, ``pk=1``, always as an upsert.

        Pinning the pk is not enough: a new instance still has ``_state.adding``
        set, so Django would INSERT and hit the primary-key constraint. Clearing it
        (and ``force_insert``) makes every save an update-or-create.
        """
        self.pk = 1
        self._state.adding = False
        kwargs['force_insert'] = False
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The settings row, created on first use. Never raises."""
        try:
            obj, _ = cls.objects.get_or_create(pk=1)
            return obj
        except Exception:       # pragma: no cover - a broken row must not 500 the calendar
            return cls()

    @property
    def lead_minutes(self):
        """``reminder_leads`` as a sorted list of ints, longest lead first."""
        leads = set()
        for chunk in (self.reminder_leads or '').split(','):
            chunk = chunk.strip()
            if chunk.isdigit() and int(chunk) > 0:
                leads.add(int(chunk))
        return sorted(leads, reverse=True) or [1440, 30]

    @property
    def coach_email_set(self):
        """``coach_emails`` as a set of lower-cased addresses.

        Accepts one address per line or comma-separated; blank entries are dropped.
        """
        raw = self.coach_emails if isinstance(self.coach_emails, str) else ''
        return {line.strip().lower() for line in raw.replace(',', '\n').splitlines() if line.strip()}

    @property
    def youtube_live(self):
        """True only when YouTube is switched on *and* credentials are present."""
        from . import youtube
        return bool(self.youtube_enabled and youtube.is_configured())


class SessionArtifact(models.Model):
    """Everything produced by one finished session, and how far along it is.

    A *stored* state machine: Teams can take hours to publish a recording and
    YouTube quota can defer an upload to tomorrow, so each scheduler tick
    resumes where the last stopped and a failure parks only this session.

    States, in order:

    * ``waiting`` — the session has not ended yet.
    * ``polling`` — ended; waiting for Teams to publish its artifacts.
    * ``filing`` — moving the recording and transcript into OneDrive.
    * ``summarising`` — transcript → recap → AI follow-up.
    * ``uploading`` — queued for, or in the middle of, the YouTube upload.
    * ``published`` — available to students as a past session.
    * ``failed`` — gave up after :attr:`MAX_ATTEMPTS`.
    * ``skipped`` — nothing was recorded, or publishing is off.

    Field groups:

    * OneDrive — the session folder, the filed recording/transcript/summary
      items and their anonymous share links. ``teams_drive_id`` /
      ``teams_item_id`` record where Teams itself saved the recording (the
      organiser's OneDrive "Recordings" folder). They are found as soon as Teams
      publishes it, so the class can be watched before — and independently of —
      the copy into the tree.
    * The published session summary (:mod:`apps.livesessions.summaries`).
      ``summary_source`` is ``teams_ai`` (Teams' own AI recap: meeting notes,
      action items), ``claude`` (written by Claude from the transcript) or
      ``extractive`` (sentences lifted from the transcript). In
      :attr:`SUMMARY_RANK` higher wins: a better summary replaces a weaker one
      already published.
    * YouTube — the private backup upload.
    * AI follow-up — written by Claude on the admin account, in the background.
    * Bookkeeping — ``unresolved_attendees`` holds Graph attendance records that
      could not be resolved to an account, as ``[{'label', 'seconds', 'keys'}]``.
      Anonymous joining makes these normal, not exceptional: an educator
      attaches each one to a candidate on the register and the answer is
      remembered as a :class:`TeamsIdentityAlias`.

    :attr:`POLL_GIVE_UP_HOURS` bounds the wait for slow Teams artifacts;
    :attr:`MAX_ATTEMPTS` is the number of consecutive failures of one step before
    the row is parked as failed.
    """

    STATE_WAITING = 'waiting'
    STATE_POLLING = 'polling'
    STATE_FILING = 'filing'
    STATE_SUMMARISING = 'summarising'
    STATE_UPLOADING = 'uploading'
    STATE_PUBLISHED = 'published'
    STATE_FAILED = 'failed'
    STATE_SKIPPED = 'skipped'
    STATE_CHOICES = [
        (STATE_WAITING, 'Waiting for the session to end'),
        (STATE_POLLING, 'Waiting for Teams artifacts'),
        (STATE_FILING, 'Filing to OneDrive'),
        (STATE_SUMMARISING, 'Summarising'),
        (STATE_UPLOADING, 'Uploading to YouTube'),
        (STATE_PUBLISHED, 'Published'),
        (STATE_FAILED, 'Failed'),
        (STATE_SKIPPED, 'Skipped'),
    ]

    POLL_GIVE_UP_HOURS = 48
    MAX_ATTEMPTS = 8

    meeting = models.OneToOneField('communication.MeetingRoom', on_delete=models.CASCADE,
                                   related_name='artifact')
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=STATE_WAITING, db_index=True)

    folder_id = models.CharField(max_length=255, blank=True)
    folder_path = models.CharField(max_length=500, blank=True,
                                   help_text='Human-readable path of the session folder.')
    folder_web_url = models.URLField(max_length=1000, blank=True)
    drive_owner_upn = models.CharField(max_length=255, blank=True)

    recording_item_id = models.CharField(max_length=255, blank=True)
    recording_web_url = models.URLField(max_length=1000, blank=True)
    recording_share_url = models.URLField(max_length=1000, blank=True,
                                          help_text='Anonymous view link, for students who are not in the tenant.')
    recording_bytes = models.BigIntegerField(default=0)
    recording_filed_at = models.DateTimeField(null=True, blank=True)
    recording_drive_id = models.CharField(max_length=255, blank=True, db_default='',
                                          help_text='Drive holding the filed copy (with recording_item_id).')
    teams_drive_id = models.CharField(max_length=255, blank=True, db_default='')
    teams_item_id = models.CharField(max_length=255, blank=True, db_default='')

    transcript_item_id = models.CharField(max_length=255, blank=True)
    transcript_web_url = models.URLField(max_length=1000, blank=True)
    transcript_share_url = models.URLField(max_length=1000, blank=True, db_default='',
                                           help_text='Anonymous view link to the transcript, for students.')
    summary_item_id = models.CharField(max_length=255, blank=True)
    summary_web_url = models.URLField(max_length=1000, blank=True)
    SUMMARY_TEAMS = 'teams_ai'
    SUMMARY_CLAUDE = 'claude'
    SUMMARY_EXTRACTIVE = 'extractive'
    SUMMARY_SOURCES = [
        (SUMMARY_TEAMS, 'Teams AI recap'),
        (SUMMARY_CLAUDE, 'AI summary from the transcript'),
        (SUMMARY_EXTRACTIVE, 'Transcript extract'),
    ]
    SUMMARY_RANK = {SUMMARY_EXTRACTIVE: 1, SUMMARY_CLAUDE: 2, SUMMARY_TEAMS: 3}
    summary_source = models.CharField(max_length=12, choices=SUMMARY_SOURCES, blank=True, db_default='')
    summary_hash = models.CharField(max_length=64, blank=True, db_default='')
    summary_share_url = models.URLField(max_length=1000, blank=True, db_default='',
                                        help_text='Anonymous view link to the summary on OneDrive.')
    summary_lesson = models.ForeignKey('learning.Lesson', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='+', help_text='The summary as a reading lesson students open.')
    summary_published_at = models.DateTimeField(null=True, blank=True)
    summary_checked_at = models.DateTimeField(null=True, blank=True)
    summary_note = models.CharField(max_length=300, blank=True, db_default='',
                                    help_text='Why there is no summary yet (e.g. transcripts disabled).')
    thumbnail_item_id = models.CharField(max_length=255, blank=True)

    youtube_video_id = models.CharField(max_length=40, blank=True, db_index=True)
    youtube_uploaded_at = models.DateTimeField(null=True, blank=True)
    youtube_error = models.CharField(max_length=300, blank=True)

    ai_followup_done = models.BooleanField(default=False)
    ai_report_id = models.PositiveIntegerField(null=True, blank=True,
                                               help_text='The ai_assistant.AiReport holding the full write-up.')
    ai_followup_attempts = models.PositiveSmallIntegerField(default=0, db_default=0)
    followup_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the class was sent its one session summary (summary, next agenda, '
                  'action items, recording and transcript links).')

    unresolved_attendees = models.JSONField(default=list, blank=True)

    announced_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the audience was told the recording is up. Stops a second '
                  'announcement when the YouTube id lands after the OneDrive link.')
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Newest artifacts first."""
        ordering = ['-created_at']
        verbose_name = 'Session artifact'

    def __str__(self):
        """The meeting and its current pipeline state."""
        return f'{self.meeting} — {self.get_state_display()}'

    def advance(self, state, **fields):
        """Move to ``state``, clear the failure counter, and save."""
        self.state = state
        self.attempts = 0
        self.last_error = ''
        self.last_attempt_at = timezone.now()
        if state == self.STATE_PUBLISHED and not self.published_at:
            self.published_at = timezone.now()
        for key, value in fields.items():
            setattr(self, key, value)
        self.save()
        return self

    def record_failure(self, error):
        """Count a failed attempt; park the row once :attr:`MAX_ATTEMPTS` is hit,
        so a recording Teams never publishes is not retried forever."""
        self.attempts += 1
        self.last_error = str(error)[:2000]
        self.last_attempt_at = timezone.now()
        if self.attempts >= self.MAX_ATTEMPTS:
            self.state = self.STATE_FAILED
        self.save(update_fields=['attempts', 'last_error', 'last_attempt_at', 'state', 'updated_at'])
        return self

    @property
    def is_terminal(self):
        """True once the row is published, failed or skipped and needs no more work."""
        return self.state in (self.STATE_PUBLISHED, self.STATE_FAILED, self.STATE_SKIPPED)

    @property
    def has_video(self):
        """A recording file the platform can play (filed copy or Teams' own)."""
        return bool(self.recording_item_id or self.teams_item_id)

    @property
    def video_url(self):
        """The platform's own player source: checks access, then hands the
        browser a short-lived Microsoft download link (see views.session_video).
        Works for a student with no Microsoft account."""
        from django.urls import reverse
        return reverse('livesessions:session-video', args=[self.meeting_id]) if self.has_video else ''

    @property
    def watch_url(self):
        """Where the platform sends someone who wants to re-watch: the session page's player.

        Never a Graph API address (needs app credentials) nor a SharePoint link
        (students land on a Microsoft sign-in page).
        """
        if self.is_watchable:
            return self.meeting.get_absolute_url() + '#recording'
        return ''

    @property
    def has_transcript(self):
        """A transcript the platform can fetch and show (filed copy or Teams' own)."""
        return bool(self.transcript_item_id
                    or (self.meeting.organizer_upn and self.meeting.teams_meeting_id
                        and (self.transcript_share_url or self.transcript_web_url)))

    @property
    def transcript_url(self):
        """The in-domain transcript page (see views.session_transcript), or ''.

        This — not ``transcript_share_url`` — is the link students are given:
        an anonymous OneDrive link to a ``.vtt`` lands on a Microsoft sign-in
        page, which a candidate with no Microsoft account cannot get past.
        """
        from django.urls import reverse
        return reverse('livesessions:session-transcript', args=[self.meeting_id]) if self.has_transcript else ''

    @property
    def has_summary(self):
        """True when a session summary has been published in any form."""
        return bool(self.summary_lesson_id or self.summary_published_at or self.summary_item_id)

    @property
    def summary_url(self):
        """The in-domain summary (see views.session_summary), or ''.

        It forwards to the summary reading lesson when there is one, so links
        already sent keep working if the lesson is ever rebuilt.
        """
        from django.urls import reverse
        return reverse('livesessions:session-summary', args=[self.meeting_id]) if self.has_summary else ''

    @property
    def embed_url(self):
        """Always '': recordings play from OneDrive (the <video> player). The
        YouTube copy is a private backup (see youtube_backup) and is never shown
        to students."""
        return ''

    @property
    def is_watchable(self):
        """True when there is a recording to play.

        Only the OneDrive copy counts — the YouTube upload is a private backup.
        """
        return bool(self.has_video)


class SessionJoin(models.Model):
    """One identified hand-off from Metanoia into a live session.

    Microsoft gives no supported way to pre-fill an anonymous attendee's
    display name, so Teams reports whatever the student typed. Every student
    reaches the call through :func:`apps.livesessions.views.session_join`
    (behind login), which writes this row; the Graph attendance report is then
    matched *against* it.

    Clicking is not attending — this settles identity only; presence is still
    measured by the Graph report and the page heartbeat.

    ``display_name`` and ``email`` are snapshots taken at the moment of the
    hand-off. They are stored rather than derived because a candidate who
    marries in June must still match the register of a class they attended in
    March under their previous surname. ``matched_identity`` is the Graph
    identity this hand-off was resolved to, once the report arrives.
    """

    meeting = models.ForeignKey('communication.MeetingRoom', on_delete=models.CASCADE,
                                related_name='joins')
    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='session_joins')

    display_name = models.CharField(max_length=200, blank=True,
                                    help_text='The name we asked them to enter in Teams.')
    email = models.EmailField(blank=True)

    first_clicked_at = models.DateTimeField(auto_now_add=True)
    last_clicked_at = models.DateTimeField(auto_now=True)
    click_count = models.PositiveIntegerField(default=1)

    matched_identity = models.CharField(max_length=255, blank=True)
    matched_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        """One row per person per meeting."""
        ordering = ['meeting', 'display_name']
        constraints = [
            models.UniqueConstraint(fields=['meeting', 'user'], name='uniq_session_join_per_user'),
        ]
        indexes = [models.Index(fields=['meeting', 'user'], name='livesess_join_meet_user')]
        verbose_name = 'Session join'

    def __str__(self):
        """The name they were asked to use, and the meeting."""
        return f'{self.display_name or self.user} → {self.meeting}'

    @property
    def is_matched(self):
        """True once the Graph attendance report has been matched to this hand-off."""
        return bool(self.matched_identity)


class TeamsIdentityAlias(models.Model):
    """A Teams identity that is known to belong to a particular person.

    An educator's correction ("Tebza 📱" → Thabo Mokoena) is stored here so
    every future session matches automatically.

    ``identity`` is lower-cased and whitespace-collapsed — the form the matcher
    looks up; ``raw`` is what it looked like before normalisation, so a human can
    recognise it. ``source`` is ``manual`` when an educator resolved it on the
    register, or ``inferred`` when it was matched confidently and remembered.
    """

    SOURCE_MANUAL = 'manual'
    SOURCE_INFERRED = 'inferred'
    SOURCE_CHOICES = [(SOURCE_MANUAL, 'Resolved by a person'),
                      (SOURCE_INFERRED, 'Matched automatically')]

    person = models.ForeignKey('accounts.Person', on_delete=models.CASCADE,
                               related_name='teams_aliases')
    identity = models.CharField(max_length=255, unique=True, db_index=True)
    raw = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    created_by = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Ordered by identity for the admin."""
        ordering = ['identity']
        verbose_name = 'Teams identity alias'
        verbose_name_plural = 'Teams identity aliases'

    def __str__(self):
        """The identity as Teams showed it, and the person it belongs to."""
        return f'{self.raw or self.identity} → {self.person}'


class CalendarSubscription(models.Model):
    """An outside calendar a user has pointed Metanoia at.

    Deliberately an **iCalendar feed URL**, not OAuth: Google, Outlook and Apple
    all offer one, it needs no consent screen, token refresh or API quota, and
    it cannot write to the person's calendar. Events are imported read-only as
    busy time for :func:`apps.livesessions.services.free_slots`.

    ``blocks_time`` decides whether these events count as busy when someone
    looks for a free slot to book.
    """

    PROVIDER_GOOGLE = 'google'
    PROVIDER_MICROSOFT = 'microsoft'
    PROVIDER_APPLE = 'apple'
    PROVIDER_OTHER = 'other'
    PROVIDER_CHOICES = [
        (PROVIDER_GOOGLE, 'Google Calendar'),
        (PROVIDER_MICROSOFT, 'Microsoft 365 / Outlook'),
        (PROVIDER_APPLE, 'Apple iCloud'),
        (PROVIDER_OTHER, 'Other (any .ics feed)'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_OK = 'ok'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [(STATUS_PENDING, 'Not synced yet'), (STATUS_OK, 'Syncing'),
                      (STATUS_FAILED, 'Failing')]

    user = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='calendar_subscriptions')
    name = models.CharField(max_length=120, help_text='What to call it on your calendar.')
    url = models.URLField(max_length=1000,
                          help_text='The private iCalendar (.ics) address from your calendar app.')
    provider = models.CharField(max_length=10, choices=PROVIDER_CHOICES, default=PROVIDER_OTHER)
    colour = models.CharField(max_length=9, default='#8E8E93')
    is_active = models.BooleanField(default=True)
    blocks_time = models.BooleanField(
        default=True,
        help_text='Count these events as busy when someone looks for a free slot to book.')

    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=STATUS_PENDING)
    last_error = models.CharField(max_length=300, blank=True)
    event_count = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """A user cannot subscribe to the same feed URL twice."""
        ordering = ['user', 'name']
        constraints = [
            models.UniqueConstraint(fields=['user', 'url'], name='uniq_calendar_feed_per_user'),
        ]
        verbose_name = 'Calendar subscription'

    def __str__(self):
        """Feed name and owner."""
        return f'{self.name} ({self.user})'

    @property
    def is_healthy(self):
        """False only while the last sync failed."""
        return self.last_status != self.STATUS_FAILED

    @property
    def safe_url(self):
        """The URL with its query string hidden.

        A Google "secret address" is a bearer credential: anyone holding the
        whole link can read that person's diary. So it is never rendered back to
        a page in full, not even to its owner.
        """
        from urllib.parse import urlparse
        try:
            parsed = urlparse(self.url)
            return f'{parsed.scheme}://{parsed.netloc}{parsed.path[:40]}…'
        except Exception:
            return '(hidden)'


class ExternalEvent(models.Model):
    """One occurrence imported from a :class:`CalendarSubscription`.

    Occurrences, not rules: recurrence is expanded at import time inside a fixed
    window (see :mod:`apps.livesessions.ics`), so rendering a month never has to
    evaluate an RRULE. Each sync replaces the window wholesale, which keeps a
    deleted or moved meeting from lingering.

    ``busy`` is False for an event marked TRANSPARENT — shown, but not treated
    as busy.
    """

    subscription = models.ForeignKey(CalendarSubscription, on_delete=models.CASCADE,
                                     related_name='events')
    uid = models.CharField(max_length=255, blank=True)
    title = models.CharField(max_length=300)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=300, blank=True)
    busy = models.BooleanField(default=True)

    class Meta:
        """Chronological, indexed for month rendering per subscription."""
        ordering = ['start']
        indexes = [models.Index(fields=['subscription', 'start'], name='livesess_ext_sub_start')]
        verbose_name = 'Imported calendar event'

    def __str__(self):
        """Title and local start time."""
        return f'{self.title} · {self.start:%Y-%m-%d %H:%M}'


class SessionReminder(models.Model):
    """One reminder actually sent, for one person, at one lead time.

    The unique constraint is the point: the job re-examines the same sessions
    every few minutes, so "already sent?" must be a database question.
    """

    meeting = models.ForeignKey('communication.MeetingRoom', on_delete=models.CASCADE,
                                related_name='reminders_sent')
    recipient = models.ForeignKey(django_settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                  related_name='session_reminders')
    lead_minutes = models.PositiveIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)
    emailed = models.BooleanField(default=False)

    class Meta:
        """Newest first; one row per meeting, recipient and lead time."""
        ordering = ['-sent_at']
        constraints = [
            models.UniqueConstraint(fields=['meeting', 'recipient', 'lead_minutes'],
                                    name='uniq_session_reminder_per_lead'),
        ]
        indexes = [models.Index(fields=['meeting', 'lead_minutes'], name='livesess_rem_meet_lead')]

    def __str__(self):
        """Recipient, meeting and lead time in minutes."""
        return f'{self.recipient} · {self.meeting} · {self.lead_minutes}m'



class YouTubePlaylist(models.Model):
    """A playlist read off the channel, plus where its videos should appear.

    The mapping fields (``institution``, ``programme``, ``programme_module``,
    ``cohort``) stay blank until a staff member assigns them; scanning never
    overwrites them. The ``show_*`` flags choose where mapped videos appear.
    """

    youtube_id = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True)
    item_count = models.PositiveIntegerField(default=0)

    institution = models.ForeignKey('learning.Institution', null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name='youtube_playlists')
    programme = models.ForeignKey('learning.Programme', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='youtube_playlists')
    programme_module = models.ForeignKey('learning.ProgrammeModule', null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name='youtube_playlists')
    cohort = models.ForeignKey('learning.Cohort', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='youtube_playlists')

    show_past_sessions = models.BooleanField(default=True)
    show_my_programme = models.BooleanField(default=True)
    show_schedule = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Alphabetical by title."""
        ordering = ['title']

    def __str__(self):
        """The playlist title, or its YouTube id when untitled."""
        return self.title or self.youtube_id

    @property
    def is_mapped(self):
        """True once staff have assigned both a module and a cohort."""
        return bool(self.programme_module_id and self.cohort_id)

    @property
    def url(self):
        """The playlist on youtube.com."""
        return f'https://www.youtube.com/playlist?list={self.youtube_id}'


class YouTubeVideo(models.Model):
    """One video inside a scanned playlist. Inherits its playlist's mapping; may
    optionally be attached to a specific lesson for the lesson player.

    ``lesson`` is that optional per-video attachment (surfaced in the lesson
    player); ``is_hidden`` lets a staff member hide one video.
    """

    youtube_id = models.CharField(max_length=32, unique=True)
    playlist = models.ForeignKey(YouTubePlaylist, on_delete=models.CASCADE, related_name='videos')
    title = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    thumbnail_url = models.URLField(max_length=500, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    position = models.PositiveIntegerField(default=0)

    lesson = models.ForeignKey('learning.Lesson', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='youtube_videos')
    is_hidden = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Playlist order, then newest first."""
        ordering = ['position', '-published_at']
        indexes = [models.Index(fields=['playlist', 'position'])]

    def __str__(self):
        """The video title, or its YouTube id when untitled."""
        return self.title or self.youtube_id

    @property
    def watch_url(self):
        """The video on youtube.com."""
        return f'https://www.youtube.com/watch?v={self.youtube_id}'

    @property
    def embed_url(self):
        """Privacy-enhanced (``youtube-nocookie.com``) embed address for the player."""
        return f'https://www.youtube-nocookie.com/embed/{self.youtube_id}'
