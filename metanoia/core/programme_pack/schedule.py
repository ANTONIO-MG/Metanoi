"""A spine for a pack that arrives without one.

``core/academic_spine_sample.py`` was written by hand from the timetable. A new
pack uploaded as a zip gets the same shape built here from what is certain —
the dated folders — plus whatever the uploader typed (session slots, exam
dates) or the AI read out of the timetable (:func:`core.programme_pack.ai.read_schedule`).

``CATALOGUE_GUESS`` maps common module codes to the canonical catalogue
(``core.academic_spine.MODULES``).
"""

import datetime as dt
import re

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

CATALOGUE_GUESS = {
    'FREP': 'FR', 'FR': 'FR', 'FAC': 'FR', 'FIN': 'FR', 'IFRS': 'FR',
    'TAX': 'TX', 'TAXA': 'TX', 'TX': 'TX',
    'MACF': 'MA', 'MAF': 'MA', 'MAC': 'MA', 'MA': 'MA', 'FM': 'MA',
    'CGAU': 'GA', 'AUD': 'GA', 'GA': 'GA', 'AUDIT': 'GA',
}
MODULE_NAMES = {'FR': 'Financial reporting', 'TX': 'Taxation',
                'MA': 'Management accounting and finance', 'GA': 'Governance and auditing'}

SLOT_RX = re.compile(r'^\s*([A-Za-z]{2,8})\s+([A-Za-z]{3,9})\s+(\d{1,2})[:h.]?(\d{2})?\s*(\d{2,3})?\s*$')


def parse_slots(text):
    """``FREP Sat 08:00 120`` per line → ``{'FREP': ('saturday', '08:00', 120)}``."""
    slots = {}
    for line in (text or '').splitlines():
        match = SLOT_RX.match(line)
        if not match:
            continue
        code, day, hour, minute, minutes = match.groups()
        weekday = next((w for w in WEEKDAYS if w.startswith(day.lower()[:3])), None)
        if weekday is None:
            continue
        slots[code.upper()] = (weekday, f'{int(hour):02d}:{minute or "00"}', int(minutes or 120))
    return slots


def _on_or_after(start, weekday):
    """The first ``weekday`` on or after ``start``."""
    target = WEEKDAYS.index(weekday)
    return start + dt.timedelta(days=(target - start.weekday()) % 7)


def spine_from_plan(plan, *, key, institution, programme, cohort, slots=None, exam_dates=(),
                    fee='750.00', bundle_total='2200.00', ai_schedule=None, provider='teams',
                    sheet_rows=None):
    """Everything :class:`core.programme_pack.builder.Builder` needs, as plain data.

    ``sheet_rows`` (:mod:`core.programme_pack.sheet`) are applied last: the
    sheet's slots win over typed ones, and its dates, sessions, unit titles and
    mock papers are written over the automatic ones.

    Weeks are the dated blocks and mocks in date order; undated ones keep folder
    order.
    """
    from . import sheet as schedule_sheet

    slots = dict(slots or {})
    slots.update(schedule_sheet.slots(sheet_rows or []))
    ai_schedule = ai_schedule or {}
    for item in ai_schedule.get('slots') or []:
        code = str(item.get('module', '')).upper()
        weekday = str(item.get('weekday', '')).lower()
        if code in plan['modules'] and weekday in WEEKDAYS and code not in slots:
            slots[code] = (weekday, str(item.get('time', '08:00'))[:5], int(item.get('minutes') or 120))

    modules = []
    for order, code in enumerate(plan['modules'], 1):
        canonical = CATALOGUE_GUESS.get(code, code)
        modules.append({'code': code, 'catalogue': canonical, 'order': order,
                        'name': MODULE_NAMES.get(canonical, code), 'description': ''})

    dated = []
    for block in plan['blocks']:
        dated.append((block.get('starts') or '9999', 0, block['number'], 'block', block))
    for mock in plan['mocks']:
        dated.append((mock.get('starts') or '9999', 1, mock['number'], 'mock', mock))
    dated.sort(key=lambda row: (row[0], row[1], row[2]))
    weeks = []
    for index, (start, _o, number, kind, item) in enumerate(dated, 1):
        starts = item.get('starts')
        following = next((row[0] for row in dated[index:] if row[0] != '9999'), None)
        if starts and following:
            ends = (dt.date.fromisoformat(following) - dt.timedelta(days=1)).isoformat()
        elif starts:
            ends = (dt.date.fromisoformat(item.get('ends') or starts) + dt.timedelta(days=6)).isoformat()
        else:
            ends = None
        label = item.get('label') or item.get('title') or ''
        if kind == 'block':
            title = f'{item["kind"].title()} {number}' + (f' · {label}' if label else '')
            spec = {'number': index, 'block': number, 'unit': None, 'title': title}
            units = {u['unit'] for u in item['units']}
            if len(units) == 1:
                spec['unit'] = units.pop()
        else:
            spec = {'number': index, 'mock': number, 'title': item['title'] + (
                f' · {item["starts"]}' if item.get('starts') else '')}
        spec.update({'starts': starts, 'ends': ends})
        weeks.append(spec)

    sessions, events = [], []
    for block in plan['blocks']:
        if not block.get('starts'):
            continue
        start = dt.date.fromisoformat(block['starts'])
        week = next((w['number'] for w in weeks if w.get('block') == block['number']), None)
        events.append({'ref': f'block-{block["number"]}', 'kind': 'session', 'start': block['starts'],
                       'end': block.get('ends') or None,
                       'title': f'{block["kind"].title()} {block["number"]}' + (
                           f' · {block["label"]}' if block.get('label') else ''),
                       'description': 'Teaching block.'})
        for unit in block['units']:
            slot = slots.get(unit['module'])
            if not slot:
                continue
            weekday, time, minutes = slot
            day = _on_or_after(start, weekday)
            sessions.append({'ref': f'live-{unit["module"]}-{unit["unit"]}', 'module': unit['module'],
                             'week': week, 'kind': 'class', 'start': f'{day.isoformat()}T{time}',
                             'minutes': minutes,
                             'title': f'{unit["module"]} live session · Unit {unit["unit"]}: {unit["title"]}',
                             'description': 'Live session for this unit.'})
    for mock in plan['mocks']:
        if mock.get('starts'):
            events.append({'ref': f'mock-{mock["number"]}', 'kind': 'mock', 'start': mock['starts'],
                           'end': mock.get('ends') or None, 'title': mock['title'],
                           'description': 'Mock sitting.'})
    if plan['blocks'] and plan['blocks'][0].get('starts'):
        events.insert(0, {'ref': 'programme-start', 'kind': 'orientation', 'start': plan['blocks'][0]['starts'],
                          'title': 'Programme starts: orientation',
                          'description': 'Read the Start-here documents first.'})
    exam_ref = None
    for index, day in enumerate(sorted({str(d) for d in exam_dates if d}), 1):
        ref = f'exam-{index}'
        exam_ref = exam_ref or ref
        events.append({'ref': ref, 'kind': 'exam', 'start': day, 'title': f'Examination · day {index}',
                       'description': 'Examination.'})
    for index, item in enumerate(ai_schedule.get('dates') or [], 1):
        try:
            dt.datetime.fromisoformat(str(item['start']))
        except (KeyError, ValueError):
            continue
        kind = item.get('kind') if item.get('kind') in ('exam', 'mock', 'deadline', 'orientation', 'other',
                                                       'holiday') else 'other'
        ref = f'ai-{index}'
        if kind == 'exam' and exam_ref is None:
            exam_ref = ref
        module = str(item.get('module') or '').upper()
        events.append({'ref': ref, 'kind': kind, 'start': str(item['start'])[:16],
                       'end': str(item.get('end') or '')[:16] or None, 'title': str(item['title'])[:200],
                       'module': module if module in plan['modules'] else None,
                       'description': 'Read from the timetable by the AI assistant — check it.'})

    starts = cohort.get('start_date') or next((w['starts'] for w in weeks if w.get('starts')), None)
    ends = cohort.get('end_date') or (max(exam_dates) if exam_dates else None) or next(
        (w['ends'] for w in reversed(weeks) if w.get('ends')), None)
    cohort = dict(cohort, start_date=starts, end_date=str(ends) if ends else None)
    spine = {
        'key': key,
        'pack': plan['root'],
        'year': int(starts[:4]) if starts else dt.date.today().year,
        'timezone': 'Africa/Johannesburg',
        'provider': provider,
        'institution': institution,
        'programme': programme,
        'cohort': cohort,
        'pricing': {'per_month': fee, 'bundle_total': bundle_total, 'bundle_name': 'All 4 modules'},
        'phase': {'title': f'{programme.get("name") or programme["code"]} preparation', 'kind': 'exam',
                  'sequence': 1, 'starts': starts, 'ends': cohort['end_date']},
        'modules': modules,
        'units': {},
        'slots': {code: {'label': f'Live session {day.title()} {time}'} for code, (day, time, _m) in slots.items()},
        'weeks': weeks,
        'sessions': sessions,
        'events': events,
        'exam_event': exam_ref,
        'mock_papers': {},
        'mock_briefings': [],
    }
    if sheet_rows:
        schedule_sheet.overlay(spine, sheet_rows, plan)
    return spine
