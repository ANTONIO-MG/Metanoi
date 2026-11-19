"""Read a CTA solution workbook's mark grid into ``metanoia-pack`` parts.

The coach's solution workbooks put one REQUIRED part per sheet, each headed
something like::

    SUGGESTED SOLUTION — (a) SAICA CPC — PIE  |  MAX 18 MARKS
    #  | Answer point                                   | Marks
    1  | Mr Marais holds shares in Ubuntu through his RA… | 2
    …
    MAXIMUM MARKS | 18

or, in the tax workbooks::

    Q1 PART (a) — s18(1) CHANGE OF USE: ANCHOR HOUSE  [16 marks]
    Mark Point | Keyword | Marks

Every scoring row becomes a rubric point, so a candidate's answer can be marked
point by point and the result page can show exactly which points earned marks
and which were left on the table. Nothing is invented: a sheet whose rows do not
add up to its own stated maximum is reported, not "fixed".

``PART_RX`` matches a sheet title or heading that names a part: "(a)",
"Part (b)", "1a_VAT".
"""

import re
from decimal import Decimal, InvalidOperation

PART_RX = re.compile(r'\(\s*([a-z](?:\)\s*\(\s*[ivx]+)?)\s*\)|PART\s+\(?([a-z])\)?|^(\d?[a-z])_', re.I)
MAX_RX = re.compile(r'MAX(?:IMUM)?\s+(\d+(?:\.\d+)?)\s*MARKS?|\[\s*(\d+(?:\.\d+)?)\s*MARKS?\s*\]|[—-]\s*(\d+)\s*$', re.I)
SKIP_ROW_RX = re.compile(r'^(MAXIMUM MARKS|(SUB-?)?TOTAL(\s+MARKS)?\s*:?\s*$|\(AVAILABLE|NOTE TO MARKER|MARKS AVAILABLE)', re.I)
TEXT_HEADERS = ('answer point', 'mark point', 'answer', 'point', 'description', 'solution', 'item')


def _num(value):
    """``value`` as a mark (a ``Decimal`` in (0, 40]), or ``None`` when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().rstrip('m').strip())
    except (InvalidOperation, ValueError):
        return None
    return number if Decimal('0') < number <= Decimal('40') else None


def _clean(value):
    """``value`` as text with whitespace collapsed."""
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _header(rows):
    """``(row_index, marks_col, text_col, authority_col)`` of the grid header, or None.

    Without a recognised text header, the widest text column that is not the
    marks column is used.
    """
    for index, row in enumerate(rows[:12]):
        labels = [_clean(c).lower() for c in row]
        if 'marks' not in labels and 'mark' not in labels:
            continue
        marks_col = labels.index('marks') if 'marks' in labels else labels.index('mark')
        text_col = next((labels.index(h) for h in TEXT_HEADERS if h in labels), None)
        if text_col is None:
            text_col = max((i for i in range(len(row)) if i != marks_col),
                           key=lambda i: len(_clean(row[i])), default=None)
        authority_col = next((i for i, label in enumerate(labels)
                              if label in ('authority', 'keyword', 'reference', 'standard')), None)
        return index, marks_col, text_col, authority_col
    return None


def parse_sheet(title, rows):
    """One worksheet → a part dict, or ``None`` when it is not a mark grid.

    ``rows`` is a list of value tuples (``ws.iter_rows(values_only=True)``).

    A row without a mark (a group heading such as "FINANCIAL INTEREST — the
    shareholding") is skipped.
    """
    rows = [list(r) for r in rows if any(c not in (None, '') for c in r)]
    if not rows:
        return None
    found = _header(rows)
    if not found:
        return None
    header_at, marks_col, text_col, authority_col = found
    if text_col is None:
        return None

    heading = _clean(rows[0][0]) if header_at else _clean(title)
    ref_match = PART_RX.search(heading) or PART_RX.search(title)
    if not ref_match:
        return None
    ref = next(g for g in ref_match.groups() if g)
    ref = re.sub(r'[^a-z0-9()]', '', ref.lower()).replace(')(', ')(')
    stated = None
    for text in (heading, title):
        m = MAX_RX.search(text)
        if m:
            stated = _num(next(g for g in m.groups() if g))
            if stated:
                break

    rubric = []
    for row in rows[header_at + 1:]:
        cells = row + [None] * (max(marks_col, text_col) + 1 - len(row))
        text = _clean(cells[text_col])
        if not text or SKIP_ROW_RX.match(text) or SKIP_ROW_RX.match(_clean(cells[0])):
            continue
        marks = _num(cells[marks_col])
        if marks is None:
            continue
        point = {'point': text[:1200], 'marks': float(marks)}
        if authority_col is not None and authority_col < len(cells) and _clean(cells[authority_col]):
            point['authority'] = _clean(cells[authority_col])[:120]
        rubric.append(point)
    if not rubric:
        return None

    total = sum(Decimal(str(p['marks'])) for p in rubric)
    title_text = re.sub(r'^SUGGESTED SOLUTION\s*[—-]\s*', '', heading, flags=re.I)
    title_text = MAX_RX.sub('', title_text).strip(' |—-')
    return {
        'ref': ref,
        'title': title_text[:300],
        'stated_max': float(stated) if stated else None,
        'available': float(total),
        'rubric': rubric,
    }


def _cap_rubric(rubric, cap):
    """Rows usually *offer* more than the maximum ("available 9, max 7"). The pack
    needs rows that add up to the part, so scale them proportionally and keep
    every point — the candidate still sees every way the marks could be earned.

    A rounding remainder that pushes the last row to zero or below is folded
    back so every row is worth at least a quarter mark.
    """
    available = sum(Decimal(str(p['marks'])) for p in rubric)
    if not cap or available == cap:
        return rubric
    out, running = [], Decimal('0')
    for i, point in enumerate(rubric):
        if i == len(rubric) - 1:
            share = cap - running
        else:
            share = (Decimal(str(point['marks'])) * cap / available).quantize(Decimal('0.25'))
        running += share
        out.append(dict(point, marks=float(share), offered=point['marks']))
    if out and out[-1]['marks'] <= 0:
        out[-2]['marks'] += out[-1]['marks'] - 0.25
        out[-1]['marks'] = 0.25
    return out


def parse_workbook(path):
    """A solution workbook → ``{'parts': [...], 'navigator': {...}}``.

    ``navigator`` maps a part ref to the "what it tests" line on the navigator
    sheet when there is one — the closest thing the workbook has to the REQUIRED.

    Only the per-part solution sheets are read: the triggers / framework sheets
    also carry "(a)" labels and numbered rows, but they are not the mark grid. A
    part is worth what its sheet says, unless its rows offer less — then it is
    worth what can actually be earned, with a ``note``. Rows are never inflated.
    """
    import warnings

    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    parts, navigator, seen = [], {}, set()
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if ws.title.lower().startswith(('navigator', '2. navigator', 'index', '0_legend')):
            for row in rows:
                cells = [_clean(c) for c in row if c not in (None, '')]
                if len(cells) >= 2 and re.match(r'^\(?[a-z]\)', cells[0], re.I):
                    navigator[cells[0].strip('()').lower()[:1]] = cells[1]
            continue
        first = next((str(c) for r in rows for c in r if c not in (None, '')), '')
        if not re.search(r'PART|SUGGESTED SOLUTION', ws.title + ' ' + first, re.I):
            continue
        part = parse_sheet(ws.title, rows)
        if part and part['ref'] not in seen:
            seen.add(part['ref'])
            parts.append(part)
    wb.close()

    for part in parts:
        stated = Decimal(str(part['stated_max'] or part['available']))
        cap = min(stated, Decimal(str(part['available'])))
        if cap < stated:
            part['note'] = (f"Source sheet states {stated:g} marks for ({part['ref']}) "
                            f"but its rows offer only {part['available']:g}.")
        part['marks'] = float(cap)
        part['rubric'] = _cap_rubric(part['rubric'], cap)
        part['what_it_tests'] = navigator.get(part['ref'][:1], '')
    return {'parts': parts}


def scenario_from_workbook(path):
    """The workbook's own account of the scenario, as HTML.

    The weekly challenge question papers were not kept, but each solution
    workbook opens with a "Scenario Triggers" sheet — every planted fact of the
    scenario with the issue it was set to test. Shown *before* the attempt, only
    the facts column is used: the "why it earns the mark" column is the answer.
    """
    import html
    import warnings

    import openpyxl
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    facts, heading = [], ''
    for ws in wb.worksheets:
        if 'trigger' not in ws.title.lower():
            continue
        rows = [[_clean(c) for c in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        heading = next((c for c in rows[0] if c), '') if rows else ''
        for row in rows[2:]:
            if row and row[0]:
                facts.append(row[0])
        break
    nav_title = ''
    for ws in wb.worksheets[:1]:
        first = next((c for r in ws.iter_rows(values_only=True) for c in r if c), '')
        second = [c for r in list(ws.iter_rows(values_only=True))[1:2] for c in r if c]
        nav_title = _clean(second[0]) if second else _clean(first)
    wb.close()
    if not facts:
        return ''
    items = ''.join(f'<li>{html.escape(f)}</li>' for f in facts)
    intro = f'<p><strong>{html.escape(nav_title)}</strong></p>' if nav_title else ''
    return (f'{intro}<p>The scenario facts you must work with:</p><ul>{items}</ul>'
            f'<p class="text-muted small">Answer each REQUIRED part below. The suggested '
            f'solution and its mark grid open once you submit.</p>')
