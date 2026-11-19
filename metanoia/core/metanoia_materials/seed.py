"""The 2026 seed: the ``Metanoia Material`` archive, placed and taught.

Each accepted document is placed on its module → intake → test block → week:
study guides become reading lessons, solution workbooks with a mark grid become
quizzes (solution released only after submitting), everything else is placed as
a document. Coach resources are staff only, never published. The calendar gets
the intake's test dates and live sessions.

Re-running is safe: every row is keyed (``source_ref`` ``seed2026/…``, lesson
slug ``seed2026-…``, ``[seed2026]`` in an event's description) and corrected in
place. ``remove()`` takes it all out again.

Module constants
----------------
``CATALOGUE``
    Catalogue module code → the ``Module`` catalogue code an offering points at.
``INTAKES``
    The 2026 intakes each programme runs. A document that names none goes to
    all of them.
``LATE_INTAKES``
    2025 intakes that sat their Final in 2026 — their own material stays theirs.
``SLOT_*`` / ``SLOT_TITLES``
    The fixed weeks every preparation block gets after its topic weeks
    (revision, mocks, past papers, coach resources).
``SLOT_NUMBER``
    The fixed weeks sit at 90+ so a topic week uploaded later ("Week 4") never
    collides with them; they display by title, never by number.
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import dates, markgrid, textconv
from .catalogue import KIND_ORDER, scan

logger = logging.getLogger('apps')

TAG = 'seed2026'
SAST = ZoneInfo('Africa/Johannesburg')
MANIFEST = os.path.join(settings.BASE_DIR, 'backups', 'seed_2026_manifest.json')

CATALOGUE = {'CGAU': 'GA', 'MACF': 'MA', 'FREP': 'FR', 'TAX': 'TX', 'IAC': 'IAC'}

INTAKES = {'BCTA': ['B26F', 'B26S'], 'PGDA': ['P26F', 'P26S']}
LATE_INTAKES = {'P25F', 'P25S'}
COHORT_NAMES = {
    'B26F': '2026 January intake', 'B26S': '2026 July intake',
    'P26F': '2026 first intake', 'P26S': '2026 second intake',
}

READING_KINDS = {'study_guide', 'blueprint', 'coach_reference', 'planner'}
SOLUTION_RX = re.compile(r'SOLUTION|MARK.?PLAN|MEMO|WORKINGS|MARKER|ANSWER|MODEL', re.I)

MATERIAL_KIND = {
    'study_guide': 'study_guide', 'blueprint': 'blueprint', 'practice': 'questions',
    'answer_sheet': 'answers', 'mock_exam': 'mock_exam', 'mock_solution': 'answers',
    'planner': 'document', 'coach_reference': 'document',
}

SLOT_REVISION, SLOT_MOCKS, SLOT_PAST, SLOT_COACH = 'revision', 'mocks', 'past', 'coach'
SLOT_TITLES = {
    SLOT_REVISION: 'Revision · study guides & blueprint',
    SLOT_MOCKS: 'Mock exams',
    SLOT_PAST: 'Past papers & solutions',
    SLOT_COACH: 'Coach resources (staff only)',
}
SLOT_NUMBER = {SLOT_REVISION: 90, SLOT_MOCKS: 91, SLOT_PAST: 92, SLOT_COACH: 93}


class SeedReport:
    """Counts and notes from a seed run, plus ``created`` (model label → ids created
    by this run) and the spine events it unpublished — both written to the
    manifest so :func:`remove` can undo the run.
    """
    def __init__(self):
        """Start an empty report."""
        self.counts = defaultdict(int)
        self.notes = []
        self.created = defaultdict(list)
        self.unpublished_spine_events = []

    def add(self, label, n=1):
        """Add ``n`` to the count for ``label``."""
        self.counts[label] += n

    def lines(self):
        """The report as aligned text lines."""
        out = [f'  {k:<38} {v}' for k, v in sorted(self.counts.items())]
        return out + [f'  · {n}' for n in self.notes]


class Seeder:
    """Place every accepted document of the archive at ``root`` (see the module docstring).

    Caches kept during a run: ``_lessons`` and ``_quizzes`` ((doc key, offering
    pk) → row), ``_files`` (doc key → stored name), ``_phases`` ((offering pk,
    cohort pk, kind, sequence) → ``ModulePhase``), ``_weeks`` ((phase pk, slot) →
    ``ModuleWeek``) and ``_sessions`` ((offering pk, cohort code) → [(start,
    end)]).
    """
    def __init__(self, root, *, publish=True, actor=None, out=None):
        """Set up the run; nothing is written until :meth:`run`."""
        self.root = root
        self.publish = publish
        self.actor = actor
        self.out = out or (lambda msg: None)
        self.report = SeedReport()
        self._lessons = {}
        self._quizzes = {}
        self._files = {}
        self._phases = {}
        self._weeks = {}
        self._sessions = defaultdict(list)

    def run(self):
        """Scan the archive and place everything in one transaction; return the :class:`SeedReport`.

        Assessment dates are planned first, because week dates hang off them. The
        manifest is written after the transaction commits.
        """
        from apps.learning.models import ProgrammeModule  # noqa: F401  (app registry ready)

        docs, scan_report = scan(self.root)
        for k, v in scan_report.items():
            self.report.add(f'archive: {k}', v)
        self.out(f'{len(docs)} documents to place.')

        with transaction.atomic():
            self._ensure_structure()
            by_doc = {d.key: d for d in docs}
            placements = []
            for doc in docs:
                for offering, cohort in self._targets(doc):
                    placements.append((doc, offering, cohort))

            self._plan_phases(placements)
            self._explicit_max = defaultdict(int)
            for doc, offering, cohort in placements:
                if isinstance(self._slot(doc), int):
                    key = (offering.pk, cohort.pk if cohort else None, *self._phase_kind(doc))
                    self._explicit_max[key] = max(self._explicit_max[key], doc.week)
            self._load_sessions(docs)
            for doc, offering, cohort in placements:
                self._place(doc, offering, cohort, by_doc)
            self._finish_weeks()
            self._calendar_sessions()
            self._week_markers()
        self._write_manifest()
        return self.report

    def _ensure_structure(self):
        """The intakes, and the two homes the spine does not have yet: Nelson
        Mandela University's PGDA and SAICA's IAC board exam."""
        from apps.learning.models import (AcademicCalendar, Cohort, Institution, Module,
                                          Programme, ProgrammeModule)

        for prog_code, codes in INTAKES.items():
            programme = Programme.objects.get(institution__code='MILPARK', code=prog_code)
            for code in codes:
                cohort, created = Cohort.objects.get_or_create(
                    programme=programme, code=code,
                    defaults={'name': COHORT_NAMES.get(code, ''),
                              'start_date': dates.COHORT_STARTS.get(code)})
                if created:
                    self.report.created['cohort'].append(cohort.pk)
                    self.report.add('cohorts created')

        nmu, created = Institution.objects.get_or_create(
            code='NMU', defaults={'name': 'Nelson Mandela University', 'slug': 'nelson-mandela-university'})
        if created:
            self.report.created['institution'].append(nmu.pk)
        nmu_pgda, created = Programme.objects.get_or_create(
            institution=nmu, code='PGDA',
            defaults={'abbreviation': 'PGDA', 'name': 'PGDA',
                      'full_name': 'Postgraduate Diploma in Accounting', 'slug': 'nmu-pgda', 'level': 'cta'})
        if created:
            self.report.created['programme'].append(nmu_pgda.pk)
        self._offering_for(nmu_pgda, 'CGAU', create=True)

        saica = Institution.objects.get(code='SAICA')
        iac, created = Programme.objects.get_or_create(
            institution=saica, code='IAC',
            defaults={'abbreviation': 'IAC', 'name': 'IAC',
                      'full_name': 'Initial Assessment of Competence (ITC board exam)',
                      'slug': 'saica-iac', 'level': 'board'})
        if created:
            self.report.created['programme'].append(iac.pk)
        module, created = Module.objects.get_or_create(
            code='IAC', defaults={'name': 'Initial Assessment of Competence (integrated)',
                                  'slug': 'initial-assessment-of-competence'})
        if created:
            self.report.created['module'].append(module.pk)
        self._offering_for(iac, 'IAC', create=True)

        for institution in Institution.objects.filter(code__in=['MILPARK', 'SAICA', 'NMU']):
            AcademicCalendar.objects.get_or_create(
                institution=institution, year=2026,
                defaults={'name': f'{institution.name} 2026'})

    def _offering_for(self, programme, module_code, *, create=False):
        """The programme's offering of ``module_code``, created when ``create`` is set."""
        from apps.learning.models import Module, ProgrammeModule
        catalogue = CATALOGUE[module_code]
        offering = ProgrammeModule.objects.filter(programme=programme, module__code=catalogue).first()
        if offering is None and create:
            offering = ProgrammeModule.objects.create(
                programme=programme, module=Module.objects.get(code=catalogue),
                code=module_code, name=Module.objects.get(code=catalogue).name)
            self.report.created['offering'].append(offering.pk)
            self.report.add('module offerings created')
        return offering

    def _targets(self, doc):
        """Every (offering, cohort) a document is placed on."""
        from apps.learning.models import Cohort, Programme

        programme = Programme.objects.filter(institution__code=doc.institution,
                                             code=doc.programme).first()
        if programme is None:
            self.report.add('skipped: programme not installed')
            return []
        offering = self._offering_for(programme, doc.module)
        if offering is None:
            self.report.add('skipped: module not offered on programme')
            return []
        intakes = INTAKES.get(doc.programme) if doc.institution == 'MILPARK' else None
        if not intakes:
            return [(offering, None)]
        if doc.cohort in intakes or doc.cohort in LATE_INTAKES:
            codes = [doc.cohort]
        else:
            codes = intakes
        cohorts = list(Cohort.objects.filter(programme=programme, code__in=codes))
        return [(offering, c) for c in cohorts]

    @staticmethod
    def _phase_kind(doc):
        """``(kind, sequence)`` of the block a document belongs to; general material goes to ``('other', 1)``.
        """
        kind, seq = doc.assessment
        if kind == 'general':
            return 'other', 1
        return kind, seq

    def _plan_phases(self, placements):
        """Create every preparation block and date it from the real assessment.

        Once a block has an intake's confirmed date, the spine's generic event for it
        (e.g. "Test 2" for the module) is unpublished so the calendar does not show
        both. Each block then opens the day after the intake's previous assessment
        (or the intake's start); when that gives no start before its own date, it
        opens six weeks before it.
        """
        from apps.learning.models import CalendarEvent, ModulePhase

        needed = {(o.pk, c.pk if c else None, *self._phase_kind(d)): (o, c)
                  for d, o, c in placements}
        for (opk, cpk, kind, seq), (offering, cohort) in sorted(needed.items(), key=lambda kv: str(kv[0])):
            phase = ModulePhase.objects.filter(programme_module=offering, cohort=cohort,
                                               kind=kind, sequence=seq).first()
            created = phase is None
            if created:
                phase = ModulePhase(programme_module=offering, cohort=cohort, kind=kind, sequence=seq)
            if kind == 'other':
                phase.title = 'Module resources'
                phase.order = 20
            elif kind == 'exam':
                phase.title = phase.title if not created else (
                    'IAC board exam preparation' if offering.code == 'IAC' else 'Final exam preparation')
                phase.order = 10 + seq
            else:
                phase.order = seq
            phase.is_published = self.publish
            phase.save()
            if created:
                self.report.created['phase'].append(phase.pk)
                self.report.add('preparation blocks created')
            self._phases[(opk, cpk, kind, seq)] = phase

        for (opk, cpk, kind, seq), phase in self._phases.items():
            if kind not in ('test', 'exam') or phase.cohort is None:
                continue
            offering, cohort = phase.programme_module, phase.cohort
            mod = _module_code(offering)
            spine = (CalendarEvent.objects
                     .filter(programme_module=offering, cohort__isnull=True, kind=kind,
                             start__year=2026,
                             title=('Final examination' if kind == 'exam' else f'Test {seq}'))
                     .first())
            on, source, confirmed = dates.assessment_date(
                offering.programme.code, cohort.code, mod, kind, seq, spine)
            if on is None:
                continue
            event = self._assessment_event(phase, on, source, confirmed)
            phase.calendar_event = event
            phase.ends_on = on
            phase.summary = (f'{cohort.code} · {phase.short_label} on {on:%a %d %b %Y}'
                             + ('' if confirmed else ' (planned date)'))[:300]
            phase.save(update_fields=['calendar_event', 'ends_on', 'summary', 'updated_at'])
            if confirmed and spine is not None and spine.is_published:
                spine.is_published = False
                spine.save(update_fields=['is_published', 'updated_at'])
                self.report.unpublished_spine_events.append(spine.pk)
                self.report.add('generic calendar events superseded')

        by_track = defaultdict(list)
        for phase in self._phases.values():
            if phase.ends_on and phase.cohort_id:
                by_track[(phase.programme_module_id, phase.cohort_id)].append(phase)
        for track in by_track.values():
            track.sort(key=lambda p: p.ends_on)
            previous = None
            for phase in track:
                start = (previous + dt.timedelta(days=1)) if previous else \
                    dates.COHORT_STARTS.get(phase.cohort.code)
                if not start or start >= phase.ends_on:
                    start = phase.ends_on - dt.timedelta(days=42)
                phase.starts_on = start
                phase.save(update_fields=['starts_on', 'updated_at'])
                previous = phase.ends_on

    def _assessment_event(self, phase, on, source, confirmed):
        """Create or update the intake's calendar entry for a test or exam on ``on``."""
        from apps.learning.models import AcademicCalendar, CalendarEvent
        offering, cohort = phase.programme_module, phase.cohort
        calendar = AcademicCalendar.objects.get(institution=offering.programme.institution, year=2026)
        noun = 'Final exam' if phase.kind == 'exam' else f'Test {phase.sequence}'
        title = f'{offering.code} {noun} · {cohort.code}'
        start = dt.datetime.combine(on, dt.time(8, 0), tzinfo=SAST)
        event, created = CalendarEvent.objects.update_or_create(
            calendar=calendar, programme_module=offering, cohort=cohort, kind=phase.kind,
            title=title,
            defaults={'programme': offering.programme, 'start': start, 'all_day': True,
                      'description': f'Date source: {source}.'
                                     + ('' if confirmed else ' Planned date — confirm against the institution.')
                                     + f' [{TAG}]',
                      'is_published': True})
        if created:
            self.report.created['event'].append(event.pk)
            self.report.add('test/exam dates on calendar')
        return event

    def _slot(self, doc):
        """The week a document goes in: its own week number, or one of the fixed ``SLOT_*`` weeks."""
        if doc.kind == 'past_paper':
            return SLOT_PAST
        if doc.audience == 'staff':
            return SLOT_COACH
        if doc.week:
            return doc.week
        if doc.kind in ('mock_exam', 'mock_solution'):
            return SLOT_MOCKS
        return SLOT_REVISION

    def _week(self, phase, slot, explicit_max):
        """The ``ModuleWeek`` for ``slot`` in ``phase``, created on first use.

        When a fixed slot's number is already taken by a human-made week, ours is put
        after everything else.
        """
        from apps.learning.models import ModuleWeek

        key = (phase.pk, slot)
        if key in self._weeks:
            return self._weeks[key]
        if isinstance(slot, int):
            number, title = slot, ''
        else:
            number, title = SLOT_NUMBER[slot], SLOT_TITLES[slot]
        week = None
        if title:
            week = ModuleWeek.objects.filter(phase=phase, title=title).first()
        if week is None:
            week = ModuleWeek.objects.filter(phase=phase, number=number).first()
            if week is not None and title and week.title and week.title != title:
                last = ModuleWeek.objects.filter(phase=phase).order_by('-number').first()
                number, week = last.number + 1, None
        created = week is None
        if created:
            week = ModuleWeek(phase=phase, number=number)
        elif week.number != number and not ModuleWeek.objects.filter(phase=phase, number=number).exists():
            week.number = number
        week.title = title or week.title
        week.order = week.number
        week.is_published = self.publish and slot != SLOT_COACH
        week.save()
        if created:
            self.report.created['week'].append(week.pk)
            self.report.add('weeks created')
        self._weeks[key] = week
        return week

    def _finish_weeks(self):
        """Date the weeks inside each block's window."""
        for (phase_pk, slot), week in self._weeks.items():
            phase = week.phase
            if not (phase.starts_on and phase.ends_on):
                continue
            if isinstance(slot, int):
                start = phase.starts_on + dt.timedelta(days=7 * (slot - 1))
                end = min(start + dt.timedelta(days=6), phase.ends_on)
            elif slot in (SLOT_REVISION, SLOT_MOCKS):
                end = phase.ends_on - dt.timedelta(days=1)
                start = max(phase.starts_on, end - dt.timedelta(days=6))
            else:
                continue
            if start > phase.ends_on:
                continue
            week.starts_on, week.ends_on = start, end
            week.save(update_fields=['starts_on', 'ends_on', 'updated_at'])

    def _place(self, doc, offering, cohort, by_doc):
        """Place one document on one offering/intake: its material row, plus a lesson and quiz where it has them.

        A quiz is placed as its own row, sat online beside (and before) its solution.
        """
        from apps.learning.models import ModuleMaterial

        kind, seq = self._phase_kind(doc)
        phase_key = (offering.pk, cohort.pk if cohort else None, kind, seq)
        phase = self._phases[phase_key]
        explicit_max = self._explicit_max[phase_key]
        slot = self._slot(doc)
        week = self._week(phase, slot, explicit_max)

        student = doc.audience == 'students'
        visible = self.publish and student and not doc.needs_review
        lesson = self._lesson(doc, offering, visible) if self._wants_lesson(doc) else None
        quiz = self._quiz(doc, offering, by_doc, visible) if student else None

        material_kind = MATERIAL_KIND.get(doc.kind, 'document')
        if doc.kind == 'past_paper':
            material_kind = 'answers' if SOLUTION_RX.search(doc.file_name) else 'mock_exam'

        ref = f'{TAG}/{cohort.code if cohort else "all"}/{doc.key}'[:140]
        material = self._material(
            ref=ref, phase=phase, week=week, kind=material_kind, title=doc.title,
            description=_student_description(doc), lesson=lesson, file_doc=doc,
            order=KIND_ORDER.get(doc.kind, 90), audience='students' if student else 'staff',
            published=visible, used_on=_iso(doc.date),
            minutes=lesson.estimated_minutes if lesson else None)
        self.report.add(f'materials: {material_kind}')
        if not student:
            self.report.add('materials kept staff-only')
        elif not visible:
            self.report.add('materials left in draft (needs review)')

        if quiz is not None:
            self._material(
                ref=f'{ref}/quiz', phase=phase, week=week,
                kind='mock_exam' if quiz.kind == 'test' else ModuleMaterial.KIND_ASSESSMENT,
                title=quiz.title, description='Sit it online, submit, then open the solution.',
                assessment=quiz, order=KIND_ORDER.get(doc.kind, 90) - 8, audience='students',
                published=visible and quiz.status == 'open',
                minutes=quiz.time_limit_minutes or None, used_on=_iso(doc.date))
            self.report.add('quizzes scheduled')

    def _material(self, *, ref, phase, week, kind, title, description='', lesson=None,
                  assessment=None, file_doc=None, url='', order=0, audience='students',
                  published=False, used_on=None, minutes=None):
        """Create or update the :class:`~apps.learning.models.ModuleMaterial` keyed ``ref``."""
        from apps.learning.models import ModuleMaterial

        material = ModuleMaterial.objects.filter(source_ref=ref).first()
        created = material is None
        if created:
            material = ModuleMaterial(source_ref=ref, created_by=self.actor)
        material.phase, material.week = phase, week
        material.kind = kind
        material.title = title[:200]
        material.description = description
        material.lesson = lesson
        material.assessment = assessment
        material.url = url
        material.order = order
        material.audience = audience
        material.is_published = published
        material.used_on = used_on
        material.estimated_minutes = minutes
        if file_doc is not None and file_doc.binary_path and lesson is None:
            material.file.name = self._store(file_doc)
        material.save()
        if created:
            self.report.created['material'].append(material.pk)
        return material

    def _store(self, doc):
        """Copy the original into the file store once; return its storage name.

        Names are shortened because file fields hold 100 characters and the
        archive's names run far longer.
        """
        from django.core.files import File

        from core.storage import files_storage
        if doc.key in self._files:
            return self._files[doc.key]
        storage = files_storage()
        safe = re.sub(r'[^\w.\-]+', '_', doc.file_name)
        digest = hashlib.sha1(doc.key.encode()).hexdigest()[:8]
        name = f'modules/2026/{doc.programme}/{doc.module}/{digest}-{safe[-60:]}'
        if not (storage.exists(name) and storage.size(name) == os.path.getsize(doc.binary_path)):
            if storage.exists(name):
                storage.delete(name)
            with open(doc.binary_path, 'rb') as fh:
                name = storage.save(name, File(fh))
            self.report.add('files copied to the file store')
        self._files[doc.key] = name
        return name

    @staticmethod
    def _wants_lesson(doc):
        """True when a document should also become a reading lesson."""
        if doc.text_path:
            return doc.kind in READING_KINDS or not doc.binary_path
        return doc.kind in READING_KINDS and doc.binary_path.lower().endswith('.docx')

    def _lesson(self, doc, offering, visible):
        """The reading lesson for ``doc`` on ``offering``, built from its text (``None`` if it has none)."""
        from apps.learning.models import Lesson, LessonBlock, LessonResource, LessonSection

        key = (doc.key, offering.pk)
        if key in self._lessons:
            lesson = self._lessons[key]
            if visible and lesson.status != Lesson.STATUS_PUBLISHED:
                lesson.status = Lesson.STATUS_PUBLISHED
                lesson.save(update_fields=['status', 'updated_at'])
            return lesson

        if doc.text_path:
            text = open(doc.text_path, encoding='utf-8', errors='replace').read()
        else:
            text = textconv.text_from_docx(doc.binary_path)
        sections = textconv.sections_from_markdown(text)
        if not sections:
            return None

        slug = f'{TAG}-' + hashlib.sha1(f'{doc.key}|{offering.pk}'.encode()).hexdigest()[:16]
        lesson = Lesson.objects.filter(slug=slug).first()
        created = lesson is None
        if created:
            lesson = Lesson(slug=slug, created_by=self.actor)
        lesson.title = doc.title
        noun = {'test': f'Test {doc.assessment[1]}', 'exam': 'Final exam'}.get(doc.assessment[0], '')
        lesson.subtitle = ' · '.join(x for x in [
            f'{offering.programme.institution.code} {offering.programme.code} {offering.code}',
            doc.cohort, noun, {'study_guide': 'Study guide', 'blueprint': 'Strategic blueprint'}.get(doc.kind, '')
        ] if x)[:300]
        lesson.module = offering
        lesson.visibility = Lesson.VIS_MODULE
        lesson.status = Lesson.STATUS_PUBLISHED if visible else Lesson.STATUS_DRAFT
        lesson.estimated_minutes = sum(s['minutes'] for s in sections)
        lesson.author_name = 'Metanoia Group'
        lesson.save()
        lesson.target_modules.set([offering])
        if created:
            self.report.created['lesson'].append(lesson.pk)
            self.report.add('reading lessons created')

        lesson.blocks.all().delete()
        lesson.sections.all().delete()
        for order, section in enumerate(sections):
            row = LessonSection.objects.create(
                lesson=lesson, title=section['heading'], order=order,
                section_type=LessonSection.TYPE_READING, duration_minutes=section['minutes'],
                open_by_default=order == 0)
            LessonBlock.objects.create(lesson=lesson, section=row, order=0,
                                       block_type=LessonBlock.TYPE_TEXT, data={'html': section['html']})
        lesson.resources.all().delete()
        if doc.binary_path:
            LessonResource.objects.create(
                lesson=lesson, kind=_resource_kind(doc.binary_path), order=0,
                title=f'Download the original ({os.path.splitext(doc.binary_path)[1].lstrip(".").upper()})',
                file=self._store(doc))
        self._lessons[key] = lesson
        return lesson

    def _quiz(self, doc, offering, by_doc, visible):
        """A markable quiz from a pre-existing questions-and-answers workbook."""
        from apps.assessments.models import Assessment, Question, Section
        from core.content_pack.importer import _question_payload

        if not (doc.binary_path.lower().endswith('.xlsx')
                and doc.kind in ('answer_sheet', 'mock_solution')):
            return None
        key = (doc.key, offering.pk)
        if key in self._quizzes:
            return self._quizzes[key]
        parsed = markgrid.parse_workbook(doc.binary_path)
        parts = [p for p in parsed['parts'] if p['rubric']]
        if len(parts) < 2:
            return None

        question_doc = _question_doc_for(doc, by_doc)
        text = ''
        if question_doc is not None:
            if question_doc.text_path:
                text = open(question_doc.text_path, encoding='utf-8', errors='replace').read()
            elif question_doc.binary_path.lower().endswith('.pdf'):
                text = textconv.text_from_pdf(question_doc.binary_path)
        required = textconv.required_by_letter(text) if text else {}
        if text:
            scenario = _scenario_html(text)
        else:
            scenario = markgrid.scenario_from_workbook(doc.binary_path)

        is_mock = doc.kind == 'mock_solution'
        title = _quiz_title(question_doc or doc, is_mock)

        ref = f'{TAG}/quiz/{offering.pk}/{doc.key}'[:140]
        paper = Assessment.objects.filter(source_ref=ref).first()
        created = paper is None
        if created:
            paper = Assessment(source_ref=ref, created_by=self.actor)
        paper.module = offering
        paper.title = title
        paper.kind = Assessment.KIND_TEST if is_mock else Assessment.KIND_ASSIGNMENT
        paper.description = scenario
        paper.total_marks = sum(Decimal(str(p['marks'])) for p in parts)
        paper.pass_mark_pct = 50
        paper.time_limit_minutes = int(round(float(paper.total_marks) * 1.8)) if is_mock else 0
        paper.attempts_allowed = 1 if is_mock else 0
        paper.release_solution = Assessment.RELEASE_ON_SUBMIT
        notes = [p['note'] for p in parts if p.get('note')]
        paper.solution_notes = {'source': f'Mark grid: {doc.file_name}', **({'note': ' '.join(notes)} if notes else {})}
        if created:
            paper.status = Assessment.STATUS_DRAFT
        paper.save()
        paper.sections.all().delete()
        section = Section.objects.create(assessment=paper, title='Required', order=0,
                                         instructions='Answer every part. Each part is marked against '
                                                      'the points in the suggested solution.')
        for order, part in enumerate(parts):
            letter = part['ref'][:1]
            ask = required.get(letter) or ' — '.join(x for x in [
                part['title'], part.get('what_it_tests', '')] if x)
            payload = {'ref': part['ref'], 'required': ask, 'marks': part['marks'],
                       'marking': 'ai_assisted', 'rubric': part['rubric']}
            qtype, config, guidance, mode = _question_payload(payload)
            Question.objects.create(section=section, type=qtype, order=order,
                                    text=f'({part["ref"]})  {ask}', marks=Decimal(str(part['marks'])),
                                    config=config, guidance=guidance, marking_mode=mode)
        if visible and paper.status == Assessment.STATUS_DRAFT:
            paper.transition_to(Assessment.STATUS_REVIEW, by=self.actor)
            paper.transition_to(Assessment.STATUS_OPEN, by=self.actor)
        if created:
            self.report.created['assessment'].append(paper.pk)
            self.report.add('quizzes created')
        self._quizzes[key] = paper
        return paper

    def _load_sessions(self, docs):
        """Collect the live coaching sessions listed in the planner documents."""
        from apps.learning.models import Programme
        for doc in docs:
            if doc.kind != 'planner' or not doc.text_path:
                continue
            text = open(doc.text_path, encoding='utf-8', errors='replace').read()
            for day, prog, cohort, start, end in dates.coaching_sessions(text):
                programme = Programme.objects.filter(institution__code='MILPARK', code=prog).first()
                offering = programme and self._offering_for(programme, doc.module)
                if offering:
                    self._sessions[(offering.pk, cohort)].append(
                        (dt.datetime.combine(day, start, tzinfo=SAST),
                         dt.datetime.combine(day, end, tzinfo=SAST)))

    def _calendar_sessions(self):
        """The live coaching sessions on the calendar, and a "Live session" row
        in the week each one falls in — the button a student clicks."""
        from apps.learning.models import AcademicCalendar, CalendarEvent, Cohort, ProgrammeModule

        for (opk, cohort_code), slots in self._sessions.items():
            offering = ProgrammeModule.objects.select_related('programme__institution').get(pk=opk)
            cohort = Cohort.objects.filter(programme=offering.programme, code=cohort_code).first()
            calendar = AcademicCalendar.objects.get(institution=offering.programme.institution, year=2026)
            for start, end in sorted(set(slots)):
                event, created = CalendarEvent.objects.update_or_create(
                    calendar=calendar, programme_module=offering, cohort=cohort,
                    kind=CalendarEvent.KIND_SESSION, start=start,
                    defaults={'programme': offering.programme, 'end': end, 'all_day': False,
                              'title': f'{offering.code} live coaching · {cohort_code}',
                              'description': f'Live coaching session (coach schedule). [{TAG}]',
                              'is_published': True})
                if created:
                    self.report.created['event'].append(event.pk)
                    self.report.add('live sessions on calendar')
                week = self._week_on(offering, cohort, start.date())
                if week is None:
                    continue
                local = start.astimezone(SAST)
                past = start < timezone.now()
                self._material(
                    ref=f'{TAG}/live/{opk}/{cohort_code}/{start:%Y%m%d%H%M}', phase=week.phase, week=week,
                    kind='live',
                    title=(f'Live session · {local:%a %d %b}, {local:%H:%M}–{end.astimezone(SAST):%H:%M}'
                           + (' (held)' if past else '')),
                    description='Open the calendar day for the joining link and the recording.',
                    url=reverse('livesessions:day', args=[local.strftime('%Y-%m-%d')]),
                    order=15, audience='students', published=self.publish)
                self.report.add('live session buttons in weeks')

    def _week_on(self, offering, cohort, day):
        """The intake's dated week on ``offering`` that contains ``day``, or ``None``."""
        from apps.learning.models import ModuleWeek
        if cohort is None:
            return None
        return (ModuleWeek.objects
                .filter(phase__programme_module=offering, phase__cohort=cohort,
                        starts_on__lte=day, ends_on__gte=day)
                .order_by('number').first())

    def _week_markers(self):
        """One calendar entry on each dated week that carried student material —
        "the date it was used" — linking straight to the module."""
        from apps.learning.models import AcademicCalendar, CalendarEvent

        for (phase_pk, slot), week in self._weeks.items():
            phase = week.phase
            if slot in (SLOT_PAST, SLOT_COACH) or not week.starts_on or phase.cohort is None:
                continue
            items = week.materials.filter(is_published=True).exclude(kind='live').count()
            if not items:
                continue
            offering = phase.programme_module
            calendar = AcademicCalendar.objects.get(institution=offering.programme.institution, year=2026)
            label = week.display_title
            event, created = CalendarEvent.objects.update_or_create(
                calendar=calendar, programme_module=offering, cohort=phase.cohort,
                kind=CalendarEvent.KIND_OTHER,
                start=dt.datetime.combine(week.starts_on, dt.time(7, 0), tzinfo=SAST),
                title=f'{offering.code} {phase.short_label} · {label}'[:200],
                defaults={'programme': offering.programme, 'all_day': True,
                          'description': f'{items} study item(s) released for {phase.cohort.code}: '
                                         f'{reverse("learning:module-feed", args=[offering.pk])} [{TAG}]',
                          'is_published': self.publish})
            if created:
                self.report.created['event'].append(event.pk)
                self.report.add('weekly material markers on calendar')

    def _write_manifest(self):
        """Merge this run's created ids into the manifest :func:`remove` reads."""
        os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
        previous = {}
        if os.path.exists(MANIFEST):
            try:
                previous = json.load(open(MANIFEST))
            except ValueError:
                previous = {}
        created = {k: sorted(set(previous.get('created', {}).get(k, [])) | set(v))
                   for k, v in {**{k: [] for k in previous.get('created', {})},
                                **self.report.created}.items()}
        data = {
            'generated': timezone.now().isoformat(),
            'created': created,
            'unpublished_spine_events': sorted(set(previous.get('unpublished_spine_events', []))
                                               | set(self.report.unpublished_spine_events)),
        }
        with open(MANIFEST, 'w') as fh:
            json.dump(data, fh, indent=1)


def _module_code(offering):
    """The catalogue module code (``CGAU``…) for an offering."""
    rev = {v: k for k, v in CATALOGUE.items()}
    return rev.get(offering.module.code, offering.code)


def _iso(value):
    """``value`` as a date, or ``None`` when empty or not ISO."""
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _student_description(doc):
    """Only the sidecar's own description — its ``notes`` are the coach's."""
    text = str(doc.meta.get('description') or '').strip()
    if doc.audience == 'staff' and doc.rights_note:
        text = (text + f' Rights: {doc.rights_note}').strip()
    return text[:600]


def _quiz_title(doc, is_mock):
    """``CGAU B26S · Week 1 Topic 1 Conduct and Legislation — challenge quiz``,
    built from the file's own name, which is more specific than its sidecar title."""
    stem = doc.key.rsplit('/', 1)[-1]
    stem = re.sub(r'_(v\d+|TEXT|Suggested|Solution|Question)(?=_|$)', '', stem, flags=re.I)
    words = stem.split('_')
    head = ' '.join(words[:2])
    rest = ' '.join(words[2:])
    rest = re.sub(r'\bT(?:est)?(\d)\b', r'Test \1', rest)
    rest = re.sub(r'\bWeek(\d)\b', r'Week \1', rest)
    rest = re.sub(r'\bTopic(\d)\b', r'· Topic \1', rest)
    suffix = 'online mock' if is_mock else 'challenge quiz'
    return f'{head} · {rest} — {suffix}'.replace('  ', ' ')[:200]


def _resource_kind(path):
    """The ``LessonResource`` kind for a file path's extension."""
    return {'.pdf': 'pdf', '.docx': 'word', '.doc': 'word', '.pptx': 'ppt',
            '.jpg': 'image', '.jpeg': 'image', '.png': 'image'}.get(
        os.path.splitext(path)[1].lower(), 'download')


def _question_doc_for(solution, by_doc):
    """The question paper a solution workbook answers: …_Solution → …_Question…"""
    stem = solution.key.rsplit('/', 1)[-1]
    prefix = re.split(r'_(Suggested_)?Solution', stem, flags=re.I)[0] + '_Question'
    head = solution.key.rsplit('/', 1)[0]
    for key, doc in by_doc.items():
        if key.startswith(f'{head}/{prefix}') and doc.kind in ('mock_exam', 'practice'):
            return doc
    return None


def _scenario_html(text):
    """The scenario part of a question paper as HTML (everything before REQUIRED)."""
    cut = re.search(r'^[|\s*]*REQUIRED[|\s*]*$', text, re.M | re.I)
    body = text[:cut.start()] if cut else text
    if body.lstrip().startswith(('#', '|', '*')):
        return ''.join(s['html'] for s in textconv.sections_from_markdown(body))
    import html
    paras = [p.strip() for p in re.split(r'\n\s*\n', body) if p.strip()]
    return ''.join(f'<p>{html.escape(" ".join(p.split()))}</p>' for p in paras)


def remove(out=None):
    """Take the 2026 seed out again: every keyed row, then the empty weeks and
    blocks and the intakes it created, and put the generic events back."""
    from apps.assessments.models import Assessment
    from apps.learning.models import (CalendarEvent, Cohort, Lesson, ModuleMaterial,
                                      ModulePhase, ModuleWeek)
    out = out or (lambda m: None)
    manifest = json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {'created': {}}
    created = manifest.get('created', {})
    with transaction.atomic():
        n = ModuleMaterial.objects.filter(source_ref__startswith=f'{TAG}/').delete()[0]
        out(f'materials removed: {n}')
        n = Assessment.objects.filter(source_ref__startswith=f'{TAG}/').delete()[0]
        out(f'quizzes removed: {n}')
        n = Lesson.objects.filter(slug__startswith=f'{TAG}-').delete()[0]
        out(f'lessons removed: {n}')
        n = CalendarEvent.objects.filter(description__contains=f'[{TAG}]').delete()[0]
        out(f'calendar entries removed: {n}')
        n = ModuleWeek.objects.filter(pk__in=created.get('week', []), materials__isnull=True).delete()[0]
        out(f'empty weeks removed: {n}')
        n = ModulePhase.objects.filter(pk__in=created.get('phase', []), weeks__isnull=True,
                                       materials__isnull=True).delete()[0]
        out(f'empty blocks removed: {n}')
        for cohort in Cohort.objects.filter(pk__in=created.get('cohort', [])):
            if not cohort.enrolments.exists():
                cohort.delete()
        CalendarEvent.objects.filter(pk__in=manifest.get('unpublished_spine_events', [])).update(is_published=True)
    if os.path.exists(MANIFEST):
        os.rename(MANIFEST, MANIFEST + '.removed')
