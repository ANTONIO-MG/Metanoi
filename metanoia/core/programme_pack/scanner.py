"""Read a programme pack's folders into a plan — no AI, no database.

A pack is laid out the way a coach hands it to candidates, and the layout *is*
the structure, so it can be read without understanding a word of it::

    <Programme>_Student_Pack/                 ← students see this
        0_Start_Here/                         ← onboarding: guide, timetable, calendar…
        1_Weekend_1_7-8_Nov/                  ← one teaching block, dated
            1_FREP_Unit_1_Group_accounting_I/ ← one module's unit in it
                FREP_U1_2_Live_session_Handout.pdf   ← step 2 of the unit
                FREP_U1_3_Live_question_Workbook.xlsx
        7_Blueprint_and_Analysis/             ← strategy documents
    <Programme>_Tutor_Pack/                   ← tutors and staff only
        (the same folders, editable, plus a facilitator pack per unit)
        7_Mocks/1_Mini_Mock_1_28-29_Nov/1_FREP/1_Mock1_FREP_Paper.docx
        8_Programme_and_Strategy/

:func:`scan` returns that as a plain, JSON-serialisable ``dict`` (the *plan*):
which files are onboarding, which belong to which module's unit in which
block, which are mocks, and which pack — student or tutor — each came from.
Nothing is guessed about content; the AI step (``ai.py``) and the spine (the
dates and session times) are layered on afterwards by the caller.

Patterns
--------
``BLOCK_RX``
    A folder that is a dated teaching block: "1_Weekend_1_7-8_Nov", "Week 3",
    "Block_2_…".
``UNIT_RX``
    A module's unit inside a block: "1_FREP_Unit_1_Group_accounting_I".
``UNIT_FILE_RX``
    A unit file: "FREP_U1_2_Live_session_Handout.pdf" — module, unit, step,
    label.
``NUMBERED_RX``
    Any numbered file or folder: "3_Programme_Calendar_(print).pdf".
``DATES_RX``
    A date range in a folder name: "7-8_Nov", "28-29 Nov", "8-10_Jan".
``STAFF_WORDS``
    File names that mark tutor material; such a file is always staff-only.
``STEP_KINDS``
    What each numbered step of a unit is, when the file name says so.
"""

import os
import re

BLOCK_RX = re.compile(r'^(?:\d+[_ .-]+)?(weekend|week|block|session|module\s*week)[_ .-]*(\d+)(?:[_ .-]+(.*))?$', re.I)
UNIT_RX = re.compile(r'^(?:\d+[_ .-]+)?([A-Za-z]{2,8})[_ .-]+(?:unit|u|topic)[_ .-]*(\d+)(?:[_ .-]+(.*))?$', re.I)
UNIT_FILE_RX = re.compile(r'^([A-Za-z]{2,8})_U(\d+)_(\d+)_(.+)$', re.I)
NUMBERED_RX = re.compile(r'^(\d+)[_ .-]+(.+)$')
DATES_RX = re.compile(r'(\d{1,2})(?:[_ ]?([A-Za-z]{3}))?[-–](\d{1,2})[_ ]?([A-Za-z]{3})')

DOC_EXTENSIONS = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt', '.md', '.txt', '.csv'}
STAFF_WORDS = re.compile(r'facilitator|tutor|marking[_ ]?(guide|sheet)|citation|programme[_ ]design|answer[_ ]?key',
                         re.I)

STEP_KINDS = [
    (re.compile(r'facilitator|tutor', re.I), 'document'),
    (re.compile(r'pre.?read|framework', re.I), 'study_guide'),
    (re.compile(r'solution|memo|answer|marking|workbook', re.I), 'answers'),
    (re.compile(r'handout', re.I), 'document'),
    (re.compile(r'question|paper|exercise', re.I), 'questions'),
    (re.compile(r'guide|self.?study|notes|summary|reading', re.I), 'study_guide'),
    (re.compile(r'blueprint|analysis|layout|strategy', re.I), 'blueprint'),
]


def humanise(name):
    """``Group_accounting_I`` → ``Group accounting I``; ``Self-study_item`` → ``Self-study item``."""
    text = re.sub(r'[_]+', ' ', name).strip()
    text = re.sub(r'\s{2,}', ' ', text)
    return text[:1].upper() + text[1:] if text else text


def _stem(filename):
    """``filename`` without its extension."""
    return os.path.splitext(filename)[0]


def twin_key(filename):
    """The name a Word original and its PDF share: ``FREP_U1_4_Topic_guide``."""
    return re.sub(r'[^a-z0-9]', '', _stem(filename).lower())


def kind_for(label, ext=''):
    """The material kind for a step ``label`` (see :data:`STEP_KINDS`); ``document`` otherwise."""
    for rx, kind in STEP_KINDS:
        if rx.search(label):
            return kind
    if ext in ('.xlsx', '.xls'):
        return 'document'
    return 'document'


def _pack_audience(name, files):
    """``students`` or ``staff`` for a top-level pack folder."""
    lower = name.lower()
    if re.search(r'tutor|facilitator|staff|teacher|lecturer|coach', lower):
        return 'staff'
    if re.search(r'student|candidate|learner', lower):
        return 'students'
    if any(STAFF_WORDS.search(f) for f in files):
        return 'staff'
    return 'students'


def parse_dates(label, *, year, after=None):
    """``"7-8_Nov"`` → ``(date(year, 11, 7), date(year, 11, 8))``.

    ``after`` is the previous block's start: a month that goes backwards
    (Nov → Jan) moves into the next year, so a pack running over New Year
    dates itself without being told.
    """
    import datetime as dt
    if not label:
        return None
    match = DATES_RX.search(label)
    if not match:
        return None
    first_day, first_mon, last_day, last_mon = match.groups()
    months = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug',
                                          'sep', 'oct', 'nov', 'dec'], 1)}
    end_month = months.get(last_mon[:3].lower())
    start_month = months.get((first_mon or last_mon)[:3].lower())
    if not (end_month and start_month):
        return None
    y = year
    if after is not None:
        y = after.year
        if start_month < after.month:
            y += 1
    try:
        start = dt.date(y, start_month, int(first_day))
        end = dt.date(y + (1 if end_month < start_month else 0), end_month, int(last_day))
    except ValueError:
        return None
    return start, end


def _file(root, path, audience, *, step=None, label=None):
    """One file's plan entry: path, step, label, kind, audience and twin key.

    A file in a student pack whose name matches :data:`STAFF_WORDS` is moved to
    the ``staff`` audience: a facilitator pack is never handed to a student.
    """
    rel = os.path.relpath(path, root)
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    stem = _stem(name)
    if label is None:
        match = UNIT_FILE_RX.match(stem)
        if match:
            step, label = int(match.group(3)), humanise(match.group(4))
        else:
            match = NUMBERED_RX.match(stem)
            if match:
                step, label = int(match.group(1)), humanise(match.group(2))
            else:
                label = humanise(stem)
    if audience == 'students' and STAFF_WORDS.search(name):
        audience = 'staff'
    return {
        'path': rel, 'name': name, 'ext': ext, 'step': step, 'label': label,
        'kind': kind_for(label, ext), 'audience': audience, 'twin': twin_key(name),
        'size': os.path.getsize(path),
    }


def _listing(path):
    """Sorted directory entries, skipping hidden, ``__MACOSX`` and Office lock files."""
    try:
        return sorted(e for e in os.listdir(path) if not e.startswith(('.', '__MACOSX', '~$')))
    except OSError:
        return []


def _numbered_order(name):
    """Sort key: the leading number of ``name`` (999 when unnumbered), then the name."""
    match = NUMBERED_RX.match(name)
    return (int(match.group(1)) if match else 999, name)


def find_packs(root):
    """The top-level pack folders under ``root`` (a zip may add one wrapper level).

    A single non-block folder is treated as a wrapper and descended into.
    """
    entries = [e for e in _listing(root) if os.path.isdir(os.path.join(root, e))]
    packs = [e for e in entries if re.search(r'pack|student|tutor|candidate|facilitator', e, re.I)]
    if packs:
        return [os.path.join(root, e) for e in packs]
    if len(entries) == 1 and not any(BLOCK_RX.match(e) for e in entries):
        return find_packs(os.path.join(root, entries[0]))
    return [root]


def scan(root, *, year=None):
    """The plan for the pack(s) under ``root``. ``year`` dates the folder labels.

    Loose documents beside the packs (a programme brief, a marketing note) are
    the coach's, not the candidates', so they are kept for staff only. Blocks are
    dated from their folder labels in order, rolling into the next year. The
    schedule sheet (:mod:`core.programme_pack.sheet`) is recorded in
    ``schedule_sheet`` and removed from the material lists: it is read, never
    placed as material.
    """
    import datetime as dt
    year = year or dt.date.today().year
    plan = {
        'version': 1, 'root': os.path.abspath(root), 'packs': [],
        'onboarding': [], 'strategy': [], 'blocks': [], 'mocks': [], 'other': [],
        'modules': [], 'notes': [],
    }
    blocks, mocks = {}, {}
    module_order = []

    packs = find_packs(root)
    for container in {os.path.dirname(p) for p in packs if p != root}:
        for name in _listing(container):
            full = os.path.join(container, name)
            if (os.path.isfile(full) and name.lower() != 'readme.md'
                    and os.path.splitext(name)[1].lower() in DOC_EXTENSIONS):
                plan['other'].append(_file(root, full, 'staff'))

    for pack_dir in packs:
        files_here = [f for _, _, fs in os.walk(pack_dir) for f in fs]
        audience = _pack_audience(os.path.basename(pack_dir), files_here)
        plan['packs'].append({'name': os.path.basename(pack_dir), 'audience': audience,
                              'files': len([f for f in files_here if not f.startswith('.')])})
        for entry in sorted(_listing(pack_dir), key=_numbered_order):
            path = os.path.join(pack_dir, entry)
            if os.path.isfile(path):
                if os.path.splitext(entry)[1].lower() in DOC_EXTENSIONS and entry.lower() != 'readme.md':
                    plan['other'].append(_file(root, path, audience))
                continue
            low = entry.lower()
            block = BLOCK_RX.match(entry)
            if re.search(r'start.?here|orientation|welcome|getting.?started|induction', low):
                for name in _listing(path):
                    full = os.path.join(path, name)
                    if os.path.isfile(full):
                        plan['onboarding'].append(_file(root, full, audience))
            elif re.search(r'mock|exam|assessment', low) and not block:
                _scan_mocks(root, path, audience, mocks, year)
            elif block:
                number = int(block.group(2))
                label = block.group(3) or ''
                entry_block = blocks.setdefault(number, {
                    'number': number, 'kind': block.group(1).lower(), 'label': humanise(label),
                    'folder': entry, 'units': {}})
                for unit_name in sorted(_listing(path), key=_numbered_order):
                    unit_path = os.path.join(path, unit_name)
                    if os.path.isfile(unit_path):
                        plan['other'].append(_file(root, unit_path, audience))
                        continue
                    match = UNIT_RX.match(unit_name)
                    if match:
                        module, unit, title = match.group(1).upper(), int(match.group(2)), humanise(match.group(3) or '')
                    else:
                        numbered = NUMBERED_RX.match(unit_name)
                        words = (numbered.group(2) if numbered else unit_name).split('_')
                        module, unit, title = words[0].upper(), number, humanise('_'.join(words[1:]))
                    if module not in module_order:
                        module_order.append(module)
                    key = f'{module}-{unit}'
                    target = entry_block['units'].setdefault(key, {
                        'module': module, 'unit': unit, 'title': title, 'files': []})
                    if title and not target['title']:
                        target['title'] = title
                    for name in _listing(unit_path):
                        full = os.path.join(unit_path, name)
                        if os.path.isfile(full) and os.path.splitext(name)[1].lower() in DOC_EXTENSIONS:
                            target['files'].append(_file(root, full, audience))
            else:
                bucket = 'strategy' if re.search(r'blueprint|strategy|analysis|programme|plan', low) else 'other'
                for dirpath, _dirs, names in os.walk(path):
                    for name in sorted(names):
                        if name.startswith('.') or os.path.splitext(name)[1].lower() not in DOC_EXTENSIONS:
                            continue
                        plan[bucket].append(_file(root, os.path.join(dirpath, name), audience))

    previous = None
    for number in sorted(blocks):
        block = blocks[number]
        span = parse_dates(block['folder'], year=year, after=previous)
        block['starts'] = span[0].isoformat() if span else None
        block['ends'] = span[1].isoformat() if span else None
        if span:
            previous = span[0]
        units = sorted(block['units'].values(),
                       key=lambda u: (module_order.index(u['module']) if u['module'] in module_order else 99))
        for unit in units:
            unit['files'].sort(key=lambda f: (f['audience'] != 'students', f['step'] if f['step'] is not None else 99,
                                              f['ext'] != '.pdf', f['name']))
        block['units'] = units
        plan['blocks'].append(block)

    first_block = blocks[min(blocks)] if blocks else None
    anchor = dt.date.fromisoformat(first_block['starts']) if first_block and first_block.get('starts') else None
    for number in sorted(mocks):
        mock = mocks[number]
        span = parse_dates(mock['folder'], year=year, after=anchor)
        mock['starts'] = span[0].isoformat() if span else None
        mock['ends'] = span[1].isoformat() if span else None
        plan['mocks'].append(mock)

    plan['modules'] = module_order
    for bucket in ('onboarding', 'strategy', 'other'):
        plan[bucket].sort(key=lambda f: (f['audience'] != 'students', f['step'] if f['step'] is not None else 99,
                                         f['ext'] != '.pdf', f['name']))
    if not plan['blocks']:
        plan['notes'].append('No teaching-block folders found (expected names like "1_Weekend_1_7-8_Nov").')

    from . import sheet as schedule_sheet
    found = schedule_sheet.find(root)
    plan['schedule_sheet'] = os.path.relpath(found, root) if found else None
    if found:
        name = os.path.basename(found)
        for bucket in ('onboarding', 'strategy', 'other'):
            plan[bucket] = [f for f in plan[bucket] if f['name'] != name]
        for block in plan['blocks']:
            for unit in block['units']:
                unit['files'] = [f for f in unit.get('files', []) if f.get('name') != name]
    return plan


def _scan_mocks(root, path, audience, mocks, year):
    """Add the mock folders under ``path`` to ``mocks``, keyed by number.

    Each file carries ``group``: the sub-folder's module ("FREP", "PAPER_1"), or
    ``None`` for a file that covers the whole sitting.
    """
    for entry in sorted(_listing(path), key=_numbered_order):
        full = os.path.join(path, entry)
        if os.path.isfile(full):
            continue
        numbered = NUMBERED_RX.match(entry)
        number = int(numbered.group(1)) if numbered else len(mocks) + 1
        title = humanise(re.sub(r'[_ ]?\d{1,2}(?:[_ ]?[A-Za-z]{3})?[-–]\d{1,2}[_ ]?[A-Za-z]{3}.*$', '',
                                numbered.group(2) if numbered else entry))
        mock = mocks.setdefault(number, {'number': number, 'title': title, 'folder': entry, 'files': []})
        for dirpath, _dirs, names in os.walk(full):
            sub = os.path.relpath(dirpath, full)
            module = None
            if sub != '.':
                head = sub.split(os.sep)[0]
                match = NUMBERED_RX.match(head)
                module = (match.group(2) if match else head).upper().replace(' ', '_')
            for name in sorted(names):
                if name.startswith('.') or os.path.splitext(name)[1].lower() not in DOC_EXTENSIONS:
                    continue
                item = _file(root, os.path.join(dirpath, name), audience)
                item['group'] = module
                mock['files'].append(item)


def summary(plan):
    """Counts for the review screen and the command's report."""
    unit_files = sum(len(u['files']) for b in plan['blocks'] for u in b['units'])
    files = (unit_files + len(plan['onboarding']) + len(plan['strategy']) + len(plan['other'])
             + sum(len(m['files']) for m in plan['mocks']))
    every = ([f for b in plan['blocks'] for u in b['units'] for f in u['files']]
             + plan['onboarding'] + plan['strategy'] + plan['other']
             + [f for m in plan['mocks'] for f in m['files']])
    return {
        'blocks': len(plan['blocks']),
        'units': sum(len(b['units']) for b in plan['blocks']),
        'modules': len(plan['modules']),
        'mocks': len(plan['mocks']),
        'files': files,
        'student_files': sum(1 for f in every if f['audience'] == 'students'),
        'staff_files': sum(1 for f in every if f['audience'] == 'staff'),
    }
