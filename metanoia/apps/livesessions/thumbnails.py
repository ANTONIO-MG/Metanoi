"""Draw a session thumbnail in the Metanoia look.

Navy leads, with Strategy Blue and Growth Green as accents and Montserrat
throughout (docs/brand/METANOIA_BRAND_GUIDE.md). Each card carries what a student
scans for: the module and intake, the topic, what was covered, and the date and
time (SAST). The same 1280×720 image is the poster on the session's player, the
card on the module's Live sessions tab, and the YouTube thumbnail.

Pillow is already a hard dependency (ImageFields), so nothing new is installed.
If a background picture is configured (``LiveSessionSettings.thumbnail_background``)
it shows faintly under the navy; if anything is missing the card still draws.

Constants:

* ``SIZE`` -- 1280×720: YouTube's thumbnail size, and a clean 16:9 for the
  platform card.
* ``MARGIN`` -- the left/right gutter.
* The palette, as RGB tuples: ``NAVY`` (#3E2415, leads every design),
  ``NAVY_DEEP``, ``BLUE`` (#0B5FC6, Strategy Blue), ``BLUE_TINT`` (#5A93DC,
  blue text on navy at 4.9:1), ``BLUE_PALE`` (#A9C8EE), ``GREEN`` (#F2A900,
  Growth Green, 6.8:1 on navy), ``SLATE_TINT`` (#A48E80) and ``WHITE``.
"""

import io
import logging
from pathlib import Path

from django.conf import settings

logger = logging.getLogger('apps')

SIZE = (1280, 720)
MARGIN = 72

NAVY = (62, 36, 21)
NAVY_DEEP = (40, 22, 12)
BLUE = (11, 95, 198)
BLUE_TINT = (90, 147, 220)
BLUE_PALE = (169, 200, 238)
GREEN = (242, 169, 0)
SLATE_TINT = (164, 142, 128)
WHITE = (255, 255, 255)

_FONTS = Path(settings.BASE_DIR) / 'static' / 'fonts' / 'certificate'
_LOGO = Path(settings.BASE_DIR) / 'static' / 'images' / 'brand' / 'metanoia-logo-line-reversed.png'


def render(meeting, *, conf=None, topics=None):
    """Return the thumbnail as JPEG ``bytes``, or None if it could not be drawn.

    ``topics`` — what the session covered (the AI pack's ``topics_covered``);
    looked up from the pack when not given.

    The card is laid out top to bottom:

    * two soft rings off the right edge in blue and green -- depth without
      clutter, in the double tick's colours, overlapping "because we do this
      together";
    * a top row with the logo on the left and the session kind as a pill on
      the right;
    * a kicker (module code · programme · intake) over a short green rule;
    * the headline (the module, or programme / session title), up to two lines;
    * the topic line;
    * up to three "what was covered" chips -- short labels read best as chips,
      so they are preferred, keeping the pack's order among them;
    * a footer bar with the date and time (SAST) and the brand line.

    Pillow is a hard dependency, but if it cannot be imported the session must
    not crash, so this returns None.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:       # pragma: no cover - Pillow is a hard dep, but never crash a session
        logger.warning('thumbnails: Pillow unavailable — skipping')
        return None

    from core import clock
    from .models import LiveSessionSettings
    conf = conf or LiveSessionSettings.load()
    if topics is None:
        topics = topics_for(meeting)

    canvas = _background(conf, Image)
    draw = ImageDraw.Draw(canvas, 'RGBA')
    width = SIZE[0] - 2 * MARGIN

    rings = Image.new('RGBA', SIZE, (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(rings)
    ring_draw.ellipse([900, -220, 1500, 380], outline=BLUE + (70,), width=30)
    ring_draw.ellipse([1030, 270, 1450, 690], outline=GREEN + (55,), width=18)
    canvas.alpha_composite(rings)
    draw = ImageDraw.Draw(canvas, 'RGBA')

    _paste_logo(canvas, Image, (MARGIN, 52), height=58)
    kind = (meeting.get_session_kind_display() or 'Live session').upper()
    pill_font = _font(22, 'Bold')
    pill_w = _width(draw, kind, pill_font) + 40
    draw.rounded_rectangle([SIZE[0] - MARGIN - pill_w, 58, SIZE[0] - MARGIN, 102], radius=22, fill=GREEN)
    draw.text((SIZE[0] - MARGIN - pill_w + 20, 68), kind, font=pill_font, fill=NAVY)

    y = 178
    kicker = ' · '.join(x for x in _kicker_parts(meeting) if x)
    if kicker:
        draw.text((MARGIN, y), _fit(draw, kicker.upper(), _font(28, 'Bold'), width),
                  font=_font(28, 'Bold'), fill=BLUE_TINT)
        y += 46
    draw.rectangle([MARGIN, y, MARGIN + 84, y + 6], fill=GREEN)
    y += 26

    head_font = _font(66, 'ExtraBold')
    for line in _wrap(draw, _headline(meeting), head_font, width - 120, max_lines=2):
        draw.text((MARGIN, y), line, font=head_font, fill=WHITE)
        y += 80

    topic = _topic(meeting) or 'Session recording'
    if topic:
        draw.text((MARGIN, y + 4), _fit(draw, topic, _font(34, 'SemiBold'), width - 120),
                  font=_font(34, 'SemiBold'), fill=BLUE_PALE)
        y += 58

    chip_font = _font(23, 'SemiBold')
    x, chip_y = MARGIN, max(y + 14, 470)
    labels = [str(t).strip() for t in (topics or []) if str(t).strip()]
    labels = sorted(labels, key=lambda t: len(t) > 30)[:3]
    for label in labels:
        label = _fit(draw, label, chip_font, 380)
        w = _width(draw, label, chip_font) + 36
        if x + w > SIZE[0] - MARGIN:
            break
        draw.rounded_rectangle([x, chip_y, x + w, chip_y + 44], radius=22,
                               fill=NAVY_DEEP + (200,), outline=GREEN + (255,), width=2)
        draw.text((x + 18, chip_y + 9), label, font=chip_font, fill=WHITE)
        x += w + 12

    draw.rectangle([0, 632, SIZE[0], SIZE[1]], fill=NAVY_DEEP + (255,))
    draw.rectangle([0, 632, SIZE[0], 636], fill=BLUE)
    start, end = clock.local(meeting.scheduled_start), clock.local(meeting.scheduled_end)
    if start:
        when = f'{start:%a %d %b %Y} · {start:%H:%M}' + (f'–{end:%H:%M}' if end else '') + ' SAST'
        draw.text((MARGIN, 660), when, font=_font(30, 'Bold'), fill=WHITE)
    tagline = 'Empowering through transformation.'
    tag_font = _font(22, 'SemiBold')
    draw.text((SIZE[0] - MARGIN - _width(draw, tagline, tag_font), 666), tagline,
              font=tag_font, fill=SLATE_TINT)

    buffer = io.BytesIO()
    canvas.convert('RGB').save(buffer, format='JPEG', quality=88, optimize=True)
    return buffer.getvalue()


def store(meeting, *, topics=None):
    """Render and save onto ``meeting.thumbnail`` (replacing any old one). True if saved."""
    from django.core.files.base import ContentFile
    blob = render(meeting, topics=topics)
    if not blob:
        return False
    try:
        if meeting.thumbnail:
            meeting.thumbnail.delete(save=False)
        meeting.thumbnail.save(filename_for(meeting), ContentFile(blob), save=True)
        return True
    except Exception:
        logger.warning('thumbnails: could not store the thumbnail for %s', meeting, exc_info=True)
        return False


def topics_for(meeting):
    """The AI pack's ``topics_covered`` for this session, or []."""
    try:
        from apps.ai_assistant.models import AiReport
        report = AiReport.objects.filter(linked_ref=f'session-followup:{meeting.pk}').first()
        return list((report.data or {}).get('topics_covered') or []) if report else []
    except Exception:
        return []


def _kicker_parts(meeting):
    """The kicker's parts: module code, programme code and intake code ('' when absent)."""
    programme = meeting.resolved_programme
    return [
        meeting.module.code if meeting.module_id else '',
        getattr(programme, 'code', '') if programme is not None else '',
        meeting.cohort.code if meeting.cohort_id else '',
    ]


def _headline(meeting):
    """The headline: the module, else "One-on-one coaching", the programme or the title."""
    if meeting.module_id:
        return meeting.module.display_name
    programme = meeting.resolved_programme
    if meeting.audience == 'private':
        return 'One-on-one coaching'
    return getattr(programme, 'display_name', '') or meeting.title or 'Live session'


def _topic(meeting):
    """What the session was: its week, else the topic in a Teams subject, else the title."""
    if meeting.week_id:
        return meeting.week.display_title
    try:
        from apps.msteams.calendar_import import parse_subject
        placed = parse_subject(meeting.title)
    except Exception:
        placed = None
    if placed is not None:
        return placed.topic or ''
    return meeting.title if meeting.audience == 'private' or not meeting.module_id else ''


def filename_for(meeting):
    """The stored file name for ``meeting``'s thumbnail, derived from its title."""
    from apps.communication.models import _safe_folder_name
    return f'{_safe_folder_name(meeting.title, limit=80)}-thumbnail.jpg'


def _background(conf, Image):
    """Navy, with the configured background picture (if any) faint beneath it."""
    base = Image.new('RGBA', SIZE, NAVY + (255,))
    source = getattr(conf, 'thumbnail_background', None)
    if source:
        try:
            with source.open('rb') as fh:
                img = Image.open(fh)
                img.load()
            photo = _cover(img.convert('RGB'), Image).convert('RGBA')
            photo.putalpha(46)
            base.alpha_composite(photo)
        except Exception:
            logger.warning('thumbnails: could not read the configured background', exc_info=True)
    return base


def _paste_logo(canvas, Image, at, *, height):
    """Composite the reversed Metanoia logo at ``at``, scaled to ``height``; skipped if missing."""
    try:
        logo = Image.open(_LOGO).convert('RGBA')
    except Exception:
        return
    ratio = height / logo.height
    logo = logo.resize((int(logo.width * ratio), height), Image.LANCZOS)
    canvas.alpha_composite(logo, dest=at)


def _cover(img, Image):
    """Scale-and-crop to :data:`SIZE` — never stretch someone's artwork."""
    target_ratio = SIZE[0] / SIZE[1]
    width, height = img.size
    ratio = width / height if height else target_ratio
    if ratio > target_ratio:
        new_width = int(height * target_ratio)
        left = (width - new_width) // 2
        img = img.crop((left, 0, left + new_width, height))
    elif ratio < target_ratio:
        new_height = int(width / target_ratio)
        top = (height - new_height) // 2
        img = img.crop((0, top, width, top + new_height))
    return img.resize(SIZE, Image.LANCZOS)


def _font(size, weight='Bold'):
    """Montserrat at ``size`` (the brand face), else a fallback, else Pillow's default.

    The default font ignores ``size`` entirely, so on a box with no fonts the
    card comes out small but still legible — which beats failing to draw it.
    """
    from PIL import ImageFont
    candidates = [
        str(_FONTS / f'Montserrat-{weight}.ttf'),
        str(_FONTS / 'Montserrat-Bold.ttf'),
        getattr(settings, 'SESSION_THUMBNAIL_FONT', ''),
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    ]
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _width(draw, text, font):
    """The rendered width of ``text`` in ``font``, in pixels."""
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit(draw, text, font, max_width):
    """Truncate ``text`` with an ellipsis so it fits on one line."""
    if _width(draw, text, font) <= max_width:
        return text
    trimmed = text
    while trimmed and _width(draw, trimmed + '…', font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + '…') if trimmed else ''


def _wrap(draw, text, font, max_width, *, max_lines=2):
    """Greedy word-wrap, ellipsising whatever will not fit in ``max_lines``."""
    words, lines, current = str(text).split(), [], ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if _width(draw, candidate, font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and len(' '.join(lines).split()) < len(words):
        lines[-1] = _fit(draw, lines[-1] + ' …', font, max_width)
    return lines or ['']
