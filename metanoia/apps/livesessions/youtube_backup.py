"""Nightly YouTube backup of class recordings.

YouTube is a **backup**, not the player: students watch recordings from OneDrive
(the pipeline publishes those links straight away). Once a day, at 00:00 SAST,
the ``youtube`` timer runs :func:`run`, which copies up to six recordings that
are not on YouTube yet — oldest class first — and files each one into a playlist
named after its institution and programme ("Milpark Education — PGDA").

Six, because one backup costs 1,650 quota units (the upload plus filing it into
its playlist) and Google allows 10,000 a day; the count is enforced by the quota
check, not just the constant, so a lower quota in the settings row is honoured.
A recording that fails is retried the next night. Nothing here changes what
students see or sends them anything.

Midnight SAST is mid-afternoon in California, where the quota resets, so each
nightly run falls in its own quota day and always has the full allowance.

Module constants:

``MAX_PER_RUN``
    Most recordings backed up per nightly run.
``SCAN_RESERVE``
    Quota units the channel scan may still use after the backups (see
    :func:`run_scan`).
"""
import logging

from django.utils import timezone

logger = logging.getLogger('apps')

MAX_PER_RUN = 6
SCAN_RESERVE = 200


def playlist_title(meeting):
    """``Milpark Education — PGDA``: the institution and programme a class belongs to."""
    institution = meeting.resolved_institution
    programme = meeting.resolved_programme
    inst = getattr(institution, 'display_name', '') or getattr(institution, 'name', '')
    prog = getattr(programme, 'name', '') or getattr(programme, 'label', '')
    if inst and prog:
        return f'{inst} — {prog}'
    if inst or prog:
        return f'{inst or prog} — Live sessions'
    return 'Metanoia Group — Live sessions'


def pending():
    """Recordings already in OneDrive and not yet on YouTube, oldest class first.
    Ones that have never failed go before ones that failed last night."""
    from .models import SessionArtifact
    return (SessionArtifact.objects
            .filter(state=SessionArtifact.STATE_PUBLISHED, youtube_video_id='')
            .exclude(recording_item_id='')
            .select_related('meeting', 'meeting__module')
            .order_by('youtube_error', 'meeting__scheduled_start', 'pk'))


def backup_one(artifact, *, client=None):
    """Upload one recording and file it into its playlist. Returns an error, or ''.

    The video id is saved on ``artifact`` straight after the upload, before the
    playlist step: the upload is the expensive part, and a recording must never
    be uploaded twice because filing it into a playlist failed.
    """
    from apps.msteams import graph

    from . import pipeline, youtube
    from .models import LiveSessionSettings

    folder = pipeline.folder_ref(artifact)
    if not folder:
        return 'The OneDrive folder for this session is not known.'
    blob = graph.download_item(folder['drive_id'], artifact.recording_item_id)
    if not blob:
        return 'Could not download the recording from OneDrive.'

    meeting = artifact.meeting
    conf = LiveSessionSettings.load()
    video_id, error = youtube.upload(
        video_bytes=blob,
        title=pipeline._video_title(meeting),
        description=pipeline._video_description(meeting),
        privacy=conf.youtube_privacy,
        tags=pipeline._video_tags(meeting),
    )
    del blob
    if error:
        return error

    artifact.youtube_video_id = video_id
    artifact.youtube_uploaded_at = timezone.now()
    artifact.youtube_error = ''
    artifact.save(update_fields=['youtube_video_id', 'youtube_uploaded_at', 'youtube_error'])

    playlist_id, error = youtube.ensure_playlist(
        title=playlist_title(meeting), institution=meeting.resolved_institution,
        programme=meeting.resolved_programme, privacy=conf.youtube_privacy, client=client)
    if not error:
        error = youtube.add_to_playlist(playlist_id, video_id, client=client)
    if error:
        logger.warning('youtube backup: %s uploaded as %s but not filed: %s',
                       meeting, video_id, error)
    return ''


def run(limit=MAX_PER_RUN):
    """Back up to ``limit`` recordings. Returns a one-line summary for the job log."""
    from . import youtube
    from .models import LiveSessionSettings

    if not LiveSessionSettings.load().youtube_live:
        return 'YouTube backup is off (switched off in settings, or not connected).'

    done, failed = 0, 0
    for artifact in pending()[:limit]:
        if not youtube.quota_available():
            break
        error = backup_one(artifact)
        if error:
            failed += 1
            artifact.youtube_error = error[:300]
            artifact.save(update_fields=['youtube_error'])
            logger.warning('youtube backup: %s failed: %s', artifact.meeting, error)
            if 'quota' in error.lower():
                break
        else:
            done += 1
    left = pending().count()
    return f'Backed up {done} recording(s), {failed} failed, {left} still waiting.'


def run_scan():
    """Re-import the channel into the library — only with quota to spare after the
    backups, so a scan can never cost a night's sixth upload."""
    from django.core.management import call_command

    from . import youtube
    if youtube.is_configured() and youtube.quota_left() < SCAN_RESERVE:
        return 'Skipped: the quota is kept for tonight\'s backups.'
    call_command('scan_youtube', verbosity=0)
    return 'Channel scanned.'
