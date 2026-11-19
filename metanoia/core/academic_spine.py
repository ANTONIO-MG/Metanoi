"""The academic spine — single source of truth for institutions, programmes,
module offerings, prices and academic calendars; ``seed()`` installs it.

The platform ships with **neutral sample data** only: one institution row for
the programmes Metanoia Group runs itself (``METANOIA``), the "Other
institution" catch-all, and one *Sample Programme* with three small modules.
Every name, description and date in the data tables below is a placeholder so a
new install has a structure that works end to end; replace it with the real
programmes in the admin (or edit these tables) before going live.

⚠️  Assessment dates are a template, not published timetables: they are written
``is_published=False`` unless ``publish=True`` and must be confirmed against the
institution's own timetable in the admin.

Logos are fetched from :data:`LOGO_SOURCES` when an institution lists any; a
generated lettermark stands in when it lists none or the download fails (or
``logos=False``), and an institution with no entry at all gets the generic
institution mark.

Seeding is idempotent and non-destructive: with ``update=False`` (the default)
it only fills blanks, so admin edits survive a re-run. It shares natural keys
with ``manage.py seed_sample_structure``, so either can run first.

Data tables
-----------

:data:`DEFAULT_PRICE`
    What one module costs a student per month, in rand, when nothing else sets
    a price (a placeholder, not a published fee).
:data:`INSTITUTIONS`
    The providers. ``METANOIA`` holds the programmes Metanoia Group runs itself.
    ``OTHER`` is the catch-all, so a student whose provider is not on the list
    can still finish registration instead of hitting a dead end at the picker;
    it is ordered 99 so every institution added later still sorts above it, and
    has no website and no :data:`LOGO_SOURCES` entry, so the seeder gives it the
    generic institution mark rather than a lettermark (see
    :meth:`SpineSeeder.give_logo`).
:data:`LOGO_SOURCES`
    Where each logo comes from, tried in order; the first that yields an image
    wins. A ``page``/``pattern`` pair scrapes the logo out of a site's own HTML.
    An empty list means "draw a lettermark".
:data:`MODULES`
    The canonical module catalogue: one row per subject, however many
    programmes teach it; each programme's version is an offering. Rows may carry
    ``is_competency_area`` for a competency-based programme.
:data:`PROGRAMMES`
    Each programme with its ``offerings`` and ``cohorts``. An offering is
    ``(the institution's own code, canonical module code, depth, name override,
    coverage note)``. A cohort is ``(code, name[, start, end])``, with dates
    only where they are known. The ``OTHER`` institution still needs a
    programme with modules on it: the registration picker cascades institution
    → programme → modules, so an institution carrying none leaves the student on
    a step they cannot complete. It has no cohorts — that support is scheduled
    with the student, not run as an intake.
:data:`ASSESSMENT_CYCLE`
    ``(title, kind, (month, day), span in days, note)``: a template grid of the
    year's tests and examination, to be replaced with each institution's
    published timetable.
:data:`COACHING_LEAD_IN`
    ``(title, kind, days before the assessment it attaches to, note)`` — what
    Metanoia Group runs around those dates. Titles carry no module code:
    ``CalendarEvent.label`` prefixes the offering, so putting it in the title too
    would give "INTRO | INTRO · Test intensive".
:data:`INSTITUTION_DATES`
    ``(title, kind, (month, day), span, note)`` — institution-level dates that
    are not module-specific.
:data:`APC_DATES`
    The year of a competency-based professional assessment, same shape. Only
    used for an institution coded ``SAICA`` (none ships); kept because the
    programme-pack builder re-scopes these dates.
:data:`FIXED_HOLIDAYS`
    South African public holidays — the no-session calendar. Fixed-date holidays
    only; the Easter pair is computed. Where one falls on a Sunday the following
    Monday is a public holiday (Public Holidays Act 36 of 1994).
:data:`BLUEPRINTS`
    A new institution's whole programme/module structure in one choice, built
    from the canonical catalogue so it matches the seeded rows: each holds
    programme specs with ``[(this institution's module code, canonical module
    code)]``. :data:`BLUEPRINT_CHOICES` is ``[(key, label)]`` for a form's
    choice field, blank last.
:data:`EXTRA_COHORTS`
    ``(institution, programme, spec)`` — cohorts that are not in
    :data:`PROGRAMMES` (the sample pack's intake, built in full by ``manage.py
    seed_sample_programme``).
:data:`COHORT_CALENDAR`
    Dated calendar rows per institution and cohort, ``(institution, programme,
    cohort, module offering, kind, title, start, end, status, note)``. The rows
    shipped are samples for the Sample Programme's intakes and show the three
    statuses, which say how far to trust a date:

    ``PUB``  published by the institution → published to students;
    ``EST``  projected from an earlier calendar → staff only;
    ``CON``  the institution's own sources disagree → staff only, both dates noted.

    Built by ``manage.py seed_cohort_calendar``; the calendar an event lands on
    is the institution's calendar for the year the event falls in.
"""
import io
import re
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.files.base import ContentFile
from django.utils import timezone

from apps.learning.models import (
    DEPTH_ADVANCED, DEPTH_FOUNDATIONAL, DEPTH_INTEGRATED, DEPTH_UNDERGRADUATE,
    AcademicCalendar, CalendarEvent, Cohort, Institution, Module,
    Programme, ProgrammeModule, ProgrammePhase,
)

ADV, FND, INT, UG = (DEPTH_ADVANCED, DEPTH_FOUNDATIONAL,
                     DEPTH_INTEGRATED, DEPTH_UNDERGRADUATE)

DEFAULT_PRICE = Decimal('500')

INSTITUTIONS = [
    {
        'code': 'METANOIA', 'name': 'Metanoia Group', 'order': 1,
        'accent_name': 'Brown', 'accent_colour': '#3E2415',
        'website': '',
        'description':
            'The programmes Metanoia Group runs itself. This row ships with the sample data so a new '
            'install has a working structure; rename it, describe it or add other institutions in the '
            'admin.',
    },
    {
        'code': 'OTHER', 'name': 'Other institution', 'order': 99,
        'accent_name': 'Slate', 'accent_colour': '#6B7A8F',
        'website': '',
        'description':
            'For students registered somewhere that is not listed above. Choose this and your '
            'programme is put together with you one on one rather than run as a standard intake.',
    },
]

LOGO_SOURCES = {
    'METANOIA': [],
}

MODULES = [
    {'code': 'INTRO', 'name': 'Introduction', 'order': 1, 'description':
        'Sample module: welcome, orientation and planning your studies.'},
    {'code': 'CORE', 'name': 'Core concepts', 'order': 2, 'description':
        'Sample module: the key terms of the programme and how they fit together.'},
    {'code': 'PRACT', 'name': 'Practice and assessment', 'order': 3, 'description':
        'Sample module: a worked example, practice questions and a mini mock.'},
]

PROGRAMMES = [
    {
        'institution': 'METANOIA', 'code': 'SAMPLE', 'abbreviation': 'SAMPLE',
        'name': 'Sample Programme', 'order': 1,
        'full_name': 'Sample Programme',
        'level': Programme.LEVEL_CTA, 'depth_default': FND,
        'description':
            'A small sample programme that ships with the platform so a new install has a working '
            'structure: three modules, each with a test-and-examination cycle. Replace it with your '
            'own programmes.',
        'offerings': [
            ('INTRO', 'INTRO', FND, '', ''),
            ('CORE', 'CORE', FND, '', ''),
            ('PRACT', 'PRACT', FND, '', ''),
        ],
        'cohorts': [('SAMPLE1', 'Sample intake 1', '2027-02-01', '2027-11-30'),
                    ('SAMPLE2', 'Sample intake 2', '2026-07-01', '2027-05-31'),
                    ('SAMPLE3', 'Sample intake 3 · dates to confirm')],
    },
    {
        'institution': 'OTHER', 'code': 'CURATED', 'abbreviation': 'Curated',
        'name': 'Curated', 'order': 1,
        'full_name': 'Curated one-on-one support',
        'level': Programme.LEVEL_CTA, 'depth_default': FND,
        'description':
            'For students whose institution is not one we run a standard programme for. Pick the '
            'modules you are carrying and the plan is put together around them and reviewed with you '
            'one on one rather than on a fixed cohort timetable.',
        'offerings': [
            ('INTRO', 'INTRO', FND, '', ''),
            ('CORE', 'CORE', FND, '', ''),
            ('PRACT', 'PRACT', FND, '', ''),
        ],
        'cohorts': [],
    },
]

ASSESSMENT_CYCLE = [
    ('Test 1', CalendarEvent.KIND_TEST, (3, 17), 1,
     'First sitting of the year. Covers the opening block of the module roadmap.'),
    ('Test 2', CalendarEvent.KIND_TEST, (5, 12), 1,
     'Second sitting. Cumulative — Test 1 material is still examinable.'),
    ('Test 3', CalendarEvent.KIND_TEST, (7, 21), 1,
     'Third sitting, and the usual formal progress review point.'),
    ('Test 4', CalendarEvent.KIND_TEST, (9, 8), 1,
     'Final test before the examination.'),
    ('Final examination', CalendarEvent.KIND_EXAM, (10, 20), 1,
     'Full-scope examination covering the whole module.'),
]

COACHING_LEAD_IN = [
    ('Test intensive', CalendarEvent.KIND_INTENSIVE, 7,
     'Short-cycle intensive in the run-up to the sitting — highest-recurrence topics first.'),
    ('Mock assessment', CalendarEvent.KIND_MOCK, 14,
     'Full mock mirroring past-paper format, marked against the mark plan with written feedback.'),
]

INSTITUTION_DATES = [
    ('Registration closes', CalendarEvent.KIND_DEADLINE, (2, 20), 1,
     'Institution admin deadline — confirm against the institution\'s own calendar.'),
    ('Orientation and start of the academic year', CalendarEvent.KIND_ORIENTATION, (3, 2), 1,
     'Welcome pack, module roadmaps and the year\'s key dates.'),
    ('Mid-year recess', CalendarEvent.KIND_HOLIDAY, (6, 29), 14,
     'No scheduled sessions. Revision timetable applies.'),
    ('Results released', CalendarEvent.KIND_RESULT, (12, 4), 1,
     'Final results for the year.'),
]

APC_DATES = [
    ('APC programme starts · Foundation phase', CalendarEvent.KIND_ORIENTATION, (3, 2), 1,
     'Diagnostic across all six competency areas, then targeted gap closure.'),
    ('Integration phase begins', CalendarEvent.KIND_SESSION, (6, 1), 1,
     'Cross-discipline linking and case drilling.'),
    ('Simulation phase begins', CalendarEvent.KIND_SESSION, (8, 3), 1,
     'Full-length integrated case simulations under exam conditions.'),
    ('Case simulation 1', CalendarEvent.KIND_MOCK, (8, 22), 1,
     'Full-length case, marked with written feedback per competency area.'),
    ('Case simulation 2', CalendarEvent.KIND_MOCK, (9, 19), 1,
     'Second full-length case. Compare competency coverage against simulation 1.'),
    ('Case simulation 3', CalendarEvent.KIND_MOCK, (10, 17), 1,
     'Final full-length case before the assessment.'),
    ('Exam-ready phase begins', CalendarEvent.KIND_SESSION, (11, 2), 1,
     'Consolidation, timing and mindset.'),
    ('APC — Assessment of Professional Competence', CalendarEvent.KIND_EXAM, (11, 19), 1,
     'The assessment itself. Confirm the sitting date with SAICA.'),
]

FIXED_HOLIDAYS = [
    ((1, 1), "New Year's Day"),
    ((3, 21), 'Human Rights Day'),
    ((4, 27), 'Freedom Day'),
    ((5, 1), "Workers' Day"),
    ((6, 16), 'Youth Day'),
    ((8, 9), "National Women's Day"),
    ((9, 24), 'Heritage Day'),
    ((12, 16), 'Day of Reconciliation'),
    ((12, 25), 'Christmas Day'),
    ((12, 26), 'Day of Goodwill'),
]


BLUEPRINTS = {
    'sample': {
        'label': 'Sample — one programme with the three sample modules',
        'summary': 'A Sample Programme carrying Introduction, Core concepts, and Practice and '
                   'assessment, each with the template test-and-examination cycle. A starting point '
                   'to rename and extend.',
        'programmes': [
            {
                'code': 'SAMPLE', 'abbreviation': 'SAMPLE', 'name': 'Sample Programme', 'order': 1,
                'full_name': 'Sample Programme',
                'level': Programme.LEVEL_CTA, 'depth_default': FND,
                'description': 'A sample programme with three modules. Rename it and add your own '
                               'modules from the institution page.',
                'modules': [('INTRO', 'INTRO'), ('CORE', 'CORE'), ('PRACT', 'PRACT')],
            },
        ],
    },
    'blank': {
        'label': 'Blank — I will add the programmes myself',
        'summary': 'Creates the institution only. Add its programmes and modules from the '
                   'institution page afterwards.',
        'programmes': [],
    },
}

BLUEPRINT_CHOICES = [(key, spec['label']) for key, spec in BLUEPRINTS.items() if key != 'blank']
BLUEPRINT_CHOICES.append(('blank', BLUEPRINTS['blank']['label']))


def apply_blueprint(institution, key, *, price=DEFAULT_PRICE, calendar=True, year=None,
                    publish=False):
    """Build ``key``'s programmes and modules under ``institution``.

    Canonical modules are reused where they exist, so cross-institution
    reporting keeps working. Returns
    ``{'programmes': [...], 'offerings': [...], 'events': int}``.
    """
    blueprint = BLUEPRINTS.get(key)
    if not blueprint:
        return {'programmes': [], 'offerings': [], 'events': 0}

    catalogue = {spec['code']: spec for spec in MODULES}
    programmes, offerings = [], []

    for spec in blueprint['programmes']:
        spec = dict(spec)
        module_specs = spec.pop('modules', [])
        phase_specs = spec.pop('phases', [])
        code = spec.pop('code')
        programme, _ = Programme.objects.get_or_create(
            institution=institution, code=code,
            defaults={'name': spec.get('name', code), **spec})
        programmes.append(programme)

        for order, (own_code, module_code) in enumerate(module_specs, 1):
            module = Module.objects.filter(code=module_code).first()
            if module is None:
                template = catalogue.get(module_code, {})
                module = Module.objects.create(
                    code=module_code,
                    name=template.get('name', module_code),
                    description=template.get('description', ''),
                    is_competency_area=template.get('is_competency_area', False),
                    order=template.get('order', order))
            offering, created = ProgrammeModule.objects.get_or_create(
                programme=programme, code=own_code,
                defaults={'module': module, 'order': order,
                          'description': module.description,
                          'price_per_month': price})
            if created:
                offerings.append(offering)

        for order, (phase_name, period, focus) in enumerate(phase_specs, 1):
            ProgrammePhase.objects.get_or_create(
                programme=programme, name=phase_name,
                defaults={'period': period, 'focus': focus, 'order': order})

    events = 0
    if calendar:
        seeder = SpineSeeder(year=year, price=price, publish=publish, verbose=False)
        events = seeder.seed_calendars({institution.code: institution})
    return {'programmes': programmes, 'offerings': offerings, 'events': events}


def easter_sunday(year):
    """Anonymous Gregorian computus — Good Friday and Family Day hang off this."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month = (h + lam - 7 * m + 114) // 31
    day = ((h + lam - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def public_holidays(year):
    """``[(date, name)]`` for the year, Sunday roll-over applied (a holiday on a
    Sunday adds the Monday as "(observed)")."""
    easter = easter_sunday(year)
    days = [(easter - timedelta(days=2), 'Good Friday'),
            (easter + timedelta(days=1), 'Family Day')]
    days += [(date(year, month, day), name) for (month, day), name in FIXED_HOLIDAYS]
    for when, name in list(days):
        if when.weekday() == 6:
            days.append((when + timedelta(days=1), f'{name} (observed)'))
    return sorted(days)


USER_AGENT = {'User-Agent': 'Mozilla/5.0 (compatible; MetanoiaGroup-LMS-seed/1.0)'}


def _download(url, timeout=40):
    """Fetch ``url`` with the seeder's user agent and return the body bytes."""
    request = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _svg_to_png(data):
    """Rasterise SVG bytes. Tries each backend that might be present; returns
    ``None`` when none of them is.

    In order: ``cairosvg`` (best quality, if the native library is there),
    ``svglib`` + reportlab's ``renderPM`` (pure Python), then macOS QuickLook
    (``qlmanage``, always there on a Mac).
    """
    try:
        import cairosvg
        return cairosvg.svg2png(bytestring=data, output_width=600)
    except Exception:
        pass
    try:
        from reportlab.graphics import renderPM
        from svglib.svglib import svg2rlg
        drawing = svg2rlg(io.BytesIO(data))
        if drawing is not None and drawing.width:
            scale = 600 / drawing.width
            drawing.scale(scale, scale)
            drawing.width, drawing.height = drawing.width * scale, drawing.height * scale
            return renderPM.drawToString(drawing, fmt='PNG', bg=0xFFFFFF)
    except Exception:
        pass
    try:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'logo.svg'
            source.write_bytes(data)
            subprocess.run(['qlmanage', '-t', '-s', '800', '-o', tmp, str(source)],
                           capture_output=True, timeout=60, check=False)
            rendered = Path(tmp) / 'logo.svg.png'
            if rendered.exists():
                return rendered.read_bytes()
    except Exception:
        pass
    return None


def _trim(image, tolerance=12):
    """Crop the uniform border off a logo, transparent or flat-coloured.

    QuickLook renders onto a square white canvas, so the logo arrives as a small
    mark floating in a lot of nothing; without this every logo would render at a
    fraction of its box.
    """
    from PIL import Image, ImageChops

    image = image.convert('RGBA')
    alpha = image.getchannel('A')
    box = alpha.getbbox() if alpha.getextrema()[0] < 255 else None
    if box is None:
        rgb = image.convert('RGB')
        background = Image.new('RGB', rgb.size, rgb.getpixel((0, 0)))
        box = (ImageChops.difference(rgb, background).convert('L')
               .point(lambda p: 255 if p > tolerance else 0).getbbox())
    return image.crop(box) if box else image


def _normalise(raw):
    """Bytes off the web → a trimmed PNG, or ``None`` if it is not an image."""
    from PIL import Image

    if raw[:5] in (b'<?xml', b'<svg ') or b'<svg' in raw[:400]:
        raw = _svg_to_png(raw)
        if raw is None:
            return None
    try:
        image = _trim(Image.open(io.BytesIO(raw)))
    except Exception:
        return None
    image.thumbnail((600, 600), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _lettermark(institution):
    """A logo of last resort: the institution's code on its own accent colour."""
    from PIL import Image, ImageDraw, ImageFont

    size, colour = (600, 300), institution.accent_colour or '#1F3864'
    image = Image.new('RGB', size, colour)
    draw = ImageDraw.Draw(image)
    text = institution.code.upper()
    font = None
    for path in ('/System/Library/Fonts/Supplemental/Arial Bold.ttf',
                 '/System/Library/Fonts/Helvetica.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            font = ImageFont.truetype(path, 96)
            break
        except Exception:
            continue
    font = font or ImageFont.load_default()
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((size[0] - box[2] + box[0]) / 2, (size[1] - box[3] + box[1]) / 2),
              text, fill='#FFFFFF', font=font)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _generic_mark(institution):
    """A neutral institution facade for a row with no logo to fetch.

    Used for the "Other institution" catch-all, where an "OTHER" lettermark would
    look like a mistake in the picker. Drawn so it matches the accent colour: a
    white pediment, architrave, four columns and plinth.
    """
    from PIL import Image, ImageDraw

    size, colour = (600, 300), institution.accent_colour or '#6B7A8F'
    image = Image.new('RGB', size, colour)
    draw = ImageDraw.Draw(image)
    ink = '#FFFFFF'

    draw.polygon([(225, 140), (375, 140), (300, 80)], fill=ink)
    draw.rectangle([232, 146, 368, 158], fill=ink)
    for left in (240, 274, 308, 342):
        draw.rectangle([left, 164, left + 18, 204], fill=ink)
    draw.rectangle([222, 210, 378, 224], fill=ink)

    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def fetch_logo(code):
    """The institution's logo as PNG bytes, from its own website. ``None`` if
    every source failed (offline, moved, or the page no longer matches)."""
    import base64

    for source in LOGO_SOURCES.get(code, []):
        try:
            if 'url' in source:
                raw = _download(source['url'])
            else:
                page = _download(source['page']).decode('utf-8', 'ignore')
                match = re.search(source['pattern'], page)
                if not match:
                    continue
                found = match.group(1)
                raw = (base64.b64decode(found.split(',', 1)[1])
                       if found.startswith('data:') else _download(found))
            png = _normalise(raw)
            if png:
                return png
        except Exception:
            continue
    return None


PROVISIONAL = ('\n\nSeeded date — the shape of the year is a template, the day is not. '
               "Confirm against the institution's published timetable and correct it here.")


class SpineSeeder:
    """Builds the spine. See :func:`seed` for the usual way in."""

    def __init__(self, *, year=None, price=DEFAULT_PRICE, update=False, logos=True,
                 refresh_logos=False, calendar=True, holidays=True, publish=False,
                 verbose=True, indent='  '):
        self.year = year or timezone.now().year
        self.price = Decimal(str(price))
        self.update = update
        self.logos = logos
        self.refresh_logos = refresh_logos
        self.calendar = calendar
        self.holidays = holidays
        self.publish = publish
        self.verbose = verbose
        self.indent = indent
        self.logo_stats = {'downloaded': 0, 'generated': 0, 'kept': 0}

    def log(self, message):
        """Print ``message`` (indented) when ``verbose``."""
        if self.verbose:
            print(f'{self.indent}{message}')

    def _apply(self, obj, fields, created):
        """Write ``fields`` onto ``obj``. On an existing row only blanks are
        filled, unless ``update=True`` — so admin edits survive a re-run."""
        changed = []
        for name, value in fields.items():
            current = getattr(obj, name)
            if created or self.update or current in ('', None, 0):
                if current != value:
                    setattr(obj, name, value)
                    changed.append(name)
        if changed:
            obj.save()
        return changed

    def give_logo(self, institution):
        """Attach a logo unless one is stored already (``refresh_logos`` re-fetches).

        An institution listed in :data:`LOGO_SOURCES` whose logo could not be
        reached (or which lists no source) gets a lettermark — its own code is
        still the most recognisable stand-in. An institution with no entry at all
        (the "Other institution" catch-all) gets the generic institution mark.
        """
        if institution.logo and not self.refresh_logos:
            self.logo_stats['kept'] += 1
            return
        png = fetch_logo(institution.code) if self.logos else None
        if png:
            self.logo_stats['downloaded'] += 1
            note = 'downloaded'
        elif institution.code in LOGO_SOURCES:
            png = _lettermark(institution)
            self.logo_stats['generated'] += 1
            note = 'generated lettermark' if not self.logos else 'download failed → lettermark'
        else:
            png = _generic_mark(institution)
            self.logo_stats['generated'] += 1
            note = 'generated institution mark'
        institution.logo.save(f'{institution.code.lower()}.png', ContentFile(png), save=True)
        self.log(f'    {institution.code:8} logo {note} ({len(png):,} bytes)')

    def seed_institutions(self):
        """Create or fill every institution in :data:`INSTITUTIONS` and give each a logo."""
        made = {}
        for spec in INSTITUTIONS:
            spec = dict(spec)
            code = spec.pop('code')
            institution, created = Institution.objects.get_or_create(
                code=code, defaults={'name': spec['name']})
            self._apply(institution, spec, created)
            self.give_logo(institution)
            made[code] = institution
        self.log(f'  {len(made)} institutions — ' + ' · '.join(
            f'{i.code} ({i.accent_name or i.accent_colour})' for i in made.values()))
        stats = self.logo_stats
        self.log(f"  logos: {stats['downloaded']} downloaded · {stats['generated']} generated "
                 f"· {stats['kept']} already stored")
        return made

    def seed_modules(self):
        """Create or fill the canonical modules in :data:`MODULES`."""
        made = {}
        for spec in MODULES:
            spec = dict(spec)
            code = spec.pop('code')
            module, created = Module.objects.get_or_create(code=code, defaults={'name': spec['name']})
            self._apply(module, spec, created)
            made[code] = module
        examined = sum(1 for m in made.values() if not m.is_competency_area)
        self.log(f'  {len(made)} canonical modules — {examined} examined, '
                 f'{len(made) - examined} competency areas')
        return made

    def seed_programmes(self, institutions, modules):
        """Create or fill programmes, their offerings, cohorts and phases.

        An offering's price is set on creation, and whenever it is still zero — rows
        made by ``seed_sample_structure`` arrive unpriced. Returns
        ``(programmes by full code, offerings)``.
        """
        programmes, offerings = {}, []
        for spec in PROGRAMMES:
            spec = dict(spec)
            institution = institutions[spec.pop('institution')]
            code = spec.pop('code')
            module_specs = spec.pop('offerings', [])
            cohort_specs = spec.pop('cohorts', [])
            phase_specs = spec.pop('phases', [])

            programme, created = Programme.objects.get_or_create(
                institution=institution, code=code, defaults={'name': spec['name']})
            self._apply(programme, spec, created)
            programmes[programme.full_code] = programme

            for order, (own_code, module_code, depth, name, coverage) in enumerate(module_specs, 1):
                offering, made = ProgrammeModule.objects.get_or_create(
                    programme=programme, code=own_code, defaults={'module': modules[module_code]})
                self._apply(offering, {
                    'module': modules[module_code], 'depth_level': depth, 'order': order,
                    'name': name, 'description': coverage or modules[module_code].description,
                }, made)
                if made or self.update or not offering.price_per_month:
                    offering.price_per_month = self.price
                    offering.save(update_fields=['price_per_month'])
                offerings.append(offering)

            for cohort_spec in cohort_specs:
                seed_cohort(programme, cohort_spec, update=self.update)
            for order, (phase_name, period, focus) in enumerate(phase_specs, 1):
                ProgrammePhase.objects.get_or_create(
                    programme=programme, name=phase_name,
                    defaults={'period': period, 'focus': focus, 'order': order})

            self.log(f'  {programme.full_code:14} {len(module_specs)} module(s)'
                     f'{f" · {len(cohort_specs)} cohort(s)" if cohort_specs else ""}'
                     f'{f" · {len(phase_specs)} phase(s)" if phase_specs else ""}'
                     f'{"" if created else "   (already there)"}')
        return programmes, offerings

    def _at(self, month, day, hour=9):
        """Aware datetime on ``self.year`` at ``hour`` o'clock."""
        return timezone.make_aware(datetime(self.year, month, day, hour, 0))

    def _event(self, calendar, title, kind, when, *, end=None, programme=None, offering=None,
               description='', location='', provisional=True):
        """Create the event if this calendar has not got it yet; refresh its date
        if it has. Identity is (calendar, kind, title, programme, offering)."""
        event, created = CalendarEvent.objects.get_or_create(
            calendar=calendar, kind=kind, title=title, programme=programme,
            programme_module=offering, defaults={'start': when})
        self._apply(event, {
            'start': when, 'end': end, 'all_day': True, 'location': location,
            'is_published': self.publish,
            'description': description + (PROVISIONAL if provisional else ''),
        }, created)
        return created

    def seed_calendars(self, institutions):
        """Fill each institution's calendar for ``self.year``; returns events created.

        * Institution-wide dates. SAICA's are the APC's own year, so they are scoped
          to the APC programme: SAICA also hosts the IAC, whose candidates must not
          see "Case simulation 1" on their calendar.
        * Public holidays — the no-session calendar (when ``holidays``).
        * Per-module assessment dates, examined programmes only, staggered a day apart
          per module so nobody sits four papers at once — each with the coaching
          lead-in the week before it (mocks are for the exam, not every test).
        """
        total = 0
        for institution in institutions.values():
            calendar, _ = AcademicCalendar.objects.get_or_create(
                institution=institution, year=self.year,
                defaults={'description': f'The {self.year} academic year at {institution.name}: '
                                         'assessment dates, deadlines, coaching sessions and the '
                                         'no-session calendar.'})
            made = 0

            owner = None
            if institution.code == 'SAICA':
                owner = institution.programmes.filter(code='APC').first()
                if owner is not None:
                    calendar.events.filter(programme__isnull=True,
                                           title__in=[t for t, *_ in APC_DATES]).update(programme=owner)
            for title, kind, (month, day), span, note in (
                    APC_DATES if institution.code == 'SAICA' else INSTITUTION_DATES):
                start = self._at(month, day)
                end = start + timedelta(days=span - 1) if span > 1 else None
                made += self._event(calendar, title, kind, start, end=end, programme=owner,
                                    description=note)

            if self.holidays:
                for when, name in public_holidays(self.year):
                    made += self._event(
                        calendar, name, CalendarEvent.KIND_HOLIDAY, self._at(when.month, when.day),
                        description='South African public holiday — no scheduled sessions.',
                        provisional=False)

            for programme in institution.programmes.all():
                if programme.level not in (Programme.LEVEL_CTA, Programme.LEVEL_BRIDGING):
                    continue
                for index, offering in enumerate(programme.modules.without_general().order_by('order', 'id')):
                    for title, kind, (month, day), span, note in ASSESSMENT_CYCLE:
                        when = self._at(month, day) + timedelta(days=index)
                        made += self._event(calendar, title, kind, when, programme=programme,
                                            offering=offering, description=note)
                        for lead_title, lead_kind, days_before, lead_note in COACHING_LEAD_IN:
                            if (kind == CalendarEvent.KIND_TEST
                                    and lead_kind == CalendarEvent.KIND_MOCK):
                                continue
                            made += self._event(
                                calendar, f'{lead_title} · before {title}', lead_kind,
                                when - timedelta(days=days_before), programme=programme,
                                offering=offering, description=lead_note)
            total += made
            self.log(f'  {institution.code:8} {calendar.events.count():>3} event(s) on the '
                     f'{self.year} calendar ({made} new)')
        return total

    def run(self):
        """Seed everything and return a summary dict of what was built."""
        institutions = self.seed_institutions()
        modules = self.seed_modules()
        programmes, offerings = self.seed_programmes(institutions, modules)
        events = self.seed_calendars(institutions) if self.calendar else 0
        return {
            'institutions': institutions,
            'modules': modules,
            'programmes': programmes,
            'offerings': offerings,
            'events_created': events,
            'events_total': (CalendarEvent.objects.filter(calendar__year=self.year).count()
                             if self.calendar else 0),
            'year': self.year,
            'price': self.price,
            'published': self.publish,
        }


def seed(**kwargs):
    """Build the spine. Keyword arguments are :class:`SpineSeeder`'s."""
    return SpineSeeder(**kwargs).run()


def seed_cohort(programme, spec, *, update=False):
    """Create or fill one cohort from ``(code, name[, start, end])``.

    Dates are ISO strings or ``None``. Like the rest of the spine, an existing
    cohort only has its blanks filled unless ``update=True``.
    """
    code, name, *dates = spec
    start, end = (list(dates) + [None, None])[:2]
    cohort, created = Cohort.objects.get_or_create(programme=programme, code=code,
                                                   defaults={'name': name})
    fields = {'name': name,
              'start_date': date.fromisoformat(start) if start else None,
              'end_date': date.fromisoformat(end) if end else None}
    changed = []
    for field, value in fields.items():
        if value is None:
            continue
        current = getattr(cohort, field)
        if (created or update or current in ('', None)) and current != value:
            setattr(cohort, field, value)
            changed.append(field)
    if changed:
        cohort.save()
    return cohort, created, changed


EXTRA_COHORTS = [
    ('METANOIA', 'SAMPLE', ('PACK2027', 'Sample pack intake 2027', '2027-02-06', '2027-03-02')),
]


PUB, EST, CON = 'published', 'estimate', 'conflict'
_T, _X, _D, _R, _O, _H, _S, _OTH = (
    CalendarEvent.KIND_TEST, CalendarEvent.KIND_EXAM, CalendarEvent.KIND_DEADLINE,
    CalendarEvent.KIND_RESULT, CalendarEvent.KIND_ORIENTATION, CalendarEvent.KIND_HOLIDAY,
    CalendarEvent.KIND_SUPPLEMENTARY, CalendarEvent.KIND_OTHER)

COHORT_CALENDAR = [
    ('METANOIA', 'SAMPLE', 'SAMPLE2', None, _D, 'Registration closes', '2026-06-26', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE2', 'INTRO', _X, 'Final examination', '2027-04-07', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE2', 'CORE', _X, 'Final examination', '2027-04-08', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE2', 'PRACT', _X, 'Final examination', '2027-04-09', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE2', None, _R, 'Results released', '2027-05-28', None, EST,
     'Sample estimate.'),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', None, _D, 'Registration closes', '2027-01-29', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', None, _O, 'Start and orientation', '2027-02-01', '2027-02-05', PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'INTRO', _T, 'Test 1', '2027-03-17', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'CORE', _T, 'Test 1', '2027-03-18', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'PRACT', _T, 'Test 1', '2027-03-19', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', None, _H, 'Mid-year recess', '2027-06-28', '2027-07-09', PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'INTRO', _X, 'Final examination', '2027-10-25', None, PUB, ''),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'CORE', _X, 'Final examination', '2027-10-26', None, CON,
     'Sample conflict: one source gives 26 October, another 28 October.'),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', 'PRACT', _X, 'Final examination', '2027-10-27', None, EST,
     'Sample estimate.'),
    ('METANOIA', 'SAMPLE', 'SAMPLE1', None, _R, 'Results released', '2027-12-03', None, EST,
     'Sample estimate.'),
]

COHORT_CALENDAR_SOURCE = 'Sample cohort calendar shipped with the platform'
_STATUS_NOTE = {
    PUB: 'Published by the institution.',
    EST: 'Estimate from the previous year\'s calendar, not yet published by the institution. '
         'Staff only until confirmed; re-check it before relying on it.',
    CON: 'The institution\'s own sources disagree. Staff only until confirmed.',
}


def _cohort_calendar_description(status, note):
    """An event description: the note, the status explanation and the source line."""
    parts = [note] if note else []
    parts.append(_STATUS_NOTE[status])
    parts.append(f'Source: {COHORT_CALENDAR_SOURCE}.')
    return '\n\n'.join(parts)


def seed_cohort_calendar(*, update=False, out=print):
    """Install :data:`COHORT_CALENDAR` (and the cohorts it needs). Idempotent.

    An event's identity is (calendar, kind, title, programme, offering, cohort).
    A seeded *template* row (the brand-pack grid, marked provisional) for the
    same date with no cohort is adopted and corrected rather than duplicated —
    e.g. the APC sitting the template put on 19 November. On re-runs, rows this
    seeder wrote are refreshed; anything an admin has edited (no source line in
    its description) only has blanks filled unless ``update=True``.

    Cohorts are seeded first — the spine's own (so this runs without a full
    re-seed) and :data:`EXTRA_COHORTS`. A newly created 2027 calendar also gets
    that year's public holidays.

    Returns counts: ``{'cohorts', 'calendars', 'created', 'updated', 'unchanged'}``.
    """
    institutions = {i.code: i for i in Institution.objects.all()}
    programmes = {(p.institution.code, p.code): p
                  for p in Programme.objects.select_related('institution')}
    stats = {'cohorts': 0, 'calendars': 0, 'created': 0, 'updated': 0, 'unchanged': 0}

    cohort_specs = [(spec['institution'], spec['code'], c)
                    for spec in PROGRAMMES for c in spec.get('cohorts', [])]
    for inst_code, prog_code, cspec in cohort_specs + EXTRA_COHORTS:
        programme = programmes.get((inst_code, prog_code))
        if programme is None:
            out(f'  skip cohort {cspec[0]}: no programme {inst_code} {prog_code}')
            continue
        cohort, created, changed = seed_cohort(programme, cspec, update=update)
        if created or changed:
            stats['cohorts'] += 1
            out(f'  cohort {programme.full_code} · {cohort.code}: '
                f'{"created" if created else "filled " + ", ".join(changed)}')

    calendars = {}

    def calendar_for(institution, year):
        """The institution's calendar for ``year``, created (with 2027 holidays) on first use."""
        key = (institution.pk, year)
        if key not in calendars:
            calendar, created = AcademicCalendar.objects.get_or_create(
                institution=institution, year=year,
                defaults={'description': f'The {year} academic year at {institution.name}.'})
            if created:
                stats['calendars'] += 1
                out(f'  calendar {institution.code} {year}: created')
                if year in (2027,):
                    for when, name in public_holidays(year):
                        CalendarEvent.objects.get_or_create(
                            calendar=calendar, kind=CalendarEvent.KIND_HOLIDAY, title=name,
                            programme=None, programme_module=None, cohort=None,
                            defaults={'start': timezone.make_aware(datetime(when.year, when.month,
                                                                            when.day, 9)),
                                      'all_day': True, 'is_published': True,
                                      'description': 'South African public holiday — '
                                                     'no scheduled sessions.'})
            calendars[key] = calendar
        return calendars[key]

    for (inst_code, prog_code, cohort_code, module_code, kind, title, start, end,
         status, note) in COHORT_CALENDAR:
        institution = institutions.get(inst_code)
        programme = programmes.get((inst_code, prog_code)) if prog_code else None
        if institution is None or (prog_code and programme is None):
            out(f'  skip {inst_code} {prog_code} {title}: not on this system')
            continue
        cohort = programme.cohorts.filter(code=cohort_code).first() if cohort_code else None
        offering = programme.modules.filter(code=module_code).first() if module_code else None
        if (cohort_code and cohort is None) or (module_code and offering is None):
            out(f'  skip {inst_code} {prog_code} {cohort_code} {module_code} {title}: '
                'cohort or module not on this system')
            continue
        when = timezone.make_aware(datetime.combine(date.fromisoformat(start), datetime.min.time())
                                   .replace(hour=9))
        until = (timezone.make_aware(datetime.combine(date.fromisoformat(end), datetime.min.time())
                                     .replace(hour=9)) if end else None)
        calendar = calendar_for(institution, when.year)
        fields = {'start': when, 'end': until, 'all_day': True,
                  'is_published': status == PUB,
                  'description': _cohort_calendar_description(status, note)}

        key = dict(calendar=calendar, kind=kind, title=title, programme=programme,
                   programme_module=offering)
        event = CalendarEvent.objects.filter(**key, cohort=cohort).first()
        if event is None and cohort is not None:
            event = CalendarEvent.objects.filter(**key, cohort__isnull=True,
                                                 description__contains=PROVISIONAL.strip()[:40]).first()
        label = (f'{inst_code} {prog_code or "-"} {cohort_code or "-"} {module_code or "-"} · '
                 f'{start}{" – " + end if end else ""} · {title} [{status}]')
        if event is None:
            CalendarEvent.objects.create(**key, cohort=cohort, **fields)
            stats['created'] += 1
            out(f'  + {label}')
            continue

        ours = COHORT_CALENDAR_SOURCE in (event.description or '')
        template = PROVISIONAL.strip()[:40] in (event.description or '')
        overwrite = update or ours or template
        changed = []
        if event.cohort_id != (cohort.pk if cohort else None):
            event.cohort = cohort
            changed.append('cohort')
        for name, value in fields.items():
            current = getattr(event, name)
            if current != value and (overwrite or current in ('', None)):
                setattr(event, name, value)
                changed.append(name)
        if changed:
            event.save()
            stats['updated'] += 1
            out(f'  ~ {label}  ({", ".join(changed)}{"; template corrected" if template else ""})')
        else:
            stats['unchanged'] += 1
    return stats
