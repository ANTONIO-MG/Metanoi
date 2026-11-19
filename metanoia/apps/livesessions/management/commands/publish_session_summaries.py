"""Import ended Teams classes and publish their summaries.

    manage.py publish_session_summaries                 # what the scheduler runs
    manage.py publish_session_summaries --import-only --dry-run --days 30
    manage.py publish_session_summaries --session 42    # one class, now

See :mod:`apps.livesessions.summaries`.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Import ended Teams group classes and publish each one\'s session summary.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='How far back to import classes.')
        parser.add_argument('--import-only', action='store_true', help='Import classes; do not chase summaries.')
        parser.add_argument('--dry-run', action='store_true', help='With --import-only: report, create nothing.')
        parser.add_argument('--session', type=int, help='Process just this MeetingRoom id.')
        parser.add_argument('--backfill', action='store_true',
                            help='The daily sweep: go back --days (default 120) for missed classes and late summaries.')

    def handle(self, *args, **opts):
        from apps.communication.models import MeetingRoom
        from apps.livesessions import summaries
        from apps.msteams import calendar_import

        if opts['session']:
            meeting = MeetingRoom.objects.get(pk=opts['session'])
            self.stdout.write(f'{meeting}: {summaries.process(meeting)}')
            artifact = getattr(meeting, 'artifact', None)
            if artifact is not None and artifact.summary_note:
                self.stdout.write(f'  note: {artifact.summary_note}')
            return

        if opts['backfill']:
            days = opts['days'] if opts['days'] != 7 else summaries.BACKFILL_DAYS
            self.stdout.write(summaries.run_backfill(days=days))
            return

        report = calendar_import.import_sessions(days=opts['days'], dry_run=opts['dry_run'])
        self.stdout.write(('DRY RUN — ' if opts['dry_run'] else '') + str(report))
        if opts['import_only']:
            return
        for meeting in summaries.candidates():
            status = summaries.process(meeting)
            note = getattr(getattr(meeting, 'artifact', None), 'summary_note', '')
            self.stdout.write(f'  {meeting.title[:60]:<60} {status}' + (f'  ({note})' if note and status == 'waiting' else ''))
