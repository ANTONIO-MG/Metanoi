"""Study content and study-session timers.

The academic spine is institution-first — the same standards are examined
differently at each institution, so every offering owns its own topics:

    Institution → Programme → ProgrammeModule → Topic → Lesson / Assessment
                              (the offering)

:class:`Module` is the one shared, canonical level (FR, TX, GA, MA, APC
competency areas) used for cross-institution reporting; topics on different
offerings are tied together by :attr:`Topic.code` (see :meth:`Topic.siblings`).
Topics are per-offering copies, which is the trade-off: correcting a topic's
description has to be done per offering, or the copies drift apart.

Scheduling is a second structure on top — :class:`ModulePhase` →
:class:`ModuleWeek` → :class:`ModuleMaterial` — because the same topic is taught
in a different week at each institution.

Lesson material (documents and media) is stored on the file server through
``core.storage.files_storage`` with unguessable upload paths.

Module constants:

``DEPTH_*`` / ``DEPTH_CHOICES``
    How deep a topic is pitched. Depth is set per topic and defaults down the
    spine: a programme's ``depth_default`` seeds its offerings, an offering's
    seeds its topics.
``GENERAL_CODE`` / ``GENERAL_NAME``
    The canonical code (on :class:`Module` and on the offering) and name of a
    programme's **General** module — the one home for onboarding and
    programme-wide material. See :mod:`apps.learning.general`.
"""

import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify

from core import validators as v

from core.storage import UnguessableUploadTo, files_storage


class TimeStampedModel(models.Model):
    """Abstract base adding ``created_at`` and ``updated_at`` to a model."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Abstract: no table of its own."""
        abstract = True


DEPTH_FOUNDATIONAL = 'FND'
DEPTH_ADVANCED = 'ADV'
DEPTH_INTEGRATED = 'INT'
DEPTH_UNDERGRADUATE = 'UG'
DEPTH_CHOICES = [
    (DEPTH_FOUNDATIONAL, 'Foundational — bridging level: principle and basic application'),
    (DEPTH_ADVANCED, 'Advanced — CTA/PGDA level: full complexity, integrated scenarios'),
    (DEPTH_INTEGRATED, 'Integrated — APC level: cross-discipline judgement, no topic silos'),
    (DEPTH_UNDERGRADUATE, 'Undergraduate — below bridging level'),
]


class Institution(TimeStampedModel):
    """A place a student is registered with — Milpark, UNISA, IAS, SAICA.

    SAICA teaches nothing but is modelled as an institution because APC
    candidates must be enrolled and tracked like everyone else.

    ``SHARP_LOGO_MIN_PX`` is the smallest logo side that still draws crisply as an
    avatar; smaller and it smears into a blur. ``OTHER_CODE`` is the code of the
    catch-all "Other institution" row (``seed_sample_structure``): picking it at
    registration or ``/join/`` asks the person to type where they study and their
    cohort or year (``Person.other_institution`` / ``other_cohort``).
    """

    code = models.CharField(max_length=20, unique=True,
                            help_text='Institution code used throughout — "MILPARK", "UNISA", "IAS", "SAICA".')
    name = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    accent_colour = models.CharField(max_length=9, blank=True, default='#1F3864',
                                     help_text='Hex colour used for this institution across the LMS.')
    accent_name = models.CharField(max_length=40, blank=True, help_text='e.g. "Steel Blue".')
    logo = models.ImageField(upload_to='institutions/', blank=True, null=True,
                             validators=v.validate_image)
    website = models.URLField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered by ``order``, then name."""
        ordering = ['order', 'name']

    def __str__(self):
        """The institution's :attr:`display_name`."""
        return self.display_name

    SHARP_LOGO_MIN_PX = 96

    OTHER_CODE = 'OTHER'

    @property
    def is_other(self):
        """True for the catch-all "Other institution"."""
        return (self.code or '').upper() == self.OTHER_CODE

    @property
    def sharp_logo_url(self):
        """The logo's URL if it is big enough to draw crisply, else ``''``
        (callers then fall back to a lettered badge)."""
        if not self.logo:
            return ''
        try:
            if min(self.logo.width, self.logo.height) < self.SHARP_LOGO_MIN_PX:
                return ''
        except (OSError, ValueError):
            return ''
        return self.logo.url

    def save(self, *args, **kwargs):
        """Derive a missing code (first word of the name) and slug, then save."""
        if not self.code:
            self.code = slugify(self.name).split('-')[0].upper()[:20]
        if not self.slug:
            self.slug = slugify(self.name)[:180] or 'institution'
        super().save(*args, **kwargs)

    @property
    def label(self):
        """The institution as it appears in a content title — ``MILPARK``."""
        return self.code.upper()

    @property
    def display_name(self):
        """``University of South Africa (UNISA)`` — full name, abbreviation in
        brackets unless it is already a word of the name ("Milpark Education")."""
        name = (self.name or '').strip()
        short = (self.code or '').strip()
        if not short:
            return name
        if not name:
            return short.upper()
        if re.search(rf'\b{re.escape(short)}\b', name, re.IGNORECASE):
            return name
        return f'{name} ({short.upper()})'

    def calendar_for(self, year=None):
        """This institution's :class:`AcademicCalendar` for ``year``, creating it
        if the year has not been opened yet. Defaults to the current year."""
        year = year or timezone.now().year
        calendar, _ = AcademicCalendar.objects.get_or_create(institution=self, year=year)
        return calendar


class Programme(TimeStampedModel):
    """What a student is registered for at an institution — Milpark PGDA,
    UNISA PGDA, SAICA APC. Identified across the practice by :attr:`full_code`
    (``MILPARK-PGDA``); :attr:`depth_default` seeds its offerings' depth.

    ``abbreviation`` is separate from ``code`` on purpose: ``code`` is identity
    (it builds ``full_code`` and must not drift); the abbreviation is a free
    display/search label. ``is_competency_based`` marks an APC-style programme that
    is not module-based — its "modules" are competency areas — so the UI can label
    them correctly. ``modules_mandatory`` makes every active module compulsory (IAC).
    """

    LEVEL_CTA = 'cta'
    LEVEL_BRIDGING = 'bridging'
    LEVEL_APC = 'apc'
    LEVEL_POSTGRADUATE = 'postgraduate'
    LEVEL_UNDERGRADUATE = 'undergraduate'
    LEVEL_CHOICES = [
        (LEVEL_CTA, 'CTA'),
        (LEVEL_BRIDGING, 'Pre-CTA bridging'),
        (LEVEL_APC, 'Final qualifying assessment'),
        (LEVEL_POSTGRADUATE, 'Postgraduate'),
        (LEVEL_UNDERGRADUATE, 'Undergraduate'),
    ]

    institution = models.ForeignKey(Institution, on_delete=models.CASCADE, related_name='programmes')
    code = models.CharField(max_length=30, help_text='Short code, e.g. "PGDA". Prefixed with the institution '
                                                     'code to give the full code, "MILPARK-PGDA".')
    abbreviation = models.CharField(max_length=30, blank=True, db_index=True,
                                    help_text='Short form shown in brackets after the full name, '
                                              'e.g. "PGDA". Blank = use the code.')
    name = models.CharField(max_length=160, help_text='Short display name, e.g. "PGDA".')
    full_name = models.CharField(max_length=250, blank=True,
                                 help_text='Full title, e.g. "Postgraduate Diploma in Accounting". '
                                           'Shown with the abbreviation after it everywhere the '
                                           'programme is named.')
    slug = models.SlugField(max_length=180, blank=True)
    level = models.CharField(max_length=16, choices=LEVEL_CHOICES, default=LEVEL_CTA, db_index=True)
    depth_default = models.CharField(max_length=4, choices=DEPTH_CHOICES, default=DEPTH_ADVANCED,
                                     help_text='Depth this programme is normally pitched at.')
    is_competency_based = models.BooleanField(
        default=False, help_text='Structured around competency areas rather than examined modules (APC).')
    modules_mandatory = models.BooleanField(
        default=False,
        help_text='Every active module is compulsory: registering for the programme registers '
                  'all of them, and students cannot pick a subset (IAC).')
    description = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered within the institution; ``code`` is unique per institution."""
        ordering = ['institution', 'order', 'name']
        constraints = [
            models.UniqueConstraint(fields=['institution', 'code'], name='uniq_programme_code_per_institution'),
        ]

    def __str__(self):
        """``MILPARK · Postgraduate Diploma in Accounting (PGDA)`` — used by every dropdown."""
        return f'{self.institution.label} · {self.display_name}'

    def save(self, *args, **kwargs):
        """Derive a missing slug (institution + code) and abbreviation (the code), then
        save.
        """
        if not self.slug:
            self.slug = slugify(f'{self.institution.code}-{self.code}')[:180] or 'programme'
        if not self.abbreviation:
            self.abbreviation = (self.code or '')[:30]
        super().save(*args, **kwargs)

    @property
    def short_code(self):
        """The abbreviation to show — falls back to the code if none is set."""
        return (self.abbreviation or self.code or '').strip()

    @property
    def display_name(self):
        """``Postgraduate Diploma in Accounting (PGDA)``; just the abbreviation
        when there is no full title."""
        full = (self.full_name or self.name or '').strip()
        short = self.short_code
        if full and short and full.lower() != short.lower():
            return f'{full} ({short})'
        return full or short

    @property
    def taught_module_count(self):
        """How many modules the programme teaches — its General module not counted."""
        return self.modules.without_general().count()

    @property
    def full_code(self):
        """``MILPARK-PGDA`` — the programme's identifier across the practice."""
        return f'{self.institution.code}-{self.code}'

    @property
    def label(self):
        """``MILPARK PGDA`` — the first field of the LMS naming convention.

        Deliberately the abbreviation, not :attr:`display_name`: it builds
        content titles and filenames, which must stay short.
        """
        return f'{self.institution.label} {self.short_code}'.strip()


class ProgrammePhase(TimeStampedModel):
    """One phase of a phased programme (the APC's Foundation → … → Exam-ready).
    A period with a focus, not a container of content."""

    programme = models.ForeignKey(Programme, on_delete=models.CASCADE, related_name='phases')
    name = models.CharField(max_length=120, help_text='e.g. "Foundation".')
    period = models.CharField(max_length=120, blank=True, help_text='e.g. "March – May".')
    focus = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered by ``order``; a phase name is unique per programme."""
        ordering = ['order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['programme', 'name'], name='uniq_phase_name_per_programme'),
        ]

    def __str__(self):
        """``MILPARK-PGDA · Foundation``."""
        return f'{self.programme.full_code} · {self.name}'


class Cohort(TimeStampedModel):
    """A dated intake stream of a programme — P25F, P25S, P26F. Same modules,
    different calendar."""

    programme = models.ForeignKey(Programme, on_delete=models.CASCADE, related_name='cohorts')
    code = models.CharField(max_length=30, help_text='e.g. "P25F".')
    name = models.CharField(max_length=120, blank=True, help_text='e.g. "2025 first intake".')
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Newest intake first; ``code`` is unique per programme."""
        ordering = ['programme', '-start_date', 'code']
        constraints = [
            models.UniqueConstraint(fields=['programme', 'code'], name='uniq_cohort_code_per_programme'),
        ]

    def __str__(self):
        """``MILPARK-PGDA · P25F``."""
        return f'{self.programme.full_code} · {self.code}'


class Module(TimeStampedModel):
    """The **canonical** module catalogue — one row per module-matter area
    (``FR``, ``TX`` …), shared by every institution's offering of it. The APC's
    competency areas are modules too, flagged :attr:`is_competency_area`.

    ``ACCENT_COLOURS`` is keyed on the canonical code, so Taxation is one colour
    whether offered as TAX or TAXA; it is a dict, not a field, so rows can never
    disagree. FR is magenta, TX lime, GA teal and MA gold. The APC's competency
    areas reuse the discipline colour where there is one, so an APC candidate reads
    the same key as a CTA student; ad hoc support modules follow the discipline they
    belong to. ``DEFAULT_ACCENT`` (brand navy) is what an unmapped module draws in.
    """

    ACCENT_COLOURS = {
        'FR': '#E1197E',
        'TX': '#93C01F',
        'GA': '#17A2A2',
        'MA': '#D4A017',
        'APC-FR': '#E1197E',
        'APC-TX': '#93C01F',
        'APC-AA': '#17A2A2',
        'APC-FM': '#D4A017',
        'APC-MD': '#7B61FF',
        'APC-ET': '#C00000',
        'APC-PV': '#1F3864',
        'ACCMGR': '#D4A017',
        'FACUG': '#E1197E',
    }
    DEFAULT_ACCENT = '#1F3864'

    code = models.CharField(max_length=20, unique=True, help_text='Canonical code, e.g. "FR" or "APC-FR".')
    name = models.CharField(max_length=160, unique=True)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    is_competency_area = models.BooleanField(
        default=False, help_text='A SAICA competency area rather than an examined module.')
    description = models.TextField(blank=True, help_text='Scope of technical content covered.')
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered by ``order``, then name."""
        ordering = ['order', 'name']

    def __str__(self):
        """``FR — Financial Reporting``."""
        return f'{self.code} — {self.name}' if self.code else self.name

    @property
    def accent(self):
        """Hex colour for this module — see :attr:`ACCENT_COLOURS`."""
        return self.ACCENT_COLOURS.get(self.code, self.DEFAULT_ACCENT)

    def save(self, *args, **kwargs):
        """Derive a missing slug from the name, then save."""
        if not self.slug:
            self.slug = slugify(self.name)[:180] or 'module'
        super().save(*args, **kwargs)


GENERAL_CODE = 'GEN'
GENERAL_NAME = 'General'


class ProgrammeModuleQuerySet(models.QuerySet):
    """Queries over offerings that tell the General module apart from the taught ones.
    """
    def general(self):
        """Only the programmes' General modules."""
        return self.filter(code=GENERAL_CODE, module__code=GENERAL_CODE)

    def without_general(self):
        """The taught (examined, billable) modules — everything but General.

        Use this wherever modules are *counted* or *priced*: fees, bundles
        ("all 4 modules"), invoices, per-module calendar dates, registration.
        """
        return self.exclude(code=GENERAL_CODE, module__code=GENERAL_CODE)


class ProgrammeModule(TimeStampedModel):
    """A canonical :class:`Module` **as offered on one programme** — the
    "offering", and the owner of its topics.

    :attr:`code` is the institution's own code (``TAXA``, ``TAX``). Unique per
    programme by **code**, not by module: one programme can offer the same
    canonical module twice (UNISA ``FAC188`` and ``FAC179``).

    * ``educators`` is set per offering, not on the canonical module: a coach may
      take FR at Milpark and not at UNISA.
    * ``chat_group`` is the module feed's Messages tab; the FK runs learning →
      communication, the existing dependency direction.
    * ``price_per_month`` is what registration and the "unlock" flow bill the
      student per month for access; 0 = free (always unlocked).
    """

    programme = models.ForeignKey(Programme, on_delete=models.CASCADE, related_name='modules')
    module = models.ForeignKey(Module, on_delete=models.PROTECT, related_name='offerings')
    code = models.CharField(max_length=20,
                            help_text="This institution's code for the module, e.g. \"FREP\" or \"TAXA\".")
    name = models.CharField(max_length=160, blank=True,
                            help_text='Overrides the canonical module name when this programme names it '
                                      'differently — Milpark\'s "…and Financial Management" vs "…and Finance".')
    depth_level = models.CharField(max_length=4, choices=DEPTH_CHOICES, blank=True,
                                   help_text="Blank = inherit the programme's default depth.")
    description = models.TextField(blank=True, help_text='Coverage of this offering.')
    educators = models.ManyToManyField('accounts.Person', blank=True, related_name='taught_modules',
                                       limit_choices_to={'user_type__in': ['educator', 'staff', 'admin']},
                                       help_text='Coaches who teach this offering.')
    chat_group = models.OneToOneField(
        'communication.ChatGroup', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='programme_module',
        help_text='Group chat for everyone studying this offering.')
    price_per_month = models.DecimalField(
        max_digits=8, decimal_places=2, default=0,
        help_text='Monthly price (ZAR) to study this module. 0 = free / always unlocked.')
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = ProgrammeModuleQuerySet.as_manager()

    class Meta:
        """Ordered within the programme; ``code`` is unique per programme."""
        ordering = ['programme', 'order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['programme', 'code'], name='uniq_module_code_per_programme'),
        ]
        verbose_name = 'Programme module'

    def __str__(self):
        """The offering's :attr:`reference`."""
        return self.reference

    def save(self, *args, **kwargs):
        """Save, forcing the price to 0 on the General module.

        General is free by definition: never billed, never in a bundle. When saving
        with ``update_fields`` the price is added to them so the correction is
        written.
        """
        if self.is_general and self.price_per_month:
            self.price_per_month = 0
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'price_per_month'}
        super().save(*args, **kwargs)

    @property
    def is_general(self):
        """True for the programme's General module (onboarding and programme-wide
        material, free and open to everyone on the programme)."""
        if self.code != GENERAL_CODE or not self.module_id:
            return False
        return self.module.code == GENERAL_CODE

    @property
    def reference(self):
        """``MILPARK-PGDA / FREP`` — how an offering is written in the records."""
        return f'{self.programme.full_code} / {self.code}'

    @property
    def label(self):
        """``MILPARK PGDA | FREP`` — the LMS naming convention, up to the module."""
        return f'{self.programme.label} | {self.code}'.strip(' |')

    @property
    def display_name(self):
        """The offering's own name, else the canonical module's."""
        return self.name or self.module.name

    @property
    def institution(self):
        """The institution the offering's programme belongs to."""
        return self.programme.institution

    @property
    def accent(self):
        """The canonical module's colour — see :attr:`Module.ACCENT_COLOURS`."""
        return self.module.accent

    @property
    def resolved_depth(self):
        """The offering's depth, else the programme's default depth."""
        return self.depth_level or self.programme.depth_default

    def completion_for(self, student):
        """Percentage of this offering's topics the student has completed (0–100)."""
        topics = [topic for topic in self.topics.all() if topic.is_active]
        if not topics:
            return 0
        done = sum(1 for topic in topics if topic.is_completed_by(student))
        return round(done / len(topics) * 100)

    @property
    def is_free(self):
        """True when the offering has no monthly price (always unlocked)."""
        return not (self.price_per_month or 0)

    @property
    def member_people(self):
        """Coaches and candidates together. Unioned on pks because ``students``
        is a distinct queryset, which cannot be OR-combined."""
        from apps.accounts.models import Person
        return Person.objects.filter(
            pk__in=set(self.educators.values_list('pk', flat=True))
                   | set(self.students.values_list('pk', flat=True)))

    @property
    def students(self):
        """Everyone with a :class:`ModuleEnrolment` here, whatever their payment state.

        Super-student preview accounts are never "on" a module for staff, so they are
        left out.
        """
        from apps.accounts.models import Person
        return Person.objects.filter(module_enrolments__programme_module=self,
                                     preview_of__isnull=True).distinct()

    def enrolment_for(self, person):
        """This person's :class:`ModuleEnrolment` for the offering, or ``None``."""
        if person is None:
            return None
        return self.enrolments.filter(person=person).first()

    def is_unlocked_for(self, person):
        """True when the person may open this module — free, on a live trial, or paid."""
        if self.is_free:
            return True
        enrolment = self.enrolment_for(person)
        return bool(enrolment and enrolment.is_unlocked)


class AcademicCalendar(TimeStampedModel):
    """One institution's year — the container its dated events hang off.
    Narrower scope (programme, module, cohort) lives on each :class:`CalendarEvent`.

    This is the institution's published year (tests, exams, deadlines), distinct
    from ``myhub.Event``, which is the practice's own diary.
    """

    institution = models.ForeignKey(Institution, on_delete=models.CASCADE, related_name='calendars')
    year = models.PositiveIntegerField(db_index=True, help_text='Academic year, e.g. 2026.')
    name = models.CharField(max_length=160, blank=True,
                            help_text='Blank = "<Institution> · <year> academic calendar".')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Newest year first; one calendar per institution per year."""
        ordering = ['-year', 'institution']
        constraints = [
            models.UniqueConstraint(fields=['institution', 'year'], name='uniq_calendar_per_institution_year'),
        ]
        verbose_name = 'Academic calendar'

    def __str__(self):
        """The calendar's name, else ``UNISA · 2026``."""
        return self.name or f'{self.institution.code} · {self.year}'

    def save(self, *args, **kwargs):
        """Default a blank name to ``<Institution> · <year> academic calendar``, then
        save.
        """
        if not self.name:
            self.name = f'{self.institution.name} · {self.year} academic calendar'[:160]
        super().save(*args, **kwargs)

    @property
    def upcoming(self):
        """Published events still ahead of now, soonest first."""
        return self.events.filter(is_published=True, start__gte=timezone.now()).order_by('start')


class CalendarEvent(TimeStampedModel):
    """A dated thing on an institution's calendar — a test, an exam, a deadline.
    Each optional programme/offering/cohort link filled in narrows who it applies to.

    ``KIND_COLOURS`` uses the same colour semantics as the student guides (alert
    red for tests and deadlines, gold for coaching).
    """

    KIND_TEST = 'test'
    KIND_EXAM = 'exam'
    KIND_SUPPLEMENTARY = 'supp'
    KIND_ASSIGNMENT = 'assignment'
    KIND_DEADLINE = 'deadline'
    KIND_SESSION = 'session'
    KIND_INTENSIVE = 'intensive'
    KIND_MOCK = 'mock'
    KIND_RESULT = 'result'
    KIND_ORIENTATION = 'orientation'
    KIND_HOLIDAY = 'holiday'
    KIND_OTHER = 'other'
    KIND_CHOICES = [
        (KIND_TEST, 'Test'),
        (KIND_EXAM, 'Examination'),
        (KIND_SUPPLEMENTARY, 'Supplementary / re-assessment'),
        (KIND_ASSIGNMENT, 'Assignment due'),
        (KIND_DEADLINE, 'Administrative deadline'),
        (KIND_SESSION, 'Coaching session'),
        (KIND_INTENSIVE, 'Test / exam intensive'),
        (KIND_MOCK, 'Mock assessment'),
        (KIND_RESULT, 'Results released'),
        (KIND_ORIENTATION, 'Orientation'),
        (KIND_HOLIDAY, 'Holiday / no session'),
        (KIND_OTHER, 'Other'),
    ]

    KIND_COLOURS = {
        KIND_TEST: '#C00000',
        KIND_EXAM: '#8B0000',
        KIND_SUPPLEMENTARY: '#C0504D',
        KIND_ASSIGNMENT: '#1F3864',
        KIND_DEADLINE: '#C00000',
        KIND_SESSION: '#C9A84C',
        KIND_INTENSIVE: '#B8860B',
        KIND_MOCK: '#1D6A4E',
        KIND_RESULT: '#1D6A4E',
        KIND_ORIENTATION: '#4682B4',
        KIND_HOLIDAY: '#555555',
        KIND_OTHER: '#1F3864',
    }

    calendar = models.ForeignKey(AcademicCalendar, on_delete=models.CASCADE, related_name='events')
    programme = models.ForeignKey(Programme, on_delete=models.CASCADE, null=True, blank=True,
                                  related_name='calendar_events')
    programme_module = models.ForeignKey(ProgrammeModule, on_delete=models.CASCADE, null=True, blank=True,
                                         related_name='calendar_events',
                                         verbose_name='Module offering')
    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, null=True, blank=True,
                               related_name='calendar_events')

    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_OTHER, db_index=True)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=True,
                                  help_text='A published date with no fixed time — most institution dates.')
    location = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    weight_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
                                     help_text='Contribution to the final mark, if published.')
    is_published = models.BooleanField(default=True,
                                       help_text='Unpublished events are visible to staff only.')

    class Meta:
        """Ordered by start; indexed for the dashboard countdown.

        The exam countdown (every page load) filters calendar + kind and orders by
        start, which ``learning_calev_cal_kind_start`` serves in one index.
        ``learning_calev_prog_start`` serves the same lookup narrowed to a programme
        —
        the countdown discards dates scoped to a programme the candidate is not on.
        """
        ordering = ['start', 'id']
        indexes = [
            models.Index(fields=['calendar', 'start']),
            models.Index(fields=['calendar', 'kind', 'start'],
                         name='learning_calev_cal_kind_start'),
            models.Index(fields=['programme', 'start'],
                         name='learning_calev_prog_start'),
        ]
        verbose_name = 'Calendar event'

    def __str__(self):
        """The event's :attr:`label` and date."""
        return f'{self.label} · {self.start:%Y-%m-%d}'

    @property
    def label(self):
        """``MILPARK PGDA | FREP | Test 1`` — the LMS naming convention."""
        if self.programme_module_id:
            return f'{self.programme_module.label} | {self.title}'
        if self.programme_id:
            return f'{self.programme.label} | {self.title}'
        return f'{self.calendar.institution.label} | {self.title}'

    @property
    def colour(self):
        """The hex colour for the event's kind (brand navy when unmapped)."""
        return self.KIND_COLOURS.get(self.kind, '#1F3864')

    @property
    def is_upcoming(self):
        """True while the event's start is still ahead."""
        return self.start >= timezone.now()

    @property
    def days_away(self):
        """Whole days from now until it happens; negative once it has passed."""
        return (self.start.date() - timezone.now().date()).days


class ProgrammeEnrolment(TimeStampedModel):
    """A student registered for a programme, optionally on a specific cohort.

    Enrolment and access: pricing is per module per month, held on
    :class:`ModuleEnrolment`; a module stays LOCKED until paid for, with an optional
    7-day free trial ("week one").
    """
    person = models.ForeignKey('accounts.Person', on_delete=models.CASCADE, related_name='programme_enrolments')
    programme = models.ForeignKey(Programme, on_delete=models.CASCADE, related_name='enrolments')
    cohort = models.ForeignKey(Cohort, on_delete=models.SET_NULL, null=True, blank=True, related_name='enrolments')
    is_active = models.BooleanField(default=True)

    class Meta:
        """Newest first; one enrolment per person per programme."""
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['person', 'programme'], name='uniq_programme_enrolment_per_person'),
        ]
        verbose_name = 'Programme enrolment'

    def __str__(self):
        """``<person> · MILPARK-PGDA``."""
        return f'{self.person} · {self.programme.full_code}'


class ModuleEnrolment(TimeStampedModel):
    """A student's access to one :class:`ProgrammeModule` — the lock/unlock and
    billing record.

    Created LOCKED (selected + invoiced, unpaid); the payment hook flips it to
    ACTIVE, or the student starts a 7-day TRIAL. ``invoice_uid`` is the
    invoice's ``public_id`` (a UUID, not an FK, to avoid a learning↔finance
    import cycle).

    ``PAID_DAYS``: one paid month opens 33 days from the payment date — the month
    paid for plus a few days' grace, so a payment made on the 31st still covers the
    same date next month. See :mod:`apps.learning.week_access` for how it gates
    weeks.
    """
    STATUS_LOCKED = 'locked'
    STATUS_TRIAL = 'trial'
    STATUS_ACTIVE = 'active'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_LOCKED, 'Locked — awaiting payment'),
        (STATUS_TRIAL, 'Free trial'),
        (STATUS_ACTIVE, 'Active — paid'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    TRIAL_DAYS = 7
    PAID_DAYS = 33

    person = models.ForeignKey('accounts.Person', on_delete=models.CASCADE, related_name='module_enrolments')
    programme_module = models.ForeignKey(ProgrammeModule, on_delete=models.CASCADE, related_name='enrolments')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_LOCKED, db_index=True)
    price_at_enrolment = models.DecimalField(
        max_digits=8, decimal_places=2, default=0,
        help_text='Monthly price captured at enrolment (so later price changes do not rewrite history).')
    started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    paid_until = models.DateField(null=True, blank=True, help_text='Access is paid up to this date.')
    paid_from = models.DateField(
        null=True, blank=True,
        help_text='Start of the current unbroken paid run (the payment date). Weeks between this '
                  'date and paid_until are the ones the payment unlocks.')
    invoice_uid = models.UUIDField(null=True, blank=True,
                                   help_text='public_id of the invoice raised for this module.')

    class Meta:
        """Ordered by offering then person; one enrolment per person per offering."""
        ordering = ['programme_module', 'person']
        constraints = [
            models.UniqueConstraint(fields=['person', 'programme_module'], name='uniq_module_enrolment_per_person'),
        ]
        verbose_name = 'Module enrolment'

    def __str__(self):
        """``<person> · FREP (trial)``."""
        return f'{self.person} · {self.programme_module.code} ({self.status})'

    @property
    def price(self):
        """Monthly price for this enrolment (captured value, else the module's)."""
        return self.price_at_enrolment or self.programme_module.price_per_month or 0

    @property
    def is_trial_valid(self):
        """True while on a trial that has not yet ended."""
        return self.status == self.STATUS_TRIAL and (
            self.trial_ends_at is None or self.trial_ends_at >= timezone.now())

    @property
    def trial_days_left(self):
        """Whole days remaining on a live trial, rounded up so any part of the
        final day still reads as "1 day left". ``None`` when not on a live trial.
        """
        if not self.is_trial_valid or self.trial_ends_at is None:
            return None
        delta = self.trial_ends_at - timezone.now()
        if delta.total_seconds() <= 0:
            return 0
        return delta.days + (1 if (delta.seconds or delta.microseconds) else 0)

    @property
    def is_unlocked(self):
        """True when the student may open the module — paid & current, or on a live trial."""
        if self.status == self.STATUS_ACTIVE:
            return self.paid_until is None or self.paid_until >= timezone.now().date()
        return self.is_trial_valid

    def start_trial(self, now=None):
        """Begin the 7-day free trial ("week one")."""
        now = now or timezone.now()
        self.status = self.STATUS_TRIAL
        self.started_at = self.started_at or now
        self.trial_ends_at = now + timedelta(days=self.TRIAL_DAYS)
        self.save(update_fields=['status', 'started_at', 'trial_ends_at', 'updated_at'])
        return self

    def activate(self, months=1, now=None):
        """Unlock as paid (called by the payment hook) and extend access by ``months``.

        Each month paid is :attr:`PAID_DAYS` (33) days. A payment made while the
        previous one is still running extends it from the current ``paid_until``
        (the paid run stays unbroken, so ``paid_from`` is kept); otherwise a new
        run starts on the payment date.
        """
        now = now or timezone.now()
        today = timezone.localdate(now) if timezone.is_aware(now) else now.date()
        extending = bool(self.paid_until and self.paid_until > today)
        start = self.paid_until if extending else today
        if not extending or self.paid_from is None:
            self.paid_from = today
        self.status = self.STATUS_ACTIVE
        self.started_at = self.started_at or now
        self.paid_until = start + timedelta(days=self.PAID_DAYS * months)
        self.save(update_fields=['status', 'started_at', 'paid_from', 'paid_until', 'updated_at'])
        PaidPeriod.objects.create(enrolment=self, starts_on=start, ends_on=self.paid_until)
        return self

    def lock(self):
        """Lock the module again (status only; paid history is kept). Returns ``self``.
        """
        self.status = self.STATUS_LOCKED
        self.save(update_fields=['status', 'updated_at'])
        return self


class PaidPeriod(models.Model):
    """One paid stretch of a module enrolment. Weeks inside any paid period stay
    open after the payment lapses: students keep what they paid for."""
    enrolment = models.ForeignKey(ModuleEnrolment, on_delete=models.CASCADE, related_name='paid_periods')
    starts_on = models.DateField()
    ends_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Ordered by enrolment, then start date."""
        ordering = ['enrolment', 'starts_on']

    def __str__(self):
        """``<enrolment> · <start>–<end>``."""
        return f'{self.enrolment} · {self.starts_on}–{self.ends_on}'


class Topic(TimeStampedModel):
    """One topic of one offering's syllabus — **owned by the offering**, so
    each institution's copy is edited independently.

    :attr:`code` (``FR-01``) is shared across copies and is the key for
    cross-institution reporting — see :meth:`siblings`. :attr:`frequency` and
    :attr:`avg_marks` are this institution's own past-paper analysis and stay
    blank until it has been run (a Milpark pattern says nothing about UNISA).

    ``FREQUENCY_META`` gives the label and Bootstrap tone per frequency — the trend
    analysis, shown as a badge. Progress is measured against published lessons and
    open assessments.
    """

    FREQ_EVERY_SITTING = 'every_sitting'
    FREQ_MOST_SITTINGS = 'most_sittings'
    FREQ_RECURRING = 'recurring'
    FREQ_PERIODIC = 'periodic'
    FREQ_RARE = 'rare'
    FREQUENCY_CHOICES = [
        (FREQ_EVERY_SITTING, 'Every sitting'),
        (FREQ_MOST_SITTINGS, 'Most sittings'),
        (FREQ_RECURRING, 'Recurring'),
        (FREQ_PERIODIC, 'Periodic'),
        (FREQ_RARE, 'Rarely tested'),
    ]
    FREQUENCY_META = {
        FREQ_EVERY_SITTING: ('Every sitting', 'danger'),
        FREQ_MOST_SITTINGS: ('Most sittings', 'warning'),
        FREQ_RECURRING: ('Recurring', 'primary'),
        FREQ_PERIODIC: ('Periodic', 'info'),
        FREQ_RARE: ('Rarely tested', 'secondary'),
    }

    programme_module = models.ForeignKey(ProgrammeModule, on_delete=models.CASCADE, related_name='topics')
    code = models.CharField(max_length=20, db_index=True,
                            help_text='Topic code from the curriculum reference, e.g. "FR-01". Shared with '
                                      'the same topic on other institutions — that is how they are reported '
                                      'together.')
    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=270, blank=True)
    reference = models.CharField(max_length=160, blank=True,
                                 help_text='Standard / legislation behind the topic — "IAS 12", "VAT Act s16(3)".')
    description = models.TextField(blank=True)
    depth = models.CharField(max_length=4, choices=DEPTH_CHOICES, blank=True,
                             help_text="Blank = inherit the offering's depth.")
    frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES, blank=True, db_index=True,
                                 help_text="How often the topic appears in this institution's papers. "
                                           'Blank = not yet analysed.')
    avg_marks = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True,
                                    help_text="🎯 Average marks the topic carries in this institution's papers.")
    is_non_negotiable = models.BooleanField(default=False, help_text='📌 Non-negotiable — prepare this every time.')
    notes = models.TextField(blank=True, help_text='How this offering treats the topic.')
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered on the raw ``order`` column; ``code`` unique per offering.

        Order on the raw column only — following the ``programme_module`` FK pulls in
        its ordering and Django raises "Infinite loop caused by ordering". Topics are
        always queried per offering, so ``order`` is enough.
        """
        ordering = ['order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['programme_module', 'code'],
                                    name='uniq_topic_code_per_programme_module'),
        ]
        indexes = [models.Index(fields=['programme_module', 'order'])]

    def __str__(self):
        """``MILPARK-PGDA / FREP / FR-01``."""
        return f'{self.programme_module.reference} / {self.code}'

    def save(self, *args, **kwargs):
        """Derive a missing slug, and inherit the offering's depth when none is set,
        then save.
        """
        if not self.slug:
            self.slug = slugify(f'{self.code}-{self.title}')[:270] or 'topic'
        if not self.depth:
            self.depth = self.programme_module.resolved_depth
        super().save(*args, **kwargs)

    @property
    def programme(self):
        """The programme of the topic's offering."""
        return self.programme_module.programme

    @property
    def institution(self):
        """The institution of the topic's offering."""
        return self.programme_module.programme.institution

    @property
    def module(self):
        """The canonical module behind this offering."""
        return self.programme_module.module

    @property
    def label(self):
        """``MILPARK PGDA | FREP | Deferred tax`` — the naming convention."""
        return f'{self.programme_module.label} | {self.title}'

    @property
    def resolved_depth(self):
        """The topic's depth, else the offering's resolved depth."""
        return self.depth or self.programme_module.resolved_depth

    def siblings(self):
        """The same topic (by :attr:`code`) on every other offering."""
        return (Topic.objects.filter(code=self.code)
                .exclude(pk=self.pk)
                .select_related('programme_module__programme__institution'))

    @property
    def is_analysed(self):
        """False until this institution's own past papers have been worked."""
        return bool(self.frequency)

    @property
    def frequency_label(self):
        """The frequency badge label ("Not yet analysed" when blank)."""
        return self.FREQUENCY_META.get(self.frequency, ('Not yet analysed', 'secondary'))[0]

    @property
    def frequency_tone(self):
        """The Bootstrap tone of the frequency badge."""
        return self.FREQUENCY_META.get(self.frequency, ('Not yet analysed', 'secondary'))[1]

    @property
    def has_content(self):
        """True once the topic has any published lesson or open assessment."""
        from apps.assessments.models import Assessment
        return (self.lessons.filter(status=Lesson.STATUS_PUBLISHED).exists()
                or self.assessments.filter(status=Assessment.STATUS_OPEN).exists())

    def required_lessons(self):
        """The published lessons a candidate is expected to work through."""
        return list(self.lessons.filter(status=Lesson.STATUS_PUBLISHED))

    def completion_for(self, student):
        """Percentage of this topic's published lessons completed (0–100)."""
        lessons = self.required_lessons()
        if not lessons:
            return 0
        from .models import StudySession
        done = (StudySession.objects
                .filter(student=student, lesson__in=lessons,
                        status=StudySession.STATUS_COMPLETED)
                .values('lesson_id').distinct().count())
        return round(min(done, len(lessons)) / len(lessons) * 100)

    def is_completed_by(self, student):
        """True when the topic has published lessons and ``student`` has completed them
        all.
        """
        lessons = self.required_lessons()
        return bool(lessons) and self.completion_for(student) >= 100

class LessonQuerySet(models.QuerySet):
    """Query helpers shared by every surface that lists lessons."""

    def for_role(self, user):
        """Drop lessons written for a different role than ``user``'s.

        Empty :attr:`Lesson.target_roles` means everyone. Staff are *not*
        exempt — they open such lessons from the manager instead.
        """
        person = getattr(user, 'profile', None)
        role = getattr(person, 'user_type', None) or 'student'
        return self.filter(models.Q(target_roles=[]) | models.Q(target_roles__contains=[role]))


class Lesson(TimeStampedModel):
    """A single study lesson: rich body + attached resources, versioned and
    publishable to a chosen audience.

    * ``topic`` is where the lesson sits on the institution-first spine; ``module``
      belongs to the legacy course spine, kept while content moves across.
    * ``body`` is legacy content; new lessons use ordered :class:`LessonBlock`
      rows. It is kept as the export fallback.
    * ``references`` is the bibliography: a list of
      ``{"text": str, "url": str, "authors": str, "year": str}``.
    * ``target_programmes`` / ``target_modules`` / ``target_users`` are optional
      targeting when the visibility is narrower than the lesson's own place.
    * ``target_roles`` lists the roles the lesson is written for, e.g.
      ``["student"]``; empty means everyone in the audience. It lets a module carry
      one lesson per role.
    """

    objects = LessonQuerySet.as_manager()

    STATUS_DRAFT = 'draft'
    STATUS_PUBLISHED = 'published'
    STATUS_ARCHIVED = 'archived'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PUBLISHED, 'Published'),
        (STATUS_ARCHIVED, 'Archived'),
        (STATUS_SCHEDULED, 'Scheduled'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    VIS_INSTITUTION = 'institution'
    VIS_PROGRAMME = 'programme'
    VIS_MODULE = 'module'
    VIS_GROUP = 'group'
    VIS_INDIVIDUAL = 'individual'
    VISIBILITY_CHOICES = [
        (VIS_INSTITUTION, 'Entire institution'),
        (VIS_PROGRAMME, 'Specific programme'),
        (VIS_MODULE, 'Specific module'),
        (VIS_GROUP, 'Study group'),
        (VIS_INDIVIDUAL, 'Individual students'),
    ]

    topic = models.ForeignKey(Topic, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='lessons')
    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE, null=True, blank=True,
                                related_name='lessons')
    title = models.CharField(max_length=200)
    subtitle = models.CharField(max_length=300, blank=True)
    slug = models.SlugField(max_length=220, blank=True)
    body = models.TextField(blank=True, help_text='Legacy rich-text content (block editor preferred).')
    cover_image = models.ImageField(upload_to='lessons/covers/', blank=True, null=True,
                                    validators=v.validate_image)

    author_name = models.CharField(max_length=160, blank=True)
    author_role = models.CharField(max_length=160, blank=True, help_text='e.g. "Senior Lecturer, Physics".')
    author_bio = models.TextField(blank=True)
    author_avatar = models.ImageField(upload_to='lessons/authors/', blank=True, null=True,
                                      validators=v.validate_avatar)
    references = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    visibility = models.CharField(max_length=12, choices=VISIBILITY_CHOICES, default=VIS_MODULE)
    target_programmes = models.ManyToManyField(Programme, blank=True, related_name='targeted_lessons')
    target_modules = models.ManyToManyField('learning.ProgrammeModule', blank=True, related_name='targeted_lessons')
    target_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='targeted_lessons')

    target_roles = models.JSONField(default=list, blank=True)

    publish_at = models.DateTimeField(null=True, blank=True)
    expire_at = models.DateTimeField(null=True, blank=True)
    estimated_minutes = models.PositiveIntegerField(default=30, help_text='Estimated study time.')
    version = models.PositiveIntegerField(default=1)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='lessons_created')

    class Meta:
        """Ordered by module, then title."""
        ordering = ['module', 'title']

    def __str__(self):
        """The lesson's title."""
        return self.title

    def save(self, *args, **kwargs):
        """Derive a missing slug from the title, then save."""
        if not self.slug:
            self.slug = slugify(self.title)[:220] or 'lesson'
        super().save(*args, **kwargs)

    @property
    def programme_module(self):
        """The offering this lesson belongs to (via its topic), if any."""
        return self.topic.programme_module if self.topic_id else None

    @property
    def programme(self):
        """The programme of the lesson's topic, if any."""
        return self.topic.programme if self.topic_id else None

    @property
    def institution(self):
        """The institution of the lesson's topic, if any."""
        return self.topic.institution if self.topic_id else None

    @property
    def uses_blocks(self):
        """True when the lesson has structured block content."""
        return self.blocks.exists()

    def body_blocks(self):
        """The lesson's top-level blocks in order; a ``section`` block among
        them renders as a collapsible panel of its ``child_blocks``."""
        return (self.blocks.filter(section__isnull=True)
                .select_related('assessment', 'meeting',
                                'holds_section')
                .order_by('order', 'id'))

    @property
    def author_display(self):
        """The author's name: the authored one, else the creator's full name or
        username.
        """
        if self.author_name:
            return self.author_name
        if self.created_by_id:
            return self.created_by.get_full_name() or self.created_by.get_username()
        return ''

    def get_absolute_url(self):
        """The lesson's view page."""
        from django.urls import reverse
        return reverse('learning:lesson-view', args=[self.pk])

    @property
    def is_live(self):
        """Published and within its publish/expire window."""
        if self.status != self.STATUS_PUBLISHED:
            return False
        now = timezone.now()
        if self.publish_at and self.publish_at > now:
            return False
        if self.expire_at and self.expire_at < now:
            return False
        return True


class LessonSection(TimeStampedModel):
    """One collapsible, timed, optionally locked **section** of a lesson.

    :attr:`section_type` and :attr:`duration_minutes` are either authored or
    inferred/estimated from the :class:`LessonBlock`s inside. ``TYPE_META`` gives,
    per resolved type, the label, icon, Bootstrap tone and the unit shown after the
    number of minutes.
    """

    TYPE_AUTO = 'auto'
    TYPE_READING = 'reading'
    TYPE_VIDEO = 'video'
    TYPE_QUIZ = 'quiz'
    TYPE_ASSESSMENT = 'assessment'
    TYPE_LIVE = 'live'
    TYPE_RESOURCES = 'resources'
    TYPE_CHOICES = [
        (TYPE_AUTO, 'Auto — detect from content'),
        (TYPE_READING, 'Reading'),
        (TYPE_VIDEO, 'Video'),
        (TYPE_QUIZ, 'Quiz'),
        (TYPE_ASSESSMENT, 'Assessment'),
        (TYPE_LIVE, 'Live session'),
        (TYPE_RESOURCES, 'Resources'),
    ]

    TYPE_META = {
        TYPE_READING: ('Reading', 'bi-book', 'info', 'min read'),
        TYPE_VIDEO: ('Video', 'bi-play-btn', 'primary', 'min watch'),
        TYPE_QUIZ: ('Quiz', 'bi-ui-checks', 'warning', 'min quiz'),
        TYPE_ASSESSMENT: ('Assessment', 'bi-clipboard-check', 'danger', 'min'),
        TYPE_LIVE: ('Live class', 'bi-camera-video', 'success', 'min live'),
        TYPE_RESOURCES: ('Resources', 'bi-paperclip', 'secondary', 'min'),
    }

    WORDS_PER_MINUTE = 200

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='sections')
    order = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=200, default='New section')
    summary = models.CharField(max_length=300, blank=True,
                               help_text='One line shown under the title when the section is open.')
    section_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default=TYPE_AUTO)
    duration_minutes = models.PositiveIntegerField(default=0, help_text='0 = estimate automatically.')

    is_required = models.BooleanField(default=True, help_text='Counts toward lesson completion.')
    requires_previous = models.BooleanField(
        default=False, help_text='Locked until every earlier required section is completed.')
    open_by_default = models.BooleanField(default=False)

    class Meta:
        """Ordered by the raw ``order`` column only.

        Ordering by the ``lesson`` FK follows Lesson→Module→ProgrammeModule ordering
        into an infinite loop (see :class:`LessonBlock`).
        """
        ordering = ['order', 'id']
        indexes = [models.Index(fields=['lesson', 'order'])]

    def __str__(self):
        """``<title> (<lesson id>)``."""
        return f'{self.title} ({self.lesson_id})'

    @property
    def resolved_type(self):
        """The authored type, or the one inferred from the blocks inside."""
        if self.section_type != self.TYPE_AUTO:
            return self.section_type
        return self.infer_type()

    def infer_type(self):
        """Detect the section's kind from its blocks, most specific first.

        A quiz block can point at a test or assignment, which reads as an assessment
        to
        the learner, so the linked object decides between quiz and assessment.
        """
        types = {b.block_type for b in self.blocks.all()}
        if LessonBlock.TYPE_QUIZ in types:
            kinds = {b.assessment.kind for b in self.blocks.all()
                     if b.block_type == LessonBlock.TYPE_QUIZ and b.assessment_id}
            if kinds - {'quiz'}:
                return self.TYPE_ASSESSMENT
            return self.TYPE_QUIZ
        if LessonBlock.TYPE_MEETING in types:
            return self.TYPE_LIVE
        if types & {LessonBlock.TYPE_VIDEO, LessonBlock.TYPE_AUDIO}:
            return self.TYPE_VIDEO
        if types and not (types - {LessonBlock.TYPE_FILE, LessonBlock.TYPE_IMAGE, LessonBlock.TYPE_DIVIDER}):
            return self.TYPE_RESOURCES
        return self.TYPE_READING

    @property
    def type_label(self):
        """The label of the resolved type."""
        return self.TYPE_META.get(self.resolved_type, self.TYPE_META[self.TYPE_READING])[0]

    @property
    def type_icon(self):
        """The Bootstrap icon of the resolved type."""
        return self.TYPE_META.get(self.resolved_type, self.TYPE_META[self.TYPE_READING])[1]

    @property
    def type_tone(self):
        """The Bootstrap tone of the resolved type."""
        return self.TYPE_META.get(self.resolved_type, self.TYPE_META[self.TYPE_READING])[2]

    @property
    def minutes(self):
        """Authored duration, else an estimate from the content (never 0)."""
        if self.duration_minutes:
            return self.duration_minutes
        return max(1, self.estimate_minutes())

    def estimate_minutes(self):
        """Estimate how long this section takes: media length + quiz time limit
        + reading time for the prose.

        Media length comes from ``data['seconds']``, written by the editor once the
        media loads (5 minutes when unknown).
        """
        total, words = 0, 0
        for block in self.blocks.all():
            data = block.data or {}
            if block.block_type in (LessonBlock.TYPE_VIDEO, LessonBlock.TYPE_AUDIO):
                total += int(round((data.get('seconds') or 0) / 60)) or 5
            elif block.block_type == LessonBlock.TYPE_QUIZ and block.assessment_id:
                total += block.assessment.time_limit_minutes or 10
            elif block.block_type == LessonBlock.TYPE_MEETING:
                total += int(data.get('duration') or 60)
            else:
                words += _count_words(block)
        return total + round(words / self.WORDS_PER_MINUTE)

    @property
    def duration_label(self):
        """The minutes with the type's unit, e.g. ``12 min read``."""
        unit = self.TYPE_META.get(self.resolved_type, self.TYPE_META[self.TYPE_READING])[3]
        return f'{self.minutes} {unit}'

    @property
    def assessments(self):
        """The assessments a learner must sit inside this section."""
        return [b.assessment for b in self.blocks.all()
                if b.block_type == LessonBlock.TYPE_QUIZ and b.assessment_id]

    def is_completed_by(self, student):
        """Quiz/assessment sections derive completion from real attempts so work
        done anywhere in the hub counts; everything else uses
        :class:`LessonSectionProgress`."""
        graded = self.assessments
        if graded:
            from apps.assessments.models import AssessmentAttempt
            return AssessmentAttempt.objects.filter(
                assessment__in=graded, student=student,
                status__in=('submitted', 'marked')).exists()
        progress = self.progress.filter(student=student).first()
        return bool(progress and progress.status == LessonSectionProgress.STATUS_COMPLETED)

    def best_attempt_for(self, student):
        """The learner's best attempt across this section's assessments."""
        graded = self.assessments
        if not graded:
            return None
        from apps.assessments.models import AssessmentAttempt
        return (AssessmentAttempt.objects
                .filter(assessment__in=graded, student=student,
                        status__in=('submitted', 'marked'))
                .select_related('assessment')
                .order_by('-score', '-created_at')
                .first())


def _count_words(block):
    """Rough word count of a block's text, HTML tags stripped."""
    import re
    data = block.data or {}
    parts = [data.get('html') or '', data.get('text') or '', data.get('caption') or '']
    for row in (data.get('rows') or []):
        parts.extend(str(c) for c in (row or []))
    text = re.sub(r'<[^>]+>', ' ', ' '.join(parts))
    return len([w for w in text.split() if w])


class LessonSectionProgress(TimeStampedModel):
    """Per-student completion + time-on-section for a :class:`LessonSection`."""

    STATUS_NOT_STARTED = 'not_started'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_COMPLETED = 'completed'
    STATUS_CHOICES = [
        (STATUS_NOT_STARTED, 'Not started'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_COMPLETED, 'Completed'),
    ]

    section = models.ForeignKey(LessonSection, on_delete=models.CASCADE, related_name='progress')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='lesson_section_progress')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NOT_STARTED)
    seconds_spent = models.PositiveIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """One progress row per section per student."""
        unique_together = ('section', 'student')

    def __str__(self):
        """``<student> · <section> (<status>)``."""
        return f'{self.student} · {self.section} ({self.status})'


class LessonNote(TimeStampedModel):
    """A student's private note against one section of a lesson."""

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='notes')
    section = models.ForeignKey(LessonSection, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='notes')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='lesson_notes')
    body = models.TextField(blank=True)

    class Meta:
        """One note per lesson section per student; most recently edited first."""
        unique_together = ('lesson', 'section', 'student')
        ordering = ['-updated_at']

    def __str__(self):
        """``Note · <student> · <section or lesson>``."""
        return f'Note · {self.student} · {self.section or self.lesson}'


class LessonBookmark(TimeStampedModel):
    """A place in a lesson a student saved to come back to.

    ``anchor`` locates the spot within the block: ``{"text": "…"}``,
    ``{"seconds": 132.4}`` or ``{"question_id": 12}``.
    """

    KIND_SECTION = 'section'
    KIND_TEXT = 'text'
    KIND_MEDIA = 'media'
    KIND_QUESTION = 'question'
    KIND_CHOICES = [
        (KIND_SECTION, 'Section'),
        (KIND_TEXT, 'Passage'),
        (KIND_MEDIA, 'Moment in media'),
        (KIND_QUESTION, 'Question'),
    ]

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='bookmarks')
    section = models.ForeignKey(LessonSection, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='bookmarks')
    block = models.ForeignKey('LessonBlock', on_delete=models.CASCADE, null=True, blank=True,
                              related_name='bookmarks')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='lesson_bookmarks')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_TEXT)
    label = models.CharField(max_length=300, blank=True, help_text='What was saved (excerpt / timestamp).')
    note = models.CharField(max_length=500, blank=True)
    anchor = models.JSONField(default=dict, blank=True)

    class Meta:
        """Newest first; indexed by lesson and student."""
        ordering = ['-created_at']
        indexes = [models.Index(fields=['lesson', 'student'])]

    def __str__(self):
        """``<kind> · <label excerpt>``."""
        return f'{self.get_kind_display()} · {self.label[:40]}'

    @property
    def icon(self):
        """The Bootstrap icon for the bookmark's kind."""
        return {self.KIND_SECTION: 'bi-bookmark', self.KIND_TEXT: 'bi-quote',
                self.KIND_MEDIA: 'bi-play-circle', self.KIND_QUESTION: 'bi-patch-question'}.get(
                    self.kind, 'bi-bookmark')

    @property
    def timestamp_label(self):
        """``mm:ss`` for a media bookmark, else ''."""
        seconds = (self.anchor or {}).get('seconds')
        if seconds is None:
            return ''
        seconds = int(seconds)
        return f'{seconds // 60}:{seconds % 60:02d}'


class LessonBlock(TimeStampedModel):
    """One ordered, typed content block of a :class:`Lesson`.

    Content lives in ``data`` JSON, plus ``media`` (upload) or ``data['url']``
    for media, or ``assessment`` / ``meeting`` for linked blocks.

    The two section fields answer different questions:

    * ``section``       — which section does this block live **in**? (NULL = top
    level,
      the lesson's main body)
    * ``holds_section`` — is this block **itself** a section? Set only on a
      ``section``-type block. Block → section deletion is handled by
      :func:`_delete_orphaned_section`, because an FK cannot cascade child → parent.

    :attr:`style` is the author's formatting for the block, whitelisted on both
    save and render by :mod:`apps.learning.styles` and never rendered raw.
    ``track`` is still read by the block editor and local progress.
    """

    TYPE_HEADING = 'heading'
    TYPE_TEXT = 'text'
    TYPE_IMAGE = 'image'
    TYPE_VIDEO = 'video'
    TYPE_AUDIO = 'audio'
    TYPE_EMBED = 'embed'
    TYPE_FILE = 'file'
    TYPE_REFERENCE = 'reference'
    TYPE_QUIZ = 'quiz'
    TYPE_MEETING = 'meeting'
    TYPE_CALLOUT = 'callout'
    TYPE_TABLE = 'table'
    TYPE_DIAGRAM = 'diagram'
    TYPE_DIVIDER = 'divider'
    TYPE_SECTION = 'section'
    TYPE_CHOICES = [
        (TYPE_HEADING, 'Heading'),
        (TYPE_TEXT, 'Text'),
        (TYPE_IMAGE, 'Image'),
        (TYPE_VIDEO, 'Video'),
        (TYPE_AUDIO, 'Audio'),
        (TYPE_EMBED, 'Embedded page / link'),
        (TYPE_FILE, 'File / download'),
        (TYPE_REFERENCE, 'Reference'),
        (TYPE_QUIZ, 'Quiz / test'),
        (TYPE_MEETING, 'Live session'),
        (TYPE_CALLOUT, 'Callout'),
        (TYPE_TABLE, 'Table'),
        (TYPE_DIAGRAM, 'Diagram'),
        (TYPE_DIVIDER, 'Divider'),
        (TYPE_SECTION, 'Dropdown section'),
    ]

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='blocks')
    section =models.ForeignKey(LessonSection, on_delete=models.CASCADE, null=True, blank=True,
                                related_name='blocks')
    holds_section = models.OneToOneField(LessonSection, on_delete=models.CASCADE, null=True, blank=True,
                                         related_name='anchor_block')
    order = models.PositiveIntegerField(default=0)
    block_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default=TYPE_TEXT)
    data = models.JSONField(default=dict, blank=True)
    style = models.JSONField(default=dict, blank=True)

    media = models.FileField(upload_to=UnguessableUploadTo('lessons/blocks'), blank=True, null=True, storage=files_storage,
                             validators=v.validate_lesson_media)
    assessment = models.ForeignKey('assessments.Assessment', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='lesson_blocks')
    meeting = models.ForeignKey('communication.MeetingRoom', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='lesson_blocks')
    track =models.BooleanField(default=False)

    class Meta:
        """Ordered by the raw ``order`` column only.

        Ordering by the ``lesson`` FK loops infinitely through
        Lesson→Module→ProgrammeModule ordering.
        """
        ordering = ['order', 'id']
        indexes = [models.Index(fields=['lesson', 'order'])]

    def __str__(self):
        """``<type> #<order> · <lesson id>``."""
        return f'{self.get_block_type_display()} #{self.order} · {self.lesson_id}'

    def save(self, *args, **kwargs):
        """Sanitise the block's data, then save.

        Author HTML (text/callout ``data['html']``) and URLs are sanitised on the way
        in — the block editor POSTs raw JSON, so its toolbar is no guarantee.
        Rendering
        cleans again (``|clean_html``) for older rows.
        """
        from core.sanitize import clean_block_data
        self.data = clean_block_data(self.data or {})
        super().save(*args, **kwargs)

    @property
    def media_url(self):
        """The best source URL for this block: uploaded file or external URL."""
        if self.media:
            try:
                return self.media.url
            except Exception:
                return ''
        from core.sanitize import safe_url
        return safe_url((self.data or {}).get('url', ''))

    @property
    def is_section(self):
        """True when this block *is* a collapsible section in the body."""
        return self.block_type == self.TYPE_SECTION and self.holds_section_id is not None

    @property
    def child_blocks(self):
        """The blocks inside this section block (empty for any other type)."""
        if not self.is_section:
            return self.__class__.objects.none()
        return self.holds_section.blocks.select_related(
            'assessment', 'meeting').order_by('order', 'id')

    @property
    def css(self):
        """This block's author styling as a safe inline ``style`` value."""
        from . import styles
        return styles.to_css(self.style or {})

    def to_dict(self):
        """Serialisable form the editor + player JS consume.

        ``data`` is cleaned again on the way out: the editor puts ``html`` straight
        into
        a contenteditable, so rows stored before save-time cleaning existed must not
        reach it raw.
        """
        from core.sanitize import clean_block_data
        return {
            'id': self.pk,
            'type': self.block_type,
            'block_type': self.block_type,
            'section_id': self.section_id,
            'holds_section_id': self.holds_section_id,
            'order': self.order,
            'data': clean_block_data(self.data or {}),
            'style': self.style or {},
            'css': self.css,
            'media_url': self.media_url,
            'track': self.track,
            'assessment_id': self.assessment_id,
            'meeting_id': self.meeting_id,
        }

    @property
    def embed_src(self):
        """A URL safe to drop into an <iframe> for embed/video blocks — YouTube &
        Vimeo watch links are normalised to their embeddable form.

        Anything else must be a plain ``http(s)`` web address — a ``javascript:`` src
        would run inside this site's origin the moment the iframe loads — or ``''``.
        """
        url = (self.data or {}).get('url', '') or ''
        if not url:
            return ''
        import re
        m = re.search(r'(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([\w-]{11})', url)
        if m:
            return f'https://www.youtube.com/embed/{m.group(1)}'
        m = re.search(r'vimeo\.com/(?:video/)?(\d+)', url)
        if m:
            return f'https://player.vimeo.com/video/{m.group(1)}'
        from core.sanitize import safe_url
        return url if safe_url(url).lower().startswith(('https://', 'http://')) else ''

    @property
    def is_iframe_video(self):
        """Video block that should render as an iframe (YouTube/Vimeo) vs <video>."""
        return self.block_type == self.TYPE_VIDEO and (self.data or {}).get('provider') in ('youtube', 'vimeo')

    @property
    def player_provider(self):
        """``youtube`` / ``vimeo`` for an externally-hosted video, else ``''``.
        Detected from the URL, not ``data['provider']``, which may be wrong."""
        url = (self.data or {}).get('url', '') or ''
        if not url:
            return ''
        import re
        if re.search(r'(?:youtube\.com|youtu\.be)', url):
            return 'youtube'
        if 'vimeo.com' in url:
            return 'vimeo'
        return ''

    @property
    def player_embed_id(self):
        """The provider's video id — what Plyr needs to build the player."""
        url = (self.data or {}).get('url', '') or ''
        if not url:
            return ''
        import re
        match = re.search(
            r'(?:youtube\.com/(?:watch\?v=|embed/|shorts/|live/)|youtu\.be/)([\w-]{11})', url)
        if match:
            return match.group(1)
        match = re.search(r'vimeo\.com/(?:video/)?(\d+)', url)
        if match:
            return match.group(1)
        return ''


@receiver(post_delete, sender=LessonBlock)
def _delete_orphaned_section(sender, instance, **kwargs):
    """Removing a section element from the body removes the section itself
    (otherwise it lingers invisibly, owning progress rows). A signal so it also
    fires for ``queryset.delete()``."""
    if instance.holds_section_id:
        LessonSection.objects.filter(pk=instance.holds_section_id).delete()


class LessonResource(models.Model):
    """A file/link/embed attached to a lesson (PDF, video, YouTube, audio, …)."""

    KIND_CHOICES = [
        ('text', 'Text'), ('pdf', 'PDF'), ('word', 'Word'), ('ppt', 'PowerPoint'),
        ('image', 'Image'), ('audio', 'Audio'), ('video', 'Video'),
        ('youtube', 'YouTube'), ('link', 'External link'), ('embed', 'Embed'),
        ('download', 'Download'),
    ]

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='resources')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='pdf')
    title = models.CharField(max_length=200, blank=True)
    file = models.FileField(upload_to=UnguessableUploadTo('lessons'), blank=True, null=True, storage=files_storage,
                            validators=v.validate_attachment)
    url = models.URLField(max_length=500, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        """Ordered by ``lesson_id`` then ``order``.

        ``lesson_id``, not ``lesson``: following the FK's ordering raises
        "Infinite loop caused by ordering".
        """
        ordering = ['lesson_id', 'order']

    def __str__(self):
        """The resource's title, else ``<kind> for <lesson>``."""
        return self.title or f'{self.get_kind_display()} for {self.lesson}'


class LessonVersion(models.Model):
    """An immutable snapshot of a lesson's content — restore / reuse next year."""

    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='versions')
    version = models.PositiveIntegerField(default=1)
    body = models.TextField(blank=True)
    resources_json = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='lesson_versions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest version first per lesson (``lesson_id``, not ``lesson`` — see
        :class:`LessonResource`); versions are unique per lesson.
        """
        ordering = ['lesson_id', '-version']
        unique_together = ('lesson', 'version')

    def __str__(self):
        """``<lesson> v<version>``."""
        return f'{self.lesson} v{self.version}'


class StudySession(TimeStampedModel):
    """Measures actual studying of a lesson with a start/pause/resume/finish timer."""

    STATUS_NOT_STARTED = 'not_started'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_PAUSED = 'paused'
    STATUS_SUBMITTED = 'submitted'
    STATUS_COMPLETED = 'completed'
    STATUS_OVERDUE = 'overdue'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_NOT_STARTED, 'Not started'),
        (STATUS_IN_PROGRESS, 'In progress'),
        (STATUS_PAUSED, 'Paused'),
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_OVERDUE, 'Overdue'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='study_sessions')
    lesson = models.ForeignKey(Lesson, on_delete=models.CASCADE, related_name='study_sessions')

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NOT_STARTED, db_index=True)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    last_resumed_at = models.DateTimeField(null=True, blank=True)
    total_seconds = models.PositiveIntegerField(default=0)
    completion_pct = models.PositiveIntegerField(default=0)

    class Meta:
        """Newest first."""
        ordering = ['-created_at']

    def __str__(self):
        """``<student> · <lesson> (<status>)``."""
        return f'{self.student} · {self.lesson} ({self.get_status_display()})'

    @property
    def minutes(self):
        """Time studied so far, in minutes (one decimal)."""
        return round(self.total_seconds / 60, 1)

    def start(self):
        """Start or resume the timer and mark the session in progress."""
        now = timezone.now()
        if self.start_time is None:
            self.start_time = now
        self.last_resumed_at = now
        self.status = self.STATUS_IN_PROGRESS
        self.save(update_fields=['start_time', 'last_resumed_at', 'status', 'updated_at'])

    def _accumulate(self):
        """Add the time since the last resume to ``total_seconds`` and stop the clock.
        """
        if self.last_resumed_at:
            self.total_seconds += int((timezone.now() - self.last_resumed_at).total_seconds())
            self.last_resumed_at = None

    def pause(self):
        """Pause the timer, banking the time studied so far."""
        self._accumulate()
        self.status = self.STATUS_PAUSED
        self.save(update_fields=['total_seconds', 'last_resumed_at', 'status', 'updated_at'])

    def finish(self, completion_pct=100):
        """Stop the timer and record completion: completed at 100%, else submitted."""
        self._accumulate()
        self.end_time = timezone.now()
        self.completion_pct = min(100, max(0, completion_pct))
        self.status = self.STATUS_COMPLETED if self.completion_pct >= 100 else self.STATUS_SUBMITTED
        self.save()


class ModulePhase(TimeStampedModel):
    """One preparation block of an offering — "Test 2 preparation".

    A preparation period, not the assessment itself: :attr:`calendar_event`
    points at the institution's published date to count down to.

    The module schedule::

        ProgrammeModule                     "MILPARK PGDA | FREP"
          └── ModulePhase        ×6         "Test 1 preparation" … "Exam 2 preparation"
                └── ModuleWeek   ×n         "Week 3 · Deferred tax"
                      └── ModuleMaterial

    A phase also owns material directly (``week=None``), e.g. its blueprint.

    ACCESS is deliberately two-sided, because the practice sells two ways:

    * pay for the **module** (ModuleEnrolment active/trial) → everything in it;
    * buy a **single item** from the shop (ModuleMaterial.product) → that item.

    Neither knows about the other; :mod:`apps.learning.access` resolves both.

    ``DEFAULT_PLAN`` is the default year: four tests then two exams.
    ``scaffold_module_schedule`` builds exactly this, and an educator edits from
    there. A blank ``cohort`` makes the block a shared template for every cohort;
    set, it is for that cohort only — scoping the phase scopes its weeks and
    materials too.
    """

    KIND_TEST = 'test'
    KIND_EXAM = 'exam'
    KIND_SUPPLEMENTARY = 'supp'
    KIND_OTHER = 'other'
    KIND_CHOICES = [
        (KIND_TEST, 'Test preparation'),
        (KIND_EXAM, 'Examination preparation'),
        (KIND_SUPPLEMENTARY, 'Supplementary preparation'),
        (KIND_OTHER, 'Other'),
    ]

    DEFAULT_PLAN = [
        (KIND_TEST, 1), (KIND_TEST, 2), (KIND_TEST, 3), (KIND_TEST, 4),
        (KIND_EXAM, 1), (KIND_EXAM, 2),
    ]

    programme_module = models.ForeignKey(ProgrammeModule, on_delete=models.CASCADE,
                                         related_name='phases', verbose_name='Module offering')
    cohort = models.ForeignKey(
        'Cohort', on_delete=models.CASCADE, null=True, blank=True, related_name='module_phases',
        help_text='Which intake this block is for. Blank = shared by every cohort of the module.')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_TEST, db_index=True)
    sequence = models.PositiveSmallIntegerField(
        default=1, help_text='Which test/exam this prepares for — 1–4 for tests, 1–2 for exams.')
    title = models.CharField(max_length=160, blank=True,
                             help_text='Blank = built from kind + sequence ("Test 2 preparation").')
    summary = models.CharField(max_length=300, blank=True,
                               help_text='One line on what this block covers.')
    calendar_event = models.ForeignKey(CalendarEvent, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='prep_phases',
                                       help_text='The test/exam on the academic calendar this prepares for.')
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True,
                                       help_text='Unpublished blocks are visible to staff only.')
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered within the offering; one block per kind and sequence per intake.

        One "Test 2 preparation" per intake, and one shared template block — B26F and
        B26S each sit their own Test 2 on their own date.
        """
        ordering = ['programme_module', 'order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['programme_module', 'cohort', 'kind', 'sequence'],
                                    name='uniq_phase_per_module_cohort'),
            models.UniqueConstraint(fields=['programme_module', 'kind', 'sequence'],
                                    condition=models.Q(cohort__isnull=True),
                                    name='uniq_shared_phase_per_module'),
        ]
        verbose_name = 'Module phase'

    def __str__(self):
        """``MILPARK PGDA | FREP | Test 2 preparation``."""
        return f'{self.programme_module.label} | {self.display_title}'

    def save(self, *args, **kwargs):
        """Build a missing title from kind and sequence, then save."""
        if not self.title:
            self.title = self.build_title(self.kind, self.sequence)
        super().save(*args, **kwargs)

    @staticmethod
    def build_title(kind, sequence):
        """``Test 2 preparation`` / ``Exam 1 preparation``."""
        noun = {ModulePhase.KIND_TEST: 'Test', ModulePhase.KIND_EXAM: 'Exam',
                ModulePhase.KIND_SUPPLEMENTARY: 'Supplementary'}.get(kind, 'Block')
        return f'{noun} {sequence} preparation'

    @property
    def display_title(self):
        """The block's title, else one built from kind and sequence."""
        return self.title or self.build_title(self.kind, self.sequence)

    @property
    def short_label(self):
        """``Test 2`` — for chips and countdowns, where "preparation" is noise."""
        noun = {self.KIND_TEST: 'Test', self.KIND_EXAM: 'Exam',
                self.KIND_SUPPLEMENTARY: 'Supp'}.get(self.kind, 'Block')
        return f'{noun} {self.sequence}'

    @property
    def tone(self):
        """Bootstrap-ish tone used by the chips on the schedule."""
        return {self.KIND_TEST: 'info', self.KIND_EXAM: 'danger',
                self.KIND_SUPPLEMENTARY: 'warning'}.get(self.kind, 'secondary')

    @property
    def assessment_date(self):
        """The date being prepared for, from the calendar event or ``ends_on``."""
        if self.calendar_event_id:
            return self.calendar_event.start.date()
        return self.ends_on

    @property
    def days_away(self):
        """Whole days until the assessment; negative once it has passed, None if undated."""
        target = self.assessment_date
        return None if target is None else (target - timezone.now().date()).days

    @property
    def is_current(self):
        """True while today falls inside the preparation window."""
        today = timezone.now().date()
        if self.starts_on and today < self.starts_on:
            return False
        end = self.ends_on or self.assessment_date
        if end and today > end:
            return False
        return bool(self.starts_on or end)


class ModuleWeek(TimeStampedModel):
    """One week inside a preparation phase. :attr:`topics` is its ordered series
    of topics (through :class:`WeekTopic`); revision/mock weeks have none.

    ``make_available`` opens the week to every student on the module, paid or not.
    Otherwise paid-access rules apply — see :mod:`apps.learning.week_access`.
    """

    phase = models.ForeignKey(ModulePhase, on_delete=models.CASCADE, related_name='weeks')
    number = models.PositiveSmallIntegerField(default=1, help_text='Week number within this phase.')
    title = models.CharField(max_length=200, blank=True,
                             help_text="Blank = the topics' names, else \"Week n\".")
    topics = models.ManyToManyField('Topic', through='WeekTopic', related_name='in_weeks',
                                    blank=True,
                                    help_text='The study-guide topics covered this week, in order.')
    summary = models.TextField(blank=True)
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    make_available = models.BooleanField(
        default=False,
        help_text='Open this week to every student on the module, whether or not they have paid.')
    is_active = models.BooleanField(default=True)

    class Meta:
        """Ordered within the phase; week numbers are unique per phase."""
        ordering = ['phase', 'order', 'number', 'id']
        constraints = [
            models.UniqueConstraint(fields=['phase', 'number'], name='uniq_week_per_phase'),
        ]
        verbose_name = 'Module week'

    def __str__(self):
        """``<phase title> · <week title>``."""
        return f'{self.phase.display_title} · {self.display_title}'

    @property
    def topic_list(self):
        """The week's topics in order (uses the WeekTopic through, prefetch-friendly)."""
        return [wt.topic for wt in self.week_topics.all()]

    @property
    def primary_topic(self):
        """The first topic of the series, or None (revision/mock weeks have none)."""
        topics = self.topic_list
        return topics[0] if topics else None

    @property
    def display_title(self):
        """The week's title, else ``Week n · <topics>``, else ``Week n``."""
        if self.title:
            return self.title
        topics = self.topic_list
        if topics:
            return f'Week {self.number} · ' + ', '.join(t.title for t in topics)
        return f'Week {self.number}'

    @property
    def reference(self):
        """The authority behind the week's first topic — ``IAS 12`` — or blank."""
        return getattr(self.primary_topic, 'reference', '')

    @property
    def is_current(self):
        """True while today falls between the week's start and end dates."""
        today = timezone.now().date()
        if self.starts_on and self.ends_on:
            return self.starts_on <= today <= self.ends_on
        return False


class WeekTopic(TimeStampedModel):
    """One topic taught in one week, with its order. Deleting either side
    removes the link, never the topic itself."""

    week = models.ForeignKey(ModuleWeek, on_delete=models.CASCADE, related_name='week_topics')
    topic = models.ForeignKey('Topic', on_delete=models.CASCADE, related_name='week_links')
    order = models.PositiveSmallIntegerField(default=0, help_text='Order of this topic within the week.')

    class Meta:
        """Ordered within the week; a topic appears once per week."""
        ordering = ['week', 'order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['week', 'topic'], name='uniq_topic_per_week'),
        ]
        verbose_name = 'Week topic'

    def __str__(self):
        """``<week title> · <topic code>``."""
        return f'{self.week.display_title} · {self.topic.code}'


class ModuleMaterial(TimeStampedModel):
    """One studiable thing — the row a student actually clicks.

    Hangs off a :class:`ModuleWeek`, or the :class:`ModulePhase` directly
    (``week=None``). :attr:`kind` says what it is; whichever link is filled in
    (file, lesson, assessment, meeting, url — exactly one is normally set) says what
    it points at.

    :attr:`product` lets someone who has not paid for the module buy this item
    alone; module payers get it anyway — see :mod:`apps.learning.access`.

    * ``audience``: coach notes, evidence bases and tutor-only past papers live in
      the same week as the student material they support, but must never reach a
      student. A staff-only row can never be published — see :meth:`save`.
    * ``used_on``: when this was actually used with the class — the date it sits
      on in the calendar history, distinct from ``available_from`` (an embargo).
    * ``source_ref``: a stable key from the content pack that placed the row, so a
      re-import updates it instead of adding a copy.
    """

    KIND_BLUEPRINT = 'blueprint'
    KIND_STUDY_GUIDE = 'study_guide'
    KIND_QUESTIONS = 'questions'
    KIND_ANSWERS = 'answers'
    KIND_MOCK_EXAM = 'mock_exam'
    KIND_ASSESSMENT = 'assessment'
    KIND_LIVE = 'live'
    KIND_RECORDING = 'recording'
    KIND_DOCUMENT = 'document'
    KIND_CHOICES = [
        (KIND_BLUEPRINT, 'Blueprint'),
        (KIND_STUDY_GUIDE, 'Topic study guide'),
        (KIND_QUESTIONS, 'Questions'),
        (KIND_ANSWERS, 'Answers / solutions'),
        (KIND_MOCK_EXAM, 'Mock exam / past paper'),
        (KIND_ASSESSMENT, 'Assessment'),
        (KIND_LIVE, 'Live session'),
        (KIND_RECORDING, 'Recording'),
        (KIND_DOCUMENT, 'Document'),
    ]

    KIND_ICONS = {
        KIND_BLUEPRINT: 'bi-diagram-3',
        KIND_STUDY_GUIDE: 'bi-journal-richtext',
        KIND_QUESTIONS: 'bi-patch-question',
        KIND_ANSWERS: 'bi-check2-square',
        KIND_MOCK_EXAM: 'bi-file-earmark-ruled',
        KIND_ASSESSMENT: 'bi-ui-checks',
        KIND_LIVE: 'bi-camera-video',
        KIND_RECORDING: 'bi-play-btn',
        KIND_DOCUMENT: 'bi-file-earmark-text',
    }
    KIND_TONES = {
        KIND_BLUEPRINT: 'primary',
        KIND_STUDY_GUIDE: 'info',
        KIND_QUESTIONS: 'warning',
        KIND_ANSWERS: 'success',
        KIND_MOCK_EXAM: 'danger',
        KIND_ASSESSMENT: 'warning',
        KIND_LIVE: 'success',
        KIND_RECORDING: 'secondary',
        KIND_DOCUMENT: 'secondary',
    }

    phase = models.ForeignKey(ModulePhase, on_delete=models.CASCADE, related_name='materials')
    week = models.ForeignKey(ModuleWeek, on_delete=models.CASCADE, null=True, blank=True,
                             related_name='materials',
                             help_text='Leave blank for material that covers the whole phase '
                                       '(e.g. the test blueprint).')
    topic = models.ForeignKey('Topic', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='materials',
                              help_text='Which topic within the week this material belongs to. '
                                        'Blank = it covers the whole week/phase, not one topic.')
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=KIND_DOCUMENT, db_index=True)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    file = models.FileField(upload_to=UnguessableUploadTo('modules/materials'), blank=True, null=True,
                            storage=files_storage, validators=v.validate_attachment)
    lesson = models.ForeignKey('Lesson', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='module_materials')
    assessment = models.ForeignKey('assessments.Assessment', on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='module_materials')
    meeting = models.ForeignKey('communication.MeetingRoom', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='module_materials')
    url = models.URLField(max_length=500, blank=True)

    product = models.ForeignKey('shop.Product', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='module_materials',
                                help_text='Set when this item is also sold on its own in the shop.')

    AUDIENCE_STUDENTS = 'students'
    AUDIENCE_STAFF = 'staff'
    AUDIENCE_CHOICES = [
        (AUDIENCE_STUDENTS, 'Students'),
        (AUDIENCE_STAFF, 'Coaches & staff only'),
    ]
    audience = models.CharField(max_length=8, choices=AUDIENCE_CHOICES, default=AUDIENCE_STUDENTS,
                                db_index=True)
    estimated_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Reading / working time shown to the student.')
    used_on = models.DateField(null=True, blank=True)

    available_from = models.DateTimeField(null=True, blank=True,
                                          help_text='Hidden from students until this moment.')
    is_published = models.BooleanField(default=True)
    is_preview = models.BooleanField(default=False,
                                     help_text='Open to everyone, even on a locked module — the taster.')
    order = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    source_ref = models.CharField(max_length=140, blank=True, db_index=True,
                                  help_text='Stable key from the content pack that placed this.')

    class Meta:
        """Ordered by phase, week and ``order``; indexed by phase and kind."""
        ordering = ['phase', 'week', 'order', 'id']
        indexes = [
            models.Index(fields=['phase', 'kind'], name='learning_material_phase_kind'),
        ]
        verbose_name = 'Module material'

    def __str__(self):
        """``<kind> · <title>``."""
        return f'{self.get_kind_display()} · {self.title}'

    def save(self, *args, **kwargs):
        """Save, never publishing staff-only material.

        The one choke point every publish path goes through: the builder, a content
        pack, the upload review queue. Staff-only material stays visible to authors
        and
        invisible to students, whatever asked. With ``update_fields``,
        ``is_published``
        is added so the correction is written.
        """
        if self.audience == self.AUDIENCE_STAFF and self.is_published:
            self.is_published = False
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'is_published'}
        super().save(*args, **kwargs)

    @property
    def is_staff_only(self):
        """True for coach/staff-only material."""
        return self.audience == self.AUDIENCE_STAFF

    @property
    def icon(self):
        """The Bootstrap icon for the material's kind."""
        return self.KIND_ICONS.get(self.kind, 'bi-file-earmark')

    @property
    def tone(self):
        """The Bootstrap tone for the material's kind."""
        return self.KIND_TONES.get(self.kind, 'secondary')

    @property
    def programme_module(self):
        """The offering the material's phase belongs to."""
        return self.phase.programme_module

    @property
    def is_released(self):
        """False while an embargo (``available_from``) is still in the future."""
        return not (self.available_from and self.available_from > timezone.now())

    @property
    def target_url(self):
        """Where clicking it goes — resolved in the order the links are ranked.

        A file goes through the gated door (``learning:material-open``), never the
        storage URL: the raw path is what a free account used to fetch paid files by.
        """
        from django.urls import reverse
        if self.lesson_id:
            return reverse('learning:lesson-view', args=[self.lesson_id])
        if self.assessment_id:
            return reverse('assessments:take', args=[self.assessment_id])
        if self.meeting_id:
            return self.meeting.get_join_url()
        if self.file:
            return reverse('learning:material-open', args=[self.pk]) if self.pk else ''
        return self.url or ''

    @property
    def is_purchasable(self):
        """True when it is listed in the shop on its own."""
        return bool(self.product_id and getattr(self.product, 'status', '') == 'active')


from .upload_models import ContentUpload, ContentUploadFile  # noqa: E402,F401
