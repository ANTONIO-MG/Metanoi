"""Point stored class links at our own domain, and take AI error text out.

    manage.py repair_session_links                 # dry run: counts and samples
    manage.py repair_session_links --apply         # rewrite the rows
    manage.py repair_session_links --apply --resummarise   # …and rebuild broken summaries

Older rows hold OneDrive / SharePoint links (a transcript link lands on a
Microsoft sign-in page). This rewrites the stored ones:

* **Notifications** — each SharePoint URL is matched to the session it belongs
  to (by the artifact's stored share / web URLs, else by the notification's own
  session and the label in front of the link) and replaced with the matching
  page on our domain: the session player, the transcript page, the summary.
* **Lesson resources** and **module materials** — the same, as site paths.
* **AI failure text** — "The admin AI hit an error: Error code: 400 …" that was
  filed as a class summary while the Anthropic account was out of credit. It is
  taken out of notifications, the session recap and the summary lesson, and
  replaced with a neutral "not ready yet" line. ``--resummarise`` then rebuilds
  those summaries from the transcript (this calls Graph, and Claude if it is
  available again — the free extractive summary otherwise).

A link that cannot be matched to a session is listed and left as it is. The
default is a dry run; nothing is written without ``--apply``.

``LINK_RX`` finds a SharePoint / OneDrive URL in free text; it stops at
whitespace, quotes and brackets, and trailing sentence punctuation is trimmed
separately. ``FAILURE_HTML_RX`` matches the paragraph an AI failure was filed as
inside lesson HTML.
"""

import re

from django.core.management.base import BaseCommand
from django.db import transaction

LINK_RX = re.compile(r'https?://[^\s<>"\'()\[\]]*(?:sharepoint\.com|1drv\.ms)[^\s<>"\'()\[\]]*', re.I)
FAILURE_HTML_RX = re.compile(r'<p>[^<]*The admin AI hit an error[^<]*</p>', re.S)


class Command(BaseCommand):
    """Management command: rewrite stored SharePoint class links to in-domain pages and remove AI error text."""
    help = 'Rewrite stored SharePoint class links to in-domain pages and remove AI error text.'

    def add_arguments(self, parser):
        """Add ``--apply``, ``--dry-run``, ``--resummarise`` and ``--samples``."""
        parser.add_argument('--apply', action='store_true', help='Write the changes (default: dry run).')
        parser.add_argument('--dry-run', action='store_true', help='Report only (the default).')
        parser.add_argument('--resummarise', action='store_true',
                            help='With --apply: rebuild each summary that held AI error text.')
        parser.add_argument('--samples', type=int, default=3, help='Examples to print per group.')

    def handle(self, *args, **opts):
        """Run every repair inside one transaction, then list the unmatched links.

        With ``--apply --resummarise`` the summaries that held AI error text are rebuilt
        afterwards; otherwise their meeting ids are printed.
        """
        apply = opts['apply'] and not opts['dry_run']
        self.samples = opts['samples']
        self.stdout.write('APPLY' if apply else 'DRY RUN — nothing is written (use --apply)')
        self.index = self._index()
        self.unmatched = []

        with transaction.atomic():
            self._notifications(apply)
            self._lesson_resources(apply)
            self._module_materials(apply)
            broken = self._ai_failures(apply)

        if self.unmatched:
            self.stdout.write(self.style.WARNING(f'\nUnmatched links left untouched: {len(self.unmatched)}'))
            for where, url in self.unmatched[:max(self.samples * 4, 10)]:
                self.stdout.write(f'  {where}: {url}')
        else:
            self.stdout.write('\nUnmatched links left untouched: 0')

        if apply and opts['resummarise'] and broken:
            self._resummarise(broken)
        elif broken:
            self.stdout.write(f'{len(broken)} session(s) can be rebuilt with --apply --resummarise: '
                              + ', '.join(str(pk) for pk in sorted(broken)))

    def _index(self):
        """``{stored Microsoft URL: (artifact, kind)}`` over every session."""
        from apps.livesessions.models import SessionArtifact
        index = {}
        fields = (('recording_share_url', 'recording'), ('recording_web_url', 'recording'),
                  ('transcript_share_url', 'transcript'), ('transcript_web_url', 'transcript'),
                  ('summary_share_url', 'summary'), ('summary_web_url', 'summary'),
                  ('folder_web_url', 'session'))
        for artifact in SessionArtifact.objects.select_related('meeting'):
            for field, kind in fields:
                url = getattr(artifact, field)
                if url:
                    index.setdefault(_key(url), (artifact, kind))
        return index

    def _target(self, url, *, meeting=None, context=''):
        """The in-domain site path for a Microsoft URL, or None.

        The stored artifact URLs first. Failing that — a link minted later, or
        stored before the field was — the notification's own session, with the
        kind read off the label in front of the link ("Read the transcript:").
        """
        hit = self.index.get(_key(url))
        if hit:
            return _path_for(*hit)
        artifact = getattr(meeting, 'artifact', None) if meeting is not None else None
        if artifact is None:
            return None
        label = context.lower()
        kind = ('transcript' if 'transcript' in label else 'summary' if 'summary' in label
                else 'recording' if ('recording' in label or 'watch' in label) else None)
        return _path_for(artifact, kind) if kind else None

    def _rewrite_text(self, text, where, *, meeting=None):
        """``text`` with every matchable Microsoft link replaced by an absolute in-domain URL."""
        from core.utils import absolute_url
        out, last, changed = [], 0, 0
        for match in LINK_RX.finditer(text or ''):
            url = match.group(0).rstrip('.,;:!?')
            end = match.start() + len(url)
            path = self._target(url, meeting=meeting, context=text[max(0, match.start() - 60):match.start()])
            out.append(text[last:match.start()])
            if path:
                out.append(absolute_url(path))
                changed += 1
            else:
                out.append(url)
                self.unmatched.append((where, url))
            last = end
        out.append((text or '')[last:])
        return ''.join(out), changed

    def _notifications(self, apply):
        """Rewrite the SharePoint links in notification bodies, summaries and URLs."""
        from apps.communication.models import Notification
        qs = (Notification.objects
              .filter(_link_q('body') | _link_q('summary') | _link_q('url'))
              .select_related('meeting__artifact'))
        rows = links = 0
        shown = 0
        for note in qs.iterator():
            where = f'Notification {note.pk}'
            body, n_body = self._rewrite_text(note.body, where, meeting=note.meeting)
            summary, n_summary = self._rewrite_text(note.summary, where, meeting=note.meeting)
            url = note.url
            if url and LINK_RX.fullmatch(url):
                path = self._target(url, meeting=note.meeting)
                if path:
                    url = path
                else:
                    self.unmatched.append((f'{where} url', url))
            if (body, summary, url) == (note.body, note.summary, note.url):
                continue
            rows += 1
            links += n_body + n_summary + (url != note.url)
            if shown < self.samples:
                shown += 1
                self.stdout.write(f'  {where} ({note.title[:60]}):\n    {_first_change(note.body, body)}')
            if apply:
                note.body, note.summary, note.url = body, summary, url
                note.save(update_fields=['body', 'summary', 'url'])
        self.stdout.write(f'Notifications with SharePoint links: {qs.count()} · '
                          f'rewritten {rows} ({links} links)')

    def _lesson_resources(self, apply):
        """Rewrite the SharePoint links on lesson resources as site paths.

        The old "summary" resource on a summary lesson is retitled and pointed at the
        session page instead: the lesson *is* the summary, so a link to itself is no
        use — what a reader wants from it is the recording and the transcript.
        """
        from apps.learning.models import LessonResource
        from apps.livesessions.models import SessionArtifact
        from apps.livesessions.summaries import OLD_SUMMARY_RESOURCE_TITLE, SESSION_RESOURCE_TITLE

        by_lesson = {a.summary_lesson_id: a for a in
                     SessionArtifact.objects.exclude(summary_lesson=None).select_related('meeting')}
        qs = LessonResource.objects.filter(_link_q('url'))
        rows = shown = 0
        for resource in qs.iterator():
            artifact = by_lesson.get(resource.lesson_id)
            title = resource.title
            if artifact is not None and resource.title == OLD_SUMMARY_RESOURCE_TITLE:
                path, title = artifact.meeting.get_absolute_url(), SESSION_RESOURCE_TITLE
            else:
                path = self._target(resource.url, meeting=getattr(artifact, 'meeting', None),
                                    context=resource.title)
            if not path:
                self.unmatched.append((f'LessonResource {resource.pk} (lesson {resource.lesson_id})',
                                       resource.url))
                continue
            rows += 1
            if shown < self.samples:
                shown += 1
                self.stdout.write(f'  LessonResource {resource.pk} “{resource.title}” → “{title}” {path}')
            if apply:
                resource.url, resource.title = path, title
                resource.save(update_fields=['url', 'title'])
        self.stdout.write(f'Lesson resources with SharePoint links: {qs.count()} · rewritten {rows}')

    def _module_materials(self, apply):
        """Rewrite the SharePoint links on module materials as site paths.

        A recording material points at the session player. Saves are quiet
        (``_quiet``) because a repaired link is not news.
        """
        from apps.learning.models import ModuleMaterial
        qs = ModuleMaterial.objects.filter(_link_q('url')).select_related('meeting__artifact')
        rows = 0
        for material in qs.iterator():
            artifact = getattr(material.meeting, 'artifact', None) if material.meeting_id else None
            path = None
            if artifact is not None and material.kind == ModuleMaterial.KIND_RECORDING:
                path = artifact.watch_url or material.meeting.get_absolute_url()
            path = path or self._target(material.url, meeting=material.meeting, context=material.title)
            if not path:
                self.unmatched.append((f'ModuleMaterial {material.pk} “{material.title}”', material.url))
                continue
            rows += 1
            if rows <= self.samples:
                self.stdout.write(f'  ModuleMaterial {material.pk} “{material.title}” → {path}')
            if apply:
                material.url = path
                material._quiet = True
                material.save(update_fields=['url'])
        self.stdout.write(f'Module materials with SharePoint links: {qs.count()} · rewritten {rows}')

    def _ai_failures(self, apply):
        """Take filed AI error text out of what students read. Returns the meeting ids.

        With ``--apply`` the affected artifacts' Claude summary source is cleared,
        so that a rebuilt summary can replace them: a Claude-ranked source would
        otherwise outrank the extractive summary a rebuild can now produce.
        """
        from apps.ai_assistant.models import AiReport
        from apps.communication.models import Notification
        from apps.learning.models import LessonBlock
        from apps.livesessions.ai_followup import SUMMARY_PENDING, without_ai_failure
        from apps.livesessions.models import SessionArtifact

        marker = 'The admin AI hit an error'
        broken = set()

        notes = Notification.objects.filter(body__contains=marker) | Notification.objects.filter(summary__contains=marker)
        shown = 0
        for note in notes.iterator():
            if shown < self.samples:
                shown += 1
                self.stdout.write(f'  Notification {note.pk} ({note.title[:60]}): '
                                  f'{_excerpt(note.body, marker)}')
            if note.meeting_id:
                broken.add(note.meeting_id)
            if apply:
                note.body = without_ai_failure(note.body)
                note.summary = without_ai_failure(note.summary)
                note.save(update_fields=['body', 'summary'])
        self.stdout.write(f'Notifications with AI error text: {notes.count()}')

        reports = AiReport.objects.filter(content__contains=marker, linked_ref__startswith='meeting:')
        for report in reports.iterator():
            meeting_id = report.linked_ref.split(':', 1)[1]
            if meeting_id.isdigit():
                broken.add(int(meeting_id))
            if apply:
                report.content = without_ai_failure(report.content)
                report.used_llm = False
                report.save(update_fields=['content', 'used_llm'])
        self.stdout.write(f'Session recaps with AI error text: {reports.count()}')

        blocks = LessonBlock.objects.filter(data__icontains=marker)
        lessons = {a.summary_lesson_id: a.meeting_id for a in
                   SessionArtifact.objects.exclude(summary_lesson=None)}
        for block in blocks.iterator():
            if block.lesson_id in lessons:
                broken.add(lessons[block.lesson_id])
            if apply:
                html = (block.data or {}).get('html', '')
                block.data = dict(block.data, html=FAILURE_HTML_RX.sub(f'<p>{SUMMARY_PENDING}</p>', html))
                block.save(update_fields=['data'])
        self.stdout.write(f'Summary lesson blocks with AI error text: {blocks.count()} '
                          f'(lessons {", ".join(str(pk) for pk in sorted({b.lesson_id for b in blocks})) or "none"})')

        if apply and broken:
            (SessionArtifact.objects.filter(meeting_id__in=broken, summary_source=SessionArtifact.SUMMARY_CLAUDE)
             .update(summary_source='', summary_hash=''))
        return broken

    def _resummarise(self, meeting_ids):
        """Rebuild the summaries of ``meeting_ids``; one failing class does not stop the rest."""
        from apps.communication.models import MeetingRoom
        from apps.livesessions import summaries
        self.stdout.write(f'\nRebuilding {len(meeting_ids)} summaries:')
        for meeting in MeetingRoom.objects.filter(pk__in=meeting_ids).order_by('pk'):
            try:
                status = summaries.process(meeting)
            except Exception as exc:
                status = f'error: {exc}'
            self.stdout.write(f'  {meeting.pk} {meeting.title[:60]}: {status}')


def _key(url):
    """``url`` normalised for lookup: trimmed, trailing punctuation and the fragment removed."""
    return (url or '').strip().rstrip('.,;:!?').split('#', 1)[0]


def _path_for(artifact, kind):
    """The site path students should have been given for ``kind`` of this session."""
    session = artifact.meeting.get_absolute_url()
    if kind == 'recording':
        return session + '#recording'
    if kind == 'transcript':
        return artifact.transcript_url or session
    if kind == 'summary':
        return artifact.summary_url or session
    return session


def _link_q(field):
    """A ``Q`` matching rows whose ``field`` contains a SharePoint or OneDrive address."""
    from django.db.models import Q
    return Q(**{f'{field}__icontains': 'sharepoint.com'}) | Q(**{f'{field}__icontains': '1drv.ms'})


def _first_change(before, after):
    """The first differing line, as ``old → new``, for the dry-run samples."""
    for old, new in zip((before or '').splitlines(), (after or '').splitlines()):
        if old != new:
            return f'{old.strip()[:110]}\n    → {new.strip()[:110]}'
    return '(changed)'


def _excerpt(text, marker):
    """About 110 characters of ``text`` from ``marker`` onwards, for the dry-run samples."""
    at = (text or '').find(marker)
    return (text[at:at + 110] + '…') if at >= 0 else ''
