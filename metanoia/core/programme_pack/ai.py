"""The optional AI step: Claude reads the pack and writes what folders cannot say.

Two jobs, both through the same client the content queue uses
(:mod:`apps.ai_assistant.generate`, enabled by ``ANTHROPIC_API_KEY`` +
``ADMIN_AI_ENABLED``):

* :func:`enrich` — a one-paragraph summary and three focus points for every
  unit, from the unit's topic guide. They become the week's summary and the
  "In this unit" panel at the top of the unit lesson.
* :func:`read_schedule` — the weekly session slots, mock sittings and exam dates
  out of the timetable / candidate guide, for a pack that arrives without a
  spine written for it.

Neither is load-bearing. With no key configured (the server today) both return
an empty result and say why; the deterministic folder parser and the spine
carry the build on their own, and the lessons still read the documents' own
text. Nothing the model returns is trusted for structure: modules, units and
files always come from the folders.
"""

import logging
import os

from . import docconv

logger = logging.getLogger('apps')

UNIT_SYSTEM = ('You write short, plain-English study notes for South African chartered-accountancy '
               'candidates (CTA, ITC/IAC, APC). You never invent facts, figures, section numbers or dates; '
               'you summarise only what the source text says. Easy English, short sentences, no hype.')

UNIT_SCHEMA = {
    'type': 'object',
    'properties': {
        'units': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'key': {'type': 'string'},
                    'summary': {'type': 'string'},
                    'focus': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['key', 'summary', 'focus'],
            },
        },
    },
    'required': ['units'],
}

SCHEDULE_SCHEMA = {
    'type': 'object',
    'properties': {
        'exam_name': {'type': 'string'},
        'slots': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'module': {'type': 'string'}, 'weekday': {'type': 'string', 'description': 'saturday … friday'},
            'time': {'type': 'string', 'description': 'HH:MM, 24-hour'}, 'minutes': {'type': 'integer'},
        }, 'required': ['module', 'weekday', 'time', 'minutes']}},
        'dates': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'title': {'type': 'string'},
            'kind': {'type': 'string', 'enum': ['exam', 'mock', 'deadline', 'orientation', 'other', 'holiday']},
            'start': {'type': 'string', 'description': 'YYYY-MM-DD or YYYY-MM-DDTHH:MM'},
            'end': {'type': 'string', 'description': 'same format, or empty'},
            'module': {'type': 'string', 'description': 'module code, or empty for the whole programme'},
        }, 'required': ['title', 'kind', 'start']}},
    },
    'required': ['slots', 'dates'],
}


def is_enabled():
    """True when the content-pack drafting AI is configured; ``False`` if it cannot be loaded."""
    try:
        from core.content_pack import drafting
        return drafting.is_enabled()
    except Exception:            # pragma: no cover
        return False


def _read(plan, entry):
    """A file's text, preferring the Word original the pack ships beside a PDF."""
    twins = {}
    for block in plan['blocks']:
        for unit in block['units']:
            for f in unit['files']:
                if f['ext'] == '.docx':
                    twins.setdefault(f['twin'], f)
    for bucket in ('onboarding', 'strategy', 'other'):
        for f in plan[bucket]:
            if f['ext'] == '.docx':
                twins.setdefault(f['twin'], f)
    source = entry if entry['ext'] == '.docx' else twins.get(entry['twin'], entry)
    return docconv.read_document(os.path.join(plan['root'], source['path']))


def enrich(plan, spine=None, *, out=None):
    """``{'units': {'FREP-1': {'summary', 'focus'}}}`` — or ``{}`` without AI."""
    out = out or (lambda message: None)
    if not is_enabled():
        out('AI step skipped: ANTHROPIC_API_KEY / ADMIN_AI_ENABLED not set. The documents\' own text is used.')
        return {}
    chunks = []
    for block in plan['blocks']:
        for unit in block['units']:
            guide = next((f for f in unit['files'] if f['step'] == 4 and f['ext'] in ('.pdf', '.docx')), None)
            guide = guide or next((f for f in unit['files'] if f['ext'] in ('.pdf', '.docx')), None)
            if guide is None:
                continue
            text = docconv.plain_text(_read(plan, guide), limit=3500)
            chunks.append(f'### key={unit["module"]}-{unit["unit"]} · {unit["module"]} unit {unit["unit"]}: '
                          f'{unit["title"]}\n{text}')
    if not chunks:
        return {}
    try:
        from apps.ai_assistant import generate
        result = generate.generate_from_prompt(
            '\n\n'.join(chunks), system=UNIT_SYSTEM, schema=UNIT_SCHEMA, constrain=False, max_tokens=16000,
            instruction='For every unit above (keep its key exactly), write: summary — two or three sentences '
                        'saying what the unit teaches and why it earns marks; focus — three short points to '
                        'master. Use only the text given.')
    except Exception as exc:
        logger.exception('programme pack: AI enrichment failed')
        out(f'AI step failed ({exc}); the documents\' own text is used.')
        return {}
    units = {}
    for item in (result or {}).get('units', []):
        key = str(item.get('key', '')).strip()
        if key:
            units[key] = {'summary': str(item.get('summary', ''))[:800],
                          'focus': [str(x)[:200] for x in (item.get('focus') or [])][:5]}
    out(f'AI wrote summaries for {len(units)} unit(s).')
    return {'units': units}


def read_schedule(plan, *, out=None):
    """Session slots and key dates from the onboarding documents — ``{}`` without AI."""
    out = out or (lambda message: None)
    if not is_enabled():
        return {}
    texts = []
    for entry in plan['onboarding']:
        if entry['ext'] in ('.pdf', '.docx') and entry['audience'] == 'students':
            texts.append(f'===== {entry["name"]} =====\n' + docconv.plain_text(_read(plan, entry), limit=12000))
    if not texts:
        return {}
    try:
        from apps.ai_assistant import generate
        result = generate.generate_from_prompt(
            '\n\n'.join(texts)[:60000], system=UNIT_SYSTEM, schema=SCHEDULE_SCHEMA, constrain=False,
            max_tokens=8000,
            instruction='From the timetable and guides above, list the weekly live-session slots (one per '
                        f'module; module codes are {", ".join(plan["modules"])}) and every dated event: mocks, '
                        'debriefs, registration deadlines, examination papers. Only dates the documents state.')
    except Exception as exc:
        logger.exception('programme pack: AI schedule read failed')
        out(f'AI could not read the timetable ({exc}).')
        return {}
    return result if isinstance(result, dict) else {}


SHEET_SCHEMA = {
    'type': 'object',
    'properties': {
        'rows': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'type': {'type': 'string', 'enum': ['phase', 'slot', 'session', 'date', 'unit', 'week', 'mock_paper',
                                                 'host']},
            'module': {'type': 'string'}, 'number': {'type': 'string'}, 'date': {'type': 'string'},
            'end': {'type': 'string'}, 'time': {'type': 'string'}, 'minutes': {'type': 'string'},
            'kind': {'type': 'string'}, 'title': {'type': 'string'}, 'details': {'type': 'string'},
        }, 'required': ['type']}},
    },
    'required': ['rows'],
}

SHEET_INSTRUCTION = """Fill in the programme's schedule sheet from the documents above. One row per item:
- slot: each module's weekly live session. module, date = weekday (Sat), time (HH:MM, 24-hour), minutes.
- session: every one-off session the documents give (debriefs, open sessions, Q&A, revision). date (YYYY-MM-DD),
  time, minutes, module (empty = whole cohort), kind = class|revision|test_review|workshop|briefing, title, details.
- date: every dated calendar item: exam papers, mocks, registration and payment deadlines, results, holidays /
  no-session days, orientation, intensives. date, end (if a range), time (if stated),
  kind = exam|test|mock|deadline|holiday|orientation|result|session|intensive|other, module (if one module), title,
  details. List the teaching blocks too (kind session, one row per block, date..end).
- unit: each unit's exact title, and its self-study item in details. module, number.
- week: a better display title for a teaching block (kind block) or mock (kind mock), number = block / mock number;
  kind extra (with date and end) for a week no folder has, such as a final Q&A week.
- mock_paper: which modules sit each full-mock paper. number = paper, module = codes joined with +.
- phase: one row: kind = test|exam, number, title, date (first day), end (last day), details = one-line summary.
- host: details = the session host's e-mail, only if a document states it.
Module codes are: {modules}. Use only what the documents state. Never invent a date, time or title."""


def draft_sheet(plan, *, out=None):
    """``(rows, problems)`` — the schedule sheet drafted from the timetable and guides.

    For a pack that arrives without ``schedule.csv``. The rows go through the
    same checks as a hand-made sheet (:func:`core.programme_pack.sheet.parse_table`)
    and are shown for a human to correct before anything is built. ``([], [...])``
    without AI.
    """
    from . import sheet

    out = out or (lambda message: None)
    if not is_enabled():
        return [], ['The AI is not switched on (ANTHROPIC_API_KEY / ADMIN_AI_ENABLED).']
    texts = []
    for entry in plan['onboarding'] + plan['other']:
        if entry['ext'] in ('.pdf', '.docx'):
            texts.append(f'===== {entry["name"]} ({entry["audience"]}) =====\n'
                         + docconv.plain_text(_read(plan, entry), limit=15000))
    folders = ['===== the pack\'s folders =====']
    for block in plan['blocks']:
        folders.append(f'{block["kind"]} {block["number"]} ({block.get("starts") or "undated"} to '
                       f'{block.get("ends") or "?"}): ' + '; '.join(
                           f'{u["module"]} unit {u["unit"]} {u["title"]}' for u in block['units']))
    for mock in plan['mocks']:
        folders.append(f'mock {mock["number"]}: {mock["title"]} ({mock.get("starts") or "undated"})')
    if not texts:
        return [], ['The pack has no timetable or guide in Start here for the AI to read.']
    try:
        from apps.ai_assistant import generate
        result = generate.generate_from_prompt(
            ('\n'.join(folders) + '\n\n' + '\n\n'.join(texts))[:120000], system=UNIT_SYSTEM,
            schema=SHEET_SCHEMA, constrain=False, max_tokens=32000,
            instruction=SHEET_INSTRUCTION.format(modules=', '.join(plan['modules'])))
    except Exception as exc:
        logger.exception('programme pack: AI sheet draft failed')
        return [], [f'The AI could not draft the sheet ({exc}).']
    table = [sheet.COLUMNS] + [[str((row or {}).get(c, '') or '') for c in sheet.COLUMNS]
                               for row in (result or {}).get('rows', [])]
    rows, problems = sheet.parse_table(table)
    out(f'AI drafted {len(rows)} sheet row(s)' + (f', {len(problems)} left out' if problems else '') + '.')
    return rows, problems
