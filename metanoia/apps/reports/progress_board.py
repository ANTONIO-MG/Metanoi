"""The student progress dashboard (``/reports/progress/``) and the facts behind it.

:func:`load` gathers, once, everything the platform records about one learner
against the modules they are registered on:

* the **schedule** each module is taught to — published blocks, weeks and
  material for the learner's own intake (the same rows the module feed shows),
* the **work** they have done against it — finished lessons and submitted
  assessments, via :mod:`apps.learning.progress`, so "done" means the same
  thing here as on the sidebar tiles,
* the **live classes** held for them since they enrolled, and which they walked
  into (a :class:`~apps.livesessions.models.SessionJoin`, or a present/late
  register mark),
* every marked **result**,
* their **grade** rows and any open **risk flag**.

:func:`build_dashboard` turns that into the page: an overall number, a card per
module, a results trend, twelve weeks of activity and a short list of "what to
improve" — each a concrete action with a link to where it is done.
:mod:`apps.reports.completions` reads the same :func:`load` to decide what has
been completed, so the two pages can never disagree about it.

What counts towards a module's progress, and why
-------------------------------------------------
Only work the platform can actually see: lessons and assessments on the
schedule (a PDF nobody can tell you opened is left out of the denominator, as in
:mod:`apps.learning.progress`), and the live classes held since enrolment. Each is a unit of "released so far"; progress is the
share of those units done. Nothing still embargoed, staff-only or unpublished is
counted.

Every section is guarded so a missing table or a half-built schedule degrades
one panel instead of 500-ing the page — the rule the hub dashboard follows.
"""

import logging
from datetime import timedelta

from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

STRONG_PCT = 70
"""A result at or above this reads as solid."""
PASS_PCT = 50
"""A result below this reads as a fail."""

QUIET_DAYS = 14
"""How many days without any activity before a module is called "gone quiet"."""

ACTIVITY_WEEKS = 12
"""Weeks of activity drawn under the headline."""


def _url(name, *args):
    """Reverse URL ``name`` with positional ``args``, or ``''`` when it cannot be reversed."""
    try:
        return reverse(name, args=args)
    except Exception:
        return ''


def _pct(part, whole):
    """``part`` as a whole-number percentage of ``whole``, or ``None`` when ``whole`` is zero."""
    return round(100 * part / whole) if whole else None


def _tone(pct):
    """Bootstrap colour for a progress percentage (success / warning / danger / secondary)."""
    if pct is None:
        return 'secondary'
    return 'success' if pct >= STRONG_PCT else ('warning' if pct >= 40 else 'danger')


def _score_tone(pct):
    """Bootstrap colour for a result percentage, using :data:`PASS_PCT` as the fail line."""
    if pct is None:
        return 'secondary'
    return 'success' if pct >= STRONG_PCT else ('warning' if pct >= PASS_PCT else 'danger')


class StudentData:
    """Everything known about one learner's study, fetched once per request.

    Each loading step runs under its own guard, so one failing source blanks one
    panel instead of the whole page.

    When ``scope`` (a set of modules) is given — an educator looking at a
    student — only those modules are loaded and marks from anyone else's module
    are dropped, not merely hidden; the risk flag is withheld too.

    Attributes filled by the loaders:

    * ``phases`` — offering id → ``[phase, …]`` with ``.released_materials``;
    * ``attempt_best`` — assessment id → best % (presence means attempted);
    * ``attempts`` — submitted/marked ``AssessmentAttempt`` rows, oldest first;
    * ``submissions`` — ``AssignmentSubmission`` rows that were handed in;
    * ``classes`` — offering id → ``[MeetingRoom]`` held since enrolment;
    * ``next_class`` — offering id → next ``MeetingRoom``;
    * ``attended`` — meeting id → when they joined;
    * ``grades`` — offering id → :class:`~apps.reports.models.Grade`;
    * ``study_rows`` — ``StudySession`` values rows;
    * ``open_assessments`` — ``Assessment`` rows open to sit now.
    """

    def __init__(self, user, scope=None):
        """Load everything for ``user``, optionally limited to the modules in ``scope``."""
        self.user = user
        self.person = getattr(user, 'profile', None)
        self.now = timezone.now()
        self.enrolments = []
        self.offerings = []
        self.phases = {}
        self.lessons_done = set()
        self.attempt_best = {}
        self.attempts = []
        self.submissions = []
        self.classes = {}
        self.next_class = {}
        self.attended = {}
        self.grades = {}
        self.risk = None
        self.study_rows = []
        self.open_assessments = []
        self.scope = scope
        for step in (self._load_enrolments, self._load_schedule, self._load_work,
                     self._load_classes, self._load_grades,
                     self._load_open_assessments):
            try:
                step()
            except Exception:
                logger.exception('progress_board: %s failed for user %s', step.__name__, self.user.pk)
        if scope is not None:
            mine = {o.pk for o in self.offerings}
            self.attempts = [a for a in self.attempts if a.assessment.module_id in mine]
            self.submissions = [s for s in self.submissions if s.assessment.module_id in mine]
            self.study_rows = [r for r in self.study_rows if r['lesson__module_id'] in mine]
            self.attempt_best = {a.assessment_id: self.attempt_best.get(a.assessment_id)
                                 for a in self.attempts}
            self.grades = {k: v for k, v in self.grades.items() if k in mine}
            self.risk = None

    def _load_enrolments(self):
        """Active module enrolments (and their offerings), in programme order."""
        from apps.learning.models import ModuleEnrolment
        if self.person is None:
            return
        rows = (ModuleEnrolment.objects
                .filter(person=self.person, programme_module__is_active=True)
                .select_related('programme_module__module',
                                'programme_module__programme__institution')
                .order_by('programme_module__order', 'programme_module__id'))
        if self.scope is not None:
            rows = rows.filter(programme_module__in=self.scope)
        self.enrolments = list(rows)
        self.offerings = [e.programme_module for e in self.enrolments]

    def _cohorts(self):
        """``{programme id: cohort}`` — which intake's schedule the learner is on."""
        if self.person is None:
            return {}
        return {pe.programme_id: pe.cohort
                for pe in self.person.programme_enrolments.select_related('cohort')}

    def _load_schedule(self):
        """Published blocks for each offering, with their released student material.

        Only student-facing material counts: published, for students, and released.
        A block is kept when it is a shared template block or belongs to the
        learner's own intake.
        """
        from apps.learning.models import ModuleMaterial, ModulePhase, ModuleWeek
        if not self.offerings:
            return
        cohorts = self._cohorts()
        released = (ModuleMaterial.objects
                    .filter(is_published=True, audience=ModuleMaterial.AUDIENCE_STUDENTS)
                    .filter(Q(available_from__isnull=True) | Q(available_from__lte=self.now))
                    .filter(Q(week__isnull=True) | Q(week__is_published=True, week__is_active=True))
                    .only('id', 'kind', 'title', 'lesson_id', 'assessment_id', 'phase_id', 'week_id'))
        phases = (ModulePhase.objects
                  .filter(programme_module__in=self.offerings, is_active=True, is_published=True)
                  .select_related('calendar_event')
                  .prefetch_related(Prefetch('materials', queryset=released, to_attr='released_materials'))
                  .order_by('programme_module_id', 'order', 'id'))
        by_offering = {o.pk: o for o in self.offerings}
        for phase in phases:
            offering = by_offering.get(phase.programme_module_id)
            if offering is None:
                continue
            cohort = cohorts.get(offering.programme_id)
            if phase.cohort_id and (cohort is None or phase.cohort_id != cohort.pk):
                continue
            self.phases.setdefault(phase.programme_module_id, []).append(phase)

    def _load_work(self):
        """Finished lessons, best attempt scores, study sessions, attempts and submissions."""
        from apps.learning import progress
        from apps.learning.models import StudySession
        self.lessons_done = progress._done_lesson_ids(self.user)
        self.attempt_best = progress._attempt_scores(self.user)
        self.study_rows = list(StudySession.objects.filter(student=self.user)
                               .values('lesson_id', 'lesson__module_id', 'status', 'updated_at'))
        from apps.assessments.models import AssessmentAttempt, AssignmentSubmission
        self.attempts = list(AssessmentAttempt.objects
                             .filter(student=self.user, status__in=['submitted', 'marked'])
                             .select_related('assessment', 'assessment__module')
                             .defer('draft')
                             .order_by('submitted_at', 'id'))
        self.submissions = list(AssignmentSubmission.objects
                                .filter(student=self.user, submitted_at__isnull=False)
                                .select_related('assessment', 'assessment__module')
                                .order_by('submitted_at', 'id'))

    def _load_classes(self):
        """Live classes held for the learner since enrolment, the next one, and attendance.

        Classes held before they registered are not theirs to have missed. A
        register mark (present / late) counts as attendance as well as a
        :class:`~apps.livesessions.models.SessionJoin`, because a class can be
        attended from a room link.
        """
        from apps.communication.models import Attendance, MeetingRoom
        from apps.livesessions.audience import visible_sessions
        from apps.livesessions.models import SessionJoin
        if not self.offerings:
            return
        started = {e.programme_module_id: (e.started_at or e.created_at) for e in self.enrolments}
        rooms = visible_sessions(self.user, MeetingRoom.objects.filter(
            module__in=self.offerings, scheduled_start__isnull=False)
            .exclude(session_kind=MeetingRoom.KIND_CONSULT))
        for room in rooms.order_by('scheduled_start').select_related('module'):
            if room.scheduled_start < self.now:
                since = started.get(room.module_id)
                if since and room.scheduled_start < since - timedelta(days=1):
                    continue
                self.classes.setdefault(room.module_id, []).append(room)
            elif room.module_id not in self.next_class:
                self.next_class[room.module_id] = room
        self.attended = dict(SessionJoin.objects.filter(user=self.user)
                             .values_list('meeting_id', 'first_clicked_at'))
        for meeting_id, seen, created in (Attendance.objects
                                 .filter(student=self.user, session__meeting__isnull=False,
                                         status__in=[Attendance.STATUS_PRESENT, Attendance.STATUS_LATE])
                                 .values_list('session__meeting_id', 'first_seen_at', 'created_at')):
            self.attended.setdefault(meeting_id, seen or created)

    def _load_grades(self):
        """Grade rows per module and the most recent unresolved risk flag."""
        from apps.analytics.models import RiskFlag
        from .models import Grade
        self.grades = {g.module_id: g for g in Grade.objects.filter(student=self.user)}
        self.risk = (RiskFlag.objects.filter(student=self.user, resolved=False)
                     .order_by('-created_at').first())

    def _load_open_assessments(self):
        """Assessments on the learner's modules that are open to sit now."""
        from apps.assessments.models import Assessment
        if not self.offerings:
            return
        self.open_assessments = list(
            Assessment.objects
            .filter(module__in=self.offerings, status=Assessment.STATUS_OPEN)
            .filter(Q(available_from__isnull=True) | Q(available_from__lte=self.now))
            .filter(Q(available_to__isnull=True) | Q(available_to__gte=self.now))
            .select_related('module').order_by('available_to', 'id'))

    def attempt_pct(self, attempt):
        """An attempt's score as a percentage clamped to 0–100, or ``None`` without total marks."""
        total = float(getattr(attempt.assessment, 'total_marks', 0) or 0)
        if total <= 0:
            return None
        return max(0.0, min(100.0, float(attempt.score or 0) / total * 100))

    def phase_items(self, phase):
        """``(tracked, done)`` for one block — lessons and assessments only."""
        tracked = done = 0
        for material in getattr(phase, 'released_materials', []):
            if material.lesson_id:
                tracked += 1
                done += material.lesson_id in self.lessons_done
            elif material.assessment_id:
                tracked += 1
                done += material.assessment_id in self.attempt_best
        return tracked, done


def load(user, scope=None):
    """Gather everything about ``user`` once — a :class:`StudentData` (limited to ``scope`` if given)."""
    return StudentData(user, scope)


def _module_card(data, enrolment):
    """Build the progress card for one module enrolment.

    Combines the schedule (blocks with trackable work and how much is done), live
    classes held since enrolment, results (oldest first, so the sparkline reads
    left to right), overall progress across every released unit of work, and the
    last time anything happened in the module. A locked enrolment gets a status
    such as "Access lapsed" instead of "Active — paid", which on a lapsed
    subscription would read as a contradiction.
    """
    offering = enrolment.programme_module
    oid = offering.pk
    now = data.now

    blocks, tracked, done = [], 0, 0
    kinds = {}
    for phase in data.phases.get(oid, []):
        t, d = data.phase_items(phase)
        tracked += t
        done += d
        for material in phase.released_materials:
            kinds[material.kind] = kinds.get(material.kind, 0) + 1
        blocks.append({
            'label': phase.short_label, 'title': phase.display_title,
            'tracked': t, 'done': d, 'pct': _pct(d, t),
            'is_current': phase.is_current, 'date': phase.assessment_date,
            'complete': bool(t) and d >= t,
        })
    materials_released = sum(kinds.values())

    held = data.classes.get(oid, [])
    joined = [room for room in held if room.pk in data.attended]
    missed = [room for room in held if room.pk not in data.attended]

    results = [{'pct': round(p), 'title': a.assessment.title, 'when': a.submitted_at,
                'passed': a.passed, 'kind': a.assessment.get_kind_display()}
               for a in data.attempts if a.assessment.module_id == oid
               for p in [data.attempt_pct(a)] if p is not None]
    avg = round(sum(r['pct'] for r in results) / len(results)) if results else None
    trend = None
    if len(results) >= 2:
        recent = results[-3:]
        trend = recent[-1]['pct'] - recent[0]['pct']

    units = tracked + len(held)
    units_done = done + len(joined)
    pct = _pct(units_done, units)

    stamps = [data.attended[r.pk] for r in joined if data.attended.get(r.pk)]
    stamps += [a.submitted_at for a in data.attempts if a.assessment.module_id == oid and a.submitted_at]
    stamps += [r['updated_at'] for r in data.study_rows if r['lesson__module_id'] == oid]
    last_active = max(stamps) if stamps else None
    quiet_days = (now - last_active).days if last_active else None

    grade = data.grades.get(oid)
    unlocked = enrolment.is_unlocked
    if not unlocked:
        status = {'active': 'Access lapsed', 'trial': 'Trial ended',
                  'expired': 'Expired'}.get(enrolment.status, 'Locked')
        tone = 'secondary'
    elif pct is None:
        status, tone = 'Nothing to track yet', 'secondary'
    elif pct >= STRONG_PCT and (avg is None or avg >= PASS_PCT):
        status, tone = 'On track', 'success'
    elif pct >= 40:
        status, tone = 'Keep going', 'warning'
    else:
        status, tone = 'Falling behind', 'danger'

    return {
        'enrolment': enrolment,
        'offering': offering,
        'name': offering.display_name,
        'code': offering.code,
        'accent': offering.accent,
        'unlocked': unlocked,
        'status': status, 'tone': tone,
        'pct': pct, 'units': units, 'units_done': units_done,
        'work': {'tracked': tracked, 'done': done, 'pct': _pct(done, tracked)},
        'materials_released': materials_released,
        'blocks': blocks,
        'classes': {'held': len(held), 'joined': len(joined), 'missed': missed,
                    'pct': _pct(len(joined), len(held)), 'next': data.next_class.get(oid)},
        'results': results, 'avg': avg, 'avg_tone': _score_tone(avg),
        'best': max((r['pct'] for r in results), default=None),
        'trend': trend,
        'spark': _sparkline([r['pct'] for r in results]),
        'grade': grade,
        'last_active': last_active, 'quiet_days': quiet_days,
        'mocks_released': kinds.get('mock_exam', 0),
        'mocks_sat': sum(1 for a in data.attempts if a.assessment.module_id == oid
                         and a.assessment.kind == 'mock_exam'),
        'url': _url('learning:module-feed', oid),
        'live_url': _url('learning:module-feed-tab', oid, 'live'),
        'assess_url': _url('learning:module-feed-tab', oid, 'assessments'),
        'mocks_url': _url('learning:module-feed-tab', oid, 'mocks'),
    }


def general_cards(user):
    """One card per programme General module ``user`` is on — for a parent.

    General is free, open and never an enrolment row (:mod:`apps.learning.general`),
    so it has no progress, results or fees to report and stays out of every
    number on this page. What it does have is the programme-wide side of the
    year — onboarding, the timetable, programme-wide briefings and sessions — so
    the card says that, names the next programme-wide class the student is in the
    audience for, and links only to pages a parent may open (the calendar and the
    attendance register; the module feed itself is the student's).
    """
    from apps.communication.models import MeetingRoom
    from apps.learning.general import general_offerings_for
    from apps.livesessions.audience import visible_sessions

    person = getattr(user, 'profile', None)
    try:
        offerings = list(general_offerings_for(person).order_by('programme__institution__code',
                                                                'programme__code'))
    except Exception:
        logger.exception('progress_board: general modules failed for user %s', user.pk)
        return []
    now = timezone.now()
    cards = []
    for offering in offerings:
        try:
            upcoming = visible_sessions(user, MeetingRoom.objects.filter(
                module=offering, scheduled_start__gte=now, is_active=True)
                .exclude(session_kind=MeetingRoom.KIND_CONSULT))
            next_class = upcoming.order_by('scheduled_start').first()
        except Exception:
            logger.exception('progress_board: general sessions failed (%s)', offering.pk)
            next_class = None
        cards.append({
            'offering': offering,
            'name': offering.display_name,
            'code': offering.code,
            'accent': offering.accent,
            'programme': offering.programme,
            'next_class': next_class,
            'calendar_url': _url('livesessions:calendar'),
        })
    return cards


def _sparkline(values, width=120, height=32):
    """SVG polyline points for a 0–100 series. ``''`` below two points."""
    if len(values) < 2:
        return ''
    step = width / (len(values) - 1)
    return ' '.join(f'{round(i * step, 1)},{round(height - v / 100 * height, 1)}'
                    for i, v in enumerate(values))


def _activity(data, weeks=ACTIVITY_WEEKS):
    """Things done per week for the last ``weeks`` weeks, oldest first."""
    stamps = []
    stamps += [when for when in data.attended.values() if when]
    stamps += [a.submitted_at for a in data.attempts if a.submitted_at]
    stamps += [s.submitted_at for s in data.submissions if s.submitted_at]
    stamps += [r['updated_at'] for r in data.study_rows if r['updated_at']]

    today = timezone.localdate()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    counts = [0] * weeks
    for when in stamps:
        day = timezone.localtime(when).date()
        index = (day - start).days // 7
        if 0 <= index < weeks:
            counts[index] += 1
    peak = max(counts) or 1
    return {
        'weeks': [{'start': start + timedelta(weeks=i), 'count': n,
                   'height': max(4, round(n / peak * 100)) if n else 0}
                  for i, n in enumerate(counts)],
        'total': sum(counts),
        'this_week': counts[-1],
        'last_week': counts[-2] if weeks > 1 else 0,
    }


def _results_trend(data, limit=20):
    """Every marked or submitted result, oldest first, for the trend chart."""
    points = []
    for attempt in data.attempts[-limit:]:
        pct = data.attempt_pct(attempt)
        if pct is None:
            continue
        points.append({'pct': round(pct), 'title': attempt.assessment.title,
                       'module': attempt.assessment.module, 'when': attempt.submitted_at,
                       'passed': attempt.passed})
    width, height = 600, 160
    if points:
        step = width / max(1, len(points) - 1)
        for i, p in enumerate(points):
            p['x'] = round(i * step if len(points) > 1 else width / 2, 1)
            p['y'] = round(height - p['pct'] / 100 * height, 1)
    return {
        'points': points,
        'line': ' '.join(f"{p['x']},{p['y']}" for p in points),
        'width': width, 'height': height,
        'pass_y': round(height - PASS_PCT / 100 * height, 1),
        'avg': round(sum(p['pct'] for p in points) / len(points)) if points else None,
    }


def _improvements(data, cards, limit=6):
    """Concrete, data-backed things to do next, most urgent first.

    Each row names the evidence (the number that triggered it) and links to the
    page where it is fixed, because "work harder" is not advice.

    Rules cover open papers not yet sat, failed papers with a retake left, and
    more. Rows are de-duplicated by title — a module can trip several rules, but
    each is said once — and at most two "is behind" rows are kept: past that it
    is one message repeated.
    """
    out = []

    def add(priority, tone, icon, title, body, cta, url):
        """Queue a suggestion; rows without a ``url`` are dropped."""
        if url:
            out.append({'priority': priority, 'tone': tone, 'icon': icon, 'title': title,
                        'body': body, 'cta': cta, 'url': url})

    for card in cards:
        name = card['name']
        if not card['unlocked']:
            add(1, 'warning', 'bi-lock', f'Unlock {name}',
                'This module is locked, so none of its classes or material can count yet.',
                'Open module', card['url'])
            continue

        missed = card['classes']['missed']
        if missed:
            n = len(missed)
            add(3, 'warning', 'bi-camera-video-off',
                f"{n} missed class{'es' if n != 1 else ''} in {name}",
                f"You joined {card['classes']['joined']} of {card['classes']['held']} live classes. "
                'Watch the recordings to catch up on what was covered.',
                'Watch recordings', card['live_url'])

        if card['avg'] is not None and card['avg'] < PASS_PCT + 10:
            add(4, 'danger' if card['avg'] < PASS_PCT else 'warning', 'bi-bullseye',
                f"Results in {name} average {card['avg']}%",
                'Redo the questions you got wrong and sit the next paper under a timer.',
                'Practise', card['assess_url'])

        if card['mocks_released'] and not card['mocks_sat']:
            add(6, 'info', 'bi-file-earmark-ruled', f'No mock exam sat yet in {name}',
                f"{card['mocks_released']} mock paper{'s are' if card['mocks_released'] != 1 else ' is'} "
                'released. A mock under exam conditions is the best predictor of the real paper.',
                'See mock exams', card['mocks_url'])

        if card['quiet_days'] is not None and card['quiet_days'] >= QUIET_DAYS:
            add(5, 'warning', 'bi-hourglass-split', f'{name} has gone quiet',
                f"Nothing done here for {card['quiet_days']} days. One short session gets it moving again.",
                'Open module', card['url'])
        elif card['pct'] is not None and card['pct'] < 40 and card['units']:
            add(5 + card['pct'] / 1000, 'warning', 'bi-graph-down', f'{name} is behind',
                f"{card['units_done']} of {card['units']} piece{'s' if card['units'] != 1 else ''} "
                'of released work done.',
                'Open module', card['url'])

    sat = set(data.attempt_best)
    for assessment in data.open_assessments:
        if assessment.pk in sat or assessment.kind == 'assignment':
            continue
        closes = assessment.available_to
        body = (f'{assessment.module.display_name if assessment.module else ""} — '
                f'{assessment.get_kind_display().lower()} open now.').lstrip(' —')
        add(2 if closes and closes - data.now <= timedelta(days=3) else 4,
            'info', 'bi-ui-checks', f'Sit "{assessment.title}"', body,
            'Start', _url('assessments:take', assessment.pk))

    taken = {}
    for attempt in data.attempts:
        taken.setdefault(attempt.assessment_id, []).append(attempt)
    for rows in taken.values():
        last = rows[-1]
        assessment = last.assessment
        allowed = assessment.attempts_allowed or 0
        if last.status == 'marked' and not last.passed and (not allowed or len(rows) < allowed) \
                and assessment.status == 'open':
            add(3, 'danger', 'bi-arrow-repeat', f'Retake "{assessment.title}"',
                f'Last attempt: {round(data.attempt_pct(last) or 0)}%. '
                f"{'Unlimited attempts' if not allowed else f'{allowed - len(rows)} attempt(s) left'}.",
                'Retake', _url('assessments:take', assessment.pk))

    out.sort(key=lambda r: r['priority'])
    seen, unique, behind = set(), [], 0
    for row in out:
        if row['title'] in seen:
            continue
        if row['icon'] == 'bi-graph-down':
            behind += 1
            if behind > 2:
                continue
        seen.add(row['title'])
        unique.append(row)
    return unique[:limit]


def _strengths(cards):
    """What is going well — said briefly, because it is still feedback."""
    out = []
    graded = [c for c in cards if c['avg'] is not None]
    if graded:
        best = max(graded, key=lambda c: c['avg'])
        if best['avg'] >= STRONG_PCT:
            out.append(f"Your strongest results are in {best['name']} ({best['avg']}% average).")
    for card in cards:
        if card['classes']['held'] >= 3 and card['classes']['pct'] == 100:
            out.append(f"Every live class in {card['name']} attended.")
    done_blocks = [(c['name'], b['label']) for c in cards for b in c['blocks'] if b['complete']]
    if done_blocks:
        name, label = done_blocks[-1]
        out.append(f'{label} preparation in {name} fully worked through.')
    return out[:3]


def build_dashboard(user, scope=None, data=None):
    """Everything the progress page renders for ``user``.

    The overall ``dash`` value drives the progress ring, drawn with
    ``stroke-dasharray`` on a circle of ``r=52`` (circumference 326.7).
    """
    data = data or load(user, scope)
    cards = []
    for enrolment in data.enrolments:
        try:
            cards.append(_module_card(data, enrolment))
        except Exception:
            logger.exception('progress_board: module card failed (%s)', enrolment.pk)

    units = sum(c['units'] for c in cards if c['unlocked'])
    units_done = sum(c['units_done'] for c in cards if c['unlocked'])
    held = sum(c['classes']['held'] for c in cards)
    joined = sum(c['classes']['joined'] for c in cards)
    trend = _results_trend(data)
    overall = _pct(units_done, units)

    readiness = countdown = None
    try:
        from apps.learning import progress
        readiness = progress.readiness(user, data.person)
    except Exception:
        logger.exception('progress_board: readiness failed')
    try:
        from apps.myhub.dashboard import exam_countdown
        countdown = exam_countdown(user)
    except Exception:
        logger.exception('progress_board: countdown failed')

    return {
        'cards': cards,
        'overall': {
            'pct': overall, 'tone': _tone(overall),
            'units': units, 'units_done': units_done,
            'dash': round((overall or 0) / 100 * 326.7, 1),
        },
        'classes': {'held': held, 'joined': joined, 'pct': _pct(joined, held)},
        'results': {'count': len(data.attempts), 'avg': trend['avg'], 'tone': _score_tone(trend['avg']),
                    'passed': sum(1 for a in data.attempts if a.passed)},
        'trend': trend,
        'activity': _activity(data),
        'improve': _improvements(data, cards),
        'strengths': _strengths(cards),
        'risk': data.risk,
        'readiness': readiness,
        'countdown': countdown,
        'has_modules': bool(cards),
    }
