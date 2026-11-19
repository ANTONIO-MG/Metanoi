"""Draw a :class:`~apps.reports.models.Certificate` as artwork.

:func:`render_image` composes the whole certificate; the PNG, the PDF and the
on-screen preview are all that same image, deliberately, so the download can
never disagree with the preview. Everything is positioned as a fraction of the
canvas, so ``scale`` only changes crispness.

Fonts are bundled under ``static/fonts/certificate/`` (Montserrat, SIL Open Font
License) rather than taken from the host, so a Linux server renders exactly what
a designer sees on a Mac. The brand artwork comes from ``static/images/brand/``.

Geometry and palette:

``BASE_W`` / ``BASE_H``
    A4 landscape at 150 dpi; everything else is a fraction of these two numbers.
``NAVY`` … ``PAPER``
    The Metanoia palette (Brand Identity Book, page 26); ``INK`` is the text colour.
``FONT_FILES``
    The bundled fonts by role. ``script`` is the holder's-name key, kept under
    that name although it is now Montserrat.
``MARGIN``
    How far the text column may run: the holder's name is fitted to the width
    between these two fractions of the sheet.
``CARD_INSET`` / ``PAD``
    The card's inset from the sheet edge, and the text's inset from the card.
``LAYOUT``
    The vertical rhythm, as fractions of the sheet height.
"""

import io
import logging
import os

from django.conf import settings

logger = logging.getLogger('apps')

BASE_W, BASE_H = 1754, 1240

NAVY = (0x0B, 0x1F, 0x4D)
BLUE = (0x0B, 0x5F, 0xC6)
GREEN = (0x7D, 0xBA, 0x28)
GREEN_DEEP = (0x4F, 0x7F, 0x12)
GOLD = (0xF5, 0xC5, 0x18)
SLATE = (0x5B, 0x64, 0x75)
LINE = (0xDF, 0xE5, 0xEF)
PAPER = (0xFF, 0xFF, 0xFF)
INK = NAVY

FONT_DIR = os.path.join(settings.BASE_DIR, 'static', 'fonts', 'certificate')
FONT_FILES = {
    'script': 'Montserrat-ExtraBold.ttf',
    'extrabold': 'Montserrat-ExtraBold.ttf',
    'italic': 'Montserrat-ExtraBoldItalic.ttf',
    'bold': 'Montserrat-Bold.ttf',
    'semibold': 'Montserrat-SemiBold.ttf',
    'regular': 'Montserrat-Regular.ttf',
}
BRAND_DIR = os.path.join(settings.BASE_DIR, 'static', 'images', 'brand')


def font_path(name):
    """Absolute path to a bundled font, or ``None`` when it is missing."""
    path = os.path.join(FONT_DIR, FONT_FILES[name])
    return path if os.path.exists(path) else None


def _font(name, size):
    """Load a bundled font at ``size``, falling back to Pillow's default.

    The fallback keeps a certificate renderable on a checkout whose fonts were
    not fetched — it will look plain, but it will never raise mid-download.
    Pillow before 10.1 has no ``size`` argument for the default font.
    """
    from PIL import ImageFont
    path = font_path(name)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            logger.warning('reports: could not load certificate font %s', path)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def fonts_available():
    """True when every bundled font is present — used by the health check."""
    return all(font_path(name) for name in FONT_FILES)


def _fit_font(draw, text, name, start_size, max_width, min_size=10):
    """Largest size of ``name`` at which ``text`` still fits ``max_width``."""
    size = start_size
    while size > min_size:
        font = _font(name, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= max(1, size // 40)
    return _font(name, min_size)


def _wrap(draw, text, font, max_width):
    """Greedy word wrap to ``max_width``, returning a list of lines."""
    words = (text or '').split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f'{current} {word}'
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_left(draw, x, cy, text, font, fill, *, tracking=0):
    """Draw ``text`` from ``x``, vertically centred on ``cy``. Returns the end x."""
    if not text:
        return x
    if not tracking:
        draw.text((x, cy), text, font=font, fill=fill, anchor='lm')
        return x + draw.textlength(text, font=font)
    for ch in text:
        draw.text((x, cy), ch, font=font, fill=fill, anchor='lm')
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


MARGIN = 0.19

CARD_INSET = 0.040
PAD = 0.062

LAYOUT = {
    'logo_y': 0.105, 'logo_w': 0.175,
    'mark_y': 0.085, 'mark_w': 0.135,
    'kicker_y': 0.345, 'kicker_size': 0.022,
    'headline_y': 0.430, 'headline_size': 0.080,
    'presented_y': 0.520, 'presented_size': 0.024,
    'name_y': 0.590, 'name_size': 0.072,
    'rule_y': 0.645,
    'body_y': 0.690, 'body_size': 0.023, 'body_step': 0.033,
    'sig_rule_y': 0.795, 'sig_name_size': 0.022, 'sig_role_size': 0.019,
    'footer_y': 0.915,
}


def _brand_image(name):
    """A brand artwork file as an RGBA image, or ``None`` (logged) when it is missing."""
    from PIL import Image
    path = os.path.join(BRAND_DIR, name)
    try:
        return Image.open(path).convert('RGBA')
    except (OSError, ValueError):
        logger.warning('reports: certificate artwork %s is missing', path)
        return None


def _background(w, h):
    """The website's hero blue, top to bottom."""
    from PIL import Image
    top, bottom = (0xBC, 0xD1, 0xF0), (0xE8, 0xF0, 0xFB)
    column = Image.new('RGB', (1, 256))
    px = column.load()
    for y in range(256):
        t = y / 255
        px[0, y] = tuple(int(top[c] + (bottom[c] - top[c]) * t) for c in range(3))
    return column.resize((w, h), Image.BILINEAR)


def _card(w, h, box, radius):
    """The white card with the four-colour strip along its foot, as an RGBA
    layer the size of the sheet (transparent outside the card).

    The faint "Metanoia" wordmark behind everything, as on the Studio artwork, is
    drawn on the card itself so the card's rounded edge clips it.
    """
    from PIL import Image, ImageDraw

    x0, y0, x1, y1 = box
    layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    face = Image.new('RGBA', (x1 - x0, y1 - y0), PAPER + (255,))
    fd = ImageDraw.Draw(face)
    fd.text((face.size[0] - int(0.02 * w), face.size[1] - int(0.035 * h)), 'Metanoia',
            font=_font('extrabold', int(0.40 * h)), fill=(0xF2, 0xF5, 0xFA, 255), anchor='rs')
    strip_h = max(4, int(0.014 * h))
    cw = x1 - x0
    x = 0
    for share, colour in ((0.45, NAVY), (0.25, BLUE), (0.20, GREEN), (0.10, GOLD)):
        width = int(round(cw * share)) if colour != GOLD else cw - x
        fd.rectangle([x, (y1 - y0) - strip_h, x + width, (y1 - y0)], fill=colour)
        x += width
    mask = Image.new('L', face.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, face.size[0] - 1, face.size[1] - 1], radius=radius, fill=255)
    layer.paste(face, (x0, y0), mask)
    return layer


def _shadow(w, h, box, radius):
    """A soft navy drop shadow under the card, as an RGBA layer."""
    from PIL import Image, ImageDraw, ImageFilter
    shadow = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    off = int(0.012 * h)
    ImageDraw.Draw(shadow).rounded_rectangle([x0, y0 + off, x1, y1 + off], radius=radius, fill=NAVY + (70,))
    return shadow.filter(ImageFilter.GaussianBlur(int(0.02 * h) or 1))


def _paste_scaled(canvas, image, x, y, width, anchor='lt'):
    """Paste ``image`` scaled to ``width``; ``anchor`` 'lt' (left-top) or 'rt'."""
    from PIL import Image
    if image is None:
        return
    height = int(image.size[1] * width / image.size[0])
    scaled = image.resize((int(width), height), Image.LANCZOS)
    if anchor == 'rt':
        x = x - int(width)
    canvas.alpha_composite(scaled, (int(x), int(y)))


def render_image(data, scale=1.0):
    """Render the certificate described by ``data`` and return a Pillow image.

    ``data`` is the plain dict from :func:`apps.reports.certificates.render_data`
    — deliberately not a model, so this stays a pure drawing function.

    From top to bottom: the logo top left and the Alumni Mark top right, with a
    gold ring and a four-point gold sparkle (drawn, because Montserrat has no
    ✦); the ``METANOIA ALUMNI · <KIND>`` kicker; the "Welcome to the alumni."
    headline (one italic blue word, a green full stop); the holder's name,
    shrunk to fit if it is a long one, over a gold rule; the award wording,
    wrapped and capped at three lines so a very long course title can never
    push the signatures off the card; two signature blocks; the three pillars
    bottom right; and the verification footer — small, but the reason the
    certificate is checkable.
    """
    from PIL import ImageDraw

    w, h = int(BASE_W * scale), int(BASE_H * scale)
    canvas = _background(w, h).convert('RGBA')

    inset = int(CARD_INSET * w)
    box = (inset, inset, w - inset, h - inset)
    radius = int(0.028 * w)
    canvas.alpha_composite(_shadow(w, h, box, radius))
    canvas.alpha_composite(_card(w, h, box, radius))

    draw = ImageDraw.Draw(canvas)
    L = LAYOUT
    left = box[0] + int(PAD * w)
    right = box[2] - int(PAD * w)
    text_width = (1 - 2 * MARGIN) * w

    _paste_scaled(canvas, _brand_image('metanoia-logo-line.png'), left, L['logo_y'] * h, L['logo_w'] * w)
    mark_w = L['mark_w'] * w
    mark_x, mark_y = right, L['mark_y'] * h
    ring = int(0.006 * w)
    draw.ellipse([mark_x - mark_w - ring, mark_y - ring, mark_x + ring, mark_y + mark_w + ring],
                 outline=GOLD, width=max(2, int(0.0022 * w)))
    _paste_scaled(canvas, _brand_image('metanoia-alumni.png'), mark_x, mark_y, mark_w, anchor='rt')
    sx, sy, r = mark_x - mark_w - 0.024 * w, mark_y + 0.012 * h, 0.012 * w
    draw.polygon([(sx, sy - r), (sx + r * .28, sy - r * .28), (sx + r, sy), (sx + r * .28, sy + r * .28),
                  (sx, sy + r), (sx - r * .28, sy + r * .28), (sx - r, sy), (sx - r * .28, sy - r * .28)], fill=GOLD)

    kicker = _font('bold', int(L['kicker_size'] * h))
    kind = (data.get('kind_line') or '').upper()
    kicker_text = 'METANOIA ALUMNI' + (f'  ·  {kind}' if kind else '')
    _draw_left(draw, left, L['kicker_y'] * h, kicker_text, kicker, GREEN_DEEP, tracking=0.0028 * w)

    size = int(L['headline_size'] * h)
    x = _draw_left(draw, left, L['headline_y'] * h, 'Welcome to the ', _font('extrabold', size), NAVY)
    x = _draw_left(draw, x, L['headline_y'] * h, 'alumni', _font('italic', size), BLUE)
    _draw_left(draw, x + 0.002 * w, L['headline_y'] * h, '.', _font('extrabold', size), GREEN)

    presented = _font('regular', int(L['presented_size'] * h))
    _draw_left(draw, left, L['presented_y'] * h, 'This recognises', presented, SLATE)

    name_font = _fit_font(draw, data['student_name'], 'script', int(L['name_size'] * h), text_width)
    _draw_left(draw, left, L['name_y'] * h, data['student_name'], name_font, NAVY)

    rule_y = L['rule_y'] * h
    draw.line([(left, rule_y), (left + text_width, rule_y)], fill=GOLD, width=max(2, int(0.003 * h)))

    body = _font('regular', int(L['body_size'] * h))
    y = L['body_y'] * h
    for line in _wrap(draw, data['award_line'], body, text_width)[:3]:
        _draw_left(draw, left, y, line, body, SLATE)
        y += L['body_step'] * h

    sig_rule_y = L['sig_rule_y'] * h
    hairline = max(1, int(0.0016 * h))
    sig_role = _font('regular', int(L['sig_role_size'] * h))
    block = 0.20 * w
    for i, (who, role) in enumerate(data['signatures']):
        x0 = left + i * (block + 0.05 * w)
        draw.line([(x0, sig_rule_y), (x0 + block, sig_rule_y)], fill=NAVY, width=hairline)
        who_font = _fit_font(draw, who, 'bold', int(L['sig_name_size'] * h), block)
        _draw_left(draw, x0, sig_rule_y + 0.030 * h, who, who_font, NAVY)
        _draw_left(draw, x0, sig_rule_y + 0.062 * h, role.upper(), sig_role, SLATE, tracking=0.0012 * w)

    pillars = _font('bold', int(0.019 * h))
    words = [('EMPOWERING', NAVY), ('THROUGH', BLUE), ('TRANSFORMATION.', GREEN_DEEP)]
    track = 0.0024 * w
    gap = 0.012 * w
    widths = [sum(draw.textlength(ch, font=pillars) + track for ch in word) - track for word, _ in words]
    x = right - (sum(widths) + gap * 2)
    for (word, colour), width in zip(words, widths):
        _draw_left(draw, x, sig_rule_y + 0.046 * h, word, pillars, colour, tracking=track)
        x += width + gap

    footer = f"Certificate No. {data['number']}  ·  Verify at {data['verify_url']}"
    tiny = _fit_font(draw, footer, 'regular', int(0.016 * h), right - left, min_size=8)
    _draw_left(draw, left, L['footer_y'] * h, footer, tiny, SLATE)
    return canvas.convert('RGB')


def render_png(data, scale=1.0):
    """The certificate as PNG ``bytes``."""
    buf = io.BytesIO()
    render_image(data, scale=scale).save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def render_pdf(data, scale=2.0):
    """The certificate as a single-page A4-landscape PDF, in ``bytes``.

    Rendered from the same image as the PNG at twice the resolution. PDF
    metadata carries the certificate number and verification URL so the file
    stays identifiable away from the platform.
    """
    image = render_image(data, scale=scale)
    buf = io.BytesIO()
    image.save(
        buf, format='PDF', resolution=150.0 * scale,
        title=f"{data['title']} — {data['student_name']}",
        author=data['issuer'],
        subject=f"Certificate No. {data['number']}",
        keywords=f"certificate {data['number']} {data['verify_url']}",
    )
    return buf.getvalue()
