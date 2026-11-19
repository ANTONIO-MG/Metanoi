"""
core/academic_spine_sample.py  —  the Sample Programme pack, written down.

The companion of ``core/academic_spine.py`` for one programme pack: a small,
neutral sample that shows how a hand-written spine and a pack folder become a
cohort with modules, dated weeks, lessons, files, live sessions and a calendar.
None of it is real teaching material and every date is an example; replace it
with a real programme, or write a new spine the same way (the zip importer
writes this shape for any pack, see ``core/programme_pack``).

Source: the pack in ``fixtures/sample_programme/`` (a student pack and a tutor
pack: Start here, two teaching weekends with one folder per module unit, a
programme overview and a mini mock).

Built by ``manage.py seed_sample_programme`` (idempotent): Metanoia Group →
Sample Programme → cohort PACK2027, the three sample modules (INTRO, CORE,
PRACT, priced at :data:`core.academic_spine.DEFAULT_PRICE` a month), four dated
weeks per module, unit lessons and pack files (tutor pack staff only), the
General module, seven live sessions (Teams sessions with no link yet: no call is
made to Microsoft) and the academic calendar.

Constants:

* ``KEY`` — the prefix of every seeded row's key; ``PACK`` — the pack folder,
  relative to ``BASE_DIR``; ``YEAR`` — the year of the first teaching weekend.
* ``FEE`` — the monthly module fee written on new offerings. There is no
  bundle: the builder's bundle is for exactly four modules.
* ``MODULES`` — the three modules, keyed to the canonical catalogue in
  ``core.academic_spine.MODULES``.
* ``UNITS`` — per module, unit number → title and the self-study note that goes
  with it.
* ``LIVE_SLOTS`` — module → (weekday offset from the Saturday, start, minutes),
  South African time. ``SLOT_LABELS`` — how each slot is shown.
* ``WEEKENDS`` — teaching weekends: (weekend = unit number, the Saturday, the
  week it sits in).
* ``SPINE`` — what :func:`core.programme_pack.seed_spine` builds. Sessions are on
  Microsoft Teams (``provider``) with links added later; ``host_email`` is the
  administrator mailbox, falling back to the first superuser when that account
  does not exist on the server. ``document_titles`` gives lesson titles for the
  Start here documents, keyed by squashed file name.
"""
import datetime as dt

from apps.learning.models import DEPTH_FOUNDATIONAL, Programme

KEY = 'sample'
PACK = 'fixtures/sample_programme'
YEAR = 2027

FEE = '500.00'

MODULES = [
    {'code': 'INTRO', 'catalogue': 'INTRO', 'name': 'Introduction', 'order': 1, 'depth': DEPTH_FOUNDATIONAL,
     'description': 'Sample module: welcome, orientation and planning your studies.'},
    {'code': 'CORE', 'catalogue': 'CORE', 'name': 'Core concepts', 'order': 2, 'depth': DEPTH_FOUNDATIONAL,
     'description': 'Sample module: the key terms of the programme and how they fit together.'},
    {'code': 'PRACT', 'catalogue': 'PRACT', 'name': 'Practice and assessment', 'order': 3,
     'depth': DEPTH_FOUNDATIONAL,
     'description': 'Sample module: a worked example, practice questions and a mini mock.'},
]

UNITS = {
    'INTRO': {
        1: {'title': 'Welcome and orientation', 'self_study': 'Read "How to use this pack"'},
        2: {'title': 'Planning your studies', 'self_study': 'Draw up a weekly study plan'},
    },
    'CORE': {
        1: {'title': 'Key terms', 'self_study': 'A glossary of the key terms'},
        2: {'title': 'Putting it together', 'self_study': 'A one-page summary of the module'},
    },
    'PRACT': {
        1: {'title': 'A worked example', 'self_study': 'Rework the example on your own'},
        2: {'title': 'Practice questions', 'self_study': 'Mark yourself against the workbook'},
    },
}

LIVE_SLOTS = {'INTRO': (0, '08:00', 90), 'CORE': (0, '10:00', 90), 'PRACT': (1, '09:00', 90)}
SLOT_LABELS = {'INTRO': 'Live session Saturday 08:00', 'CORE': 'Live session Saturday 10:00',
               'PRACT': 'Live session Sunday 09:00'}

WEEKENDS = [(1, '2027-02-06', 1), (2, '2027-02-13', 2)]


def _weeks():
    """The four dated programme weeks: two teaching weekends, the mini mock and the examination week."""
    return [
        {'number': 1, 'block': 1, 'unit': 1, 'title': 'Weekend 1 · 6/7 Feb', 'starts': '2027-02-06',
         'ends': '2027-02-12'},
        {'number': 2, 'block': 2, 'unit': 2, 'title': 'Weekend 2 · 13/14 Feb', 'starts': '2027-02-13',
         'ends': '2027-02-19'},
        {'number': 3, 'mock': 1, 'title': 'Mini mock 1 · 20/21 Feb', 'starts': '2027-02-20', 'ends': '2027-02-26',
         'summary': 'Mini mock 1: one short paper per module on units 1 and 2. No classes this weekend.'},
        {'number': 4, 'title': 'Debrief and examination · 27 Feb – 2 Mar', 'starts': '2027-02-27',
         'ends': '2027-03-02',
         'summary': 'The mini mock debrief on Saturday 27 February, then the sample examination on '
                    'Tuesday 2 March.'},
    ]


def _at(saturday, offset, time):
    """ISO local datetime ``offset`` days after ``saturday`` at ``time``."""
    day = dt.date.fromisoformat(saturday) + dt.timedelta(days=offset)
    return f'{day.isoformat()}T{time}'


def _sessions():
    """Every live session of the programme: one per module per weekend, and the mock debrief."""
    sessions = []
    for unit, saturday, week in WEEKENDS:
        for code, (offset, time, minutes) in LIVE_SLOTS.items():
            title = UNITS[code][unit]['title']
            sessions.append({
                'ref': f'live-{code}-{unit}', 'module': code, 'week': week, 'kind': 'class',
                'start': _at(saturday, offset, time), 'minutes': minutes,
                'title': f'{code} live session · Unit {unit}: {title}',
                'description': 'Sample live session. Read the topic guide (file 4) before it and open the '
                               'handout (file 2) in the session.',
            })
    sessions.append({
        'ref': 'mock1-debrief', 'module': None, 'kind': 'test_review', 'start': '2027-02-27T10:00',
        'minutes': 90, 'title': 'Mini mock 1 debrief (all modules)',
        'description': 'Bring your marked paper. Solutions were released when the paper closed.',
    })
    return sessions


def _events():
    """The academic calendar events: the start, the teaching weekends, the mini mock and the examination."""
    events = [
        {'ref': 'programme-start', 'kind': 'orientation', 'start': '2027-02-06', 'cohort': True,
         'title': 'Programme starts: orientation and unit 1',
         'description': 'Read the Start here documents first.'},
    ]
    for unit, saturday, _week in WEEKENDS:
        sunday = (dt.date.fromisoformat(saturday) + dt.timedelta(days=1)).isoformat()
        events.append({
            'ref': f'weekend-{unit}', 'kind': 'session', 'start': saturday, 'end': sunday,
            'title': f'Teaching weekend {unit} · Unit {unit}',
            'description': 'Saturday: INTRO 8am, CORE 10am. Sunday: PRACT 9am.',
        })
    for code, start, end in (('INTRO', '2027-02-20T09:00', '2027-02-20T10:30'),
                             ('CORE', '2027-02-20T11:00', '2027-02-20T12:30'),
                             ('PRACT', '2027-02-21T09:00', '2027-02-21T10:30')):
        events.append({'ref': f'mm1-{code}', 'kind': 'mock', 'module': code, 'start': start, 'end': end,
                       'title': 'Mini mock 1 (sample paper)',
                       'description': 'Units 1 and 2, under exam conditions. Solutions are released straight '
                                      'after the paper.'})
    events.append({'ref': 'exam-1', 'kind': 'exam', 'start': '2027-03-02T09:00', 'end': '2027-03-02T12:00',
                   'title': 'Sample examination',
                   'description': 'The sample examination for all three modules. The date is an example.'})
    return events


def _p(text):
    """Wrap ``text`` in a paragraph tag."""
    return f'<p>{text}</p>'


MOCK_BRIEFINGS = [
    {'mock': 1, 'title': 'Mini mock 1: how it runs', 'subtitle': 'Saturday 20 and Sunday 21 February 2027',
     'sections': [
         ('When', _p('Saturday 20 February: INTRO 9am, CORE 11am. Sunday 21 February: PRACT 9am. '
                     'No classes this weekend.')),
         ('What it covers', _p('One short paper per module on units 1 and 2.')),
         ('After the paper', _p('The marking sheet is released when the paper closes. Mark yourself, then '
                                'bring the paper to the debrief on Saturday 27 February.')),
     ]},
]


SPINE = {
    'key': KEY,
    'pack': PACK,
    'year': YEAR,
    'timezone': 'Africa/Johannesburg',
    'provider': 'teams',
    'host_email': 'admin@metanoiagroup.co.za',
    'institution': {'code': 'METANOIA', 'name': 'Metanoia Group'},
    'programme': {
        'code': 'SAMPLE', 'abbreviation': 'SAMPLE', 'name': 'Sample Programme', 'slug': 'metanoia-sample',
        'order': 1, 'full_name': 'Sample Programme',
        'level': Programme.LEVEL_CTA, 'depth_default': DEPTH_FOUNDATIONAL,
        'description': 'A small sample programme that ships with the platform so a new install has a working '
                       'structure: three modules, two teaching weekends, a mini mock and an examination. '
                       'Replace it with your own programmes.',
    },
    'cohort': {'code': 'PACK2027', 'name': 'Sample pack intake 2027', 'start_date': '2027-02-06',
               'end_date': '2027-03-02'},
    'pricing': {'per_month': FEE},
    'phase': {'title': 'Sample Programme', 'kind': 'exam', 'sequence': 1,
              'starts': '2027-02-06', 'ends': '2027-03-02',
              'summary': 'Two teaching weekends, a mini mock and the examination.'},
    'modules': MODULES,
    'units': UNITS,
    'slots': {code: {'label': label} for code, label in SLOT_LABELS.items()},
    'weeks': _weeks(),
    'sessions': _sessions(),
    'events': _events(),
    'exam_event': 'exam-1',
    'mock_papers': {},
    'mock_briefings': MOCK_BRIEFINGS,
    'document_titles': {
        '0programmerundown': 'Sample Programme: programme rundown',
        '1howtousethispack': 'How to use this pack',
        '1programmeoverview': 'Sample Programme: overview',
    },
}


def seed(**kwargs):
    """Build (or top up) the programme. Keyword arguments are the builder's."""
    from core.programme_pack import seed_spine
    return seed_spine(SPINE, **kwargs)
