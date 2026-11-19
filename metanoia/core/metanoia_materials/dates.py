"""The 2026 assessment dates, as the documents themselves state them.

Every confirmed date below is quoted from a document in the archive — a
blueprint's cover, an actual test paper's header, an evidence base. Where the
archive does not state a date, :func:`assessment_date` falls back to the
offering's existing calendar event for that test (the academic spine's planned
date, usually within a day) and says so, rather than inventing one.

Tables
------
``CONFIRMED``
    ``(programme, cohort, module, kind, sequence)`` → ``(date, where it is
    stated)``.
``_MASTER``
    The "Assessment" rows of the Metanoia Master Calendar 2026 (built 13 Oct 2026
    from both Metanoia calendars, sent mail and project notes). Where it and a
    document both give a date they agree; these only fill in the gaps in
    ``CONFIRMED``.
``SPINE_ALIGNED_COHORTS``
    Intakes whose year runs to the academic spine's planned calendar, so a
    missing date may fall back to the offering's existing event.
``COHORT_STARTS``
    When each 2026 intake starts — the first preparation block opens here.
"""

import datetime as dt
import os
import re

D = dt.date

CONFIRMED = {
    ('BCTA', 'B26F', 'MACF', 'test', 1): (D(2026, 3, 10), 'Actual Test Paper (B26F Test 1, 10 Mar 2026) — Stellar Air'),
    ('BCTA', 'B26F', 'CGAU', 'test', 1): (D(2026, 3, 11), 'B26F Test 1 — Apex Academy | 11 March 2026'),
    ('BCTA', 'B26F', 'MACF', 'exam', 1): (D(2026, 6, 1), 'Actual Final Exam - Scenario (1 Jun 2026) — Ubuntu Brew'),
    ('BCTA', 'B26S', 'CGAU', 'test', 1): (D(2026, 8, 17), 'CGAU B26S Test 1 blueprint: 17 August 2026'),
    ('BCTA', 'B26S', 'TAX', 'test', 1): (D(2026, 8, 17), 'TAX B26S Test 1 coverage (17 August 2026, 50 marks)'),
    ('BCTA', 'B26S', 'MACF', 'test', 1): (D(2026, 8, 18), 'B26S Test 1 (MACF & FREP) = 18 August 2026'),
    ('BCTA', 'B26S', 'FREP', 'test', 1): (D(2026, 8, 18), 'B26S Test 1 (MACF & FREP) = 18 August 2026'),
    ('BCTA', 'B26S', 'TAX', 'test', 2): (D(2026, 10, 7), 'TAX B26S Test 2 — 7 October 2026, 80 marks'),
    ('BCTA', 'B26S', 'CGAU', 'test', 2): (D(2026, 10, 8), 'CGAU B26S Test 2: Thursday 8 October 2026'),
    ('BCTA', 'B26S', 'MACF', 'test', 2): (D(2026, 10, 8), 'B26S Test 2 — Imbewu (8 Oct 2026)'),
    ('BCTA', 'B26S', 'FREP', 'test', 2): (D(2026, 10, 8), 'FREP B26S Test 2, Thursday 8 October 2026'),
    ('PGDA', 'P26F', 'FREP', 'test', 1): (D(2026, 3, 18), 'P26F Test 1 on 18 March 2026 (FREP blueprints)'),
    ('PGDA', 'P26F', 'TAX', 'test', 1): (D(2026, 3, 18), 'PGDA planner: TEST 1 - TAXA & FREP'),
    ('PGDA', 'P26F', 'CGAU', 'test', 2): (D(2026, 5, 13), 'AUD/CGAU Test 2 Strategic Blueprint — Test Date: 13 May 2026'),
    ('PGDA', 'P26F', 'MACF', 'test', 2): (D(2026, 5, 13), 'PGDA planner: TEST 2 - MACF & CGAU'),
    ('PGDA', 'P26F', 'FREP', 'test', 3): (D(2026, 7, 22), 'FREP Test 3 Strategic Blueprint — two-month plan to 22 July 2026'),
}

_MASTER = [
    ('2026-03-19', 'PGDA', 'P26F', ['CGAU'], 'test', 1),
    ('2026-07-23', 'PGDA', 'P26F', ['CGAU'], 'test', 3),
    ('2026-07-27', 'PGDA', 'P26F', ['TAX'], 'test', 3),
    ('2026-07-28', 'PGDA', 'P26F', ['MACF'], 'test', 3),
    ('2026-09-22', 'PGDA', 'P26F', ['MACF', 'CGAU'], 'test', 4),
    ('2026-09-23', 'PGDA', 'P26F', ['FREP', 'TAX'], 'test', 4),
    ('2026-10-21', 'PGDA', 'P26F', ['CGAU'], 'exam', 1),
    ('2026-10-22', 'PGDA', 'P26F', ['TAX'], 'exam', 1),
    ('2026-10-26', 'PGDA', 'P26F', ['MACF'], 'exam', 1),
    ('2026-10-27', 'PGDA', 'P26F', ['FREP'], 'exam', 1),
    ('2026-08-19', 'PGDA', 'P26S', ['FREP', 'TAX'], 'test', 1),
    ('2026-08-20', 'PGDA', 'P26S', ['MACF', 'CGAU'], 'test', 1),
    ('2026-10-12', 'PGDA', 'P26S', ['MACF'], 'test', 2),
    ('2026-10-13', 'PGDA', 'P26S', ['TAX', 'CGAU'], 'test', 2),
    ('2026-11-10', 'BCTA', 'B26S', ['CGAU'], 'exam', 1),
    ('2026-11-11', 'BCTA', 'B26S', ['TAX'], 'exam', 1),
    ('2026-11-12', 'BCTA', 'B26S', ['MACF'], 'exam', 1),
]
for _on, _prog, _cohort, _mods, _kind, _seq in _MASTER:
    for _mod in _mods:
        CONFIRMED.setdefault((_prog, _cohort, _mod, _kind, _seq),
                             (D.fromisoformat(_on), 'Metanoia Master Calendar 2026'))

SPINE_ALIGNED_COHORTS = {'B26F', 'P26F'}

COHORT_STARTS = {
    'B26F': D(2026, 1, 26), 'B26S': D(2026, 7, 20),
    'P26F': D(2026, 2, 2), 'P26S': D(2026, 7, 20),
    'P25F': D(2026, 1, 12), 'P25S': D(2026, 1, 12),
}


def assessment_date(programme, cohort, module, kind, sequence, spine_event=None):
    """``(date, source, confirmed)`` for one assessment, or ``(None, '', False)``."""
    hit = CONFIRMED.get((programme, cohort, module, kind, sequence))
    if hit:
        return hit[0], hit[1], True
    if spine_event is not None and cohort in SPINE_ALIGNED_COHORTS:
        return spine_event.start.date(), f'Academic calendar: {spine_event.title}', False
    return None, '', False


SESSION_ROW = re.compile(
    r'^\|\s*(\d{1,2} [A-Z][a-z]{2} 2026)\s*\|\s*\w+\s*\|\s*MP (BCTA|PGDA) (\w+)\s*\|\s*(\d\d:\d\d)\s*\|\s*(\d\d:\d\d)\s*\|')


def coaching_sessions(text):
    """Every session row in a coaching schedule: ``(date, programme, cohort, start, end)``.

    The BCTA schedules label every session "B26F", but the July (B26S) intake
    took over the evening slot from July — so a BCTA session from 1 July on is
    filed under B26S, which is the class that was actually in the room.
    """
    out = []
    for line in (text or '').splitlines():
        m = SESSION_ROW.match(line.strip())
        if not m:
            continue
        day = dt.datetime.strptime(m.group(1), '%d %b %Y').date()
        programme, cohort = m.group(2), m.group(3)
        if programme == 'BCTA' and cohort == 'B26F' and day >= D(2026, 7, 1):
            cohort = 'B26S'
        start = dt.time.fromisoformat(m.group(4))
        end = dt.time.fromisoformat(m.group(5))
        out.append((day, programme, cohort, start, end))
    return out


MASTER_ROW = re.compile(
    r'^\|\s*(?P<date>\d{1,2}\s+[A-Za-z]{3,9}\s+2026|2026-\d\d-\d\d)\s*\|(?P<rest>.+)\|\s*$')


def load_master_calendar(path):
    """Read a master calendar ``.md`` and let its dates override the table above.

    Any Markdown table row that starts with a 2026 date and names a programme,
    an intake, a module and an assessment is understood, in any column order::

        | 17 Aug 2026 | BCTA | B26S | CGAU | Test 1 |
        | 2026-10-22  | PGDA P26F TAXA Final exam |

    Returns how many assessment dates were loaded.
    """
    loaded = 0
    for line in open(path, encoding='utf-8', errors='replace'):
        m = MASTER_ROW.match(line.strip())
        if not m:
            continue
        raw = m.group('date')
        try:
            day = (dt.date.fromisoformat(raw) if raw[:4] == '2026'
                   else dt.datetime.strptime(' '.join(raw.split()[:2]) + ' 2026', '%d %b %Y').date())
        except ValueError:
            try:
                day = dt.datetime.strptime(raw, '%d %B %Y').date()
            except ValueError:
                continue
        rest = m.group('rest').upper()
        programme = 'BCTA' if 'BCTA' in rest else 'PGDA' if 'PGDA' in rest else None
        cohort = re.search(r'\b([BP]\d\d[SF])\b', rest)
        module = next((code for code, rx in (('CGAU', r'CGAU|\bAUD\b'), ('MACF', r'MACF|\bMAF\b'),
                                             ('FREP', r'FREP'), ('TAX', r'TAXA?\b'))
                       if re.search(rx, rest)), None)
        test = re.search(r'\bTEST\s*([1-4])\b', rest)
        exam = re.search(r'\bFINAL\b|\bEXAM\b', rest)
        if not (programme and cohort and module and (test or exam)):
            continue
        kind, seq = ('test', int(test.group(1))) if test else ('exam', 1)
        CONFIRMED[(programme, cohort.group(1), module, kind, seq)] = (day, f'Master calendar: {os.path.basename(path)}')
        loaded += 1
    return loaded
