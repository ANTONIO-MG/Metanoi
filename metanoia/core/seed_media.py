"""Images for seeded content, drawn with Pillow into ``media/`` so seeds work
offline and do not depend on an image host.

Deterministic: the same spec always produces the same file, so re-running a seed
does not churn the repository.

The module-level colour constants (``INK``, ``MUTED``, ``LINE``, ``PAPER``,
``WASH`` and the accents ``CYAN``, ``GREEN``, ``AMBER``, ``INDIGO``, ``RED``)
are the same Metanoia brand palette the interface uses; ``TONES`` maps accent
names to them. ``HAVE_PIL`` is ``False`` when Pillow cannot be imported, in
which case drawing helpers return ``''`` instead of crashing a seed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger('apps')

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:  # pragma: no cover - Pillow is in requirements, but never crash a seed
    HAVE_PIL = False

INK = (52, 71, 103)
MUTED = (131, 146, 171)
LINE = (227, 230, 238)
PAPER = (255, 255, 255)
WASH = (248, 249, 254)
CYAN = (23, 193, 232)
GREEN = (45, 206, 137)
AMBER = (251, 140, 0)
INDIGO = (94, 114, 228)
RED = (245, 54, 92)

TONES = {'cyan': CYAN, 'green': GREEN, 'amber': AMBER, 'indigo': INDIGO, 'red': RED}


def _font(size, bold=False):
    """A real TrueType face when one is available, else Pillow's bitmap default."""
    candidates = [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf' if bold else '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/Library/Fonts/Arial.ttf',
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except Exception:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _media_path(relative):
    """Absolute path under ``MEDIA_ROOT`` for ``relative``, creating its folder."""
    target = Path(settings.MEDIA_ROOT) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _rounded(draw, box, radius, fill=None, outline=None, width=1):
    """Draw a rounded rectangle on ``draw``."""
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _text(draw, xy, text, font, fill=INK, anchor=None):
    """Draw ``text`` at ``xy`` in ``font``."""
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _wrap(draw, text, font, max_width):
    """Greedy word wrap to ``max_width`` pixels."""
    words, lines, line = text.split(), [], ''
    for word in words:
        trial = f'{line} {word}'.strip()
        if draw.textlength(trial, font=font) <= max_width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def wireframe(relative, title, blocks, *, tone='cyan', size=(1100, 660)):
    """Draw a labelled diagram of part of the interface.

    ``blocks`` is a list of dicts describing rectangles in a 0–100 coordinate
    space, so a layout reads like the thing it depicts::

        [{'x': 0,  'y': 0,  'w': 18, 'h': 100, 'label': 'Menu', 'kind': 'nav'},
         {'x': 20, 'y': 0,  'w': 80, 'h': 12,  'label': 'Search', 'kind': 'bar'},
         {'x': 20, 'y': 15, 'w': 52, 'h': 85,  'label': 'The lesson', 'kind': 'panel'},
         {'x': 74, 'y': 15, 'w': 26, 'h': 85,  'label': 'Your notes', 'kind': 'accent'}]

    ``kind`` picks the styling: ``nav`` (dark), ``bar`` (wash), ``panel``
    (white card), ``accent`` (tinted), ``row`` (a list row). The title is drawn
    as a caption strip along the top. Optional ``note`` adds smaller text under
    the label; optional ``cells`` (``[(text, x_percent), …]``) draws text at fixed
    x positions inside the block, so a table row lines up as a table instead of
    collapsing into a sentence.

    An existing file is never redrawn. Returns ``relative``, or ``''`` without
    Pillow.
    """
    if not HAVE_PIL:
        return ''
    target = _media_path(relative)
    if target.exists():
        return relative

    width, height = size
    accent = TONES.get(tone, CYAN)
    pad, header = 34, 76
    image = Image.new('RGB', size, WASH)
    draw = ImageDraw.Draw(image)

    title_font = _font(28, bold=True)
    _text(draw, (pad, 26), title, title_font, fill=INK)
    draw.line([(pad, header - 12), (width - pad, header - 12)], fill=LINE, width=2)

    stage = (pad, header, width - pad, height - pad)
    sw = stage[2] - stage[0]
    sh = stage[3] - stage[1]
    label_font = _font(19, bold=True)
    small_font = _font(15)

    for block in blocks:
        x0 = stage[0] + sw * block.get('x', 0) / 100
        y0 = stage[1] + sh * block.get('y', 0) / 100
        x1 = x0 + sw * block.get('w', 10) / 100
        y1 = y0 + sh * block.get('h', 10) / 100
        kind = block.get('kind', 'panel')

        if kind == 'nav':
            _rounded(draw, [x0, y0, x1, y1], 14, fill=INK)
            text_fill, sub_fill = PAPER, (196, 205, 222)
        elif kind == 'bar':
            _rounded(draw, [x0, y0, x1, y1], 12, fill=PAPER, outline=LINE, width=2)
            text_fill, sub_fill = MUTED, MUTED
        elif kind == 'accent':
            tint = tuple(int(c + (255 - c) * 0.86) for c in accent)
            _rounded(draw, [x0, y0, x1, y1], 14, fill=tint, outline=accent, width=2)
            text_fill, sub_fill = INK, MUTED
        elif kind == 'row':
            _rounded(draw, [x0, y0, x1, y1], 10, fill=PAPER, outline=LINE, width=2)
            draw.ellipse([x0 + 14, (y0 + y1) / 2 - 9, x0 + 32, (y0 + y1) / 2 + 9],
                         fill=tuple(int(c + (255 - c) * 0.75) for c in accent))
            text_fill, sub_fill = INK, MUTED
        else:
            _rounded(draw, [x0, y0, x1, y1], 14, fill=PAPER, outline=LINE, width=2)
            text_fill, sub_fill = INK, MUTED

        inset = 46 if kind == 'row' else 16
        label = block.get('label', '')
        note = block.get('note', '')

        cells = block.get('cells')
        if cells:
            cy = (y0 + y1) / 2 - 10
            for text, cx in cells:
                _text(draw, (x0 + inset + (x1 - x0 - inset) * cx / 100, cy),
                      str(text), label_font, fill=text_fill)
            continue

        if label:
            lines = _wrap(draw, label, label_font, (x1 - x0) - inset - 12)
            ty = y0 + 14 if not note and (y1 - y0) < 70 else y0 + 14
            if kind == 'row':
                ty = (y0 + y1) / 2 - 11
            for line in lines[:3]:
                _text(draw, (x0 + inset, ty), line, label_font, fill=text_fill)
                ty += 24
            if note:
                for line in _wrap(draw, note, small_font, (x1 - x0) - inset - 12)[:4]:
                    _text(draw, (x0 + inset, ty + 4), line, small_font, fill=sub_fill)
                    ty += 21

    image.save(target, 'PNG', optimize=True)
    return relative


def media_url(relative):
    """Public URL for a generated file, for use in a lesson block's ``data.url``."""
    if not relative:
        return ''
    return f"{settings.MEDIA_URL.rstrip('/')}/{relative.lstrip('/')}"
