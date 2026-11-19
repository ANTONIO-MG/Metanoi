"""Reading lessons as a Metanoia blog post.

A lesson that is *only* reading (blueprints, study guides, summaries) is
rendered as an article with a contents list instead of the section player.
Nothing stored is changed. Lessons with video, quiz, live class, lock or score
keep the player.

Module constants:

- ``ARTICLE_BLOCKS`` -- block types an article can show. Anything else (video,
  quiz, meeting, embed...) needs the full lesson player.
- ``_KEEP_UPPER_EXTRA`` -- upper-case words kept upper-case when a SHOUTED
  heading is put in sentence case.
- ``_COMMON`` -- short words that are ordinary English, not acronyms.
- ``_PROPER`` -- words that keep a capital in sentence case.
"""

import re

from django.utils.text import slugify

ARTICLE_BLOCKS = {'text', 'heading', 'callout', 'image', 'table', 'divider', 'reference', 'file', 'diagram'}

_KEEP_UPPER_EXTRA = {'IFRS', 'IAS', 'IFRIC', 'SOCIE', 'SPLOCI', 'SOFP', 'SOCI', 'NCI', 'PPE', 'CTA', 'PGDA',
                     'BCTA', 'FREP', 'MACF', 'CGAU', 'TAX', 'VAT', 'SARS', 'SAICA', 'ITC', 'APC', 'IRBA',
                     'ISA', 'ISAE', 'SPPI', 'FVOCI', 'FVPL', 'FVTPL', 'OCI', 'JE', 'JES', 'JNL', 'CGU', 'EPS', 'NPV',
                     'IRR', 'WACC', 'CAPM', 'KPI', 'KPIS', 'ESG', 'P&L', 'COS', 'SA', 'UK', 'NCI', 'RE', 'FX',
                     'CPI', 'GAAP', 'IASB', 'PV', 'FV', 'CF', 'DTA', 'DTL', 'ROU', 'CIT', 'PAYE', 'UIF', 'SDL',
                     'MCQ', 'QA', 'BEE', 'B-BBEE', 'SME', 'CFO', 'CEO', 'AGM', 'SOE', 'PFMA', 'MFMA', 'ISQM'}
_COMMON = {'A', 'AN', 'AND', 'ARE', 'AS', 'AT', 'BE', 'BY', 'DO', 'FOR', 'FROM', 'HOW', 'IF', 'IN', 'IS',
           'IT', 'ITS', 'NO', 'NOT', 'OF', 'OFF', 'ON', 'OR', 'OUT', 'SO', 'THE', 'TO', 'UP', 'US', 'WE',
           'WHAT', 'WHEN', 'WHY', 'WHO', 'YOU', 'YOUR', 'ALL', 'ANY', 'CAN', 'GET', 'HAS', 'NEW', 'NOW',
           'ONE', 'TWO', 'TOP', 'KEY', 'USE', 'WAY', 'WILL', 'WITH', 'THIS', 'THAT', 'THEM', 'THEY', 'THEN',
           'TOOK', 'TABLE', 'TEST', 'NEED', 'KNOW', 'BEFORE', 'AFTER', 'EACH', 'EVERY', 'MOST', 'MUST',
           'ONLY', 'OVER', 'PART', 'PLAN', 'RULE', 'RULES', 'MARK', 'MARKS', 'TIME', 'TIPS', 'STEP', 'DAY',
           'WEEK', 'EXAM', 'FINAL', 'FOCUS', 'THESE', 'THREE', 'FOUR', 'FIVE', 'BEST', 'SHOW', 'HERE'}


_PROPER = {'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september',
           'october', 'november', 'december', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
           'saturday', 'sunday', 'milpark', 'unisa', 'metanoia', 'saica', 'irba', 'sars', 'south', 'africa'}


def is_article(rows):
    """True when every part of the lesson is plain reading the article can show."""
    sections = [r for r in rows if r['kind'] == 'section']
    if not sections:
        return False
    for row in rows:
        if row.get('locked') or row.get('score'):
            return False
        section = row.get('section')
        if section is not None and (section.resolved_type != 'reading' or section.assessments):
            return False
        for block in row.get('blocks') or []:
            if block.block_type not in ARTICLE_BLOCKS:
                return False
    return True


def prepare(rows, lesson):
    """Give each row ``items`` to render and return the contents list.

    An item is ``{'html': …}`` for prose (sanitised, then restructured) or
    ``{'blocks': [block]}`` for anything the normal block partial draws (an
    image, a file, a diagram). The contents list is
    ``[{'id', 'label', 'kicker', 'level'}]`` — one entry per section heading.
    """
    from core.sanitize import clean_html

    toc, used = [], set()
    first = True
    for row in rows:
        items = []
        for block in row.get('blocks') or []:
            if block.block_type == 'text':
                html = clean_html((block.data or {}).get('html', ''))
                html, headings = transform(html, used, title=lesson.title if first else '')
                first = False
                toc.extend(headings)
                items.append({'html': html, 'block': block})
            else:
                items.append({'blocks': [block]})
        row['items'] = items
    return toc


def transform(html, used_ids=None, *, title=''):
    """``(html, headings)`` — imported guide HTML restructured as an article.

    The guide repeats its own title as a first bold line, which is dropped because
    the hero shows it. A short paragraph that is all bold capitals becomes a
    sub-heading.
    """
    from bs4 import BeautifulSoup

    used_ids = used_ids if used_ids is not None else set()
    soup = BeautifulSoup(html or '', 'html.parser')
    headings = []

    if title:
        first_p = soup.find('p')
        if first_p is not None and _similar(first_p.get_text(' ', strip=True), title):
            first_p.decompose()

    for box in soup.select('div.lesson-callout'):
        text = _clean(box.get_text(' ', strip=True))
        if not text:
            box.decompose()
            continue
        if _is_shouted_heading(box, text):
            headings.append(_to_heading(soup, box, text, used_ids, level=2))
        elif text.upper().startswith('METANOIA GROUP'):
            box['class'] = ['ar-source']
        else:
            _to_key_point(soup, box, text)

    for p in soup.find_all('p'):
        text = _clean(p.get_text(' ', strip=True))
        if text and len(text) <= 90 and _all_bold(p, text) and _mostly_upper(text):
            heading = _to_heading(soup, p, text, used_ids, level=3)
            if heading['level'] == 2:
                headings.append(heading)

    for table in soup.find_all('table'):
        table['class'] = ['ar-table__grid']
        wrap = soup.new_tag('div', attrs={'class': 'ar-table'})
        table.wrap(wrap)

    return str(soup), headings


def _clean(text):
    """Collapse whitespace (including non-breaking spaces) and strip."""
    return re.sub(r'\s+', ' ', (text or '').replace('\xa0', ' ')).strip()


def _similar(a, b):
    """True when one squashed string starts with, or contains, the other."""
    squash = lambda s: re.sub(r'[^0-9a-z]', '', (s or '').lower())  # noqa: E731
    a, b = squash(a), squash(b)
    return bool(a and b) and (a.startswith(b) or b.startswith(a) or b in a)


def _letters(text):
    """The alphabetic characters of ``text``."""
    return [c for c in text if c.isalpha()]


def _mostly_upper(text):
    """True when at least 80% of the letters are upper case."""
    letters = _letters(text)
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.8


def _all_bold(node, text):
    """True when bold text makes up (nearly) all of ``node``."""
    bold = _clean(' '.join(s.get_text(' ', strip=True) for s in node.find_all(['strong', 'b'])))
    return len(bold) >= 0.9 * len(text)


def _is_shouted_heading(box, text):
    """True for a short, mostly upper-case, all-bold line."""
    return len(text) <= 90 and _mostly_upper(text) and _all_bold(box, text)


def sentence_case(text):
    """``WHAT IS TESTED — TOPIC FREQUENCY`` → ``What is tested — topic frequency``,
    keeping acronyms (IFRS 3, SOCIE, P26F) as they are.

    Only real acronyms stay in capitals: known ones, codes with digits (P26F;
    the "12" of IAS 12 is untouched anyway), and vowel-less tokens (FX). The first
    letter is then capitalised.
    """
    words, out = text.split(' '), []
    for i, word in enumerate(words):
        core = re.sub(r'[^A-Za-z0-9&]', '', word)
        keep = core.isupper() and (
            any(c.isdigit() for c in core)
            or core in _KEEP_UPPER_EXTRA
            or (len(core) >= 2 and not re.search(r'[AEIOUY]', core)))
        if keep:
            out.append(word)
        elif core.lower() in _PROPER:
            out.append(word.capitalize())
        else:
            lowered = word.lower()
            out.append(lowered)
    sentence = ' '.join(out)
    for i, ch in enumerate(sentence):
        if ch.isalpha():
            return sentence[:i] + ch.upper() + sentence[i + 1:]
    return sentence


def _anchor(label, used_ids):
    """A unique anchor id for ``label``, recorded in ``used_ids``."""
    base = slugify(label)[:60] or 'section'
    anchor, n = base, 2
    while anchor in used_ids:
        anchor, n = f'{base}-{n}', n + 1
    used_ids.add(anchor)
    return anchor


def _to_heading(soup, node, text, used_ids, *, level):
    """Replace ``node`` with an ``<h2>``/``<h3>``; "SECTION 2: X" gets a kicker."""
    kicker = ''
    match = re.match(r'^(SECTION|PART|STEP)\s+(\d+)\s*[:.\-—–]\s*(.+)$', text, re.I)
    if match:
        kicker, text, level = f'{match.group(1).title()} {match.group(2)}', match.group(3), 2
    label = sentence_case(text.rstrip(':')) if _mostly_upper(text) else text.rstrip(':')
    anchor = _anchor(label, used_ids)
    heading = soup.new_tag(f'h{level}', attrs={'id': anchor, 'class': f'ar-h{level}'})
    if kicker:
        k = soup.new_tag('span', attrs={'class': 'ar-kicker'})
        k.string = kicker
        heading.append(k)
    heading.append(label)
    node.replace_with(heading)
    return {'id': anchor, 'label': label, 'kicker': kicker, 'level': level}


def _to_key_point(soup, box, text):
    """A callout with substance becomes a highlighted key point. A SHOUTED
    lead-in ("THE 3 RULES:") becomes its label."""
    box['class'] = ['ar-callout']
    match = re.match(r'^([A-Z0-9][A-Z0-9 &/\-—–()=~]{2,60}?)\s*:\s*(.+)$', text)
    if match and _mostly_upper(match.group(1)):
        label, rest = match.group(1), match.group(2)
        box.clear()
        tag = soup.new_tag('span', attrs={'class': 'ar-callout__label'})
        tag.string = sentence_case(label)
        body = soup.new_tag('p')
        body.string = sentence_case(rest) if _mostly_upper(rest) else rest
        box.append(tag)
        box.append(body)
