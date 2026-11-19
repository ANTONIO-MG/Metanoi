"""Give upcoming Teams sessions a real Teams meeting before the day.

    python manage.py prepare_teams_sessions --programme SAMPLE            # dry run: show the plan
    python manage.py prepare_teams_sessions --programme SAMPLE --apply    # do it

Each session either links to the meeting the organiser already made by hand in
Outlook/Teams for that slot, or gets a meeting of its own (lobby bypassed,
recording on, staff invited on Outlook). Nothing in Teams is changed or deleted.
See :mod:`apps.livesessions.provisioning`.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = 'Link or create the Teams meeting for every upcoming Teams session that has none.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: dry run).')
        parser.add_argument('--programme', help='Only this programme code, e.g. SAMPLE.')
        parser.add_argument('--cohort', help='Only this cohort code.')
        parser.add_argument('--since', help='Start date YYYY-MM-DD (default: today).')
        parser.add_argument('--days', type=int, default=120, help='How far ahead (default 120 days).')
        parser.add_argument('--no-link', action='store_true',
                            help='Never link to hand-made meetings; create a new one for every session.')
        parser.add_argument('--organizer', help='Calendar to look in for hand-made meetings '
                                                '(default: MS_GRAPH_DEFAULT_ORGANIZER).')

    def handle(self, *args, apply=False, programme=None, cohort=None, since=None, days=120,
               no_link=False, organizer=None, **options):
        from apps.msteams import services as teams
        from apps.livesessions import provisioning

        if not teams.teams_active():
            raise CommandError('Microsoft Teams is not switched on here (MEETING_PROVIDER / Graph '
                               'settings), so no meeting can be linked or created.')
        organizer = (organizer or getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip()
        if not organizer:
            raise CommandError('No organiser: pass --organizer or set MS_GRAPH_DEFAULT_ORGANIZER.')

        tz = timezone.get_current_timezone()
        day = dt.date.fromisoformat(since) if since else timezone.localdate()
        start = timezone.make_aware(dt.datetime.combine(day, dt.time.min), tz)
        meetings = provisioning.sessions_needing_meetings(
            since=start, until=start + dt.timedelta(days=days), programme=programme, cohort=cohort)

        self.stdout.write(f'{meetings.count()} Teams session(s) without a meeting from {day} '
                          f'({days} days){" in " + programme if programme else ""}.')
        plan = provisioning.prepare(meetings, organizer=organizer, apply=apply,
                                    link_existing=not no_link, out=self.stdout.write)
        self.stdout.write(str(plan))
        if not apply:
            self.stdout.write(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
        elif plan.failed:
            self.stdout.write(self.style.ERROR('Some sessions failed: see the error log, then run again.'))
        else:
            self.stdout.write(self.style.SUCCESS('Done.'))
