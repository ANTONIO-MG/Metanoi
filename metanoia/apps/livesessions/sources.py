"""Everything that lands on a calendar, from every app, in one shape.

The platform grew two calendars. ``myhub.events_feed`` aggregated the practice
diary, the academic dates, task deadlines, lessons, assessments and meetings into
a FullCalendar feed; the live-session work then added a month grid of its own that
knew only about sessions and academic dates. Two aggregations meant two answers
to "what is on my calendar this week", and only one of them knew about audience
scoping.

So there is now one aggregation — this module — and both surfaces render from it.
A source is added here once and appears everywhere: the month grid, the day view,
the events page, the JSON feed and the exported ``.ics``.

Every entry is a :class:`CalendarEntry`, which is deliberately flat and dumb: a
title, a span, a kind, a colour and a link. Sources do their own permission
scoping *before* returning — the live-session source defers to
:mod:`apps.livesessions.audience`, the academic source to the reader's
institutions, personal reminders to their owner — so a caller can render whatever
it is handed without re-checking anything.

``KINDS`` maps every source to the label, colour and icon it draws in, ordered
as they should appear in a legend: what you attend, then what you owe, then
context. The module then has three parts: the aggregate (:func:`entries_for`,
:func:`group_by_day`, :func:`busy_spans`), one private function per source, and
adapters (:func:`as_fullcalendar`, :func:`as_items`) for the surfaces that had
their own shapes.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger('apps')


KINDS = {
    'session':   ('Live session', '#20c997', 'camera-video'),
    'booking':   ('1-on-1 session', '#E5484D', 'person-video3'),
    'assessment': ('Assessment',  '#dc3545', 'ui-checks'),
    'academic':  ('Academic date', '#C00000', 'mortarboard'),
    'lesson':    ('Lesson',       '#0dcaf0', 'journal-richtext'),
    'event':     ('Event',        '#3a87ad', 'calendar-event'),
    'reminder':  ('Reminder',     '#6f42c1', 'bell'),
    'external':  ('From your calendar', '#8E8E93', 'calendar2-week'),
}


@dataclass
class CalendarEntry:
    """One dated thing, from any app, ready to render.

    ``kind`` is a key of ``KINDS``; it supplies the default ``colour`` and the
    ``label`` / ``icon`` in ``meta``. ``busy`` is True when the entry blocks
    out time for the free-slot finder.
    """

    title: str
    start: datetime
    kind: str = 'event'
    end: datetime = None
    all_day: bool = False
    url: str = ''
    location: str = ''
    detail: str = ''
    colour: str = ''
    uid: str = ''
    busy: bool = False
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        """Fill the colour, label and icon from ``KINDS`` where not given."""
        label, colour, icon = KINDS.get(self.kind, KINDS['event'])
        self.colour = self.colour or colour
        self.meta.setdefault('label', label)
        self.meta.setdefault('icon', icon)

    @property
    def label(self):
        """The kind's human label, e.g. "Live session"."""
        return self.meta.get('label', '')

    @property
    def icon(self):
        """The Bootstrap icon name the kind draws with."""
        return self.meta.get('icon', 'calendar-event')

    @property
    def local_start(self):
        """``start`` in the current time zone, or None."""
        return timezone.localtime(self.start) if self.start else None

    @property
    def is_past(self):
        """True when the entry has already started."""
        return bool(self.start and self.start < timezone.now())


def entries_for(user, start, end, *, kinds=None, limit_per_source=500):
    """Every calendar entry ``user`` may see between ``start`` and ``end``.

    ``kinds`` optionally restricts which sources run — the exported ``.ics``
    uses it to leave out the events it imported in the first place, which would
    otherwise loop straight back into the calendar they came from.

    Each source runs in isolation: one that raises is logged and skipped, since
    a single broken source must never blank the calendar.
    """
    wanted = set(kinds) if kinds else set(KINDS)
    collected = []

    for name, source in (
        ('session', _sessions),
        ('booking', _bookings),
        ('academic', _academic_dates),
        ('event', _events),
        ('lesson', _lessons),
        ('assessment', _assessments),
        ('external', _external),
    ):
        if name not in wanted:
            continue
        try:
            collected.extend(source(user, start, end, limit_per_source) or [])
        except Exception:
            logger.exception('calendar: source %r failed', name)

    collected.sort(key=lambda entry: (entry.start or timezone.now()))
    return collected


def group_by_day(entries):
    """``{date: [entry, ...]}`` in the reader's own time zone."""
    grouped = {}
    for entry in entries:
        if not entry.start:
            continue
        grouped.setdefault(timezone.localtime(entry.start).date(), []).append(entry)
    return grouped


def busy_spans(user, start, end):
    """``(start, end)`` pairs for everything that occupies ``user``'s time.

    Used by the free-slot finder. Only genuinely blocking entries count — an
    assessment closing date is a deadline, not a meeting, and an imported event
    marked *free* by its own calendar is not busy either.
    """
    spans = []
    for entry in entries_for(user, start, end, kinds=('session', 'booking', 'external')):
        if not entry.busy or not entry.start:
            continue
        spans.append((entry.start, entry.end or entry.start + timedelta(hours=1)))
    return spans


def _sessions(user, start, end, limit):
    """Live sessions, scoped by :mod:`apps.livesessions.audience`.

    This is the source the old myhub feed got wrong: it showed only sessions the
    user hosted or was individually invited to, so a module class never appeared
    on the calendar of the students it was for.
    """
    from .audience import visible_sessions

    entries = []
    query = (visible_sessions(user)
             .filter(scheduled_start__gte=start, scheduled_start__lte=end)
             .select_related('module', 'host', 'artifact')
             .order_by('scheduled_start')[:limit])
    for meeting in query:
        entries.append(CalendarEntry(
            kind='session',
            title=meeting.title,
            start=meeting.scheduled_start,
            end=meeting.scheduled_end,
            url=meeting.get_absolute_url(),
            detail=meeting.audience_label,
            uid=f'session-{meeting.pk}@metanoia',
            busy=True,
            meta={'live': meeting.is_live_now,
                  'module': meeting.module.label if meeting.module_id else '',
                  'watchable': bool(getattr(meeting, 'artifact', None)
                                    and meeting.artifact.is_watchable)},
        ))
    return entries


def _bookings(user, start, end, limit):
    """1-on-1s the user booked or teaches that have no live session yet.

    A paid booking gets its own live session (``shop.booking.confirm``), which
    the session source already shows; this covers the rest — a slot held in a
    cart awaiting payment, or a paid one whose room could not be created — so
    the educator's calendar always shows the hour as taken.
    """
    from django.db.models import Q

    from apps.shop.models import Booking

    person = getattr(user, 'profile', None)
    who = Q(student=user) | (Q(educator=person) if person is not None else Q(pk__in=[]))
    now = timezone.now()
    query = (Booking.objects.filter(who, meeting__isnull=True, start__lt=end, end__gt=start,
                                    status__in=Booking.BLOCKING)
             .filter(Q(status=Booking.STATUS_CONFIRMED) | Q(hold_expires_at__gt=now))
             .select_related('product', 'educator__user', 'student')
             .order_by('start')[:limit])
    from core.utils import display_name

    entries = []
    for booking in query:
        teaching = person is not None and booking.educator_id == person.pk
        other = display_name(booking.student) if teaching else str(booking.educator)
        held = booking.status == Booking.STATUS_HELD
        entries.append(CalendarEntry(
            kind='booking',
            title=f'1-on-1 with {other}' + (' (awaiting payment)' if held else ''),
            start=booking.start, end=booking.end,
            url=reverse('communication:meetings') + '#one-on-one',
            detail=booking.product.name,
            uid=f'booking-{booking.pk}@metanoia',
            busy=True,
        ))
    return entries


def _academic_dates(user, start, end, limit):
    """The institution's published dates — tests, exams, deadlines, holidays.

    A student sees only the dates of their own programme: a date narrowed to a
    programme, a module offering or an intake shows only to someone on it (an
    intake's date also to students on its programme with no intake set yet, the
    rule of :func:`apps.livesessions.audience.class_students`). So a PGDA
    candidate is not shown BCTA's exams, nor another intake's coaching nights.
    An educator sees the dates of the modules they teach.

    A parent's calendar is their children's, as
    :func:`~apps.livesessions.audience.visible_sessions` does it.
    """
    from apps.learning.models import CalendarEvent
    from core.roles import role_of_user

    query = (CalendarEvent.objects
             .filter(start__gte=start, start__lte=end)
             .select_related('calendar__institution', 'programme', 'programme_module'))

    role = role_of_user(user)
    if role not in ('admin', 'staff'):
        query = query.filter(is_published=True).filter(_academic_scope(user, role))

    return [CalendarEntry(
        kind='academic',
        title=f'{event.get_kind_display()} · {event.label}',
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        colour=event.colour,
        detail=event.description,
        location=event.location,
        uid=f'academic-{event.pk}@metanoia',
        meta={'event_kind': event.kind, 'event_kind_label': event.get_kind_display(),
              'module': event.programme_module.label if event.programme_module_id else ''},
    ) for event in query.order_by('start')[:limit]]


def _academic_scope(user, role):
    """A ``Q`` on :class:`CalendarEvent` for the dates that are ``user``'s own.

    For a student a date qualifies when it is on one of their institutions and
    is institution-wide (a holiday) or for their programme; a module's date
    only for a module they are on; and an intake's only for their own intake.
    A parent gets the union of their children's scopes.
    """
    from django.db.models import Q

    from .audience import intake_scope

    person = getattr(user, 'profile', None)
    if person is None:
        return Q(pk__in=[])
    if role == 'parent':
        from core.scoping import children_of
        scope = Q(pk__in=[])
        for child in children_of(user):
            child_user = getattr(child, 'user', None)
            if child_user is not None:
                scope |= _academic_scope(child_user, 'student')
        return scope
    if role == 'educator':
        taught = person.taught_modules.all()
        return (Q(programme_module__in=taught)
                | Q(programme_module__isnull=True, programme__in=taught.values('programme'))
                | Q(programme_module__isnull=True, programme__isnull=True,
                    calendar__institution__in=taught.values('programme__institution')))

    enrolments = person.programme_enrolments.all()
    return (
        Q(calendar__institution__in=enrolments.values('programme__institution'))
        & (Q(programme__isnull=True) | Q(programme__in=enrolments.values('programme')))
        & (Q(programme_module__isnull=True)
           | Q(programme_module__in=person.module_enrolments.values('programme_module')))
        & intake_scope(person)
    )


def _events(user, start, end, limit):
    """The practice's own diary and the reader's personal reminders."""
    from django.db.models import Q

    from apps.myhub.models import Event

    if not getattr(user, 'is_authenticated', False):
        query = Event.objects.filter(owner__isnull=True)
    else:
        query = Event.objects.filter(Q(owner__isnull=True) | Q(owner=user))

    entries = []
    for event in query.filter(start__gte=start, start__lte=end).order_by('start')[:limit]:
        is_reminder = event.category == Event.CATEGORY_REMINDER
        entries.append(CalendarEntry(
            kind='reminder' if is_reminder else 'event',
            title=event.title,
            start=event.start,
            end=event.end,
            all_day=event.all_day,
            location=event.location,
            detail=event.description,
            colour='' if is_reminder else (event.color or ''),
            url=reverse('myhub:event-detail', args=[event.pk]),
            uid=f'event-{event.pk}@metanoia',
            busy=not is_reminder,
        ))
    return entries


def _lessons(user, start, end, limit):
    """Lessons publishing inside the window, for the reader's own modules.

    The module ids are a subquery rather than a list, so one round trip
    instead of two. A class's summary lesson (``summaries._publish_lesson``,
    slug ``session-summary-<meeting pk>``) sits on the module every intake
    shares, but belongs on the calendar of whoever can see that class, not the
    module, so summaries of classes the reader cannot see are excluded.
    """
    person = getattr(user, 'profile', None)
    if person is None:
        return []
    from apps.learning.models import Lesson

    module_ids = person.module_enrolments.values('programme_module_id')
    query = Lesson.objects.filter(module_id__in=module_ids, publish_at__gte=start, publish_at__lte=end)
    summaries = query.filter(slug__startswith='session-summary-').values_list('slug', flat=True)
    if summaries:
        from .audience import visible_sessions
        meeting_ids = {s.rsplit('-', 1)[-1] for s in summaries}
        seen = visible_sessions(user).filter(pk__in=[m for m in meeting_ids if m.isdigit()])
        hidden = [s for s in summaries
                  if s.rsplit('-', 1)[-1] not in {str(pk) for pk in seen.values_list('pk', flat=True)}]
        query = query.exclude(slug__in=hidden)
    query = query.order_by('publish_at')[:limit]
    return [CalendarEntry(
        kind='lesson',
        title=lesson.title,
        start=lesson.publish_at,
        url=lesson.get_absolute_url(),
        detail='Lesson released',
        uid=f'lesson-{lesson.pk}@metanoia',
        meta={'summary': lesson.slug.startswith('session-summary-')},
    ) for lesson in query]


def _assessments(user, start, end, limit):
    """Assessment closing dates for the reader's own modules."""
    person = getattr(user, 'profile', None)
    if person is None:
        return []
    from apps.assessments.models import Assessment

    module_ids = person.module_enrolments.values('programme_module_id')
    query = (Assessment.objects
             .filter(module_id__in=module_ids, available_to__gte=start, available_to__lte=end)
             .order_by('available_to')[:limit])

    entries = []
    for assessment in query:
        try:
            url = reverse('assessments:take', args=[assessment.pk])
        except Exception:
            url = ''
        entries.append(CalendarEntry(
            kind='assessment',
            title=assessment.title,
            start=assessment.available_to,
            url=url,
            detail='Closes',
            uid=f'assessment-{assessment.pk}@metanoia',
        ))
    return entries


def _external(user, start, end, limit):
    """Events imported from the reader's own Google / Outlook / Apple calendar."""
    if not getattr(user, 'is_authenticated', False):
        return []
    from .models import ExternalEvent

    query = (ExternalEvent.objects
             .filter(subscription__user=user, subscription__is_active=True,
                     start__gte=start, start__lte=end)
             .select_related('subscription')
             .order_by('start')[:limit])
    return [CalendarEntry(
        kind='external',
        title=event.title,
        start=event.start,
        end=event.end,
        all_day=event.all_day,
        location=event.location,
        colour=event.subscription.colour,
        detail=event.subscription.name,
        uid=f'ext-{event.pk}@metanoia',
        busy=bool(event.busy and event.subscription.blocks_time),
    ) for event in query]


def as_fullcalendar(entries):
    """FullCalendar's JSON shape, as ``myhub:events-feed`` has always returned."""
    return [{
        'title': f'{_emoji(entry.kind)} {entry.title}'.strip(),
        'start': entry.start.isoformat() if entry.start else None,
        'end': entry.end.isoformat() if entry.end else None,
        'allDay': entry.all_day,
        'color': entry.colour,
        'url': entry.url,
    } for entry in entries if entry.start]


def as_items(entries):
    """The dict shape ``myhub:events`` renders its list from."""
    return [{
        'title': entry.title,
        'when': entry.start,
        'category': entry.label,
        'color': entry.colour,
        'icon': entry.icon,
        'url': entry.url,
        'location': entry.location,
        'detail_url': entry.url,
    } for entry in entries if entry.start]


_EMOJI = {'session': '🎥', 'booking': '🧑‍🏫', 'academic': '🎓', 'lesson': '📘',
          'assessment': '📝', 'reminder': '🔔', 'event': '', 'external': '🗓'}


def _emoji(kind):
    """The emoji prefix FullCalendar titles carry for ``kind`` ('' for none)."""
    return _EMOJI.get(kind, '')


def window_around(anchor=None, *, before=30, after=180):
    """A sensible default window: a month back, six months on."""
    anchor = anchor or timezone.now()
    if isinstance(anchor, date) and not isinstance(anchor, datetime):
        anchor = timezone.make_aware(datetime.combine(anchor, datetime.min.time()),
                                     timezone.get_current_timezone())
    return anchor - timedelta(days=before), anchor + timedelta(days=after)
