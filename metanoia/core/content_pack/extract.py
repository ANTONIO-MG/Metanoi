"""Extract text locally from PDFs, Word documents and Excel workbooks before a
model shapes it into a metanoia-pack.

Local extraction keeps a ``.xlsx`` mark grid a table (not guessed-at prose),
works with no API key, and makes an empty extraction visible instead of yielding
an invented pack. Nothing here talks to a model or writes to the database.

Limits:

:data:`SUPPORTED`
    Extensions this module knows how to read. A ``.zip`` is unpacked and its
    supported members are read (the common "crash-course pack" shape).
:data:`MAX_BYTES`
    A document bigger than this (40 MB) is refused rather than streamed into
    memory.
:data:`MAX_CHARS`
    Beyond this many characters a single document is truncated — well past any
    real study guide, and a guard against a runaway spreadsheet.

The per-format readers (``_from_zip``, ``_from_pdf``, ``_from_docx``, …) each
return ``(text, meta)``.
"""

import io
import logging
import zipfile

logger = logging.getLogger('apps')

SUPPORTED = ('.pdf', '.docx', '.xlsx', '.xlsm', '.txt', '.md', '.zip')

MAX_BYTES = 40 * 1024 * 1024

MAX_CHARS = 400_000


class ExtractionError(Exception):
    """The file could not be read — wrong type, corrupt, or empty."""


def _suffix(filename):
    """The lower-cased extension of ``filename`` (with the dot), or ``''``."""
    name = (filename or '').lower()
    return name[name.rfind('.'):] if '.' in name else ''


def extract(data, filename):
    """``(text, meta)`` for one document.

    ``meta`` records how it was read and how much came out, so a caller can tell
    a 3-page guide from a workbook that yielded four words.
    """
    if not data:
        raise ExtractionError(f'{filename} is empty.')
    if len(data) > MAX_BYTES:
        raise ExtractionError(
            f'{filename} is {len(data) // (1024 * 1024)} MB — the limit is '
            f'{MAX_BYTES // (1024 * 1024)} MB.')

    suffix = _suffix(filename)
    if suffix == '.zip':
        text, meta = _from_zip(data, filename)
    elif suffix == '.pdf':
        text, meta = _from_pdf(data, filename)
    elif suffix == '.docx':
        text, meta = _from_docx(data, filename)
    elif suffix in ('.xlsx', '.xlsm'):
        text, meta = _from_xlsx(data, filename)
    elif suffix in ('.txt', '.md'):
        text, meta = data.decode('utf-8', errors='replace'), {'kind': 'text'}
    else:
        raise ExtractionError(
            f'{filename}: cannot read "{suffix or "no extension"}". '
            f'Supported: {", ".join(SUPPORTED)}.')

    text = (text or '').strip()
    if not text:
        raise ExtractionError(
            f'{filename} produced no text. If it is a scan, it needs OCR first — '
            f'this reads documents, not images of them.')
    truncated = len(text) > MAX_CHARS
    meta.update({'filename': filename, 'chars': len(text), 'truncated': truncated})
    return text[:MAX_CHARS], meta


def _from_zip(data, filename):
    """Unpack a ``.zip`` and read every supported member into one blob.

    The common "crash-course pack" arrives zipped — the guide, the questions,
    the solution workbook, sometimes a revision sheet. macOS ``__MACOSX`` resource
    forks and hidden dot-files are skipped, a member that can't be read is noted
    inline rather than aborting the whole pack, and a nested ``.zip`` (e.g. a
    bundled handout) is descended one level only, to avoid zip-bomb recursion.
    Members are read through :func:`extract`, reusing the per-format readers.
    """
    parts, read, skipped = [], 0, []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionError(f'{filename} is not a readable .zip: {exc}')
    for info in zf.infolist():
        name = info.filename
        base = name.rsplit('/', 1)[-1]
        if info.is_dir() or name.startswith('__MACOSX/') or base.startswith('.') or not base:
            continue
        sfx = _suffix(base)
        if sfx not in SUPPORTED or sfx == '.zip':
            if sfx == '.zip':
                try:
                    inner = zf.read(info)
                    itext, _ = _from_zip_flat(inner, base)
                    if itext.strip():
                        parts.append(f'----- {base} -----\n{itext}')
                        read += 1
                        continue
                except Exception:
                    pass
            skipped.append(base)
            continue
        try:
            member = zf.read(info)
            text, _ = extract(member, base)
            parts.append(f'----- {base} -----\n{text}')
            read += 1
        except ExtractionError as exc:
            skipped.append(f'{base} ({exc})')
    if not parts:
        raise ExtractionError(
            f'{filename}: nothing readable inside. Skipped: {", ".join(skipped) or "(empty)"}.')
    return '\n\n'.join(parts), {'kind': 'zip', 'files': read, 'skipped': skipped}


def _from_zip_flat(data, filename):
    """Read supported members of an inner zip WITHOUT descending further."""
    parts = []
    zf = zipfile.ZipFile(io.BytesIO(data))
    for info in zf.infolist():
        base = info.filename.rsplit('/', 1)[-1]
        if info.is_dir() or info.filename.startswith('__MACOSX/') or base.startswith('.') or not base:
            continue
        if _suffix(base) in ('.pdf', '.docx', '.xlsx', '.xlsm', '.txt', '.md'):
            try:
                text, _ = extract(zf.read(info), base)
                parts.append(f'--- {base} ---\n{text}')
            except ExtractionError:
                continue
    return '\n\n'.join(parts), {'kind': 'zip-inner'}



def _from_pdf(data, filename):
    """Text of every non-empty PDF page, each under a page marker."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        raise ExtractionError('pypdf is not installed — cannot read PDFs.')
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or '') for page in reader.pages]
    except Exception as exc:
        raise ExtractionError(f'{filename} could not be read as a PDF — {exc}')
    body = '\n\n'.join(f'--- page {i + 1} ---\n{t}' for i, t in enumerate(pages) if t.strip())
    return body, {'kind': 'pdf', 'pages': len(pages)}


def _from_docx(data, filename):
    """Paragraphs, then tables, of a Word document.

    Tables carry the REQUIRED grid — the parts and their marks — so they are kept
    as pipe-delimited rows rather than being flattened into a paragraph.
    """
    try:
        import docx
    except ImportError:  # pragma: no cover
        raise ExtractionError('python-docx is not installed — cannot read .docx files.')
    try:
        document = docx.Document(io.BytesIO(data))
    except (zipfile.BadZipFile, Exception) as exc:
        raise ExtractionError(f'{filename} could not be read as a Word document — {exc}')

    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for index, table in enumerate(document.tables):
        rows = []
        for row in table.rows:
            cells = [c.text.replace('\n', ' ').strip() for c in row.cells]
            if any(cells):
                rows.append(' | '.join(cells))
        if rows:
            parts.append(f'\n--- table {index + 1} ---\n' + '\n'.join(rows))
    return '\n\n'.join(parts), {'kind': 'docx', 'tables': len(document.tables)}


def _from_xlsx(data, filename):
    """A workbook, sheet by sheet, as delimited rows.

    ``data_only=True`` reads the *cached values* rather than the formulas. The
    solution workbooks compute their figures — ``=SUM(D4:D9)`` — and the figure
    is what earns the mark, so the value is what matters. A workbook that has
    never been opened in Excel has no cached values, and that shows up here as
    blank cells rather than as silently wrong numbers.
    """
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        raise ExtractionError('openpyxl is not installed — cannot read spreadsheets.')
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise ExtractionError(f'{filename} could not be read as a workbook — {exc}')

    blocks, formula_only = [], False
    try:
        for sheet in book.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = ['' if v is None else str(v).replace('\n', ' ').strip() for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    rows.append(' | '.join(cells))
            if rows:
                blocks.append(f'--- sheet: {sheet.title} ---\n' + '\n'.join(rows))
    finally:
        book.close()

    text = '\n\n'.join(blocks)
    if text and '=' in text[:2000] and text.count('=') > text.count('|'):
        formula_only = True
    return text, {'kind': 'xlsx', 'sheets': len(blocks), 'formula_only': formula_only}


def extract_many(files):
    """``(combined_text, metas, errors)`` for several documents read as one pack.

    One unreadable file is reported in ``errors`` and does not stop the rest.
    """
    chunks, metas, errors = [], [], []
    for data, filename in files:
        try:
            text, meta = extract(data, filename)
        except ExtractionError as exc:
            errors.append(str(exc))
            continue
        metas.append(meta)
        chunks.append(f'===== FILE: {filename} ({meta["kind"]}) =====\n{text}')
    return '\n\n'.join(chunks), metas, errors
