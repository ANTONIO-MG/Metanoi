"""Rebuild a programme's live sessions: standard titles and a Teams meeting each.

    python manage.py rebuild_programme_sessions --programme SAMPLE            # dry run
    python manage.py rebuild_programme_sessions --programme SAMPLE --apply

1. Classes that already happened (from ``--since``, default 7 days back) and
   have no Teams link are linked to the hand-made meeting they ran in, so the
   recording pipeline can find their recording. They are never given a new one.
2. Every upcoming session gets the standard title
   (``METANOIA SAMPLE INTRO PACK2027 · Sat 13 Feb 2027 08:00 | Live session · Unit 2 …``)
   and a Teams meeting of its own. Whole-cohort sessions (no module) stay
   cohort-wide; the programme pack lists them on the General module.

Sessions are changed in place (same pages, registers and schedule entries).
Nothing in Teams is deleted; nobody is notified (each class's reminders carry
its link). See apps.livesessions.provisioning.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    """Management command: link past classes to their Teams meeting and rebuild upcoming sessions for one programme."""
    help = "Standard titles and one Teams meeting per session for a programme's live sessions."

    def add_arguments(self, parser):
        """Add ``--programme`` (required), ``--cohort``, ``--since`` and ``--apply``."""
        parser.add_argument('--programme', required=True, help='Programme code, e.g. SAMPLE.')
        parser.add_argument('--cohort', help='Only this cohort code.')
        parser.add_argument('--since', help='Link past classes from this date (YYYY-MM-DD; default 7 days ago).')
        parser.add_argument('--apply', action='store_true', help='Make the changes (default: dry run).')

    def handle(self, *args, programme, cohort=None, since=None, apply=False, **options):
        """Run both passes (past classes, then upcoming sessions) and report the outcome.

        A dry run unless ``--apply`` is given. Requires Microsoft Teams to be switched
        on. Whole-cohort sessions keep no module: the programme pack lists them on the
        General module's schedule while the session stays cohort-wide. A run that
        fails part-way can be repeated; it picks up where it stopped.
        """
        from apps.communication.models import MeetingRoom
        from apps.msteams import services as teams
        from apps.livesessions import provisioning

        if not teams.teams_active():
            raise CommandError('Microsoft Teams is not switched on here, so no meeting can be made.')
        organizer = (getattr(settings, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip()
        now = timezone.now()
        tz = timezone.get_current_timezone()
        first = (timezone.make_aware(dt.datetime.combine(dt.date.fromisoformat(since), dt.time.min), tz)
                 if since else now - dt.timedelta(days=7))

        say = self.stdout.write
        say(self.style.MIGRATE_HEADING('1. Past classes with no Teams link → the meeting they ran in'))
        past = provisioning.sessions_needing_meetings(since=first, until=now, programme=programme,
                                                      cohort=cohort)
        plan = provisioning.prepare(past, organizer=organizer, apply=apply, out=say)
        say(str(plan))

        say(self.style.MIGRATE_HEADING('2. Upcoming sessions → standard title, own Teams meeting'))
        upcoming = (MeetingRoom.objects
                    .filter(provider=MeetingRoom.PROVIDER_TEAMS, is_active=True,
                            programme__code__iexact=programme, scheduled_start__gt=now)
                    .select_related('module', 'programme', 'cohort', 'institution', 'host')
                    .order_by('scheduled_start'))
        if cohort:
            upcoming = upcoming.filter(cohort__code__iexact=cohort)
        result = provisioning.rebuild(upcoming, apply=apply, out=say)
        say(str(result))
        if not apply:
            say(self.style.WARNING('Dry run: nothing was changed. Add --apply to do it.'))
        elif plan.failed or result.failed:
            say(self.style.ERROR('Some sessions failed: see above and the error log, then run again '
                                 '(it picks up where it stopped).'))
        else:
            say(self.style.SUCCESS('Done.'))
