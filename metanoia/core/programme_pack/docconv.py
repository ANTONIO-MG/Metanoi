"""Documents → lesson sections, in reading order.

A programme pack's Word files are built the same way every time: three
cover lines ("METANOIA GROUP", the title, a meta line), then ``Heading 1``
parts with ``Heading 2`` sub-parts, bullet lists, one-cell tables used as
callout boxes ("Floor first", "How to use this solution", "Weak answer 1") and
ordinary grids. :func:`docx_document` walks the body *in order* — paragraphs and
tables interleaved, which ``core.metanoia_materials.textconv`` does not do — and
returns the cover lines plus one section per ``Heading 1``, each already HTML in
the shape the lesson article (``apps/learning/article.py``) styles:

* a one-cell table → ``<div class="lesson-callout">`` (a highlighted key point);
* a grid → ``<table class="table table-sm table-bordered">`` (a floating card);
* ``Heading 2`` → ``<h4>``; bullets → ``<ul>``; bold and italic runs kept.

PDFs have no styles to read, so :func:`pdf_document` falls back to the text
layer: numbered lines ("1. Why this topic matters") open a section, page
footers are dropped. Good enough for a pack that only ships PDFs; a pack with
the Word originals beside them always reads the Word file.

``_BRAND_LINES`` matches the cover lines every pack document opens with (not
content); ``_FOOTER_RX`` matches the PDF page footers that are dropped.
"""

import html
import re

WORDS_PER_MINUTE = 200

_BRAND_LINES = re.compile(r'^(METANOIA(\s+GROUP)?)\s*$', re.I)
_FOOTER_RX = re.compile(r'^(Metanoia Group\s*\|.*|Page \d+|\d+)$', re.I)


def reading_minutes(text):
    """Estimated reading time of ``text`` (HTML tags ignored), at least one minute."""
    words = len(re.sub(r'<[^>]+>', ' ', text or '').split())
    return max(1, round(words / WORDS_PER_MINUTE))


def _esc(text):
    """HTML-escape ``text`` for element content (quotes left alone)."""
    return html.escape(text or '', quote=False)


def _runs_html(paragraph):
    """A paragraph's runs as HTML, keeping bold and italic."""
    out = []
    for run in paragraph.runs:
        text = run.text
        if not text:
            continue
        piece = _esc(text).replace('\n', '<br>')
        if run.bold:
            piece = f'<strong>{piece}</strong>'
        if run.italic:
            piece = f'<em>{piece}</em>'
        out.append(piece)
    joined = ''.join(out).replace('</strong><strong>', '').replace('</em><em>', '')
    return joined.strip() or _esc(paragraph.text.strip())


def _cells(row):
    """A row's distinct cells — Word repeats a merged cell once per grid column."""
    seen, cells = set(), []
    for cell in row.cells:
        key = id(cell._tc)
        if key in seen:
            continue
        seen.add(key)
        cells.append(cell)
    return cells


def _cell_html(cell):
    """A table cell's non-empty paragraphs as HTML, joined with ``<br>``."""
    parts = [_runs_html(p) for p in cell.paragraphs if p.text.strip()]
    return '<br>'.join(parts)


def _callout_html(cell):
    """A one-cell table: its first line is the label, the rest the body.

    A short label with no full stop ("FLOOR FIRST: …") is rendered as a shouted
    lead-in, which the article turns into the panel's label
    (``apps.learning.article._to_key_point``).
    """
    lines = [p for p in cell.paragraphs if p.text.strip()]
    if not lines:
        return ''
    label = lines[0].text.strip()
    body = [_runs_html(p) for p in lines[1:]]
    if len(lines) == 1:
        return f'<div class="lesson-callout"><p>{_runs_html(lines[0])}</p></div>'
    if len(label) <= 48 and not label.endswith('.'):
        first = f'<p><strong>{_esc(label.upper().rstrip(":"))}:</strong> {body[0]}</p>'
        rest = ''.join(f'<p>{b}</p>' for b in body[1:])
        return f'<div class="lesson-callout">{first}{rest}</div>'
    return ('<div class="lesson-callout">'
            f'<p><strong>{_esc(label)}</strong></p>' + ''.join(f'<p>{b}</p>' for b in body) + '</div>')


def _table_html(table):
    """A grid table as a Bootstrap table; the first non-empty row is the header."""
    rows = [[_cell_html(c) for c in _cells(r)] for r in table.rows]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ''
    head, body = rows[0], rows[1:]
    out = ['<table class="table table-sm table-bordered"><thead><tr>']
    out += [f'<th>{c}</th>' for c in head]
    out.append('</tr></thead><tbody>')
    for row in body:
        out.append('<tr>' + ''.join(f'<td>{c}</td>' for c in row) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def _heading_level(paragraph):
    """The paragraph's heading level (``0`` for the Title style), or ``None``."""
    style = (paragraph.style.name or '').lower() if paragraph.style is not None else ''
    match = re.match(r'heading\s*(\d)', style)
    if match:
        return int(match.group(1))
    if style == 'title':
        return 0
    return None


def docx_document(path):
    """``{'title', 'meta', 'intro_html', 'sections': [{'heading', 'html', 'minutes'}]}``.

    Parts are the ``Heading 1`` paragraphs — unless the document spends its one
    ``Heading 1`` on its own title and numbers its parts as ``Heading 2`` ("How to
    use this pack"), in which case ``Heading 2`` is the part level. A heading
    above the part level met before any content is the document's title; the
    first short, unpunctuated lines before any content are the cover (brand line,
    title, meta line). Content before the first part goes into ``intro_html``.
    """
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    cover, intro, sections = [], [], []
    current = None
    bullets = []

    levels = [_heading_level(Paragraph(el, document)) for el in document.element.body.iterchildren()
              if el.tag.endswith('}p') and Paragraph(el, document).text.strip()]
    part_level = 2 if levels.count(1) <= 1 and levels.count(2) >= 2 else 1

    def sink():
        """The list new HTML goes into: the current section, or the intro before the first part."""
        return current['parts'] if current is not None else intro

    def flush_bullets():
        """Emit the pending bullet paragraphs as one ``<ul>``."""
        if bullets:
            sink().append('<ul>' + ''.join(f'<li>{b}</li>' for b in bullets) + '</ul>')
            bullets.clear()

    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            level = _heading_level(paragraph)
            style = (paragraph.style.name or '').lower() if paragraph.style is not None else ''
            if level is not None and level < part_level:
                if current is None and not intro:
                    cover.insert(0, text)
                    continue
                flush_bullets()
                sink().append(f'<h3>{_esc(text)}</h3>')
                continue
            if level == part_level:
                flush_bullets()
                current = {'heading': text, 'parts': []}
                sections.append(current)
                continue
            if level is not None and level > part_level:
                flush_bullets()
                sink().append(f'<h4>{_esc(text)}</h4>')
                continue
            if (current is None and not intro and not bullets
                    and (_BRAND_LINES.match(text)
                         or (len(cover) < 3 and len(text) < 160 and not text.endswith('.')))):
                if not _BRAND_LINES.match(text):
                    cover.append(text)
                continue
            if 'list' in style:
                bullets.append(_runs_html(paragraph))
                continue
            flush_bullets()
            sink().append(f'<p>{_runs_html(paragraph)}</p>')
        elif tag == 'tbl':
            flush_bullets()
            table = Table(element, document)
            cells = [c for r in table.rows for c in _cells(r)]
            if len(table.rows) == 1 and len(cells) == 1:
                piece = _callout_html(cells[0])
            else:
                piece = _table_html(table)
            if piece:
                sink().append(piece)
    flush_bullets()

    out_sections = []
    for section in sections:
        body = ''.join(section['parts'])
        if not body.strip():
            continue
        out_sections.append({'heading': section['heading'][:200], 'html': body,
                             'minutes': reading_minutes(body)})
    return {
        'title': cover[0] if cover else '',
        'meta': ' · '.join(cover[1:]),
        'cover': cover,
        'intro_html': ''.join(intro),
        'sections': out_sections,
    }


_PDF_HEADING_RX = re.compile(r'^(\d{1,2})\.\s+([A-Z][^.]{2,80})$')


def pdf_document(path, *, max_pages=60, per_page=False):
    """The PDF's text layer as the same shape as :func:`docx_document`.

    ``per_page=True`` makes each page a section headed by its first real line —
    the right reading of a slide deck exported to PDF.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        pages = [(page.extract_text() or '') for page in reader.pages[:max_pages]]
    except Exception:
        pages = []
    cleaned = []
    for page in pages:
        lines = []
        for raw in page.splitlines():
            line = raw.strip()
            if not line or _FOOTER_RX.match(line) or _BRAND_LINES.match(line):
                continue
            lines.append(line)
        cleaned.append(lines)

    if not any(cleaned):
        return {'title': '', 'meta': '', 'intro_html': '', 'sections': []}

    first = cleaned[0]
    title = first[0] if first else ''

    def paragraphs(lines):
        """Re-join the PDF's hard-wrapped lines into paragraphs."""
        paras, buffer = [], []
        for line in lines:
            buffer.append(line)
            if line.endswith(('.', ':', '?', '!')) or len(line) < 50:
                paras.append(' '.join(buffer))
                buffer = []
        if buffer:
            paras.append(' '.join(buffer))
        return ''.join(f'<p>{_esc(p)}</p>' for p in paras)

    sections = []
    if per_page:
        for index, lines in enumerate(cleaned):
            if not lines:
                continue
            heading, body = lines[0], lines[1:]
            if index == 0:
                heading = 'Introduction'
                body = lines
            if body:
                html_body = paragraphs(body)
                sections.append({'heading': heading[:200], 'html': html_body,
                                 'minutes': reading_minutes(html_body)})
        return {'title': title, 'meta': '', 'intro_html': '', 'sections': sections}

    current, intro = None, []
    for lines in cleaned:
        for line in lines:
            match = _PDF_HEADING_RX.match(line)
            if match:
                current = {'heading': line, 'lines': []}
                sections.append(current)
                continue
            (current['lines'] if current is not None else intro).append(line)
    out = []
    for section in sections:
        body = paragraphs(section['lines'])
        if body:
            out.append({'heading': section['heading'][:200], 'html': body, 'minutes': reading_minutes(body)})
    if not out and intro:
        body = paragraphs(intro[1:])
        return {'title': title, 'meta': '', 'intro_html': '',
                'sections': [{'heading': 'Overview', 'html': body, 'minutes': reading_minutes(body)}]}
    return {'title': title, 'meta': intro[1] if len(intro) > 1 else '',
            'intro_html': paragraphs(intro[2:]) if len(intro) > 2 else '', 'sections': out}


def read_document(path, **kwargs):
    """Whichever reader suits ``path``; an empty document for anything else."""
    lower = str(path).lower()
    try:
        if lower.endswith('.docx'):
            return docx_document(path)
        if lower.endswith('.pdf'):
            return pdf_document(path, **kwargs)
    except Exception:
        pass
    return {'title': '', 'meta': '', 'intro_html': '', 'sections': []}


def plain_text(document, limit=None):
    """A document dict as plain text — what the AI step reads."""
    parts = [document.get('title', ''), document.get('meta', '')]
    for section in document.get('sections', []):
        parts.append(section['heading'])
        parts.append(re.sub(r'<[^>]+>', ' ', section['html']))
    text = re.sub(r'[ \t]+', ' ', '\n'.join(p for p in parts if p)).strip()
    return text[:limit] if limit else text
