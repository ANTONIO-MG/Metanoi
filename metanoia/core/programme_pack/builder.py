"""Spine + plan → the programme on the LMS, idempotently.

The **spine** is the programme written down in Python (see
``core/academic_spine_sample.py``); the **plan** is what
:func:`core.programme_pack.scanner.scan` read from the pack's folders.
:class:`Builder` creates institution → programme → cohort → module offerings,
one General block for programme-wide material, a dated ModulePhase/ModuleWeek
per module with its unit lessons and files, live sessions and calendar dates.

Every row is keyed so a re-run corrects instead of duplicating:
``ModuleMaterial.source_ref`` = ``<key>/…``, ``Lesson.slug`` = ``<key>-…``,
``CalendarEvent.description`` carries ``[<key>:<ref>]`` and a session's
``MeetingRoom.external_event_id`` is ``seed:<key>:<ref>``. Lessons that already
have content are left alone (students' section progress hangs off them) unless
``refresh=True`` (rebuild, progress lost) or ``update=True`` (new pack on a live
cohort: text rewritten into existing sections so progress, notes and bookmarks
survive; changed files replaced; rows for dropped files retired onto their
renumbered successor, or unpublished). General's rows are keyed
``<key>/<cohort>/GEN/…``; older per-module copies are retired after anything
pointing at them is re-pointed. Tutor-pack material is ``audience='staff'``,
which ``ModuleMaterial.save`` never lets be published. Everything is saved
``_quiet`` so seeding does not send the class a notice per row.

Module constants
----------------
``STEP_TITLES``
    What each numbered step of a unit is called on the lesson, and when to use
    it (the pack's own "How to use this pack", file by file).
``FACILITATOR_STEP``
    Step 0: the unit's facilitator pack, readable by tutors only.
``MATERIAL_ORDER``
    Base sort orders for a module's materials: the unit lesson, live sessions,
    student files, then staff-only files.
"""

import datetime as dt
import hashlib
import logging
import os
import re
from collections import defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from . import docconv

logger = logging.getLogger('apps')

STEP_TITLES = {
    1: ('Pre-read: framework on a page', 'Before the live session (10 minutes).'),
    2: ('Live session handout', 'Open it in the live session. It holds the method and the live question. '
                                'Work the question at exam pace.'),
    3: ('Live question: suggested solution', 'After the session. Mark your own answer line by line in the '
                                             'Excel workbook, then read the solution for the reasons and the '
                                             'common weak answers.'),
    4: ('Topic guide', 'After the session. The concepts, the method, a worked example and the common traps.'),
    5: ('Practice question', 'Attempt it on your own, timed at 2 minutes a mark, before you look at the '
                             'solution.'),
    6: ('Practice solution', 'Mark yourself in the Excel workbook, read the common weak answers, and record '
                             'the score in your Attempt Log.'),
    7: ('Self-study item', 'Study it and answer the three check questions before the next open session.'),
    8: ('One-pager', 'Keep it beside you when you practise: the method on a single page.'),
}
FACILITATOR_STEP = 0

MATERIAL_ORDER = {'lesson': 1, 'live': 3, 'student': 10, 'staff': 200}


def _hash(text, n=10):
    """The first ``n`` hex digits of the SHA-1 of ``text``."""
    return hashlib.sha1(text.encode()).hexdigest()[:n]


def _quiet_save(obj, **kwargs):
    """Save ``obj`` flagged ``_quiet`` so seeding sends no notification per row."""
    obj._quiet = True
    obj.save(**kwargs)
    return obj


class Report:
    """Tally of rows created and kept/updated during a build, plus free-text notes."""
    def __init__(self):
        """Start an empty report."""
        self.created = defaultdict(int)
        self.updated = defaultdict(int)
        self.notes = []

    def made(self, label, created=True):
        """Count one ``label`` row as created (or as kept/updated)."""
        (self.created if created else self.updated)[label] += 1

    def lines(self):
        """The report as aligned text lines for the command output and review page."""
        labels = sorted(set(self.created) | set(self.updated))
        out = [f'  {label:<28} {self.created.get(label, 0):>4} new  {self.updated.get(label, 0):>4} kept/updated'
               for label in labels]
        return out + [f'  · {n}' for n in self.notes]


class Builder:
    """Load one spine + plan onto the LMS (see the module docstring).

    :param spine: the programme as plain data (:func:`core.programme_pack.schedule.spine_from_plan`
        or a hand-written spine module).
    :param plan: what :func:`core.programme_pack.scanner.scan` read from the pack.
    :param refresh: rebuild lessons that already have content (progress is lost).
    :param update: rewrite a live cohort in place, keeping progress (ignored with ``refresh``).
    :param into: an existing :class:`~apps.learning.models.Cohort`. The pack is then
        loaded into that cohort's programme and its existing modules, and nothing is
        created at the programme level: no institution, programme, cohort, module
        offering, price or bundle (see :func:`map_modules`).
    :param progress: ``progress(done, total, label)``, called as the build moves on
        (the review page's bar).

    State gathered during the run: ``offerings`` (module code → taught
    ``ProgrammeModule``), ``phases`` (code → ``ModulePhase``), ``general`` /
    ``general_phase`` / ``general_weeks`` (the programme's General module, its
    block for the cohort and its weeks by spine week number), ``weeks`` ((code,
    week number) → ``ModuleWeek``), ``topics`` ((code, unit) → ``Topic``),
    ``calendars`` (year → ``AcademicCalendar``), ``events`` (ref →
    ``CalendarEvent``), ``_legacy`` ((old per-module ref, the General row
    replacing it)) and ``_touched`` (every material ``source_ref`` this run
    placed).
    """
    def __init__(self, spine, plan, *, actor=None, refresh=False, update=False, publish=True, ai=None,
                 out=None, into=None, progress=None):
        """Set up the build state; nothing is written until :meth:`run`."""
        self.into = into
        self.spine = spine
        self.plan = plan
        self.key = spine['key']
        self.actor = actor
        self.refresh = refresh
        self.update = update and not refresh
        self.publish = publish
        self.ai = ai or {}
        self.out = out or (lambda message: None)
        self.progress = progress
        self._done = 0
        self._total = 8 + sum(len(b['units']) for b in plan.get('blocks', [])) + len(plan.get('mocks', []))
        self.report = Report()
        self.tz = ZoneInfo(spine.get('timezone') or 'Africa/Johannesburg')
        self.root = plan['root']
        self._stored = {}
        self._docs = {}
        self._twins = self._index_twins()
        self.offerings = {}
        self.phases = {}
        self.general = None
        self.general_phase = None
        self.general_weeks = {}
        self._legacy = []
        self.weeks = {}
        self.topics = {}
        self.calendars = {}
        self.events = {}
        self.onboarding_lessons = []
        self._touched = set()

    def run(self):
        """Run every stage in one transaction and return the :class:`Report`.

        Units and mocks report one progress step each.
        """
        with transaction.atomic():
            self._stage('Matching modules and the cohort', self._spine_rows)
            self._stage('Setting up the weeks', self._phases_and_weeks)
            self._stage('Writing calendar dates', self._calendar)
            self._stage('Loading onboarding and blueprint files', self._onboarding)
            self._units()
            self._mocks()
            self._stage('Scheduling live sessions', self._sessions)
            self._stage('Tidying replaced material', self._retire_legacy)
            if self.update:
                self._stage('Retiring files the new version dropped', self._retire_stale)
            else:
                self._step('')
            self._stage('Publishing', self._finish)
        self._tick('Done')
        return self.report

    def _tick(self, label):
        """Report progress; a failing progress callback never stops a build."""
        if self.progress:
            try:
                self.progress(min(self._done, self._total), self._total, label)
            except Exception:
                pass

    def _step(self, label):
        """Count one step done and report it."""
        self._done += 1
        self._tick(label)

    def _stage(self, label, work):
        """Report ``label``, run ``work`` and count it as one step."""
        self._tick(label)
        work()
        self._done += 1

    def at(self, value):
        """``'2026-11-07T08:00'`` (local) → an aware datetime. A bare date is 09:00 local, so an
        all-day date lands on the same day whatever time zone the site renders in."""
        if isinstance(value, dt.datetime):
            return value if timezone.is_aware(value) else value.replace(tzinfo=self.tz)
        text = str(value)
        if 'T' not in text:
            text += 'T09:00'
        return dt.datetime.fromisoformat(text).replace(tzinfo=self.tz)

    @staticmethod
    def day(value):
        """``value`` as a :class:`datetime.date` (ISO text accepted; ``None`` passes through)."""
        if value is None or isinstance(value, dt.date):
            return value
        return dt.date.fromisoformat(str(value)[:10])

    def _fill(self, obj, fields, created):
        """Set ``fields`` on a new row; on an existing one only fill blanks."""
        changed = []
        for name, value in fields.items():
            current = getattr(obj, name)
            if created or current in ('', None):
                if current != value:
                    setattr(obj, name, value)
                    changed.append(name)
        if changed and not created:
            obj.save()
        return changed

    def _index_twins(self):
        """twin key → Word original, wherever in the pack it sits."""
        index = {}
        for entry in self._every_file():
            if entry['ext'] == '.docx':
                index.setdefault(entry['twin'], entry)
        return index

    def _every_file(self):
        """Every file entry in the plan: units, loose buckets and mocks."""
        plan = self.plan
        for block in plan['blocks']:
            for unit in block['units']:
                yield from unit['files']
        for bucket in ('onboarding', 'strategy', 'other'):
            yield from plan[bucket]
        for mock in plan['mocks']:
            yield from mock['files']

    def abspath(self, entry):
        """The absolute path of a plan file entry."""
        return os.path.join(self.root, entry['path'])

    def document(self, entry):
        """The readable text of ``entry``: its Word twin if the pack has one."""
        source = entry if entry['ext'] == '.docx' else self._twins.get(entry['twin'], entry)
        path = self.abspath(source)
        if path not in self._docs:
            self._docs[path] = docconv.read_document(path)
        return self._docs[path]

    def store(self, entry):
        """Copy the file into the file store once; return its storage name.

        Stored names are capped so they fit the 100-character ``FileField``.
        """
        from django.core.files import File

        from core.storage import files_storage

        path = self.abspath(entry)
        if path in self._stored:
            return self._stored[path]
        storage = files_storage()
        folder = 'modules/staff-only' if entry['audience'] == 'staff' else 'modules'
        safe = re.sub(r'[^\w.\-]+', '_', entry['name'])
        prefix = f'{folder}/{self.key}/{_hash(entry["path"], 8)}-'
        name = prefix + safe[-max(20, 100 - len(prefix)):]
        size = os.path.getsize(path)
        if storage.exists(name) and storage.size(name) == size and not self._differs(storage, name, path):
            self.report.made('files stored', created=False)
        else:
            replacing = storage.exists(name)
            if replacing:
                storage.delete(name)
            with open(path, 'rb') as fh:
                name = storage.save(name, File(fh))
            self.report.made('files stored')
            if replacing:
                self.report.made('files replaced (new version)')
        self._stored[path] = name
        return name

    @staticmethod
    def _md5(fh):
        """MD5 hex digest of an open binary file, read in chunks."""
        digest = hashlib.md5()
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            digest.update(chunk)
        return digest.hexdigest()

    def _differs(self, storage, name, path):
        """Same size: compare the bytes as well (only when updating — a re-save of
        a document can keep its size and still change a figure)."""
        if not self.update:
            return False
        with storage.open(name, 'rb') as stored, open(path, 'rb') as local:
            return self._md5(stored) != self._md5(local)

    def _existing_rows(self):
        """Existing-only mode: look everything up, create nothing programme-level."""
        cohort = self.into
        self.cohort = cohort
        self.programme = cohort.programme
        self.institution = self.programme.institution
        mapped, missing = map_modules(self.programme, [m['code'] for m in self.spine['modules']])
        if missing:
            raise BuildError(f'{self.programme.full_code} has no module for: {", ".join(missing)}. '
                             'Add the module to the programme first, or rename the pack folders.')
        self.offerings.update(mapped)
        for code, offering in mapped.items():
            self.report.notes.append(f'{code} → {offering.code} ({offering.display_name})')
        from apps.learning.general import general_module_for
        self.general = general_module_for(self.programme)

    def _spine_rows(self):
        """Create or match institution, programme, cohort, module offerings and pricing.

        With ``into`` set this only looks rows up (:meth:`_existing_rows`). Anyone
        already registered on the programme without an intake joins this cohort when
        it is the programme's only intake; otherwise their schedule, which is built
        per intake, would come up empty.
        """
        from apps.learning.models import (Cohort, Institution, Module, ProgrammeEnrolment, Programme,
                                          ProgrammeModule)
        from core import academic_spine

        if self.into is not None:
            return self._existing_rows()

        spec = dict(self.spine['institution'])
        code = spec.pop('code')
        institution, created = Institution.objects.get_or_create(code=code, defaults={'name': spec['name']})
        self._fill(institution, spec, created)
        if created:
            institution.save()
        self.institution = institution
        self.report.made('institution', created)

        spec = dict(self.spine['programme'])
        code = spec.pop('code')
        programme, created = Programme.objects.get_or_create(
            institution=institution, code=code, defaults={'name': spec.get('name', code)})
        self._fill(programme, spec, created)
        if created:
            programme.save()
        self.programme = programme
        self.report.made('programme', created)

        spec = dict(self.spine['cohort'])
        code = spec.pop('code')
        for field in ('start_date', 'end_date'):
            if spec.get(field):
                spec[field] = self.day(spec[field])
        cohort, created = Cohort.objects.get_or_create(programme=programme, code=code,
                                                       defaults={'name': spec.get('name', '')})
        self._fill(cohort, spec, created)
        if created:
            cohort.save()
        self.cohort = cohort
        self.report.made('cohort', created)

        pricing = self.spine.get('pricing') or {}
        fee = Decimal(str(pricing.get('per_month', academic_spine.DEFAULT_PRICE)))
        catalogue = {m['code']: m for m in academic_spine.MODULES}
        for order, module_spec in enumerate(self.spine['modules'], 1):
            canonical = module_spec['catalogue']
            module = Module.objects.filter(code=canonical).first()
            if module is None:
                template = catalogue.get(canonical, {'name': module_spec.get('name', canonical)})
                module = Module.objects.create(code=canonical, name=template['name'],
                                               description=template.get('description', ''),
                                               order=template.get('order', order))
            offering, made = ProgrammeModule.objects.get_or_create(
                programme=programme, code=module_spec['code'],
                defaults={'module': module, 'price_per_month': fee})
            self._fill(offering, {
                'name': module_spec.get('name', ''), 'description': module_spec.get('description', ''),
                'order': module_spec.get('order', order), 'depth_level': module_spec.get('depth', ''),
            }, made)
            if made:
                offering.save()
            if not offering.price_per_month:
                offering.price_per_month = fee
                offering.save(update_fields=['price_per_month', 'updated_at'])
            self.offerings[module_spec['code']] = offering
            self.report.made('module offerings', made)

        self._bundle(pricing, fee)

        from apps.learning.general import general_module_for
        self.general = general_module_for(programme)

        try:
            from apps.communication.models import Community
            Community.for_programme(programme)
        except Exception:                               # pragma: no cover — optional app
            logger.exception('programme pack: no community for %s', programme)

        if programme.cohorts.filter(is_active=True).count() == 1:
            placed = ProgrammeEnrolment.objects.filter(programme=programme, cohort__isnull=True).update(
                cohort=cohort)
            if placed:
                self.report.notes.append(f'{placed} enrolment(s) on {programme.code} placed in {cohort.code}')

    def _bundle(self, pricing, fee):
        """The "all four modules" bundle, exactly as ``apply_module_pricing`` makes it."""
        try:
            from apps.shop import catalogue
            from apps.shop.models import Discount, Product
        except Exception:                               # pragma: no cover
            return
        offerings = list(self.programme.modules.without_general().filter(is_active=True)
                         .order_by('order', 'pk'))
        ours = [o for o in offerings if o.code in self.offerings]
        total = pricing.get('bundle_total')
        if not total or len(ours) != 4:
            return
        for offering in ours:
            catalogue.sync_module(offering)
        items = list(Product.objects.filter(unlocks=Product.UNLOCKS_MODULE, module__in=ours))
        if len(items) != 4:
            self.report.notes.append('bundle skipped: module fee items missing (run manage.py sync_catalogue)')
            return
        name = pricing.get('bundle_name', 'All 4 modules')
        bundle = Discount.objects.filter(name=name, programme=self.programme, code__isnull=True).first()
        created = bundle is None
        if created:
            bundle = Discount(name=name, programme=self.programme)
        bundle.institution = self.institution
        bundle.discount_type = Discount.TYPE_AMOUNT
        bundle.value = fee * 4 - Decimal(str(total))
        bundle.min_spend = Decimal('0')
        bundle.starts_at = bundle.ends_at = bundle.max_uses = None
        bundle.is_active = True
        bundle.save()
        bundle.products.set(items)
        catalogue.sync_programme(self.programme)
        self.report.made('fee bundle', created)

    def _phases_and_weeks(self):
        """Create the General block, each module's phase, its unit topics and dated weeks.

        When loading ``into`` an existing cohort, several packs share that cohort's
        General block, so its dates are widened, never narrowed.
        """
        from apps.learning.models import ModulePhase, ModuleWeek, Topic, WeekTopic

        phase_spec = self.spine['phase']
        units = self.spine.get('units') or {}
        plan_units = {(u['module'], u['unit']): u for b in self.plan['blocks'] for u in b['units']}
        week_specs = self.spine['weeks']

        from apps.learning.general import general_phase_for
        made = general_phase_for(self.general, cohort=self.cohort, create=False) is None
        general = general_phase_for(self.general, cohort=self.cohort)
        starts = self.day(self.spine['cohort'].get('start_date'))
        ends = self.day(phase_spec.get('ends') or self.spine['cohort'].get('end_date'))
        if self.into is not None:
            starts = min(d for d in (general.starts_on, starts) if d) if (general.starts_on or starts) else None
            ends = max(d for d in (general.ends_on, ends) if d) if (general.ends_on or ends) else None
        general.starts_on, general.ends_on = starts, ends
        general.is_published = self.publish
        general.save()
        self.general_phase = general
        self.report.made('general block', made)

        for code, offering in self.offerings.items():
            phase, created = ModulePhase.objects.get_or_create(
                programme_module=offering, cohort=self.cohort,
                kind=phase_spec.get('kind', ModulePhase.KIND_EXAM), sequence=phase_spec.get('sequence', 1),
                defaults={'title': phase_spec['title']})
            phase.title = phase.title or phase_spec['title']
            phase.summary = phase.summary or phase_spec.get('summary', '')[:300]
            phase.starts_on = self.day(phase_spec.get('starts') or self.spine['cohort'].get('start_date'))
            phase.ends_on = self.day(phase_spec.get('ends') or self.spine['cohort'].get('end_date'))
            phase.order = phase_spec.get('order', 0)
            phase.is_published = self.publish
            phase.save()
            self.phases[code] = phase
            self.report.made('preparation blocks', created)

            for unit_number, unit_spec in sorted(self._units_for(code, units, plan_units).items()):
                topic, made = Topic.objects.get_or_create(
                    programme_module=offering, code=topic_code(phase_spec, unit_number),
                    defaults={'title': unit_spec['title'][:250], 'order': unit_number})
                if not made and not topic.title:
                    topic.title = unit_spec['title'][:250]
                    topic.save()
                if unit_spec.get('self_study') and not topic.notes:
                    topic.notes = f'Self-study item: {unit_spec["self_study"]}'
                    topic.save(update_fields=['notes', 'updated_at'])
                self.topics[(code, unit_number)] = topic
                self.report.made('topics', made)

            for spec in week_specs:
                week, made = ModuleWeek.objects.get_or_create(phase=phase, number=spec['number'])
                title = spec['title']
                unit_number = spec.get('unit')
                topic = self.topics.get((code, unit_number)) if unit_number else None
                if topic is not None:
                    title = f'{title} · Unit {unit_number}: {topic.title}'
                week.title = title[:200]
                summary = spec.get('summary', '')
                ai_unit = (self.ai.get('units') or {}).get(f'{code}-{unit_number}') if unit_number else None
                if ai_unit and ai_unit.get('summary'):
                    summary = ai_unit['summary']
                elif topic is not None and not summary:
                    summary = self._unit_why(code, unit_number)
                if made or self.refresh or not week.summary:
                    week.summary = summary
                week.starts_on = self.day(spec.get('starts'))
                week.ends_on = self.day(spec.get('ends'))
                week.order = spec['number']
                week.is_published = self.publish
                week.save()
                if topic is not None:
                    WeekTopic.objects.get_or_create(week=week, topic=topic, defaults={'order': 0})
                self.weeks[(code, spec['number'])] = week
                self.report.made('weeks', made)

    def _units_for(self, code, units, plan_units):
        """``{unit number: spec}`` for module ``code``: the plan's units, overlaid with the spine's."""
        found = {}
        for (module, number), unit in plan_units.items():
            if module == code:
                found[number] = {'title': unit['title']}
        for number, spec in (units.get(code) or {}).items():
            found.setdefault(int(number), {}).update(spec)
        return found

    def _unit_why(self, code, unit_number):
        """The topic guide's own "why this topic matters", as the week's one-liner."""
        for block in self.plan['blocks']:
            for unit in block['units']:
                if unit['module'] != code or unit['unit'] != unit_number:
                    continue
                for entry in unit['files']:
                    if entry['step'] == 4 and entry['ext'] in ('.pdf', '.docx'):
                        doc = self.document(entry)
                        for section in doc['sections']:
                            if 'why' in section['heading'].lower():
                                text = re.sub(r'<[^>]+>', ' ', section['html'])
                                return ' '.join(text.split())[:600]
        return ''

    def week_for(self, code, number):
        """The module week ``number`` of ``code``, or ``None``."""
        return self.weeks.get((code, number)) if number else None

    def week_for_block(self, code, block_number):
        """The module week the spine assigns to teaching block ``block_number``."""
        for spec in self.spine['weeks']:
            if spec.get('block') == block_number:
                return self.weeks.get((code, spec['number']))
        return None

    def week_for_mock(self, code, mock_number):
        """The module week the spine assigns to mock ``mock_number``."""
        for spec in self.spine['weeks']:
            if spec.get('mock') == mock_number:
                return self.weeks.get((code, spec['number']))
        return None

    def general_week(self, number):
        """The week on General standing for spine week ``number`` (same title and
        dates as the modules' week), made on first use: General carries only the
        weeks something programme-wide happens in."""
        from apps.learning.models import ModuleWeek

        if not number:
            return None
        if number not in self.general_weeks:
            spec = next((s for s in self.spine['weeks'] if s['number'] == number), None)
            if spec is None:
                return None
            week, made = ModuleWeek.objects.get_or_create(phase=self.general_phase, number=number)
            week.title = spec['title'][:200]
            if made or self.refresh or not week.summary:
                week.summary = spec.get('summary', '')
            week.starts_on = self.day(spec.get('starts'))
            week.ends_on = self.day(spec.get('ends'))
            week.order = number
            week.is_published = self.publish
            week.save()
            self.general_weeks[number] = week
            self.report.made('weeks (general)', made)
        return self.general_weeks[number]

    def spine_week_at(self, when):
        """The number of the spine week whose dates hold ``when``, or ``None``."""
        day = when.astimezone(self.tz).date()
        for spec in self.spine['weeks']:
            starts, ends = self.day(spec.get('starts')), self.day(spec.get('ends'))
            if starts and ends and starts <= day <= ends:
                return spec['number']
        return None

    def calendar_for(self, year):
        """The institution's academic calendar for ``year``, created on first use."""
        from apps.learning.models import AcademicCalendar
        if year not in self.calendars:
            calendar, created = AcademicCalendar.objects.get_or_create(
                institution=self.institution, year=year,
                defaults={'name': f'{self.institution.name} · {year} academic calendar',
                          'description': f'The {year} dates at {self.institution.name}.'})
            self.calendars[year] = calendar
            self.report.made('academic calendars', created)
        return self.calendars[year]

    def _calendar(self):
        """Write the spine's calendar dates (tagged ``[<key>:<ref>]``), the public holidays,
        and link each phase to the exam event.
        """
        from apps.learning.models import CalendarEvent

        for spec in self.spine.get('events', []):
            start = self.at(spec['start'])
            end = self.at(spec['end']) if spec.get('end') else None
            calendar = self.calendar_for(start.year)
            tag = f'[{self.key}:{spec["ref"]}]'
            offering = self.offerings.get(spec.get('module')) if spec.get('module') else None
            event = CalendarEvent.objects.filter(calendar__institution=self.institution,
                                                 description__contains=tag).first()
            created = event is None
            if created:
                event = CalendarEvent(calendar=calendar)
            event.calendar = calendar
            event.programme = self.programme
            event.programme_module = offering
            event.cohort = self.cohort if spec.get('cohort', True) else None
            event.title = spec['title'][:200]
            event.kind = spec.get('kind', CalendarEvent.KIND_OTHER)
            event.start = start
            event.end = end
            event.all_day = spec.get('all_day', 'T' not in str(spec['start']))
            event.location = spec.get('location', '')
            event.description = (spec.get('description', '') + f' {tag}').strip()
            event.is_published = self.publish
            event.save()
            self.events[spec['ref']] = event
            self.report.made('calendar events', created)

        self._holidays()

        exam_ref = self.spine.get('exam_event')
        if exam_ref and exam_ref in self.events:
            for phase in self.phases.values():
                phase.calendar_event = self.events[exam_ref]
                phase.save(update_fields=['calendar_event', 'updated_at'])

    def _holidays(self):
        """South Africa's public holidays inside the programme's window — the
        no-session days — on the institution's calendar, once."""
        from apps.learning.models import CalendarEvent
        from core.academic_spine import public_holidays

        start = self.day(self.spine['cohort'].get('start_date'))
        end = self.day(self.spine['cohort'].get('end_date'))
        if not (start and end):
            return
        for year in range(start.year, end.year + 1):
            for when, name in public_holidays(year):
                if not (start <= when <= end):
                    continue
                calendar = self.calendar_for(when.year)
                event, created = CalendarEvent.objects.get_or_create(
                    calendar=calendar, kind=CalendarEvent.KIND_HOLIDAY, title=name,
                    programme=None, programme_module=None,
                    defaults={'start': dt.datetime.combine(when, dt.time(9, 0), tzinfo=self.tz),
                              'all_day': True, 'is_published': True,
                              'description': 'South African public holiday — no scheduled sessions.'})
                self.report.made('public holidays', created)

    def full_ref(self, ref):
        """``<key>/<ref>``, shortened with a hash when it would not fit the column."""
        ref = f'{self.key}/{ref}'
        if len(ref) > 140:
            ref = ref[:120] + _hash(ref, 12)
        return ref

    def material(self, *, ref, phase, week=None, kind, title, description='', lesson=None, meeting=None,
                 entry=None, url='', order=0, audience='students', topic=None, preview=False, minutes=None):
        """Create or update the :class:`~apps.learning.models.ModuleMaterial` keyed ``ref``.

        On General, a copy ``organise_general_modules`` already moved there (with its
        key re-written, or not when it was too long to re-write) is adopted, so the
        same thing is never placed on General twice. Staff-audience rows are never
        published.
        """
        from apps.learning.models import ModuleMaterial

        ref = self.full_ref(ref)
        self._touched.add(ref)
        material = ModuleMaterial.objects.filter(source_ref=ref).first()
        if material is None and phase == self.general_phase:
            same = ModuleMaterial.objects.filter(phase=phase, audience=audience)
            if lesson is not None:
                same = same.filter(lesson=lesson)
            elif meeting is not None:
                same = same.filter(meeting=meeting)
            elif entry is not None:
                same = same.filter(file=self.store(entry))
            else:
                same = same.none()
            material = same.first()
            if material is not None:
                material.source_ref = ref
        created = material is None
        if created:
            material = ModuleMaterial(source_ref=ref, created_by=self.actor)
        material.phase, material.week, material.topic = phase, week, topic
        material.kind = kind
        material.title = title[:200]
        material.description = description
        material.lesson = lesson
        material.meeting = meeting
        material.url = url
        material.order = order
        material.audience = audience
        material.is_published = self.publish and audience == 'students'
        material.is_preview = preview
        material.estimated_minutes = minutes
        if entry is not None:
            material.file.name = self.store(entry)
        _quiet_save(material)
        self.report.made('materials (students)' if audience == 'students' else 'materials (staff only)',
                         created)
        return material

    def file_title(self, entry, prefix=''):
        """A material title for a file: its label plus the format (``Excel``, ``Word``…)."""
        ext = entry['ext'].lstrip('.').upper()
        ext = {'XLSX': 'Excel', 'DOCX': 'Word', 'PPTX': 'PowerPoint', 'MD': 'Markdown'}.get(ext, ext)
        return f'{prefix}{entry["label"]} ({ext})'

    def lesson(self, *, slug, title, subtitle, sections, module=None, staff_only=False, programme_wide=False,
               attachments=()):
        """Create (or keep) one reading lesson. ``sections`` is
        ``[{'title', 'html', 'minutes', 'files': [entry]}]`` — or a callable
        returning it, so an existing lesson is kept without re-reading its files.

        ``sections`` is called only when the lesson is (re)built, so the documents are
        read only when they will be used.
        """
        from apps.learning.models import Lesson, LessonBlock, LessonSection

        slug = f'{self.key}-{slug}'[:220]
        lesson = Lesson.objects.filter(slug=slug).first()
        created = lesson is None
        if created:
            lesson = Lesson(slug=slug, created_by=self.actor)
        status = Lesson.STATUS_DRAFT if (staff_only or not self.publish) else Lesson.STATUS_PUBLISHED
        rebuild = created or self.refresh or self.update or not lesson.sections.exists()
        if rebuild and callable(sections):
            sections = sections()
        in_place = rebuild and self.update and not created and lesson.sections.exists()
        if rebuild:
            lesson.title = title[:200]
            lesson.subtitle = subtitle[:300]
            lesson.author_name = 'Metanoia Group'
            lesson.estimated_minutes = max(5, sum(s.get('minutes', 0) for s in sections))
        lesson.module = module
        lesson.status = status
        lesson.visibility = Lesson.VIS_PROGRAMME if programme_wide else Lesson.VIS_MODULE
        _quiet_save(lesson)
        if programme_wide:
            lesson.target_programmes.set([self.programme])
        elif module is not None:
            lesson.target_modules.set([module])
        self.report.made('lessons', created)
        if not rebuild:
            return lesson
        if in_place:
            self._update_sections(lesson, sections)
            return lesson

        lesson.blocks.all().delete()
        lesson.sections.all().delete()
        order = 0
        for index, spec in enumerate(sections):
            section = LessonSection.objects.create(
                lesson=lesson, order=index, title=spec['title'][:200],
                summary=(spec.get('summary') or '')[:300],
                section_type=LessonSection.TYPE_READING, duration_minutes=spec.get('minutes', 0),
                open_by_default=index == 0)
            LessonBlock.objects.create(lesson=lesson, section=None, holds_section=section,
                                       block_type=LessonBlock.TYPE_SECTION, order=order,
                                       data={'title': section.title, 'summary': section.summary})
            order += 1
            LessonBlock.objects.create(lesson=lesson, section=section, block_type=LessonBlock.TYPE_TEXT,
                                       order=order, data={'html': spec['html']})
            order += 1
            for entry in spec.get('files', []):
                block = LessonBlock.objects.create(
                    lesson=lesson, section=section, block_type=LessonBlock.TYPE_FILE, order=order,
                    data={'title': self.file_title(entry), 'description': 'Download the original.'})
                block.media.name = self.store(entry)
                block.save(update_fields=['media'])
                order += 1
        return lesson

    def _update_sections(self, lesson, specs):
        """Write ``specs`` into ``lesson``'s existing sections, position by position,
        keeping each section (and its text block) so the progress, notes and
        bookmarks hanging off them survive. Extra parts are added; parts the new
        version no longer has are removed.

        Existing section and block orders are first parked out of the way
        (``+10000`` / ``+100000``) so the new ones never collide; anything else the
        editor added stays, after the pack's own blocks.
        """
        from apps.learning.models import LessonBlock, LessonSection

        existing = list(lesson.sections.order_by('order', 'pk'))
        changed = False
        for section in existing:
            section.order += 10000
            section.save(update_fields=['order'])
        for block in lesson.blocks.all():
            block.order += 100000
            block.save(update_fields=['order'])
        order = 0
        for index, spec in enumerate(specs):
            if index < len(existing):
                section = existing[index]
                new = (spec['title'][:200], (spec.get('summary') or '')[:300], spec.get('minutes', 0))
                if (section.title, section.summary, section.duration_minutes) != new:
                    changed = True
                section.title, section.summary, section.duration_minutes = new
                section.order = index
                section.open_by_default = index == 0
                section.save()
            else:
                changed = True
                section = LessonSection.objects.create(
                    lesson=lesson, order=index, title=spec['title'][:200],
                    summary=(spec.get('summary') or '')[:300],
                    section_type=LessonSection.TYPE_READING, duration_minutes=spec.get('minutes', 0),
                    open_by_default=index == 0)
            header = LessonBlock.objects.filter(holds_section=section).first()
            if header is None:
                header = LessonBlock(lesson=lesson, section=None, holds_section=section,
                                     block_type=LessonBlock.TYPE_SECTION)
            header.order, header.data = order, {'title': section.title, 'summary': section.summary}
            header.save()
            order += 1
            blocks = list(section.blocks.order_by('order', 'pk'))
            texts = [b for b in blocks if b.block_type == LessonBlock.TYPE_TEXT]
            files = [b for b in blocks if b.block_type == LessonBlock.TYPE_FILE]
            text = texts[0] if texts else LessonBlock(lesson=lesson, section=section,
                                                      block_type=LessonBlock.TYPE_TEXT)
            if (text.data or {}).get('html') != spec['html']:
                changed = True
            text.order, text.data = order, {'html': spec['html']}
            text.save()
            order += 1
            for extra in texts[1:]:
                extra.delete()
            for position, entry in enumerate(spec.get('files', [])):
                block = files[position] if position < len(files) else LessonBlock(
                    lesson=lesson, section=section, block_type=LessonBlock.TYPE_FILE)
                block.order = order
                block.data = {'title': self.file_title(entry), 'description': 'Download the original.'}
                block.media.name = self.store(entry)
                block.save()
                order += 1
            for extra in files[len(spec.get('files', [])):]:
                extra.delete()
                changed = True
        for gone in existing[len(specs):]:
            gone.delete()
            changed = True
            self.report.notes.append(f'{lesson.slug}: part "{gone.title}" is not in the new pack — removed')
        for block in lesson.blocks.filter(order__gte=100000).order_by('order'):
            block.order = order
            block.save(update_fields=['order'])
            order += 1
        if changed:
            lesson.estimated_minutes = max(5, sum(s.get('minutes', 0) for s in specs))
            lesson.save(update_fields=['estimated_minutes'])
            self.report.made('lessons updated in place')

    def _retire_stale(self):
        """Rows an older version of the pack placed for files this version no longer
        has. A renumbered file (``0_How_to_use…`` → ``1_How_to_use…``) hands its row
        over to its successor; anything else is unpublished, never deleted."""
        from apps.learning.general_organise import retire
        from apps.learning.models import ModuleMaterial

        def squash(ref):
            """``ref`` with the leading step number stripped from its last segment."""
            head, _, tail = ref.rpartition('/')
            return f'{head}/{re.sub(r"^[0-9]+[_ .-]*", "", tail)}'

        successors = {}
        for ref in self._touched:
            successors.setdefault(squash(ref), ref)
        prefix = self.full_ref(f'{self.cohort.code}/')
        stale = (ModuleMaterial.objects.filter(source_ref__startswith=prefix)
                 .exclude(source_ref__in=self._touched)
                 .filter(re_source_ref_kinds()))
        keep_by_ref = {}
        for row in stale:
            successor = successors.get(squash(row.source_ref))
            if successor is not None:
                keep = keep_by_ref.get(successor) or ModuleMaterial.objects.filter(source_ref=successor).first()
                keep_by_ref[successor] = keep
                if keep is not None and keep.pk != row.pk:
                    retire(row, keep)
                    self.report.made('renumbered files handed over')
                    continue
            if row.is_published:
                row.is_published = False
                _quiet_save(row)
            self.report.made('files no longer in the pack (unpublished)')
            self.report.notes.append(f'not in the new pack, unpublished: {row.title}')

    @staticmethod
    def step_heading(number, label):
        """The shouted callout the article turns into a numbered heading."""
        text = f'STEP {number}: {label.upper()}' if number else label.upper()
        return f'<div class="lesson-callout"><p><strong>{docconv._esc(text)}</strong></p></div>'

    def doc_html(self, doc, *, sub_level='h3'):
        """A whole document as one HTML body, its parts as ``sub_level`` headings."""
        parts = []
        if doc.get('meta'):
            parts.append(f'<p class="text-muted"><em>{docconv._esc(doc["meta"])}</em></p>')
        if doc.get('intro_html'):
            parts.append(doc['intro_html'])
        for section in doc['sections']:
            parts.append(f'<{sub_level}>{docconv._esc(section["heading"])}</{sub_level}>{section["html"]}')
        return ''.join(parts)

    def document_sections(self, doc):
        """One lesson section per part of a document (its ``Heading 1``s)."""
        sections = []
        for index, part in enumerate(doc['sections']):
            head = part['heading']
            html = ''
            if index == 0:
                if doc.get('meta'):
                    html += f'<p class="text-muted"><em>{docconv._esc(doc["meta"])}</em></p>'
                html += doc.get('intro_html', '')
            shout = (f'<div class="lesson-callout"><p><strong>{docconv._esc(head.upper())}</strong></p></div>'
                     if len(head) <= 90 else f'<h2>{docconv._esc(head)}</h2>')
            body = shout + html + part['html']
            sections.append({'title': head, 'html': body, 'minutes': docconv.reading_minutes(body)})
        if not sections and doc.get('intro_html'):
            sections.append({'title': doc.get('title') or 'Overview', 'html': doc['intro_html'],
                             'minutes': docconv.reading_minutes(doc['intro_html'])})
        return sections

    def _onboarding(self):
        """Start-here and blueprint documents: a lesson each (programme-wide) plus
        every file — placed once, on the programme's General module, which is
        free and open to everyone on the programme.

        A one-page calendar or a slide deck reads as a jumble of fragments, so only a
        document with at least two real parts becomes a lesson; every file is still
        placed as material.
        """
        from apps.learning.models import Lesson

        titles = self.spine.get('document_titles') or {}
        lessons = []
        for bucket, kind in (('onboarding', 'study_guide'), ('strategy', 'blueprint')):
            for entry in self.plan[bucket]:
                if entry['audience'] != 'students' or entry['ext'] not in ('.pdf', '.docx'):
                    continue
                slug = f'{bucket}-{slugify(entry["label"])[:60]}'
                existing = Lesson.objects.filter(slug=f'{self.key}-{slug}').first()
                if existing is None or self.refresh or self.update:
                    doc = self.document(entry)
                    sections = self.document_sections(doc)
                    if len(sections) < 2:
                        continue
                    subtitle = doc.get('meta') or ''
                else:
                    sections, subtitle = (lambda: []), existing.subtitle
                title = titles.get(entry['twin']) or entry['label']
                lesson = self.lesson(
                    slug=slug, title=title, subtitle=subtitle or self.spine['programme'].get('full_name', ''),
                    sections=sections, programme_wide=True)
                lessons.append((bucket, kind, entry, lesson, title))

        phase = self.general_phase
        order = 0
        for bucket, kind, entry, lesson, title in lessons:
            order += 1
            tail = f'{bucket}/lesson/{entry["twin"]}'
            placed = self.material(ref=f'{self.cohort.code}/GEN/{tail}', phase=phase,
                                   kind=kind, title=f'{"Start here" if bucket == "onboarding" else "Blueprint"} · '
                                                    f'{title}',
                                   lesson=lesson, order=order, preview=True,
                                   minutes=lesson.estimated_minutes,
                                   description='Read online, then download the original below.')
            self._replaces(tail, placed)
        for bucket in ('onboarding', 'strategy', 'other'):
            for entry in self.plan[bucket]:
                order += 1
                staff = entry['audience'] == 'staff'
                kind = 'blueprint' if bucket == 'strategy' else 'document'
                tail = f'file/{entry["path"]}'
                placed = self.material(ref=f'{self.cohort.code}/GEN/{tail}', phase=phase,
                                       kind=kind, entry=entry, audience=entry['audience'],
                                       title=self.file_title(entry, 'Tutor · ' if staff else ''),
                                       order=(MATERIAL_ORDER['staff'] if staff else 50) + order,
                                       preview=not staff)
                self._replaces(tail, placed)

    def _replaces(self, tail, placed, codes=None):
        """Note that ``placed`` (on General) replaces the per-module copies an
        older seed keyed ``<cohort>/<CODE>/<tail>`` — retired at the end of the run."""
        for code in (self.offerings if codes is None else codes):
            self._legacy.append((self.full_ref(f'{self.cohort.code}/{code}/{tail}'), placed))

    def _retire_legacy(self):
        """Retire the older per-module copies recorded by :meth:`_replaces` onto their General row."""
        from apps.learning.general_organise import retire
        from apps.learning.models import ModuleMaterial

        replacing = dict(self._legacy)
        for row in ModuleMaterial.objects.filter(source_ref__in=list(replacing)):
            keep = replacing[row.source_ref]
            if row.pk != keep.pk:
                retire(row, keep)
                self.report.made('per-module copies retired')

    def _units(self):
        """Load every unit of every block, skipping units whose module or week the spine lacks."""
        for block in self.plan['blocks']:
            for unit in block['units']:
                code = unit['module']
                self._step(f'Loading {code} unit {unit["unit"]} · {unit.get("title") or ""}'.rstrip(' ·'))
                if code not in self.offerings:
                    self.report.notes.append(f'unit {code} {unit["unit"]}: module not in the spine — skipped')
                    continue
                week = self.week_for_block(code, block['number'])
                if week is None:
                    self.report.notes.append(f'block {block["number"]}: no week in the spine — skipped')
                    continue
                self._unit(code, block, unit, week)

    def _unit(self, code, block, unit, week):
        """Place one unit: its study-pack lesson, facilitator pack and every file.

        The facilitator pack (step 0, Word) becomes a staff-only lesson readable by
        the unit's tutors.
        """
        offering, phase = self.offerings[code], self.phases[code]
        topic = self.topics.get((code, unit['unit']))
        number = unit['unit']
        title = topic.title if topic else unit['title']
        steps = defaultdict(list)
        for entry in unit['files']:
            if entry['audience'] == 'students' and entry['step'] is not None:
                steps[entry['step']].append(entry)

        ai_unit = (self.ai.get('units') or {}).get(f'{code}-{number}') or {}

        def sections():
            """The unit lesson's sections, one per student step, with the AI overview first."""
            built = []
            for step in sorted(steps):
                if step == FACILITATOR_STEP:
                    continue
                entries = steps[step]
                readable = next((e for e in entries if e['ext'] in ('.pdf', '.docx')), None)
                label, when = STEP_TITLES.get(step, (entries[0]['label'], ''))
                html = self.step_heading(step, label)
                if when:
                    html += f'<div class="lesson-callout"><p><strong>WHEN:</strong> {docconv._esc(when)}</p></div>'
                if readable is not None:
                    html += self.doc_html(self.document(readable))
                built.append({'title': label, 'html': html, 'minutes': docconv.reading_minutes(html),
                              'files': entries})
            if built and ai_unit.get('summary'):
                overview = ('<div class="lesson-callout"><p><strong>IN THIS UNIT:</strong> '
                            f'{docconv._esc(ai_unit["summary"])}</p></div>')
                if ai_unit.get('focus'):
                    overview += '<ul>' + ''.join(f'<li>{docconv._esc(x)}</li>' for x in ai_unit['focus']) + '</ul>'
                built[0]['html'] = overview + built[0]['html']
            return built

        lesson = None
        if any(step != FACILITATOR_STEP for step in steps):
            when = self.spine.get('slots', {}).get(code, {}).get('label', '')
            lesson = self.lesson(
                slug=f'{code.lower()}-u{number}', module=offering,
                title=f'{code} Unit {number}: {title}',
                subtitle=' · '.join(x for x in [week.title.split(' · Unit')[0], when] if x),
                sections=sections)
            self.material(ref=f'{self.cohort.code}/{code}/U{number}/lesson', phase=phase, week=week, topic=topic,
                          kind='study_guide', title=f'Unit {number} study pack · {title}', lesson=lesson,
                          order=MATERIAL_ORDER['lesson'], minutes=lesson.estimated_minutes,
                          description='The whole unit in reading order: pre-read, live handout, solutions, '
                                      'topic guide, practice and self-study.')

        facilitator = [e for e in unit['files'] if e['step'] == FACILITATOR_STEP and e['ext'] == '.docx']
        for entry in facilitator:
            fac = self.lesson(slug=f'{code.lower()}-u{number}-facilitator', module=offering,
                              title=f'{code} Unit {number}: facilitator pack', staff_only=True,
                              subtitle=f'{week.title} · for tutors',
                              sections=lambda entry=entry: self.document_sections(self.document(entry)))
            if fac.sections.exists():
                self.material(ref=f'{self.cohort.code}/{code}/U{number}/facilitator', phase=phase, week=week,
                              topic=topic, kind='document', title=f'Tutor · Unit {number} facilitator pack (read)',
                              lesson=fac, audience='staff', order=MATERIAL_ORDER['staff'])

        for index, entry in enumerate(unit['files']):
            staff = entry['audience'] == 'staff'
            base = MATERIAL_ORDER['staff'] if staff else MATERIAL_ORDER['student']
            step = entry['step'] if entry['step'] is not None else 9
            self.material(ref=f'{self.cohort.code}/{code}/file/{entry["path"]}', phase=phase, week=week,
                          topic=topic, kind=entry['kind'], entry=entry, audience=entry['audience'],
                          title=self.file_title(entry, 'Tutor · ' if staff else ''),
                          order=base + step * 3 + (1 if entry['ext'] in ('.xlsx', '.xls') else 0))

    def _mocks(self):
        """Place every mock's briefing lesson and files.

        The briefing and whatever is for the whole sitting (a marking sheet, a
        full-mock paper examining two modules) sit once on General, in the mock's
        week; a module's own paper stays on that module.
        """
        papers = self.spine.get('mock_papers') or {}
        briefings = {b['mock']: b for b in self.spine.get('mock_briefings', [])}
        for mock in self.plan['mocks']:
            number = mock['number']
            self._step(f'Loading mock {number}')
            briefing = briefings.get(number)
            lesson = None
            if briefing:
                sections = []
                for head, body in briefing['sections']:
                    html = (f'<div class="lesson-callout"><p><strong>{docconv._esc(head.upper())}</strong></p></div>'
                            + body)
                    sections.append({'title': head, 'html': html, 'minutes': docconv.reading_minutes(html)})
                lesson = self.lesson(slug=f'mock-{number}-briefing', title=briefing['title'],
                                     subtitle=briefing.get('subtitle', ''), sections=sections, programme_wide=True)
            spec = next((s for s in self.spine['weeks'] if s.get('mock') == number), None)
            general_number = spec['number'] if spec else None
            if lesson is not None:
                tail = f'mock{number}/briefing'
                placed = self.material(ref=f'{self.cohort.code}/GEN/{tail}', phase=self.general_phase,
                                       week=self.general_week(general_number), kind='mock_exam',
                                       title=f'{mock["title"]} · how it runs', lesson=lesson,
                                       order=MATERIAL_ORDER['lesson'], minutes=lesson.estimated_minutes)
                self._replaces(tail, placed,
                               [c for c in self.offerings if self.week_for_mock(c, number) is not None])
            for entry in mock['files']:
                group = entry.get('group')
                if group in self.offerings:
                    week = self.week_for_mock(group, number)
                    if week is not None:
                        self._mock_file(entry, number, phase=self.phases[group], week=week,
                                        ref=f'{self.cohort.code}/{group}/file/{entry["path"]}')
                    continue
                placed = self._mock_file(entry, number, phase=self.general_phase,
                                         week=self.general_week(general_number),
                                         ref=f'{self.cohort.code}/GEN/file/{entry["path"]}')
                self._replaces(f'file/{entry["path"]}', placed, papers.get(group) if group else None)

    def _mock_file(self, entry, number, *, phase, week, ref):
        """Place one mock file; staff papers and guides say to release them once the paper closes."""
        group = entry.get('group')
        staff = entry['audience'] == 'staff'
        label = entry['label'].lower()
        kind = ('mock_exam' if re.search(r'paper', label) else
                'answers' if re.search(r'guide|solution|memo', label) else 'document')
        return self.material(ref=ref, phase=phase, week=week,
                             kind=kind, entry=entry, audience=entry['audience'],
                             title=self.file_title(entry, ('Tutor · ' if staff else '')
                                                   + (f'{group.replace("_", " ").title()} · '
                                                      if group and group.startswith('PAPER') else '')),
                             description=('Release to candidates when the paper closes: switch the audience '
                                          'to Students.' if staff and kind != 'document' else ''),
                             order=(MATERIAL_ORDER['staff'] if staff else MATERIAL_ORDER['student'])
                             + (entry['step'] or 0))

    def _host(self):
        """The user who hosts live sessions: the spine's ``host_email``, else the first active superuser."""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        email = self.spine.get('host_email')
        host = User.objects.filter(email__iexact=email).first() if email else None
        return host or User.objects.filter(is_superuser=True, is_active=True).order_by('pk').first()

    def _sessions(self):
        """Create the spine's live sessions and list each one as material.

        Title, description and times come from the timetable only when a session is
        created. After that the session belongs to whoever runs it: a class moved or
        renamed on the platform (which moves it in Teams too) must not be put back by
        the next top-up run, and a session cancelled on the platform stays cancelled.
        A session for the whole programme (the full-mock debriefs) is listed once, on
        General, in the week it falls in; the session itself stays cohort-wide.
        """
        from apps.communication.models import MeetingRoom
        from apps.livesessions import services

        host = self._host()
        provider = self.spine.get('provider', MeetingRoom.PROVIDER_TEAMS)
        for spec in self.spine.get('sessions', []):
            code = spec.get('module')
            offering = self.offerings.get(code) if code else None
            start = self.at(spec['start'])
            end = start + dt.timedelta(minutes=spec.get('minutes', 60))
            key = f'seed:{self.key}:{spec["ref"]}'
            meeting = MeetingRoom.objects.filter(external_event_id=key).first()
            created = meeting is None
            if created:
                meeting = MeetingRoom(external_event_id=key, host=host, provider=provider)
            week = self.week_for(code, spec.get('week')) if code else None
            if created:
                meeting.title = spec['title'][:200]
                meeting.description = spec.get('description', '')
                meeting.scheduled_start, meeting.scheduled_end = start, end
            meeting.module = offering
            meeting.institution = self.institution
            meeting.programme = self.programme
            meeting.cohort = self.cohort
            meeting.week = week
            meeting.session_kind = spec.get('kind', MeetingRoom.KIND_CLASS)
            meeting.audience = MeetingRoom.AUDIENCE_MODULE if offering else MeetingRoom.AUDIENCE_COHORT
            if created:
                meeting.is_active = True
            meeting.save()
            self.report.made('live sessions', created)
            if offering is not None:
                try:
                    services.ensure_class_session(meeting)
                except Exception:
                    logger.exception('programme pack: no register for %s', meeting)
            if created:
                try:
                    services.ensure_thumbnail(meeting)
                except Exception:
                    logger.exception('programme pack: no thumbnail for %s', meeting)
            if offering is not None:
                phase = self.phases[code]
                begins = meeting.scheduled_start or start
                finishes = meeting.scheduled_end or end
                local = begins.astimezone(self.tz)
                self.material(ref=f'{self.cohort.code}/{code}/session/{spec["ref"]}', phase=phase, week=week,
                              kind='live', meeting=meeting, title=meeting.title,
                              description=f'{local:%H:%M}–{finishes.astimezone(self.tz):%H:%M} South African time, '
                                          'on Microsoft Teams. The joining link is posted on the session page.',
                              order=MATERIAL_ORDER['live'])
            else:
                begins = meeting.scheduled_start or start
                finishes = meeting.scheduled_end or end
                local = begins.astimezone(self.tz)
                self.material(ref=f'{self.cohort.code}/GEN/session/{spec["ref"]}', phase=self.general_phase,
                              week=self.general_week(spec.get('week') or self.spine_week_at(begins)),
                              kind='live', meeting=meeting, title=meeting.title,
                              description=f'{local:%H:%M}–{finishes.astimezone(self.tz):%H:%M} South African time, '
                                          'on Microsoft Teams. The joining link is posted on the session page.',
                              order=MATERIAL_ORDER['live'])

    def _finish(self):
        """Fix-ups that keep the cohort's calendar its own.

        The APC's phase dates were seeded institution-wide on SAICA's calendar, so
        every SAICA candidate — an IAC candidate included — saw "Case simulation 1".
        They belong to the APC programme and are re-scoped to it.
        """
        from apps.learning.models import CalendarEvent, Programme
        from core.academic_spine import APC_DATES

        if self.institution.code == 'SAICA':
            apc = Programme.objects.filter(institution=self.institution, code='APC').first()
            if apc is not None:
                moved = (CalendarEvent.objects
                         .filter(calendar__institution=self.institution, programme__isnull=True,
                                 title__in=[title for title, *_ in APC_DATES])
                         .update(programme=apc))
                if moved:
                    self.report.notes.append(f'{moved} APC date(s) on the SAICA calendar scoped to the APC')

    def totals(self):
        """This build's :func:`programme_totals`."""
        return programme_totals(self.programme, self.cohort, self.key)


def programme_totals(programme, cohort, key):
    """What is on the LMS for the programme's cohort — the numbers a re-run must not change."""
    from apps.communication.models import MeetingRoom
    from apps.learning.models import CalendarEvent, Lesson, ModuleMaterial, ModulePhase, ModuleWeek

    materials = ModuleMaterial.objects.filter(source_ref__startswith=f'{key}/')
    return {
        'modules': programme.modules.without_general().count(),
        'general': programme.modules.general().count(),
        'phases': ModulePhase.objects.filter(programme_module__programme=programme, cohort=cohort).count(),
        'weeks': ModuleWeek.objects.filter(phase__programme_module__programme=programme,
                                           phase__cohort=cohort).count(),
        'lessons': Lesson.objects.filter(slug__startswith=f'{key}-').count(),
        'materials': materials.count(),
        'materials_students': materials.filter(audience='students').count(),
        'materials_staff': materials.filter(audience='staff').count(),
        'events': CalendarEvent.objects.filter(description__contains=f'[{key}:').count(),
        'sessions': MeetingRoom.objects.filter(external_event_id__startswith=f'seed:{key}:').count(),
    }


def re_source_ref_kinds():
    """Only the rows a pack's files and documents place (``…/file/…`` and the
    onboarding lessons ``…/lesson/<twin>``) — never sessions, weeks or briefings."""
    from django.db.models import Q
    return Q(source_ref__contains='/file/') | Q(source_ref__regex=r'/(onboarding|strategy)/lesson/')


class BuildError(Exception):
    """The pack cannot go where it was pointed; nothing was written."""


def topic_code(phase_spec, unit_number):
    """``U1`` for the default block (exam 1, as the IAC was built); ``T3-U1`` for
    unit 1 of Test 3 — so two packs on one module never share a unit's topic."""
    kind = (phase_spec or {}).get('kind', 'exam')
    sequence = int((phase_spec or {}).get('sequence', 1) or 1)
    if kind == 'exam' and sequence == 1:
        return f'U{unit_number}'
    return f'{kind[:1].upper()}{sequence}-U{unit_number}'


def map_modules(programme, codes):
    """``({pack code: ProgrammeModule}, [unmatched codes])`` on an existing programme.

    The pack's folder code is matched to the programme's own module code first
    (FREP → FREP), then by subject (TAX in the pack → TAXA on Milpark, through
    the canonical catalogue). Nothing is created.
    """
    from .schedule import CATALOGUE_GUESS

    offerings = list(programme.modules.without_general().filter(is_active=True).select_related('module'))
    by_code = {o.code.upper(): o for o in offerings}
    mapped, missing = {}, []
    for code in codes:
        offering = by_code.get(code.upper())
        if offering is None:
            canonical = CATALOGUE_GUESS.get(code.upper(), code.upper())
            same = [o for o in offerings if o.module_id and o.module.code.upper() == canonical]
            offering = same[0] if len(same) == 1 else None
        if offering is None:
            missing.append(code)
        else:
            mapped[code] = offering
    return mapped, missing


def pack_key(cohort, phase_spec):
    """The key a pack for ``cohort``'s block writes under (``source_ref`` prefix,
    lesson slugs, calendar tags, session ids). A block that was built before —
    the sample pack's ``sample`` — keeps its key, so uploading a new version updates it
    instead of adding a second copy; a new block gets its own."""
    from django.utils.text import slugify

    from apps.learning.models import ModuleMaterial

    kind = (phase_spec or {}).get('kind', 'exam')
    sequence = int((phase_spec or {}).get('sequence', 1) or 1)
    refs = (ModuleMaterial.objects.filter(phase__cohort=cohort, phase__kind=kind, phase__sequence=sequence)
            .exclude(source_ref='').values_list('source_ref', flat=True)[:200])
    keys = [r.split('/', 1)[0] for r in refs if '/' in r]
    if keys:
        return max(set(keys), key=keys.count)
    return slugify(f'{cohort.programme.code}{cohort.code}{kind[:1]}{sequence}').replace('-', '')[:15]


def build(spine, plan, **kwargs):
    """Build ``spine`` + ``plan`` and return ``(builder, report)``; ``kwargs`` go to :class:`Builder`."""
    builder = Builder(spine, plan, **kwargs)
    report = builder.run()
    return builder, report
