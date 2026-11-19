"""Read the ``Metanoia Material`` archive into one clean list of documents.

The archive is a set of Google Drive exports, several of them split into
numbered zips ("… Project 2", "… Project 3") and some exported more than once.
Each file sits beside a ``<file>.json`` sidecar, and many binaries also have a
``<file>.md`` text extract. Fifteen different sidecar dialects are in use
(``material_type`` / ``document_type`` / ``doc_type`` / ``category`` …), so this
module normalises them onto one vocabulary:

    institution  MILPARK | SAICA | IAS | NMU
    programme    BCTA | PGDA | IAC
    module       CGAU | MACF | FREP | TAX
    cohort       B26S, P26F … ('' when the document does not say)
    assessment   ('test', 1) … ('exam', 1) for the Final … ('exam', 9) a board sitting
    kind         study_guide | blueprint | practice | answer_sheet | mock_exam |
                 mock_solution | past_paper | planner | coach_reference | …
    audience     students | staff

and folds duplicates (identical bytes) and binary/extract pairs into one
:class:`Document` each. Nothing here touches the database.

Rule tables
-----------
``SKIP_EXTENSIONS``
    File types that are never teaching material.
``KIND_ORDER``
    Teaching-material kinds, in the order a candidate meets them in a week.
``KIND_RULES``
    ``(kind, pattern)`` pairs. Order matters: the first rule that matches wins.
    The file name is tried first, then the sidecar's own type, then the folder —
    folders such as "Mock_Exams_and_Answer_Sheets" would otherwise label
    everything in them.
``MODULE_RULES``
    ``(module code, pattern)`` pairs for recognising the module.
``PRIVATE_RX``
    Documents about one person — never published, never seeded.
``EXTRACT_RX``
    The suffixes a text extract adds to its original's name.
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

SKIP_EXTENSIONS = {'.json', '.js', '.py', '.csv', '.zip', '.ds_store', '.wav', '.m4a'}

KIND_ORDER = {
    'study_guide': 10, 'recording': 15, 'blueprint': 20, 'practice': 30,
    'answer_sheet': 40, 'mock_exam': 50, 'mock_solution': 55, 'past_paper': 60,
    'planner': 70, 'coach_reference': 80,
}

KIND_RULES = [
    ('build', r'BUILD|SCRIPT|TOOLKIT|HELPER|PROMPT|README|CATALOGUE|MANIFEST|SUPPORTING_FIGURES|VALIDATION_CHECKLIST'),
    ('planner', r'PLANNER|ALLOCATION|SCHEDULE|CALENDAR|RUNNING.ORDER'),
    ('past_paper', r'PAST.?PAPER|PAST_PAPERS|OFFICIAL|ACTUAL.?(TEST|FINAL|PAPER)|BOARD_EXAMS|IAC.?(JUNE|JAN)|ITC_|EXAM.SCRIPT|STUDENT.EXAM|PRACTICE.PACK|QOSAT'),
    ('blueprint', r'BLUEPRINT'),
    ('mock_solution', r'MOCK.*(SOLUTION|ANSWER|MARK.?PLAN|MODEL)|(SOLUTION|ANSWER).*MOCK'),
    ('mock_exam', r'MOCK|UNSEEN|EXAM.QUESTION|INTEGRATION'),
    ('answer_sheet', r'SOLUTION|ANSWER|MARK.?PLAN|MODEL.ANSWER|MARKPLAN|MEMO|WORKINGS|MARKER'),
    ('practice', r'CHALLENGE|PRACTICE|QUESTION|EXERCISE|DRILL|QUIZ|WORKSHOP|CASE.STUDY'),
    ('study_guide', r'STUDY.?GUIDE|STUDENT.GUIDE|TOPIC.GUIDE|REVISION|NOTES|EXPLAINER|ONE.PAGER|REFERENCE.TABLE|KEY.CONCEPTS|SUMMARY|GUIDE|HANDOUT|TEACHING|COMPANION'),
    ('coach_reference', r'EVIDENCE|COACH|TUTOR|DIAGNOSTIC|ANALYSIS|POST.MORTEM|RECAP|FEEDBACK|\bPLAN\b|DELIVERABLE|BUILD.LOG|REMARK|VERIFIED.FIGURES'),
]

MODULE_RULES = [
    ('CGAU', r'CGAU|AUD_AUDITING|(^|[^A-Z])AUD([^A-Z]|$)|AUDITING|GOVERNANCE'),
    ('MACF', r'MACF|(^|[^A-Z])MAF([^A-Z]|$)|MANAGEMENT.ACCOUNTING|MANAGERIAL'),
    ('FREP', r'FREP|FINANCIAL.REPORTING'),
    ('TAX', r'TAXA|(^|[^A-Z])TAX([^A-Z]|$)|TAXATION'),
]

PRIVATE_RX = re.compile(r'REMARK|STUDENT_REMARK|PRIVATE|_LETTER|EXAM.SCRIPT', re.I)

EXTRACT_RX = re.compile(
    r'(\.(pdf|docx|xlsx|pptx|txt|jpg|jpeg|png))?(__?TEXT(_EXTRACT)?|__REBUILT_FROM_TEXT|\.text)?\.md$', re.I)


@dataclass
class Document:
    """One logical document: a binary and/or its text, plus normalised metadata.

    Fields worth knowing:

    * ``key`` — stable identity across re-runs (stem + metadata).
    * ``assessment`` — ``('test', 1)``, ``('exam', 1)`` or ``('general', 0)``.
    * ``date`` — ISO date the document was made or used, if known.
    * ``binary_path`` — absolute path of the original file.
    * ``text_path`` — absolute path of a content-bearing ``.md``.
    * ``text_kind`` — ``content``, ``card`` or ``thin`` (see :func:`md_text_kind`).
    """

    key: str
    title: str
    institution: str
    programme: str
    module: str
    cohort: str
    assessment: tuple
    week: int | None
    kind: str
    audience: str
    version: int
    date: str
    description: str
    needs_review: bool
    rights_note: str
    binary_path: str = ''
    text_path: str = ''
    text_kind: str = ''
    marks: str = ''
    topic: str = ''
    meta: dict = field(default_factory=dict)

    @property
    def file_name(self):
        """The original's file name (or the text extract's when there is no binary)."""
        return os.path.basename(self.binary_path or self.text_path)

    @property
    def is_student_facing(self):
        """True when the document is for students."""
        return self.audience == 'students'


def _g(meta, *keys):
    """The first of ``keys`` in ``meta`` with a real value, as a string (empty otherwise)."""
    for k in keys:
        v = meta.get(k)
        if v not in (None, '', 'None', '—', 'n/a', '-'):
            return str(v)
    return ''


def norm_institution(value, path):
    """The institution code (``SAICA``, ``NMU``, ``IAS``, ``MILPARK``) from metadata and path."""
    text = f'{value} {path}'.lower()
    if 'saica' in text or re.search(r'(^|/)iac|itc_|board_exams', text):
        return 'SAICA'
    if 'nelson' in text or re.search(r'(^|/)nmu(/|_)', text):
        return 'NMU'
    if value.strip().upper() == 'IAS' or text.startswith('ias/'):
        return 'IAS'
    if 'milpark' in text:
        return 'MILPARK'
    return ''


def norm_programme(value, path, name, institution):
    """The programme code (``IAC``, ``BCTA``, ``PGDA``), using a cohort code in the name as a last resort."""
    if institution == 'SAICA':
        return 'IAC'
    text = f'{value} {path}'.upper()
    if 'BCTA' in text or 'BRIDGING' in text:
        return 'BCTA'
    if 'PGDA' in text or 'POSTGRADUATE' in text:
        return 'PGDA'
    m = re.search(r'(?<![A-Z0-9])([BP])\d\d[SF](?![A-Z0-9])', name.upper().replace('_', ' '))
    if m:
        return 'BCTA' if m.group(1) == 'B' else 'PGDA'
    return ''


def norm_module(value, path, name):
    """The module code from the first of ``value``, ``name``, ``path`` that names one."""
    for text in (value, name, path):
        upper = text.upper()
        for code, rx in MODULE_RULES:
            if re.search(rx, upper):
                return code
    return ''


def norm_cohort(value, name, path):
    """The cohort code (``B26S``, ``P26F``…) from the first text that carries one."""
    for text in (value, name, path):
        m = re.search(r'(?<![A-Z0-9])([BP]\d\d[SF])(?![A-Z0-9])', text.upper().replace('_', ' '))
        if m:
            return m.group(1)
    return ''


def norm_assessment(value, name, path, programme):
    """``('test', n)``, ``('exam', 1)`` for the Final, or ``('general', 0)``."""
    for text in (value, name, path):
        upper = text.upper().replace('_', ' ')
        m = re.search(r'\bT(?:EST)?\s?([1-4])\b', upper)
        if m:
            return ('test', int(m.group(1)))
        if 'FINAL' in upper:
            return ('exam', 1)
        if programme == 'IAC' or re.search(r'\bIAC\b|\bITC\b|BOARD', upper):
            return ('exam', 1)
    return ('general', 0)


def norm_week(value, name):
    """The week number (1–19) named in ``value`` or ``name``, or ``None``."""
    for text in (value, name):
        m = re.search(r'(?<![A-Z])W(?:EEK)?\s?_?(\d{1,2})(?!\d)', text.upper().replace('_', ' '))
        if m and 0 < int(m.group(1)) < 20:
            return int(m.group(1))
    return None


def norm_kind(meta, name, path):
    """The material kind from the name, sidecar type, category and path (see :data:`KIND_RULES`)."""
    upper_name = name.upper()
    if upper_name.startswith('_INDEX') or 'MISSING' in upper_name or 'ORIGINALS_TO_COLLECT' in upper_name:
        return 'index'
    if re.search(r'PAYHIP|THUMBNAIL|MARKETING|DISTRIBUTION', upper_name + ' ' + path.upper()):
        return 'marketing'
    for blob in (upper_name,
                 _g(meta, 'material_type', 'document_type', 'doc_type', 'content_type', 'kind').upper(),
                 _g(meta, 'category').upper(), path.upper()):
        if not blob:
            continue
        for kind, rx in KIND_RULES:
            if re.search(rx, blob):
                return kind
    return 'other'


def norm_audience(meta, kind):
    """``staff`` for coach material, staff labels and private records, else ``students``."""
    label = _g(meta, 'audience', 'access').lower()
    if kind in ('build', 'coach_reference', 'planner'):
        return 'staff'
    if any(w in label for w in ('coach', 'tutor', 'internal')):
        return 'staff'
    if meta.get('private_student_record') in (True, 'True', 'true'):
        return 'staff'
    return 'students'


def norm_date(meta):
    """The first ISO date in the date-ish sidecar fields (day defaults to the 1st), or empty."""
    raw = _g(meta, 'source_date', 'date_created', 'date_built', 'created', 'created_at', 'exported_on')
    m = re.search(r'(20\d\d)-(\d\d)(?:-(\d\d))?', raw)
    if not m:
        return ''
    return f'{m.group(1)}-{m.group(2)}-{m.group(3) or "01"}'


def version_of(name, meta):
    """The document version from a ``_v2`` in the name or the sidecar, default 1."""
    m = re.search(r'[_ ]v(\d+)', name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'\d+', _g(meta, 'version'))
    return int(m.group(0)) if m else 1


def doc_stem(name):
    """Name shared by a binary and its text extract: X.pdf / X.pdf.md / X_TEXT.md → X.

    A leading 8-hex-digit prefix (Drive's hash-prefixed copies) is removed too.
    """
    name = re.sub(r'__REBUILT_FROM_TEXT', '', name, flags=re.I)
    if name.lower().endswith('.md'):
        base = EXTRACT_RX.sub('', name)
    else:
        base = os.path.splitext(name)[0]
    base = re.sub(r'(__?TEXT(_EXTRACT)?)$', '', base, flags=re.I)
    return re.sub(r'^[0-9a-f]{8}-', '', base)


def md_text_kind(text):
    """``content`` (real teaching text), ``card`` (a placeholder for a missing
    original) or ``thin`` (a metadata table and a line or two).

    Only the catalogue's own "| Field | Value |" table is dropped before counting:
    a schedule or a frequency table *is* the content.
    """
    if re.search(r'ORIGINAL_NOT_IN_ZIP|ORIGINAL_NOT_AVAILABLE|^## Retrieval', text, re.M) and len(text) < 6000:
        return 'card'
    body = re.sub(r'^\|\s*(Field|field)\s*\|\s*Value\s*\|.*?(?=^[^|]|\Z)', '', text, flags=re.M | re.S)
    body = re.sub(r'^\|\s*[a-z_]+\s*\|[^|\n]*\|\s*$', '', body, flags=re.M)
    return 'content' if len(body.split()) > 250 else 'thin'


def _load_sidecar(path):
    """The JSON metadata sidecar beside ``path`` (``.json`` / ``.meta.json``), or ``{}``."""
    for cand in (path + '.json', path + '.meta.json'):
        try:
            if os.path.getsize(cand):
                data = json.load(open(cand, encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


def scan(root):
    """``(documents, report)`` for the archive at ``root``.

    ``report`` counts what was skipped and why, so a run can say plainly that
    356 files arrived empty rather than silently importing less.

    Files are de-duplicated by content hash: one representative per unique file,
    with every copy's sidecar merged. A binary is then grouped with its text
    extract(s) by stem and module hint.
    """
    by_hash = defaultdict(list)
    report = defaultdict(int)
    empty = set()

    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if name.startswith('.') or ext in SKIP_EXTENSIONS or name.endswith('.PENDING.json'):
                continue
            path = os.path.join(dirpath, name)
            size = os.path.getsize(path)
            if size == 0:
                empty.add(name)
                continue
            with open(path, 'rb') as fh:
                digest = hashlib.md5(fh.read()).hexdigest()
            by_hash[digest].append(path)

    files = []
    for digest, paths in by_hash.items():
        paths.sort(key=lambda p: (-len(_load_sidecar(p)), len(p)))
        meta = {}
        for p in reversed(paths):
            meta.update({k: v for k, v in _load_sidecar(p).items() if v not in (None, '')})
        files.append((paths[0], meta, digest))
    report['unique_files'] = len(files)
    report['empty_files'] = len(empty - {os.path.basename(p) for p, _m, _d in files})

    groups = defaultdict(list)
    for path, meta, digest in files:
        rel = os.path.relpath(path, root)
        rel_wo_top = rel.split('/', 1)[1] if '/' in rel else rel
        folder = os.path.dirname(rel_wo_top)
        groups[(doc_stem(os.path.basename(path)), _module_hint(meta, rel_wo_top))].append(
            (path, rel_wo_top, folder, meta, digest))

    documents = []
    for (stem, _hint), members in groups.items():
        doc = _build_document(stem, members, report)
        if doc is not None:
            documents.append(doc)

    documents = _drop_superseded(documents, report)
    documents.sort(key=lambda d: (d.institution, d.programme, d.module, d.cohort,
                                  d.assessment, d.week or 0, KIND_ORDER.get(d.kind, 99), d.title))
    report['documents'] = len(documents)
    return documents, dict(report)


def _module_hint(meta, path):
    """The module the metadata or path names; part of the grouping key."""
    return norm_module(_g(meta, 'module_code', 'module'), path, '')


def _build_document(stem, members, report):
    """One :class:`Document` from a stem's files, or ``None`` with the reason counted in ``report``."""
    binaries = [m for m in members if not m[0].lower().endswith('.md')]
    texts = [m for m in members if m[0].lower().endswith('.md')]

    text_path, text_kind = '', ''
    for path, *_rest in sorted(texts, key=lambda m: -os.path.getsize(m[0])):
        kind = md_text_kind(open(path, encoding='utf-8', errors='replace').read())
        if kind == 'content':
            text_path, text_kind = path, kind
            break
        text_kind = text_kind or kind

    primary = binaries[0] if binaries else texts[0]
    path, rel, folder, meta, _digest = primary
    merged = {}
    for m in reversed(members):
        merged.update({k: v for k, v in m[3].items() if v not in (None, '')})
    name = os.path.basename(path)

    if not binaries and text_kind != 'content':
        report['skipped_placeholder_cards' if text_kind == 'card' else 'skipped_thin_text'] += 1
        return None

    kind = norm_kind(merged, name, rel)
    if kind in ('build', 'index', 'marketing', 'other'):
        report[f'skipped_{kind}'] += 1
        return None
    if PRIVATE_RX.search(name) or merged.get('private_student_record') in (True, 'True', 'true'):
        report['skipped_private_student_record'] += 1
        return None

    institution = norm_institution(_g(merged, 'institution'), rel)
    programme = norm_programme(_g(merged, 'programme', 'program'), rel, name, institution)
    module = norm_module(_g(merged, 'module_code', 'module'), rel, name)
    if programme == 'IAC':
        module = 'IAC'
    if not (institution and programme and module):
        report['skipped_unplaced'] += 1
        return None

    cohort = norm_cohort(_g(merged, 'cohort'), name, rel)
    title = _g(merged, 'title') or stem.replace('_', ' ')
    title = re.sub(r'\s+', ' ', title).strip()[:200]
    rights = _g(merged, 'rights_note', 'ownership')
    return Document(
        key=f'{institution}/{programme}/{module}/{stem}'[:130],
        title=title,
        institution=institution,
        programme=programme,
        module=module,
        cohort=cohort,
        assessment=norm_assessment(_g(merged, 'assessment'), name, rel, programme),
        week=norm_week(_g(merged, 'week'), name),
        kind=kind,
        audience=norm_audience(merged, kind),
        version=version_of(name, merged),
        date=norm_date(merged),
        description=_g(merged, 'description', 'notes', 'note')[:600],
        needs_review=merged.get('needs_review') in (True, 'True', 'true'),
        rights_note=rights,
        binary_path=binaries[0][0] if binaries else '',
        text_path=text_path,
        text_kind=text_kind,
        marks=_g(merged, 'marks'),
        topic=_g(merged, 'topic'),
        meta=merged,
    )


def _drop_superseded(documents, report):
    """Keep the newest version of a document; v1 of a v2 is history, not content."""
    def family(doc):
        """Identity of a document regardless of its version suffix."""
        return (doc.institution, doc.programme, doc.module,
                re.sub(r'[_ ]v\d+.*$', '', doc.key, flags=re.I))

    best = {}
    for doc in documents:
        fam = family(doc)
        if fam not in best or doc.version > best[fam].version:
            best[fam] = doc
    kept = [d for d in documents if best[family(d)] is d]
    report['skipped_older_versions'] = len(documents) - len(kept)
    return kept
