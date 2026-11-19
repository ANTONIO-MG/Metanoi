"""The after-class chain: finished meeting → filed, summarised, playable session.

Run by the ``run_session_pipeline`` job every few minutes; each tick advances
every non-terminal :class:`~apps.livesessions.models.SessionArtifact` and
stops. Teams publishes a recording minutes to hours after a call ends, so
polling is resumable, and a failure parks only one session.

The states, in order::

    waiting → polling → filing → summarising → uploading → published
                                    ↺ waits here (up to 48h after the class) for
                                      Teams Copilot's meeting insight
                  ↘ skipped (nothing was recorded / publishing is off)
                  ↘ failed  (same step failed MAX_ATTEMPTS times)

Students are never blocked on the end of the chain: the session is watchable
as soon as ``filing`` puts a OneDrive link on the record. YouTube is a backup
only (:mod:`apps.livesessions.youtube_backup`, nightly at 00:00): students
watch from OneDrive, so a session is published as soon as it is summarised and
the nightly run copies the recording to YouTube later.

Tuning constants:

* :data:`ANNOUNCE_WITHIN_DAYS` — students are told about a recording and
  summary only while the class is recent; older ones (backfills, very late
  Teams recordings) publish quietly.
* :data:`MAX_STEPS_PER_TICK` — steps one session may take in a single tick
  (each step is idempotent).
* :data:`RECENT_HOURS` — the quick run (every few minutes) only looks at
  classes this recent; older ones are left to the twice-daily run.
* :data:`RECENT_BACKOFF_MINUTES` — after a failed attempt the quick run waits
  this long × the attempts so far before trying that session again, so a slow
  OneDrive copy or a Graph hiccup cannot use up the session's ``MAX_ATTEMPTS``
  inside an hour.
* :data:`ATTENDANCE_RESYNC_HOURS` — while waiting for Copilot, attendance is
  re-synced from Graph at most this often.
"""

import logging

from django.utils import timezone

from apps.msteams import graph, onedrive
from core import clock

logger = logging.getLogger('apps')

ANNOUNCE_WITHIN_DAYS = 7
MAX_STEPS_PER_TICK = 6


def artifact_for(meeting):
    """Get-or-create the pipeline row for a session."""
    from .models import SessionArtifact
    artifact, _ = SessionArtifact.objects.get_or_create(meeting=meeting)
    return artifact


def enqueue_finished_sessions():
    """Open a pipeline row for every Teams session that has ended. Returns the count.

    Rows are opened in ``waiting``, not ``polling``: the waiting step asks "may
    this recording be published?". Skipping it would publish sessions with
    publishing off, including every class imported from Teams.
    """
    from apps.communication.models import MeetingRoom
    from .models import SessionArtifact

    ended = (MeetingRoom.objects
             .filter(provider=MeetingRoom.PROVIDER_TEAMS, is_active=True,
                     scheduled_end__lt=timezone.now())
             .exclude(teams_meeting_id='')
             .exclude(artifact__isnull=False))
    opened = 0
    for meeting in ended.iterator():
        SessionArtifact.objects.get_or_create(
            meeting=meeting, defaults={'state': SessionArtifact.STATE_WAITING})
        opened += 1
    return opened


RECENT_HOURS = 36
RECENT_BACKOFF_MINUTES = 30


def run(limit=25, *, recent_only=False, now=None):
    """Advance up to ``limit`` pending sessions.

    ``recent_only`` is the quick run: classes ended in the last
    :data:`RECENT_HOURS`, each backing off after a failure. The twice-daily run
    sweeps everything. Returns a short human summary (recorded as job output).

    Newest classes go first, so this week's class never waits behind a backlog
    of old ones. The quick run also includes any class still waiting for its
    Copilot summary, so the wait ends (and the fallback is written) on time
    rather than at the next twice-daily run. An exception in one session is
    logged and counted against that session only; it never stops the rest.
    """
    from .models import SessionArtifact

    now = now or timezone.now()
    opened = enqueue_finished_sessions()
    pending = (SessionArtifact.objects
               .exclude(state__in=[SessionArtifact.STATE_PUBLISHED,
                                   SessionArtifact.STATE_FAILED,
                                   SessionArtifact.STATE_SKIPPED])
               .select_related('meeting', 'meeting__module', 'meeting__host')
               .order_by('-meeting__scheduled_end', 'created_at'))
    if recent_only:
        from django.db.models import Q
        from .summaries import COPILOT_WAIT_HOURS
        pending = pending.filter(
            Q(meeting__scheduled_end__gte=now - timezone.timedelta(hours=RECENT_HOURS))
            | Q(state=SessionArtifact.STATE_SUMMARISING,
                meeting__scheduled_end__gte=now - timezone.timedelta(hours=COPILOT_WAIT_HOURS + 6)),
            meeting__scheduled_end__lte=now)
        pending = [a for a in pending[:limit * 4] if _due_for_quick_run(a, now)][:limit]
    else:
        pending = list(pending[:limit])

    advanced, failed = 0, 0
    for artifact in pending:
        try:
            moved = False
            for _ in range(MAX_STEPS_PER_TICK):
                if artifact.is_terminal or not step(artifact):
                    break
                moved = True
            advanced += int(moved)
        except Exception as exc:
            logger.exception('livesessions: pipeline step failed for %s', artifact.meeting)
            artifact.record_failure(exc)
            failed += 1
    return (f'{opened} opened, {advanced} advanced, {failed} failed '
            f'({len(pending)} examined)')


def _due_for_quick_run(artifact, now):
    """Whether the quick run should try ``artifact`` again at ``now``.

    A session waiting in ``summarising`` is skipped when Graph was asked for its
    Copilot summary within :data:`RECENT_BACKOFF_MINUTES`. Otherwise a session
    that has failed waits :data:`RECENT_BACKOFF_MINUTES` × its attempts so far.
    """
    from .models import SessionArtifact
    if (artifact.state == SessionArtifact.STATE_SUMMARISING and artifact.summary_checked_at
            and now - artifact.summary_checked_at < timezone.timedelta(minutes=RECENT_BACKOFF_MINUTES)):
        return False
    if not artifact.attempts or not artifact.last_attempt_at:
        return True
    wait = timezone.timedelta(minutes=RECENT_BACKOFF_MINUTES * artifact.attempts)
    return now - artifact.last_attempt_at >= wait


def step(artifact):
    """Advance one session by exactly one state. True if anything changed."""
    from .models import SessionArtifact

    handlers = {
        SessionArtifact.STATE_WAITING: _step_waiting,
        SessionArtifact.STATE_POLLING: _step_polling,
        SessionArtifact.STATE_FILING: _step_filing,
        SessionArtifact.STATE_SUMMARISING: _step_summarising,
        SessionArtifact.STATE_UPLOADING: _step_uploading,
    }
    handler = handlers.get(artifact.state)
    return bool(handler and handler(artifact))


def _step_waiting(artifact):
    """Nothing to do until the session is actually over."""
    from .models import LiveSessionSettings, SessionArtifact

    meeting = artifact.meeting
    if not meeting.has_ended:
        return False
    conf = LiveSessionSettings.load()
    if not (conf.publish_recordings and meeting.publish_recording):
        artifact.advance(SessionArtifact.STATE_SKIPPED)
        return True
    artifact.advance(SessionArtifact.STATE_POLLING)
    return True


def _step_polling(artifact):
    """Ask Graph whether Teams has published the recording yet.

    Does nothing on a dormant install (Graph not configured) so it resumes once
    switched on. As soon as a recording appears, Teams' own file is located so
    the class is playable before the copy into OneDrive. A class nobody recorded
    never produces artifacts, so polling gives up after
    ``POLL_GIVE_UP_HOURS`` and the row is skipped.
    """
    from .models import SessionArtifact

    meeting = artifact.meeting
    if not (meeting.is_teams and meeting.teams_meeting_id and meeting.organizer_upn):
        artifact.advance(SessionArtifact.STATE_SKIPPED)
        return True
    if not graph.is_configured():
        return False

    recordings = session_recordings(meeting)
    transcripts = session_transcripts(meeting)
    if recordings:
        locate_teams_recording(artifact, recordings)
    if recordings or transcripts:
        artifact.advance(SessionArtifact.STATE_FILING)
        return True

    ended = meeting.scheduled_end or meeting.scheduled_start
    if ended and (timezone.now() - ended).total_seconds() > artifact.POLL_GIVE_UP_HOURS * 3600:
        logger.info('livesessions: no artifacts for %s after %sh — skipping',
                    meeting, artifact.POLL_GIVE_UP_HOURS)
        artifact.advance(SessionArtifact.STATE_SKIPPED,
                         last_error='Teams published no recording or transcript.')
        return True
    return False


def _step_filing(artifact):
    """Build the OneDrive folder and move the recording + transcript into it.

    A week-pinned session files into that week's buckets (``Recordings`` /
    ``Session summaries``); a session with no week keeps the flat dated folder.
    Filed in order: the recording, the Microsoft transcript (verbatim VTT, next
    to the video), then the thumbnail — generated here so it exists before
    YouTube needs it.

    A copy still in flight keeps what is filed so far and stays in ``filing``.
    The next run finds the copy by name (it is never started twice); a copy that
    never lands parks the session as failed after ``MAX_ATTEMPTS`` runs.
    """
    from .models import SessionArtifact

    meeting = artifact.meeting
    folder = onedrive.ensure_session_folder(meeting)
    if not folder:
        artifact.record_failure('Could not create the OneDrive session folder.')
        return False

    artifact.folder_id = folder['folder_id']
    artifact.folder_path = folder['path']
    artifact.folder_web_url = folder['web_url']
    artifact.drive_owner_upn = folder['owner_upn']

    rec_folder = folder
    if meeting.week_id:
        rec_folder = onedrive.ensure_subfolder(folder, 'Recordings') or folder

    recordings = session_recordings(meeting)
    if recordings and not artifact.recording_item_id:
        item = onedrive.file_recording(meeting, rec_folder, recordings[0])
        if item and item.get('id'):
            artifact.recording_item_id = item['id']
            artifact.recording_drive_id = rec_folder['drive_id']
            artifact.recording_web_url = item.get('webUrl', '')
            artifact.recording_bytes = int(item.get('size') or 0)
            artifact.recording_filed_at = timezone.now()
            artifact.recording_share_url = onedrive.anonymous_link(folder, item['id'])

    if not artifact.transcript_item_id:
        vtt = transcript_text(meeting)
        if vtt:
            item = onedrive.put_text(rec_folder, f'{onedrive.artifact_stem(meeting)}-transcript.vtt',
                                     vtt, 'text/vtt')
            if item:
                artifact.transcript_item_id = item.get('id', '')
                artifact.transcript_web_url = item.get('webUrl', '')
    if artifact.transcript_item_id and not artifact.transcript_share_url:
        artifact.transcript_share_url = onedrive.anonymous_link(rec_folder, artifact.transcript_item_id) or ''

    _ensure_thumbnail(artifact, folder)

    if recordings and not artifact.recording_item_id:
        artifact.save()
        artifact.record_failure('Waiting for OneDrive to finish copying the recording.')
        return False

    artifact.advance(SessionArtifact.STATE_SUMMARISING)
    return True


def _step_summarising(artifact):
    """Reconcile attendance, wait for Copilot's summary, and publish to the schedule.

    The recording is scheduled and announced on the first pass. The summary
    waits for Teams Copilot's insight (see :mod:`.summaries`) for up to
    ``COPILOT_WAIT_HOURS`` after the class, then falls back to the transcript.

    Attendance (and the raw recording URL) comes from the after-class sync, run
    without its own recap because the class gets one summary, from
    :mod:`.summaries`. While waiting for Copilot it re-syncs at most every
    :data:`ATTENDANCE_RESYNC_HOURS`; the first pass always syncs
    (``announced_at`` is set at its end). The published summary is also the
    fallback the class receives if the full AI follow-up pack cannot be written;
    it is filed into the week's "Session summaries" bucket when the session has
    a week. People are told about the recording now, not after YouTube, because
    the OneDrive link already plays.
    """
    from apps.msteams import services as teams_services
    from .models import SessionArtifact

    meeting = artifact.meeting

    if artifact.announced_at is None or _attendance_sync_due(meeting):
        try:
            teams_services.sync_meeting(meeting, force=True, recap=False)
        except Exception:
            logger.exception('livesessions: sync_meeting failed for %s', meeting)

    from . import summaries
    if not artifact.summary_published_at:
        try:
            summaries.process(meeting)
            artifact.refresh_from_db()
        except Exception:
            logger.exception('livesessions: summary failed for %s', meeting)

    if not artifact.summary_item_id and artifact.folder_id:
        folder = folder_ref(artifact)
        if folder and meeting.week_id:
            folder = onedrive.ensure_subfolder(folder, 'Session summaries') or folder
        markdown = recap_markdown(meeting)
        if markdown and folder:
            item = onedrive.put_text(folder, f'{onedrive.artifact_stem(meeting)}-summary.md',
                                     markdown, 'text/markdown')
            if item:
                artifact.summary_item_id = item.get('id', '')
                artifact.summary_web_url = item.get('webUrl', '')

    publish_to_schedule(artifact)

    _announce_recording(artifact)

    if _waiting_for_copilot(artifact, meeting):
        artifact.save()
        return False

    notify_one_on_one_attendance(meeting)

    artifact.advance(SessionArtifact.STATE_PUBLISHED)
    return True


ATTENDANCE_RESYNC_HOURS = 6


def _attendance_sync_due(meeting, now=None):
    """Whether attendance should be re-synced from Graph for ``meeting``.

    True when it has never been synced, was last synced before the class ended,
    or was synced more than :data:`ATTENDANCE_RESYNC_HOURS` ago.
    """
    synced = getattr(meeting, 'artifacts_synced_at', None)
    ended = meeting.scheduled_end or meeting.scheduled_start
    if not synced or (ended and synced < ended):
        return True
    return (now or timezone.now()) - synced >= timezone.timedelta(hours=ATTENDANCE_RESYNC_HOURS)


def _waiting_for_copilot(artifact, meeting):
    """True while the summary step should hold the session for Copilot's insight.

    Only for classes the summary job publishes (not private sessions or 1:1s),
    only while no summary is published, and only while the summary job itself
    says it is waiting (it stops at ``COPILOT_WAIT_HOURS`` or on a 403).
    """
    from apps.communication.models import MeetingRoom
    from . import summaries
    if artifact.summary_published_at or meeting.audience == MeetingRoom.AUDIENCE_PRIVATE:
        return False
    return (artifact.summary_note or '').startswith(summaries.COPILOT_WAIT_NOTE)


def _step_uploading(artifact):
    """Retired: uploads moved to the nightly backup (youtube_backup.run). A row
    still parked in UPLOADING from before the change is simply published; the
    backup picks its recording up."""
    from .models import SessionArtifact
    artifact.advance(SessionArtifact.STATE_PUBLISHED)
    _announce_recording(artifact)
    return True


def publish_to_schedule(artifact):
    """Put the recording on the module's schedule as a ``recording`` ModuleMaterial.

    Idempotent — re-running updates the same row.
    """
    meeting = artifact.meeting
    if not meeting.module_id or not artifact.is_watchable:
        return None

    from apps.learning.models import ModuleMaterial

    phase = phase_for(meeting)
    if phase is None:
        return None

    return quiet_upsert(
        ModuleMaterial,
        {'phase': phase, 'kind': ModuleMaterial.KIND_RECORDING, 'meeting': meeting},
        {
            'week': meeting.week if meeting.week_id and meeting.week.phase_id == phase.pk else None,
            'title': f'Recording — {meeting.title}',
            'description': meeting.description or '',
            'url': artifact.watch_url,
        },
    )


def quiet_upsert(model, lookup, defaults):
    """``update_or_create`` that raises no automatic "new item" notice.

    The class is told about a session once, by the pipeline itself; a generic
    notice per material row would repeat it — and would reach every intake on
    the module rather than the one that was taught.
    """
    obj = model.objects.filter(**lookup).first() or model(**lookup)
    for key, value in defaults.items():
        setattr(obj, key, value)
    obj._quiet = True
    obj.save()
    return obj


def phase_for(meeting):
    """Which preparation block this recording belongs under.

    The session's scheduled week, else the block its date falls inside, else
    the module's most recent block. Each intake runs its own blocks, so a
    cohort's class is never filed under another intake's block.
    """
    if meeting.week_id:
        return meeting.week.phase
    phases = meeting.module.phases.filter(is_active=True)
    if meeting.cohort_id:
        from django.db.models import Q
        phases = phases.filter(Q(cohort_id=meeting.cohort_id) | Q(cohort__isnull=True)).order_by('cohort', 'order', 'id')
    phases = list(phases.order_by('order', 'id') if not meeting.cohort_id else phases)
    if not phases:
        return None
    when = (meeting.scheduled_start or timezone.now()).date()
    for phase in phases:
        starts, ends = phase.starts_on, phase.ends_on or phase.assessment_date
        if starts and ends and starts <= when <= ends:
            return phase
    return phases[-1]


def notify_one_on_one_attendance(meeting):
    """E-mail the attendee their own attendance record for a 1:1."""
    if not meeting.is_one_on_one:
        return False
    session = getattr(meeting, 'class_session', None)
    if session is None:
        return False

    from apps.communication import services as comm
    from apps.communication.models import Attendance

    invitee = meeting.participants.exclude(role='host').select_related('user').first()
    if invitee is None or invitee.user is None:
        return False

    record = Attendance.objects.filter(session=session, student=invitee.user).first()
    if record is None:
        return False

    comm.notify(
        invitee.user, category='meetings', level='info',
        verb='session attendance',
        title=f'Your session record — {meeting.title}',
        body=(f'You attended {record.minutes_attended} minute(s) of '
              f'“{meeting.title}” ({record.get_status_display()}). '
              f'The recording and notes are on the session page.'),
        url=meeting.get_absolute_url(), meeting=meeting, email=True,
    )
    return True


def folder_ref(artifact):
    """Rebuild the ``{'drive_id', 'folder_id', ...}`` dict from stored ids (resumed ticks)."""
    if not artifact.folder_id or not artifact.drive_owner_upn:
        return None
    drive_id = graph.get_drive_id(artifact.drive_owner_upn)
    if not drive_id:
        return None
    return {'drive_id': drive_id, 'folder_id': artifact.folder_id,
            'path': artifact.folder_path, 'web_url': artifact.folder_web_url,
            'owner_upn': artifact.drive_owner_upn}


def locate_teams_recording(artifact, recordings=None):
    """Find (and remember) Teams' own file for this session's recording.

    Lets the class be watched from the moment Teams publishes it. Returns True
    when the session has a recording file it can play.
    """
    if artifact.teams_item_id:
        return True
    meeting = artifact.meeting
    for recording in (recordings if recordings is not None else session_recordings(meeting)):
        item_id, drive_id = onedrive._recording_item_ref(recording)
        if not item_id:
            found = onedrive.find_teams_recording(meeting, recording)
            if found:
                item_id, drive_id = found['id'], found['drive_id']
        if item_id and drive_id:
            artifact.teams_item_id, artifact.teams_drive_id = item_id, drive_id
            artifact.save(update_fields=['teams_item_id', 'teams_drive_id', 'updated_at'])
            return True
    return False


def session_recordings(meeting):
    """This occurrence's Teams recordings (see :func:`summaries.for_session`)."""
    from .summaries import for_session
    return for_session(graph.list_recordings(meeting.organizer_upn, meeting.teams_meeting_id), meeting)


def session_transcripts(meeting):
    """This occurrence's Teams transcripts."""
    from .summaries import for_session
    return for_session(graph.list_transcripts(meeting.organizer_upn, meeting.teams_meeting_id), meeting)


def transcript_text(meeting):
    """This occurrence's transcripts, concatenated as VTT."""
    chunks = []
    for transcript in session_transcripts(meeting):
        content = graph.get_transcript_content(meeting.organizer_upn, meeting.teams_meeting_id,
                                               transcript.get('id'))
        if content:
            chunks.append(content)
    return '\n\n'.join(chunks)


def is_recent(meeting, now=None):
    """Still recent enough that the class should be told about it."""
    held = meeting.scheduled_end or meeting.scheduled_start
    if held is None:
        return False
    return ((now or timezone.now()) - held).days < ANNOUNCE_WITHIN_DAYS


def recap_markdown(meeting):
    """The recap ``sync_meeting`` filed, as markdown."""
    try:
        from apps.ai_assistant.models import AiReport
        report = (AiReport.objects.filter(linked_ref=f'meeting:{meeting.pk}')
                  .order_by('-created_at').first())
        return getattr(report, 'content', '') or ''
    except Exception:
        return ''


def _ensure_thumbnail(artifact, folder):
    """Draw the session card if it is missing, save it locally and to OneDrive."""
    from django.core.files.base import ContentFile
    from . import thumbnails

    meeting = artifact.meeting
    blob = _thumbnail_bytes(meeting)
    if blob is None:
        blob = thumbnails.render(meeting)
        if not blob:
            return
        try:
            meeting.thumbnail.save(thumbnails.filename_for(meeting), ContentFile(blob), save=True)
        except Exception:
            logger.warning('livesessions: could not store the thumbnail for %s', meeting, exc_info=True)

    if folder and not artifact.thumbnail_item_id:
        item = onedrive.put_bytes(folder, f'{onedrive.artifact_stem(meeting)}-thumbnail.jpg',
                                  blob, 'image/jpeg')
        if item:
            artifact.thumbnail_item_id = item.get('id', '')


def _thumbnail_bytes(meeting):
    """The stored thumbnail's bytes, or None if there isn't one."""
    if not meeting.thumbnail:
        return None
    try:
        with meeting.thumbnail.open('rb') as fh:
            return fh.read()
    except Exception:
        return None


def _video_title(meeting):
    """``MILPARK PGDA | FREP — Week 3: IAS 12`` — searchable, and unique per class."""
    bits = [meeting.title]
    if meeting.module_id:
        bits.insert(0, meeting.module.label)
    elif meeting.resolved_programme is not None:
        bits.insert(0, meeting.resolved_programme.label)
    return ' — '.join(b for b in bits if b)[:100]


def _video_description(meeting):
    """The session details, so the YouTube page stands alone if it is opened directly."""
    lines = []
    if meeting.description:
        lines += [meeting.description.strip(), '']
    for label, value in (
        ('Institution', getattr(meeting.resolved_institution, 'display_name', '')),
        ('Programme', getattr(meeting.resolved_programme, 'label', '')),
        ('Module', meeting.module.display_name if meeting.module_id else ''),
        ('Covers', meeting.week.display_title if meeting.week_id else ''),
        ('Prepares for', meeting.calendar_event.title if meeting.calendar_event_id else ''),
        ('Held', f'{clock.local(meeting.scheduled_start):%A %d %B %Y, %H:%M} SAST'
                 if meeting.scheduled_start else ''),
    ):
        if value:
            lines.append(f'{label}: {value}')
    lines += ['', 'Recorded on Metanoia Group. This video is unlisted and intended for '
                  'registered candidates on this module.']
    return '\n'.join(lines)


def _video_tags(meeting):
    """YouTube tags: institution, programme and module codes plus the module name."""
    tags = []
    institution = meeting.resolved_institution
    programme = meeting.resolved_programme
    if institution is not None:
        tags.append(institution.code)
    if programme is not None:
        tags.append(programme.code)
    if meeting.module_id:
        tags += [meeting.module.code, meeting.module.display_name]
    return [t for t in tags if t]


def _announce_recording(artifact):
    """Tell the class the recording (and transcript) is up — once, in-app.

    The e-mail comes with the session pack (:mod:`.ai_followup`), which carries
    the same links, so a class gets one e-mail per session rather than two.
    """
    from apps.communication import services as comm
    from .audience import class_students

    meeting = artifact.meeting
    if artifact.announced_at or not artifact.is_watchable:
        return 0
    sent = 0
    if is_recent(meeting):
        url = meeting.get_absolute_url()
        has_transcript = artifact.has_transcript
        title = (f'Recording and transcript available — {meeting.title}' if has_transcript
                 else f'Recording available — {meeting.title}')
        body = ('The recording' + (' and the transcript' if has_transcript else '') +
                ' of this session are on its page. You can watch it in full whenever suits '
                'you; the summary, next agenda and action items follow shortly.')
        people = list(class_students(meeting))
        if meeting.host_id and meeting.host not in people:
            people.append(meeting.host)
        for user in people:
            if comm.notify(user, category='meetings', level='success',
                           verb='recording available', title=title[:200], body=body,
                           url=url, meeting=meeting, email=False):
                sent += 1
    artifact.announced_at = timezone.now()
    artifact.save(update_fields=['announced_at', 'updated_at'])
    return sent
