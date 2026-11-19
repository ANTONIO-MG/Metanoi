"""Which weeks of a module a student may open: the one rule.

The owner's rule — a week's content is unlocked for a student when EITHER

    (a) their enrolment is paid AND the week is inside the paid window
        (payment date to + 33 days) or is the previous, current or next week.
        A live free trial counts as paid for its own window; OR
    (b) the week falls inside any period they paid for before, even after the
        payment lapsed (students keep what they paid for); OR
    (c) the week is ticked "make available" (:attr:`ModuleWeek.make_available`); OR
    (d) their enrolment is paid (or on a live trial) right now AND the week has
        already started — every past week stays open, so a student can always
        go back to earlier material. Only future weeks wait for the window.

    Otherwise only its title and dates show. Staff, admins and the module's
    educators always see everything; a free module is open to all.

Everything that hands out week content asks this module (via
``Gate.week_verdict`` or :func:`week_is_unlocked`). Nothing else decides it.

**Week dates**, in order: the week's own dates; its phase's ``starts_on``
(week *n* = days ``7(n-1)``..``7(n-1)+6``); or backwards from the phase's
assessment date. An *undated* week is never "this week" nor outside a paid
window: a paying student gets it, an unpaid one does not.

**Previous / current / next** is in schedule order over the weeks the student
can see. "Current" holds today; between weeks or before the start it is the
next to start; after the end, the last.

**Reasons** a week is open (``REASON_*``) or shut (``LOCK_*``). The open reasons
for staff/educator/free/trial/paid are shared with :mod:`apps.learning.access`,
so a row carries one vocabulary. ``LOCK_OUT_OF_WINDOW`` means paid, but this
week is outside the paid month.
"""

from datetime import timedelta

from django.utils import timezone

WEEK_DAYS = 7

REASON_STAFF = 'staff'
REASON_EDUCATOR = 'educator'
REASON_FREE = 'free'
REASON_TRIAL = 'trial'
REASON_PAID = 'paid'
REASON_MADE_AVAILABLE = 'made_available'
REASON_PAID_BEFORE = 'paid_before'

LOCK_UNPAID = 'unpaid'
LOCK_EXPIRED = 'expired'
LOCK_OUT_OF_WINDOW = 'window'


class WeekRow:
    """The few facts about a week the rule needs — loaded in one query."""

    __slots__ = ('id', 'phase_id', 'starts_on', 'ends_on', 'make_available', 'visible_to_students',
                 'cohort_id', 'sort_key', 'span')

    def __init__(self, **kw):
        """Set every slot from the keyword arguments (missing ones are ``None``)."""
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def load_week_rows(offering):
    """Every active week of ``offering`` as :class:`WeekRow`, in schedule order,
    with its dates resolved. One query (two if a phase points at a calendar event)."""
    from .models import ModuleWeek

    rows = list(
        ModuleWeek.objects
        .filter(phase__programme_module=offering, is_active=True, phase__is_active=True)
        .order_by('phase__order', 'phase_id', 'order', 'number', 'id')
        .values('id', 'phase_id', 'starts_on', 'ends_on', 'make_available', 'is_published',
                'phase__is_published', 'phase__cohort_id', 'phase__order', 'order', 'number',
                'phase__starts_on', 'phase__ends_on', 'phase__calendar_event__start'))

    by_phase = {}
    for r in rows:
        by_phase.setdefault(r['phase_id'], []).append(r)

    out = []
    for r in rows:
        siblings = by_phase[r['phase_id']]
        out.append(WeekRow(
            id=r['id'], phase_id=r['phase_id'],
            starts_on=r['starts_on'], ends_on=r['ends_on'],
            make_available=r['make_available'],
            visible_to_students=bool(r['is_published'] and r['phase__is_published']),
            cohort_id=r['phase__cohort_id'],
            sort_key=(r['phase__order'], r['phase_id'], r['order'], r['number'], r['id']),
            span=_span(r, siblings.index(r), len(siblings)),
        ))
    return out


def _span(r, index, count):
    """``(first_day, last_day)`` of one week, or ``None`` when it cannot be placed."""
    if r['starts_on']:
        return r['starts_on'], (r['ends_on'] or r['starts_on'] + timedelta(days=WEEK_DAYS - 1))
    if r['ends_on']:
        return r['ends_on'] - timedelta(days=WEEK_DAYS - 1), r['ends_on']
    if r['phase__starts_on']:
        start = r['phase__starts_on'] + timedelta(days=WEEK_DAYS * index)
        return start, start + timedelta(days=WEEK_DAYS - 1)
    assessment = r['phase__calendar_event__start']
    assessment = timezone.localdate(assessment) if assessment else r['phase__ends_on']
    if assessment:
        end = assessment - timedelta(days=WEEK_DAYS * (count - 1 - index))
        return end - timedelta(days=WEEK_DAYS - 1), end
    return None


def near_week_ids(ordered, today):
    """The previous, current and next week (ids) among ``ordered`` WeekRows.

    Empty when no week in ``ordered`` can be dated: there is no "this week" then.
    """
    dated = [(i, w.span) for i, w in enumerate(ordered) if w.span]
    if not dated:
        return set()
    anchor = next((i for i, (start, end) in dated if start <= today <= end), None)
    if anchor is None:
        upcoming = [(start, i) for i, (start, _end) in dated if start > today]
        if upcoming:
            anchor = min(upcoming)[1]
        else:
            anchor = max(dated, key=lambda d: (d[1][1], d[0]))[0]
    return {ordered[j].id for j in (anchor - 1, anchor, anchor + 1) if 0 <= j < len(ordered)}


def access_window(enrolment, today):
    """``(start, end)`` of the period the enrolment currently pays for, or
    ``None`` when it is not paid right now. Either bound may be ``None`` (open).

    A live trial → its own days; an active payment → ``paid_from`` to
    ``paid_until`` (no ``paid_until`` = open-ended grant). A payment made before
    ``paid_from`` existed (Nov 2026) is taken to have begun when the module was first
    unlocked — generous for a multi-month payment, rather than squeezing a 12-month
    grant into its last 33 days.
    """
    if enrolment is None:
        return None
    if enrolment.status == enrolment.STATUS_TRIAL:
        ends = enrolment.trial_ends_at
        if today == timezone.localdate():
            live = enrolment.is_trial_valid
        else:
            live = ends is None or timezone.localdate(ends) >= today
        if not live:
            return None
        end = timezone.localdate(ends) if ends else None
        if enrolment.started_at:
            start = timezone.localdate(enrolment.started_at)
        else:
            start = end - timedelta(days=enrolment.TRIAL_DAYS) if end else None
        return start, end
    if enrolment.status == enrolment.STATUS_ACTIVE:
        if enrolment.paid_until is None:
            return None, None
        if enrolment.paid_until < today:
            return None
        start = enrolment.paid_from
        if start is None and enrolment.started_at:
            start = timezone.localdate(enrolment.started_at)
        if start is None:
            start = enrolment.paid_until - timedelta(days=enrolment.PAID_DAYS)
        return start, enrolment.paid_until
    return None


def paid_periods(enrolment):
    """Every ``(start, end)`` the enrolment was paid for: the recorded
    :class:`PaidPeriod` rows plus its own paid run (covers rows paid before the
    records existed). Trials are not payments and are not included."""
    if enrolment is None or enrolment.pk is None:
        return []
    periods = list(enrolment.paid_periods.values_list('starts_on', 'ends_on'))
    if enrolment.paid_until:
        start = enrolment.paid_from
        if start is None and enrolment.started_at:
            start = timezone.localdate(enrolment.started_at)
        if start is None:
            start = enrolment.paid_until - timedelta(days=enrolment.PAID_DAYS)
        periods.append((start, enrolment.paid_until))
    return periods


def _overlaps(span, window):
    """True when the ``span`` of days overlaps ``window`` (either window bound may be
    open).
    """
    start, end = window
    first, last = span
    return (end is None or first <= end) and (start is None or last >= start)


class WeekAccess:
    """One viewer's verdict on every week of one offering, resolved once.

    Built lazily by :class:`apps.learning.access.Gate` (``gate.weeks``); one
    query for the weeks (plus one for the intake), then nothing per week.
    """

    def __init__(self, gate, today=None):
        """Hold the gate and the day; everything else is computed on first use.

        ``_window`` starts as ``False`` (not computed yet), because ``None`` is a
        meaningful value — "not paid right now".
        """
        self.gate = gate
        self.today = today or timezone.localdate()
        self._rows = None
        self._near = None
        self._window = False
        self._paid_periods = None

    @property
    def bypass_reason(self):
        """Why every week is open for this viewer (staff, educator, free module), else
        ``None``.
        """
        if self.gate.is_staff:
            return REASON_STAFF
        if self.gate.is_educator:
            return REASON_EDUCATOR
        if self.gate.offering.is_free:
            return REASON_FREE
        return None

    @property
    def rows(self):
        """``{week id: WeekRow}`` for every active week of the offering, loaded once."""
        if self._rows is None:
            self._rows = {w.id: w for w in load_week_rows(self.gate.offering)}
        return self._rows

    def ordered_for_student(self):
        """The weeks this student's schedule shows, in order: published, and in
        their intake's blocks or the shared ones."""
        cohort_id = self.gate.cohort_id
        return [w for w in self.rows.values()
                if w.visible_to_students and (w.cohort_id is None or w.cohort_id == cohort_id)]

    @property
    def near(self):
        """The ids of the previous, current and next week on this student's schedule."""
        if self._near is None:
            self._near = near_week_ids(self.ordered_for_student(), self.today)
        return self._near

    @property
    def window(self):
        """The period the enrolment currently pays for (see :func:`access_window`)."""
        if self._window is False:
            self._window = access_window(self.gate.enrolment, self.today)
        return self._window

    @property
    def paid_periods(self):
        """Every period the enrolment was ever paid for (see :func:`paid_periods`)."""
        if self._paid_periods is None:
            self._paid_periods = paid_periods(self.gate.enrolment)
        return self._paid_periods

    def verdict(self, week):
        """``(is_open, reason)`` for one week (a ModuleWeek, a WeekRow or an id).

        A week that is inactive or on another module has nothing to open. While paid
        up, going back is always allowed: a student on Test 3 week 6 can still open
        Test 1 week 2 (owner, 2026-11-12); only weeks still to come are held to the
        paid window. Undated weeks cannot be placed in a past period, so they stay
        shut
        once the payment lapses.
        """
        bypass = self.bypass_reason
        if bypass:
            return True, bypass
        week_id = week if isinstance(week, int) else week.id
        row = self.rows.get(week_id)
        if row is None:
            return False, self._unpaid_reason()
        if row.make_available:
            return True, REASON_MADE_AVAILABLE
        window = self.window
        if window is not None:
            paid = REASON_TRIAL if self.gate.enrolment.status == self.gate.enrolment.STATUS_TRIAL else REASON_PAID
            if row.id in self.near:
                return True, paid
            if row.span is None or _overlaps(row.span, window):
                return True, paid
            if row.span[0] <= self.today:
                return True, paid
        if row.span is not None and any(_overlaps(row.span, p) for p in self.paid_periods):
            return True, REASON_PAID_BEFORE
        return False, (LOCK_OUT_OF_WINDOW if window is not None else self._unpaid_reason())

    def is_open(self, week):
        """True when the week is open to this viewer."""
        return self.verdict(week)[0]

    def _unpaid_reason(self):
        """``LOCK_EXPIRED`` for a lapsed trial or payment, else ``LOCK_UNPAID``."""
        enrolment = self.gate.enrolment
        if enrolment is not None and enrolment.status in (enrolment.STATUS_TRIAL, enrolment.STATUS_EXPIRED):
            return LOCK_EXPIRED
        if (enrolment is not None and enrolment.status == enrolment.STATUS_ACTIVE
                and enrolment.paid_until and enrolment.paid_until < self.today):
            return LOCK_EXPIRED
        return LOCK_UNPAID

    def initial_ids(self, ordered_ids):
        """Which of ``ordered_ids`` (the weeks a page lists, in order) to render
        straight away: the previous, current and next week; with no dates at all
        to go on, the first three."""
        ordered = [self.rows[i] for i in ordered_ids if i in self.rows]
        near = near_week_ids(ordered, self.today)
        return near or {w.id for w in ordered[:3]}


def week_is_unlocked(person, week, today=None):
    """May ``person`` open the content of ``week``? The rule, stand-alone.

    For one-off checks; a page asking about many weeks should build one
    :class:`apps.learning.access.Gate` and ask ``gate.week_verdict`` instead.
    """
    from .access import Gate

    offering = week.phase.programme_module
    user = getattr(person, 'user', None)
    return Gate(user, offering, person=person, today=today).week_verdict(week)[0]
