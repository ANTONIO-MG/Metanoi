"""The schedule sheet: what a pack's folders cannot say, in one table.

Folders give the modules, units, weeks and files. The rest — when each module
meets, the extra sessions, the calendar's dates, the unit titles, which modules
sit which mock paper, who hosts — lives in the timetable and the candidate
guide. Written by hand, that was ``core/academic_spine_sample.py``. The sheet
is the same information as a table anyone can fill in Excel, saved in the pack
as ``0_Start_Here/schedule.csv`` (or ``schedule.xlsx``). It is staff-only: it is
read, never published.

The AI step (:func:`core.programme_pack.ai.draft_sheet`) writes the same sheet
from the timetable, for a human to check before anything is built — so the
manual and the AI route feed one builder and look exactly alike.

Columns (header row required, any order, case-insensitive)::

    type, module, number, date, end, time, minutes, kind, title, details

One row per thing. ``type`` is one of:

    phase       the block this pack is: kind=test|exam, number=1.., title, date, end, details=summary
    slot        a module's weekly live session: module, date=weekday (Sat), time, minutes
    session     a one-off session: date, time, minutes, module (blank = whole cohort),
                kind=class|revision|test_review|workshop|briefing, title, details
    date        a calendar date: date, end, time (optional), kind=exam|test|mock|deadline|holiday|
                orientation|result|session|intensive|other, module (optional), title, details
    unit        a unit: module, number, title, details=its self-study item
    week        a week's display title: kind=block|mock, number, title, details=summary;
                kind=extra adds a week no folder has (a final Q&A): number=week, date, end
    mock_paper  number=paper, module=MACF+CGAU (the modules that sit it)
    document    a friendly title for a file: details=file name, title
    host        details=the e-mail address of the session host

A sheet that lists ``date`` rows of a kind (session, mock, orientation, exam)
replaces the automatic dates of that kind, so the calendar shows exactly what
the sheet says.

``REPLACES`` lists the automatic dates a sheet replaces when it lists its own
of the same kind (kind → spine ref prefix).
"""
import csv
import datetime as dt
import io
import os
import re

from django.utils.text import slugify

COLUMNS = ['type', 'module', 'number', 'date', 'end', 'time', 'minutes', 'kind', 'title', 'details']
TYPES = ('phase', 'slot', 'session', 'date', 'unit', 'week', 'mock_paper', 'document', 'host')
SHEET_RX = re.compile(r'^(?:\d+[_ .-]+)?(?:programme[_ .-]+)?schedule(?:[_ .-]+sheet)?\.(csv|xlsx)$', re.I)

WEEKDAYS = {'mon': 'monday', 'tue': 'tuesday', 'wed': 'wednesday', 'thu': 'thursday', 'fri': 'friday',
            'sat': 'saturday', 'sun': 'sunday'}
DATE_KINDS = ('exam', 'test', 'mock', 'deadline', 'holiday', 'orientation', 'result', 'session',
              'intensive', 'other', 'supp', 'assignment')
SESSION_KINDS = ('class', 'revision', 'test_review', 'workshop', 'briefing')
REPLACES = {'session': 'block-', 'mock': 'mock-', 'orientation': 'programme-start', 'exam': 'exam-'}

HEADER_HELP = ('type,module,number,date,end,time,minutes,kind,title,details\n'
               'phase,,1,2027-02-06,2027-03-02,,,exam,Sample Programme,\n'
               'slot,INTRO,,Sat,,08:00,90,,,\n'
               'session,,,2027-02-27,,10:00,90,test_review,Mini mock 1 debrief,\n'
               'date,,,2027-03-02,,,,exam,Sample examination,\n'
               'unit,INTRO,1,,,,,,Welcome and orientation,Read the Start here documents\n'
               'week,,3,,,,,mock,Mini mock 1 · 20/21 Feb,\n'
               'mock_paper,INTRO+CORE,1,,,,,,,\n'
               'host,,,,,,,,,coach@example.com\n')


def find(root):
    """The sheet's path under ``root`` (the unpacked pack), or ``None``.

    When there are several, the one in a Start-here folder wins, then the
    shallowest.
    """
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__MACOSX'))]
        for name in files:
            if SHEET_RX.match(name):
                found.append(os.path.join(folder, name))
    found.sort(key=lambda p: (0 if 'start' in p.lower() else 1, p.count(os.sep), p))
    return found[0] if found else None


def read_file(path):
    """``(rows, problems)`` from a .csv or .xlsx sheet."""
    if path.lower().endswith('.xlsx'):
        try:
            from openpyxl import load_workbook
        except ImportError:                              # pragma: no cover
            return [], ['Excel sheets need openpyxl on the server; save it as .csv instead.']
        book = load_workbook(path, read_only=True, data_only=True)
        sheet = book.worksheets[0]
        table = [['' if v is None else v for v in row] for row in sheet.iter_rows(values_only=True)]
        book.close()
        return parse_table(table)
    with open(path, encoding='utf-8-sig') as fh:
        return parse_text(fh.read())


def parse_text(text):
    """``(rows, problems)`` from CSV text."""
    return parse_table(list(csv.reader(io.StringIO(text or ''))))


def _cell(value):
    """Normalise one spreadsheet value to the string form the sheet uses (ISO dates, ``HH:MM``)."""
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat(timespec='minutes')
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime('%H:%M')
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_table(table):
    """``(rows, problems)`` from a list of rows (the first is the header).

    Each row comes back as a dict of :data:`COLUMNS` plus ``line``; problems are
    ``'Line n: …'`` strings. A row with a problem is left out.

    Rows whose type starts with ``#`` (a comment) or is empty are skipped.
    """
    table = [[_cell(c) for c in row] for row in table if any(_cell(c) for c in row)]
    if not table:
        return [], []
    header = [h.strip().lower() for h in table[0]]
    if 'type' not in header:
        return [], ['The first row must be the header: ' + ', '.join(COLUMNS) + '.']
    rows, problems = [], []
    for line, raw in enumerate(table[1:], 2):
        row = {c: '' for c in COLUMNS}
        for index, name in enumerate(header):
            if name in row and index < len(raw):
                row[name] = raw[index]
        row['type'] = row['type'].lower().replace(' ', '_')
        if row['type'].startswith('#') or not row['type']:
            continue
        problem = _check(row)
        if problem:
            problems.append(f'Line {line}: {problem}')
            continue
        row['line'] = line
        rows.append(row)
    return rows, problems


def _date(value):
    """The ISO date at the start of ``value``, or ``None``."""
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _time(value):
    """True when ``value`` is a ``HH:MM`` time."""
    return bool(re.match(r'^([01]?\d|2[0-3]):[0-5]\d$', str(value or '')))


def _check(row):
    """The problem with ``row`` as a message, or ``''`` when it is valid."""
    kind = row['type']
    if kind not in TYPES:
        return f'unknown type "{kind}" (use one of: {", ".join(TYPES)})'
    if kind == 'slot':
        if not row['module']:
            return 'a slot needs its module'
        if row['date'][:3].lower() not in WEEKDAYS:
            return 'a slot\'s date column is the weekday, e.g. Sat'
        if not _time(row['time']):
            return 'a slot needs a time like 08:00'
    if kind == 'week' and ((row['date'] and not _date(row['date'])) or (row['end'] and not _date(row['end']))):
        return 'a week\'s date and end must be dates like 2026-11-07'
    if kind in ('session', 'date', 'phase'):
        if not _date(row['date']):
            return f'"{row["date"]}" is not a date (use 2027-01-26)'
        if row['end'] and not _date(row['end']):
            return f'"{row["end"]}" is not a date'
    if kind == 'session':
        if not _time(row['time']):
            return 'a session needs a time like 10:00'
        if row['kind'] and row['kind'].lower() not in SESSION_KINDS:
            return 'a session\'s kind is one of: ' + ', '.join(SESSION_KINDS)
        if not row['title']:
            return 'a session needs a title'
    if kind == 'date':
        if row['kind'].lower() not in DATE_KINDS:
            return f'a date\'s kind is one of: {", ".join(DATE_KINDS)}'
        if not row['title']:
            return 'a date needs a title'
        if row['time'] and not _time(row['time']):
            return 'the time must look like 09:00'
    if kind == 'phase' and row['kind'].lower() not in ('test', 'exam'):
        return 'a phase\'s kind is test or exam'
    if kind in ('unit', 'week', 'mock_paper', 'phase') and not str(row['number']).isdigit():
        return f'a {kind} needs its number'
    if kind == 'unit' and not (row['module'] and row['title']):
        return 'a unit needs its module and title'
    if kind == 'week' and row['kind'].lower() not in ('block', 'mock', 'extra'):
        return 'a week\'s kind is block, mock or extra'
    if kind == 'week' and row['kind'].lower() == 'extra' and not (_date(row['date']) and row['title']):
        return 'an extra week needs its date and title'
    if kind == 'mock_paper' and not row['module']:
        return 'a mock paper lists its modules, e.g. MACF+CGAU'
    if kind in ('document', 'host') and not row['details']:
        return f'a {kind} row needs the details column'
    if row['minutes'] and not str(row['minutes']).isdigit():
        return 'minutes must be a whole number'
    return ''


def to_csv(rows):
    """``rows`` as CSV text with the :data:`COLUMNS` header."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row.get(c, '') for c in COLUMNS])
    return out.getvalue()


def slots(rows):
    """``{module: (weekday, 'HH:MM', minutes)}`` — what ``spine_from_plan`` takes."""
    out = {}
    for row in rows:
        if row['type'] == 'slot':
            out[row['module'].upper()] = (WEEKDAYS[row['date'][:3].lower()], row['time'].zfill(5),
                                          int(row['minutes'] or 120))
    return out


def phase(rows):
    """The sheet's ``phase`` row as spine fields, or ``{}``."""
    row = next((r for r in rows if r['type'] == 'phase'), None)
    if row is None:
        return {}
    out = {'kind': row['kind'].lower(), 'sequence': int(row['number']),
           'title': row['title'] or f'{row["kind"].title()} {row["number"]}'}
    if row['date']:
        out['starts'] = row['date'][:10]
    if row['end']:
        out['ends'] = row['end'][:10]
    if row['details']:
        out['summary'] = row['details']
    return out


def _ref(prefix, *parts):
    """A spine ref: ``prefix`` plus a slug of the non-empty ``parts``."""
    return f'{prefix}-' + slugify('-'.join(str(p) for p in parts if p))[:60]


def _week_at(spine, day):
    """The number of the spine week whose dates contain ``day``, or ``None``."""
    for spec in spine.get('weeks', []):
        if spec.get('starts') and spec.get('ends') and spec['starts'] <= day <= spec['ends']:
            return spec['number']
    return None


def overlay(spine, rows, plan=None):
    """Write the sheet's rows onto ``spine`` (from ``spine_from_plan``), in place.

    A class session is titled after its unit: the sheet's unit title wins over
    the folder's.
    """
    from .scanner import twin_key

    modules = set((plan or {}).get('modules') or [m['code'] for m in spine.get('modules', [])])
    by_type = {t: [r for r in rows if r['type'] == t] for t in TYPES}

    found = phase(rows)
    if found:
        spine['phase'] = dict(spine.get('phase') or {}, **found)

    for row in by_type['host']:
        spine['host_email'] = row['details'].strip()

    units = spine.setdefault('units', {})
    for row in by_type['unit']:
        code = row['module'].upper()
        units.setdefault(code, {})[int(row['number'])] = {'title': row['title'],
                                                          'self_study': row['details']}

    for row in by_type['week']:
        key, number = row['kind'].lower(), int(row['number'])
        if key == 'extra':
            weeks = spine.setdefault('weeks', [])
            spec = next((w for w in weeks if w['number'] == number), None)
            if spec is None:
                spec = {'number': number}
                weeks.append(spec)
                weeks.sort(key=lambda w: w['number'])
            spec.update({'title': row['title'], 'starts': row['date'][:10],
                         'ends': (row['end'] or row['date'])[:10]})
            if row['details']:
                spec['summary'] = row['details']
            continue
        spec = next((w for w in spine.get('weeks', []) if w.get(key) == number), None)
        if spec is None:
            continue
        if row['title']:
            spec['title'] = row['title']
        if _date(row['date']):
            spec['starts'] = row['date'][:10]
        if _date(row['end']):
            spec['ends'] = row['end'][:10]
        if row['details']:
            spec['summary'] = row['details']

    for s_ in spine.get('sessions', []):
        match = re.match(r'^live-([A-Za-z]+)-(\d+)$', str(s_.get('ref', '')))
        unit = units.get(match.group(1), {}).get(int(match.group(2))) if match else None
        if unit:
            s_['title'] = f'{match.group(1)} live session · Unit {match.group(2)}: {unit["title"]}'

    papers = {}
    for row in by_type['mock_paper']:
        papers[f'PAPER_{int(row["number"])}'] = [c.strip().upper() for c in re.split(r'[+,/ ]+', row['module'])
                                                 if c.strip()]
    if papers:
        spine['mock_papers'] = papers

    titles = spine.setdefault('document_titles', {})
    for row in by_type['document']:
        titles[twin_key(row['details'])] = row['title']

    sessions = spine.setdefault('sessions', [])
    for row in by_type['session']:
        code = row['module'].upper() if row['module'] else None
        if code and code not in modules:
            code = None
        day = row['date'][:10]
        sessions.append({'ref': _ref('sheet', day, row['time'], code, row['title']), 'module': code,
                         'week': _week_at(spine, day), 'kind': (row['kind'] or 'class').lower(),
                         'start': f'{day}T{row["time"].zfill(5)}', 'minutes': int(row['minutes'] or 60),
                         'title': row['title'], 'description': row['details']})

    dates = by_type['date']
    if dates:
        listed = {r['kind'].lower() for r in dates}
        events = [e for e in spine.get('events', [])
                  if not any(kind in listed and e['ref'].startswith(prefix) for kind, prefix in REPLACES.items())]
        exam_ref = spine.get('exam_event') if 'exam' not in listed else None
        for row in dates:
            kind = row['kind'].lower()
            start = row['date'][:10] + (f'T{row["time"].zfill(5)}' if row['time'] else '')
            ref = _ref('sheet', kind, row['date'][:10], row['title'])
            code = row['module'].upper() if row['module'] else None
            events.append({'ref': ref, 'kind': kind, 'start': start, 'end': row['end'][:10] or None,
                           'title': row['title'], 'description': row['details'],
                           'module': code if code in modules else None})
            if kind == 'exam' and exam_ref is None:
                exam_ref = ref
        spine['events'] = events
        spine['exam_event'] = exam_ref

    slot_rows = slots(rows)
    if slot_rows:
        spine['slots'] = {code: {'label': f'Live session {day.title()} {time}'}
                          for code, (day, time, _m) in slot_rows.items()}
    return spine


def from_spine(spine):
    """A spine (hand-written, like ``academic_spine_sample``) written out as sheet rows.

    The weekly slot of each module is read off its first class session; every
    other session, every date, the unit titles, week titles, mock papers,
    document titles and the host become rows. Folders + this sheet rebuild the
    same programme through :func:`core.programme_pack.schedule.spine_from_plan`.

    Live class sessions of a module that has a slot are not written out: the
    slot row recreates them.
    """
    rows = []

    def add(**cells):
        """Append one sheet row built from ``cells``."""
        rows.append({c: str(cells.get(c, '') if cells.get(c) is not None else '') for c in COLUMNS})

    ph = spine.get('phase') or {}
    if ph:
        add(type='phase', number=ph.get('sequence', 1), date=ph.get('starts'), end=ph.get('ends'),
            kind=ph.get('kind', 'exam'), title=ph.get('title'), details=ph.get('summary'))
    if spine.get('host_email'):
        add(type='host', details=spine['host_email'])

    slot_of = {}
    for s in spine.get('sessions', []):
        if s.get('kind', 'class') == 'class' and s.get('module') and s['module'] not in slot_of \
                and str(s.get('ref', '')).startswith('live-'):
            when = dt.datetime.fromisoformat(s['start'])
            slot_of[s['module']] = (when.strftime('%a'), when.strftime('%H:%M'), s.get('minutes', 120))
    for code, (day, time, minutes) in slot_of.items():
        add(type='slot', module=code, date=day, time=time, minutes=minutes)
    for s in spine.get('sessions', []):
        if str(s.get('ref', '')).startswith('live-') and s.get('module') in slot_of:
            continue
        when = dt.datetime.fromisoformat(s['start'])
        add(type='session', module=s.get('module') or '', date=when.date().isoformat(),
            time=when.strftime('%H:%M'), minutes=s.get('minutes', 60), kind=s.get('kind', 'class'),
            title=s.get('title'), details=s.get('description'))
    for e in spine.get('events', []):
        start = str(e['start'])
        add(type='date', module=e.get('module') or '', date=start[:10], end=(e.get('end') or '')[:10],
            time=start[11:16] if 'T' in start else '', kind=e.get('kind', 'other'), title=e.get('title'),
            details=e.get('description'))
    for code, units in (spine.get('units') or {}).items():
        for number, unit in sorted(units.items(), key=lambda kv: int(kv[0])):
            add(type='unit', module=code, number=number, title=unit.get('title'), details=unit.get('self_study'))
    for w in spine.get('weeks', []):
        kind = 'mock' if w.get('mock') else 'block' if w.get('block') else None
        if kind:
            add(type='week', number=w.get(kind), kind=kind, title=w.get('title'), details=w.get('summary'),
                date=w.get('starts'), end=w.get('ends'))
        else:
            add(type='week', number=w['number'], kind='extra', date=w.get('starts'), end=w.get('ends'),
                title=w.get('title'), details=w.get('summary'))
    for paper, codes in (spine.get('mock_papers') or {}).items():
        add(type='mock_paper', number=re.sub(r'\D', '', paper) or 1, module='+'.join(codes))
    return rows
