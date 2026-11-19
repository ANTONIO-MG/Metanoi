"""The ``metanoia-upload/1`` format: one document, one sidecar, one text.

Every upload is three files with the same name::

    CGAU_B26S_T1_W1_Controls_StudyGuide_v1.pdf     the document students download
    CGAU_B26S_T1_W1_Controls_StudyGuide_v1.json    where it goes   (the sidecar)
    CGAU_B26S_T1_W1_Controls_StudyGuide_v1.md      what it says    (the text)

The sidecar places it — institution, programme, module, intake, which test,
which week — and says what kind of document it is. The ``.md`` follows the
template for that kind (``docs/upload_format/``), and when it does, it converts
to a lesson or a quiz **exactly, with no model involved**. When it does not (an
old Word export, a PDF with no ``.md``), Claude drafts the same content-pack
JSON instead, and a human reviews either draft before anything is imported.

This module is pure: it reads text and returns data. The queue
(``apps.learning.content_uploads``) owns the database.

``READING``, ``PAPERS`` and ``FILE_ONLY`` are the doc types that become a
lesson, a quiz, or only a placed file. ``TYPE_WORDS`` maps legacy sidecar
wording to a doc type, for the 15 dialects already in the archive.
"""

import html
import re
from decimal import Decimal, InvalidOperation

UPLOAD_VERSION = 'metanoia-upload/1'

DOC_TYPES = ('study_guide', 'blueprint', 'questions', 'mock_exam', 'past_paper',
             'answers', 'planner', 'recording', 'reference')
READING = ('study_guide', 'blueprint')
PAPERS = ('questions', 'mock_exam')
FILE_ONLY = ('past_paper', 'answers', 'reference')

TYPE_WORDS = [
    ('blueprint', r'BLUEPRINT'),
    ('past_paper', r'PAST.?PAPER|ACTUAL.?(TEST|PAPER|FINAL)'),
    ('mock_exam', r'MOCK'),
    ('answers', r'SOLUTION|ANSWER|MARK.?PLAN|MEMO'),
    ('questions', r'QUESTION|CHALLENGE|PRACTICE|QUIZ|Q&A'),
    ('planner', r'PLANNER|SCHEDULE|CALENDAR'),
    ('recording', r'RECORDING|LIVE.?SESSION|VIDEO'),
    ('reference', r'COACH|EVIDENCE|TUTOR|INTERNAL|REFERENCE'),
    ('study_guide', r'STUDY|GUIDE|NOTES|REVISION|SUMMARY'),
]


def normalise_sidecar(raw, *, filename=''):
    """Any sidecar (the standard one or a legacy dialect) → ``(meta, problems)``.

    ``meta`` keys: doc_type, title, institution, programme, module, cohort,
    assessment {kind, sequence}, week, week_title, topic {code, title},
    audience, used_on, marks, minutes, video_url, live_url, notes.
    """
    from core.metanoia_materials import catalogue as cat

    raw = raw if isinstance(raw, dict) else {}
    problems = []
    get = lambda *k: cat._g(raw, *k)                                    # noqa: E731

    doc_type = get('doc_type', 'type').lower()
    if doc_type not in DOC_TYPES:
        blob = ' '.join([get('doc_type', 'material_type', 'document_type', 'category', 'type'),
                         filename]).upper()
        doc_type = next((t for t, rx in TYPE_WORDS if re.search(rx, blob)), '')
    if not doc_type:
        problems.append(f'doc_type: say what this is — one of {", ".join(DOC_TYPES)}')

    institution = cat.norm_institution(get('institution'), '') or get('institution').upper()
    programme = cat.norm_programme(get('programme', 'program'), '', filename, institution) or \
        get('programme').upper()
    module = cat.norm_module(get('module', 'module_code'), '', filename)
    cohort = cat.norm_cohort(get('cohort', 'intake'), filename, '')

    assessment = raw.get('assessment')
    if isinstance(assessment, dict):
        kind = str(assessment.get('kind') or 'test').lower()
        try:
            sequence = int(assessment.get('sequence') or 1)
        except (TypeError, ValueError):
            sequence = 1
    else:
        kind, sequence = cat.norm_assessment(str(assessment or ''), filename, '', programme)
        if kind == 'general':
            kind, sequence = 'other', 1
    if kind not in ('test', 'exam', 'supp', 'other'):
        problems.append(f'assessment.kind: test, exam, supp or other — got {kind!r}')

    week = raw.get('week')
    try:
        week = int(re.sub(r'\D', '', str(week))) if week not in (None, '') else cat.norm_week('', filename)
    except ValueError:
        week = None

    topic = raw.get('topic')
    if isinstance(topic, dict):
        topic = {'code': str(topic.get('code') or '')[:20], 'title': str(topic.get('title') or '')[:250]}
    elif topic:
        topic = {'code': re.sub(r'\W+', '-', str(topic))[:20].strip('-').upper(), 'title': str(topic)[:250]}
    else:
        topic = None

    audience = get('audience', 'access').lower()
    audience = 'staff' if any(w in audience for w in ('staff', 'coach', 'tutor', 'internal')) \
        or doc_type == 'reference' else 'students'

    meta = {
        'format': UPLOAD_VERSION,
        'doc_type': doc_type,
        'title': (get('title') or re.sub(r'[_]+', ' ', re.sub(r'\.\w+$', '', filename))).strip()[:200],
        'institution': institution,
        'programme': programme,
        'module': module,
        'cohort': cohort,
        'assessment': {'kind': kind, 'sequence': sequence},
        'week': week,
        'week_title': get('week_title')[:200],
        'topic': topic,
        'audience': audience,
        'used_on': cat.norm_date({'source_date': get('used_on', 'date', 'source_date', 'date_created')}),
        'marks': _num(get('marks', 'total_marks')),
        'minutes': _num(get('minutes', 'time_limit_minutes')),
        'video_url': get('video_url', 'recording_url'),
        'live_url': get('live_url', 'meeting_url'),
        'notes': get('notes', 'description')[:1000],
    }
    for field in ('institution', 'programme', 'module'):
        if not meta[field]:
            problems.append(f'{field}: required to place the document')
    return meta, problems


def _num(value):
    """A float from a sidecar value, or ``None`` when blank or not a number."""
    try:
        return float(Decimal(str(value).strip())) if str(value).strip() else None
    except (InvalidOperation, ValueError):
        return None


def resolve_placement(meta):
    """``(offering, cohort, problems)`` for a normalised sidecar."""
    from apps.learning.models import Cohort, ProgrammeModule
    from core.metanoia_materials.seed import CATALOGUE

    problems = []
    offering = (ProgrammeModule.objects
                .filter(programme__institution__code__iexact=meta['institution'],
                        programme__code__iexact=meta['programme'])
                .filter(module__code=CATALOGUE.get(meta['module'], meta['module']))
                .select_related('programme__institution').first())
    if offering is None:
        offering = (ProgrammeModule.objects
                    .filter(programme__institution__code__iexact=meta['institution'],
                            programme__code__iexact=meta['programme'], code__iexact=meta['module'])
                    .first())
    if offering is None:
        problems.append(f'No {meta["module"]} module on {meta["institution"]} {meta["programme"]}.')
        return None, None, problems
    cohort = None
    if meta.get('cohort'):
        cohort = Cohort.objects.filter(programme=offering.programme, code__iexact=meta['cohort']).first()
        if cohort is None:
            problems.append(f'Intake {meta["cohort"]} does not exist on {offering.programme} — '
                            f'it will be placed on the shared plan instead.')
    return offering, cohort, problems


_H1 = re.compile(r'^#\s+(.+?)\s*$', re.M)
_H2 = re.compile(r'^##\s+(.+?)\s*$', re.M)
_QUESTION_RX = re.compile(r'^Question\s+\(?([a-z0-9]+(?:\)\s*\([ivx]+)?)\)?\s*[—–-]\s*([\d.]+)\s*marks?'
                          r'(?:\s*[—–-]\s*(MCQ|MULTI|COMPUTATION|WRITTEN))?', re.I)


def _sections(body):
    """``[(heading, text)]`` split on ``## `` headings."""
    parts = _H2.split(body)
    out = []
    if parts[0].strip():
        out.append(('', parts[0]))
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ''))
    return out


def _subsections(text):
    """``{'required': …, 'marking guide': …, 'options': …}`` from ``### `` headings."""
    found = {}
    pieces = re.split(r'^###\s+(.+?)\s*$', text, flags=re.M)
    for i in range(1, len(pieces), 2):
        found[pieces[i].strip().lower()] = pieces[i + 1].strip() if i + 1 < len(pieces) else ''
    return found


def _md_html(text):
    """Markdown body → HTML, with the materials text converter."""
    from core.metanoia_materials.textconv import _to_html
    return _to_html(text.strip())


def _table_rows(text):
    """Rows of the first Markdown pipe table in ``text``, without the header row."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('|') or re.match(r'^\|[\s:|-]+\|$', line):
            continue
        rows.append([c.strip() for c in line.strip('|').split('|')])
    return rows[1:] if rows else []


def looks_like_template(doc_type, text):
    """Whether ``text`` follows the template for ``doc_type`` closely enough to
    convert without a model."""
    if not text or not _H1.search(text):
        return False
    if doc_type in READING:
        return bool(_H2.search(text))
    if doc_type in PAPERS:
        return any(_QUESTION_RX.match(h) for h, _ in _sections(text))
    return False


def pack_from_template(text, meta, offering):
    """A template ``.md`` → ``(pack, problems)``. Never calls a model.

    A reading type becomes one section per ``##`` heading (a ``Video:`` or
    ``Recording:`` line becomes the section's video). A paper is ``## Scenario``
    then ``## Question (a) — 12 marks [— MCQ]`` headings; anything that is not a
    question (Scenario, Background, Instructions, Appendix …) is part of the case
    the candidate reads first.
    """
    title_match = _H1.search(text)
    title = title_match.group(1).strip() if title_match else meta['title']
    body = text[title_match.end():] if title_match else text
    doc_type = meta['doc_type']

    if doc_type in READING:
        sections = []
        for heading, chunk in _sections(body):
            if not heading:
                if chunk.strip():
                    sections.append({'heading': 'Overview', 'body_html': _md_html(chunk)})
                continue
            video = re.search(r'^\s*(?:Video|Recording):\s*(https?://\S+)\s*$', chunk, re.M | re.I)
            if video:
                chunk = chunk[:video.start()] + chunk[video.end():]
            section = {'heading': heading[:200], 'body_html': _md_html(chunk) or '<p></p>'}
            if video:
                section['video_url'] = video.group(1)
            sections.append(section)
        item = {'type': doc_type, 'title': title[:200], 'sections': sections,
                'audience': meta['audience']}
        return _wrap(meta, offering, [item]), []

    scenario, parts, problems = '', [], []
    for heading, chunk in _sections(body):
        q = _QUESTION_RX.match(heading)
        if not q:
            scenario += f'<h4>{html.escape(heading)}</h4>' if heading else ''
            scenario += _md_html(chunk)
            continue
        ref = re.sub(r'\s+', '', q.group(1)).lower()
        marks = float(q.group(2))
        style = (q.group(3) or '').upper()
        sub = _subsections(chunk)
        required = sub.get('required') or chunk.split('###')[0].strip()
        part = {'ref': ref[:12], 'required': required.strip(), 'marks': marks}
        options = sub.get('options')
        if options or style in ('MCQ', 'MULTI'):
            choices = [{'text': m.group(2).strip()[:400], 'correct': m.group(1).lower() == 'x'}
                       for m in re.finditer(r'^\s*[-*]\s*\[([ xX])\]\s*(.+)$', options or '', re.M)]
            part.update(marking='auto', choices=choices,
                        explanation=(sub.get('explanation') or '').strip())
        else:
            guide = next((v for k, v in sub.items() if k.startswith('marking guide')), '')
            rows = _table_rows(guide)
            computation = 'computation' in ' '.join(sub) or style == 'COMPUTATION'
            if computation:
                part['marking'] = 'auto'
                part['lines'] = [{'label': r[0], 'authority': r[1] if len(r) > 3 else '',
                                  'amount': _num(re.sub(r'[R\s,]', '', r[-2])) or 0,
                                  'marks': _num(r[-1]) or 0} for r in rows if len(r) >= 3]
            else:
                part['marking'] = 'ai_assisted'
                part['rubric'] = [{'point': (r[1] if len(r) >= 3 else r[0]),
                                   'authority': (r[2] if len(r) >= 4 else ''),
                                   'marks': _num(r[-1]) or 0}
                                  for r in rows if len(r) >= 2 and _num(r[-1])]
            if not (part.get('rubric') or part.get('lines')):
                problems.append(f'Question ({ref}): no marking guide rows found')
        parts.append(part)

    total = meta.get('marks') or sum(p['marks'] for p in parts)
    item_type = 'exam' if meta['doc_type'] == 'mock_exam' else (
        'quiz' if parts and all(p.get('choices') for p in parts) else 'challenge')
    item = {'type': item_type, 'title': title[:200], 'scenario_html': scenario or f'<p>{html.escape(title)}</p>',
            'total_marks': total, 'parts': parts, 'release_solution': 'on_submit',
            'audience': meta['audience']}
    if meta.get('minutes'):
        item['minutes'] = int(meta['minutes'])
    if item_type == 'challenge':
        item['week'] = meta.get('week') or 1
        item['scenario_key'] = re.sub(r'\W+', '-', title)[:60]
    return _wrap(meta, offering, [item]), problems


def _wrap(meta, offering, items):
    """The pack around ``items``, placed where the sidecar says."""
    from core.content_pack.schema import PACK_VERSION
    from core.content_pack.authoring import topic_code
    topic = meta.get('topic') or {}
    week = meta.get('week')
    kind, seq = meta['assessment']['kind'], meta['assessment']['sequence']
    code = topic_code(kind, seq, week) if week else (topic.get('code') or f'{kind[:1].upper()}{seq}')
    pack = {
        'pack': PACK_VERSION,
        'module': offering.code,
        'programme': f'{offering.programme.institution.code}-{offering.programme.code}',
        'topic': {'code': code[:20], 'title': (topic.get('title') or meta['title'])[:250]},
        'phase': {'kind': meta['assessment']['kind'], 'sequence': meta['assessment']['sequence']},
        'items': items,
        'audience': meta['audience'],
    }
    if meta.get('cohort'):
        pack['cohort'] = meta['cohort']
    if week:
        pack['week'] = week
    if meta.get('week_title'):
        pack['week_title'] = meta['week_title']
    return pack
