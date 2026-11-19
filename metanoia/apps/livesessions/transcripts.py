"""A session's transcript, fetched through Graph and shown on our own page.

Microsoft serves a filed ``.vtt`` behind a sign-in page that students without a
Microsoft account cannot pass, so the platform fetches it with the app's
credentials (after checking access) and renders it via
:func:`views.session_transcript`.

Nothing here is public and nothing is stored beyond a short cache: the file
stays in OneDrive, where staff manage it.

``CACHE_SECONDS`` is how long a fetched transcript is kept (six hours).
Transcripts do not change once filed; the cache only stops a class of thirty
opening the page from becoming thirty Graph calls.
"""

import logging
import re

logger = logging.getLogger('apps')

CACHE_SECONDS = 6 * 60 * 60

_TIMING = re.compile(r'^(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3})\s+-->\s+\S+')
_VOICE = re.compile(r'<v\s+([^>]+)>(.*?)(?:</v>|$)', re.S)
_TAG = re.compile(r'<[^>]+>')


def fetch_vtt(artifact):
    """The session's transcript as VTT text, or ''.

    The filed copy first (what staff see in the session folder); failing that,
    Teams' own transcript of this occurrence, for a class the pipeline has not
    filed yet. Cached, because neither changes once it exists.

    A Graph failure while fetching the Teams transcript is logged and treated
    as "try later" (an empty result), never as a server error.
    """
    from django.core.cache import cache
    from apps.msteams import graph

    if not graph.is_configured():
        return ''
    meeting = artifact.meeting
    key = f'session-transcript:{meeting.pk}:{artifact.transcript_item_id or "teams"}'
    text = cache.get(key)
    if text is not None:
        return text

    text = ''
    if artifact.transcript_item_id:
        drive_id = artifact.recording_drive_id or graph.get_drive_id(
            artifact.drive_owner_upn or meeting.organizer_upn)
        if drive_id:
            blob = graph.download_item(drive_id, artifact.transcript_item_id)
            if blob:
                text = blob.decode('utf-8', errors='replace')
    if not text and meeting.organizer_upn and meeting.teams_meeting_id:
        from .pipeline import transcript_text
        try:
            text = transcript_text(meeting) or ''
        except Exception:
            logger.exception('livesessions: could not fetch the Teams transcript for %s', meeting)
            text = ''
    if text:
        cache.set(key, text, CACHE_SECONDS)
    return text


def cues(vtt):
    """VTT → ``[{'start', 'speaker', 'text'}]``, consecutive lines by one speaker merged.

    Teams writes a cue every sentence or two; merging turns them into readable
    paragraphs, keeping the first cue's time.
    """
    out = []
    start = ''
    for block in re.split(r'\n\s*\n', (vtt or '').replace('\r\n', '\n')):
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if not lines or lines[0].startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
            continue
        body = []
        for line in lines:
            timing = _TIMING.match(line)
            if timing:
                start = _short_time(timing.group('start'))
                continue
            if not body and '-->' not in line and _looks_like_cue_id(line, lines):
                continue
            body.append(line)
        if not body:
            continue
        joined = ' '.join(body)
        voice = _VOICE.search(joined)
        speaker = voice.group(1).strip() if voice else ''
        text = _TAG.sub('', voice.group(2) if voice else joined).strip()
        if not speaker:
            plain = re.match(r'^([A-Z][\w .\'-]{1,40}):\s+(.*)$', text)
            if plain:
                speaker, text = plain.group(1).strip(), plain.group(2).strip()
        if not text:
            continue
        if out and out[-1]['speaker'] == speaker:
            out[-1]['text'] += ' ' + text
        else:
            out.append({'start': start, 'speaker': speaker, 'text': text})
    return out


def _looks_like_cue_id(line, lines):
    """A cue identifier is the line before the timing line (Teams uses GUIDs)."""
    return len(lines) > 1 and bool(_TIMING.match(lines[1])) and line == lines[0]


def _short_time(stamp):
    """``00:03:07.120`` → ``3:07``; ``01:02:03.000`` → ``1:02:03``."""
    parts = stamp.replace(',', '.').split('.')[0].split(':')
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        parts = [0] + parts
    hours, minutes, seconds = parts
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes}:{seconds:02d}'
