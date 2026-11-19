"""Turn a document's text into reading sections a lesson can show.

The ``.md`` extracts in the archive come from three places — Word exports
(headings and callout boxes rendered as one-cell tables), PDF text extracts, and
hand-written Markdown — and each marks its sections differently:

    ## 1. What is tested                       ← Markdown heading
    |  | **SECTION 1   ****The four words** |   ← a Word heading row
    | **WHAT YOU LEARN THIS WEEK** … |          ← a Word callout box

:func:`sections_from_markdown` finds whichever is there and splits on it, so a
twelve-page study guide becomes eight readable sections with a reading time
each rather than one wall of text. A metadata table at the top of an extract
(``| Field | Value |``) is dropped — it is catalogue data, not teaching.
"""

import html
import re

import markdown as md_lib

WORDS_PER_MINUTE = 200

_SECTION_ROW_RX = re.compile(r'^\|\s*\|\s*\*\*(?:SECTION\s+)?([0-9]+[A-Z]?)\.?\s*\**\s*(.+?)\**\s*\|\s*$', re.I)
_HEADING_RX = re.compile(r'^(#{1,3})\s+(.+?)\s*#*\s*$')
_META_TABLE_RX = re.compile(r'^\|\s*Field\s*\|\s*Value\s*\|', re.I)


def reading_minutes(text):
    """Whole minutes to read ``text`` at 200 wpm — never less than one."""
    words = len(re.sub(r'<[^>]+>', ' ', text or '').split())
    return max(1, round(words / WORDS_PER_MINUTE))


def _strip_front_matter(text):
    """Drop a leading metadata table, ``**File:**`` lines and retrieval notes."""
    lines = text.splitlines()
    out, skipping_table = [], False
    for line in lines:
        if _META_TABLE_RX.match(line):
            skipping_table = True
            continue
        if skipping_table:
            if line.startswith('|'):
                continue
            skipping_table = False
        if re.match(r'^\*\*(File|Status|In this zip|Notes)\:?\*\*', line):
            continue
        out.append(line)
    return '\n'.join(out).strip()


_ONE_CELL_RX = re.compile(r'^\|(?P<body>[^|\n]+(?:\|\s*)?)\|?\s*\n\|\s*-{3,}\s*\|\s*$', re.M)


def _clean_word_markup(text):
    """Repair what a Word → Markdown export leaves behind: bold runs split as
    ``**SECTION 1   ****The four words**``, and callout boxes exported as a
    one-cell table (``| **WHY THIS MATTERS** … |`` over ``| --- |``), which
    become a callout block instead of a grid.

    A section-heading row that survived as a two-cell table becomes bold text.
    """
    text = text.replace('****', ' ')
    text = _ONE_CELL_RX.sub(
        lambda m: '\n<div class="lesson-callout" markdown="1">\n\n'
                  + m.group('body').strip().strip('|').strip() + '\n\n</div>\n', text)
    text = re.sub(r'^\|\s*\|\s*(.+?)\s*\|\s*\n\|\s*-{3,}\s*\|\s*-{3,}\s*\|\s*$', r'**\1**', text, flags=re.M)
    return text


def _to_html(chunk):
    """Render a Markdown chunk to lesson HTML with Bootstrap tables.

    As defence in depth, ``script`` / ``style`` / ``iframe`` elements and ``on*``
    handlers are removed: nothing executable survives, whatever the source held.
    """
    rendered = md_lib.markdown(_clean_word_markup(chunk), extensions=['tables', 'sane_lists', 'md_in_html'])
    rendered = rendered.replace('<table>', '<table class="table table-sm table-bordered">')
    rendered = re.sub(r'<\s*(script|style|iframe)[^>]*>.*?<\s*/\s*\1\s*>', '', rendered, flags=re.S | re.I)
    rendered = re.sub(r'\son\w+\s*=\s*"[^"]*"', '', rendered)
    return rendered


def sections_from_markdown(text, *, default_title='Overview'):
    """``[{'heading', 'html', 'minutes'}]`` — one per section of the document.

    A single ``# Title`` at the top is the document title, not a section. A
    document with no headings at all is split into ~600-word parts so the reader
    still gets progress through it.
    """
    text = _strip_front_matter(text or '')
    if not text:
        return []

    sections, current_title, buffer = [], default_title, []

    def flush():
        """Close the current section if it holds at least three words."""
        body = '\n'.join(buffer).strip()
        if body and len(body.split()) >= 3:
            rendered = _to_html(body)
            sections.append({'heading': current_title[:200], 'html': rendered,
                             'minutes': reading_minutes(rendered)})

    first_heading_seen = False
    for line in text.splitlines():
        heading = None
        m = _HEADING_RX.match(line)
        if m and len(m.group(1)) >= 2:
            heading = m.group(2)
        elif m and not first_heading_seen:
            first_heading_seen = True
            continue
        else:
            m = _SECTION_ROW_RX.match(line)
            if m:
                heading = f'{m.group(1)}. {m.group(2)}'
        if heading:
            flush()
            current_title = re.sub(r'[*_`]+', '', heading).strip() or default_title
            buffer = []
            continue
        buffer.append(line)
    flush()

    if len(sections) == 1 and len(text.split()) > 1500:
        return _split_long(sections[0])
    return sections


def _split_long(section):
    """Split one long section into ~600-word parts at paragraph, table or list ends."""
    paragraphs = re.split(r'(?<=</p>)|(?<=</table>)|(?<=</ul>)', section['html'])
    parts, chunk, words = [], [], 0
    for para in paragraphs:
        chunk.append(para)
        words += len(re.sub(r'<[^>]+>', ' ', para).split())
        if words >= 600:
            parts.append(''.join(chunk))
            chunk, words = [], 0
    if chunk:
        parts.append(''.join(chunk))
    return [{'heading': f'{section["heading"]} — part {i + 1}', 'html': p, 'minutes': reading_minutes(p)}
            for i, p in enumerate(parts) if p.strip()]


def text_from_docx(path):
    """A Word document as Markdown-ish text: headings keep their level."""
    import docx
    document = docx.Document(path)
    lines = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or '').lower() if para.style is not None else ''
        level = re.search(r'heading\s*(\d)', style)
        if level:
            lines.append('#' * min(3, int(level.group(1)) + 1) + ' ' + text)
        elif style == 'title':
            lines.append('# ' + text)
        else:
            lines.append(text)
        lines.append('')
    for table in document.tables:
        rows = [[html.escape(c.text.strip()).replace('\n', ' ') for c in r.cells] for r in table.rows]
        if not rows:
            continue
        lines.append('| ' + ' | '.join(rows[0]) + ' |')
        lines.append('|' + '---|' * len(rows[0]))
        for row in rows[1:]:
            lines.append('| ' + ' | '.join(row) + ' |')
        lines.append('')
    return '\n'.join(lines)


def text_from_pdf(path, max_pages=60):
    """Plain text of a PDF, page by page. Empty for a scanned (image-only) PDF."""
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        pages = []
        for page in reader.pages[:max_pages]:
            pages.append(page.extract_text() or '')
        return '\n\n'.join(pages).strip()
    except Exception:
        return ''


def required_by_letter(text):
    """``{'a': 'Describe the risks …', 'b': …}`` from a paper's REQUIRED block.

    Reads both shapes the archive uses: the Word-export table
    (``| **(a)(i)** | Describe … | **6** |``) and PDF text, where the marker sits
    on its own line and the text and marks follow. Sub-parts of one letter are
    joined, so a workbook that marks (a)(i) and (a)(ii) together still finds the
    whole requirement.

    Trailing marks, and marks standing on their own line, are dropped from the
    text.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(r'^[|\s*]*REQUIRED[|\s*]*$', line.strip(), re.I)), None)
    if start is None:
        return {}
    found, letter, sub, buffer = {}, None, '', []

    def flush():
        """Store the buffered text under the current letter (sub-parts joined)."""
        if letter and buffer:
            body = re.sub(r'\s{2,}', ' ', ' '.join(buffer)).strip()
            body = re.sub(r'\s*\b\d{1,3}\s*$', '', body)
            if body:
                found[letter] = (found.get(letter, '') + '\n' + (f'{sub} ' if sub else '') + body).strip()

    for raw in lines[start + 1:]:
        line = raw.strip()
        if re.match(r'^[|*\s]*(TOTAL MARKS|END OF REQUIRED)', line, re.I):
            break
        cells = [c.strip(' *') for c in line.strip('|').split('|')] if line.startswith('|') else None
        head = cells[0] if cells else line
        m = re.match(r'^\(([a-z])\)(\([ivx]+\))?\s*(.*)$', head, re.I)
        if m:
            flush()
            letter, sub, buffer = m.group(1).lower(), m.group(2) or '', []
            rest = cells[1] if cells and len(cells) > 1 else m.group(3)
            if rest:
                buffer.append(re.sub(r'[*_]+', '', rest))
            continue
        if letter and line and not line.startswith('|'):
            if re.match(r'^\d{1,3}$', line):
                continue
            buffer.append(line)
    flush()
    return found
