"""Write static/css/dark-twins.css: dark-theme twins for stylesheets that have none.

The hub's dark theme (``<html data-bs-theme="dark">``, UserSettings.theme) is
painted by brand.css and metanoia-ui.css, which re-point Bootstrap's variables.
Page stylesheets written later (lessons, the exam guard, the notification
reader, the Django admin…) paint their own white cards and navy text with
literal colours, so in dark mode they stayed white islands with navy-on-navy
text.

This command reads those files and, for every rule that paints a light
surface, navy text or a light border, emits the same selector under
``[data-bs-theme="dark"]`` with the dark equivalent. The twin's extra
attribute selector out-ranks the original, so load order does not matter;
``!important`` is carried across where the original used it. ``@media``
wrappers are kept. Inline ``<style>`` blocks in templates get the same
treatment (print documents and e-mails excepted).

Re-run after editing one of the source files:

    manage.py build_dark_twins          # writes the file
    manage.py build_dark_twins --check  # exits 1 if the file is stale

A file that already has hand-written dark rules is skipped (it is assumed to
handle itself); list it in FORCE to include it anyway.

Settings at the top of the module:

* ``TEMPLATE_SKIP`` — templates whose ``<style>`` is for paper or an e-mail
  client, never the dark hub.
* ``SKIP`` — stylesheets never twinned (vendor CSS, the theme files
  themselves, the output).
* ``FORCE`` — files with some dark rules that still leave light surfaces
  uncovered.
* ``SURFACE`` … ``LINE`` — the dark palette. ``SURFACE`` is a card on the dark
  page (Bootstrap's ``--bs-tertiary-bg`` in ``brand.css``).
* ``_WHITE`` … ``_LINES`` — patterns for the light values that get a twin.

At-rules other than ``@media`` / ``@supports`` (keyframes, font-face …) are
skipped whole.
"""

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

CSS_DIR = Path(settings.BASE_DIR) / 'static' / 'css'
TEMPLATE_DIR = Path(settings.BASE_DIR) / 'templates'
TEMPLATE_SKIP = ('packing-slip', 'receipt_pdf', 'certificate', 'mailbox/body', '_error_style', '/email/',
                 'invoice_pdf', '_paper')
OUT = CSS_DIR / 'dark-twins.css'
SKIP = {'vendor.css', 'vendor.min.css', 'style.css', 'style.min.css', 'brand.css', 'metanoia-ui.css',
        'dark-twins.css'}
FORCE = {'mailbox.css', 'metanoia-shop.css', 'chrome.css', 'commerce.css', 'people.css', 'broadcast.css'}

SURFACE = '#4A2E1D'
TINT = 'rgba(255, 255, 255, .05)'
TEXT = '#FFFFFF'
TEXT_2 = '#E8DED5'
MUTED = 'rgba(255, 255, 255, .64)'
LINE = 'rgba(255, 255, 255, .12)'

_WHITE = r'(?:#fff\b|#ffffff\b|white\b|rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*(?:1|0?\.[5-9]\d*)\s*\)|var\(--t-surface\))'
_TINTS = r'(?:#f[0-9a-f]{5}\b|#e[4-9a-f][0-9a-f]{4}\b|var\(--t-cloud\)|var\(--t-blue-10\)|var\(--t-gold-10\)|var\(--t-brown-10\)|var\(--nr-bg\))'
_NAVY = r'(?:#3E2415\b|var\(--t-brown\)|var\(--t-text\)|var\(--nr-ink\)|#1a2b5f\b|#3E2415\b)'
_NAVY_2 = r'(?:#5E4335\b|var\(--t-brown-80\)|#344767\b|#5E4335\b)'
_SLATE = r'(?:#6B5E55\b|var\(--t-slate\)|var\(--t-text-2\)|var\(--nr-soft\)|#67748e\b|#6c757d\b)'
_LINES = r'(?:#dfe5ef\b|#c6d1e2\b|#e9ecef\b|#eef1f6\b|#dee2e6\b|var\(--t-line\)|var\(--t-line-strong\)|var\(--nr-line\))'


def _twin(prop, value):
    """The dark declaration for one light one, or None."""
    v = value.lower()
    imp = ' !important' if '!important' in v else ''
    if prop in ('background', 'background-color'):
        if 'gradient' in v or 'url(' in v:
            return None
        if re.search(rf'^\s*{_WHITE}', v):
            return f'background-color: {SURFACE}{imp}'
        if re.search(rf'^\s*{_TINTS}', v):
            return f'background-color: {TINT}{imp}'
    elif prop == 'color':
        if re.search(rf'^\s*{_NAVY}', v):
            return f'color: {TEXT}{imp}'
        if re.search(rf'^\s*{_NAVY_2}', v):
            return f'color: {TEXT_2}{imp}'
        if re.search(rf'^\s*{_SLATE}', v):
            return f'color: {MUTED}{imp}'
    elif prop in ('border', 'border-top', 'border-bottom', 'border-left', 'border-right', 'border-color'):
        if re.search(_LINES, v) or (prop != 'border-color' and re.search(_WHITE, v) and 'solid' in v):
            side = '' if prop in ('border', 'border-color') else prop[len('border'):]
            return f'border{side}-color: {LINE}{imp}'
    return None


def _rules(css):
    """Yield (media, selector, body) for every plain rule, one level of @media deep."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    pos, n = 0, len(css)
    while pos < n:
        brace = css.find('{', pos)
        if brace == -1:
            return
        head = css[pos:brace].strip()
        if head.startswith('@media') or head.startswith('@supports'):
            depth, j = 1, brace + 1
            while j < n and depth:
                depth += {'{': 1, '}': -1}.get(css[j], 0)
                j += 1
            for _m, sel, body in _rules(css[brace + 1:j - 1]):
                yield head, sel, body
            pos = j
            continue
        end = css.find('}', brace)
        if head.startswith('@'):
            depth, j = 1, brace + 1
            while j < n and depth:
                depth += {'{': 1, '}': -1}.get(css[j], 0)
                j += 1
            pos = j
            continue
        yield None, head, css[brace + 1:end]
        pos = end + 1


def _prefix(selector):
    """Prefix each selector in a list with ``[data-bs-theme="dark"]``.

    Selectors already scoped to a theme, and ``:root`` / ``html`` ones, are dropped.
    """
    out = []
    for part in selector.split(','):
        part = part.strip()
        if not part or 'data-bs-theme' in part:
            continue
        if part.startswith((':root', 'html')):
            continue
        out.append(f'[data-bs-theme="dark"] {part}')
    return ',\n'.join(out)


def _twins(css):
    """The dark-twin rules (as CSS text) for every light-painting rule in ``css``."""
    out = []
    for media, selector, body in _rules(css):
        decls = []
        for decl in body.split(';'):
            if ':' not in decl:
                continue
            prop, value = decl.split(':', 1)
            twin = _twin(prop.strip().lower(), value.strip())
            if twin and twin not in decls:
                decls.append(twin)
        sel = _prefix(selector)
        if decls and sel:
            rule = f'{sel} {{ {"; ".join(decls)}; }}'
            out.append(f'{media} {{ {rule} }}' if media else rule)
    return out


def build():
    """Build the whole ``dark-twins.css`` text from the stylesheets and template ``<style>`` blocks."""
    chunks = ['/* GENERATED by `manage.py build_dark_twins` — do not edit by hand.\n'
              '   Dark-theme twins for page stylesheets that paint light surfaces with\n'
              '   literal colours. See apps/accounts/management/commands/build_dark_twins.py. */\n']
    for path in sorted(CSS_DIR.glob('*.css')):
        if path.name in SKIP:
            continue
        css = path.read_text(encoding='utf-8')
        if 'data-bs-theme' in css and path.name not in FORCE:
            continue
        out = _twins(css)
        if out:
            chunks.append(f'\n/* ---- {path.name} ---- */\n' + '\n'.join(out) + '\n')
    for path in sorted(TEMPLATE_DIR.rglob('*.html')):
        rel = str(path.relative_to(TEMPLATE_DIR))
        if any(word in rel for word in TEMPLATE_SKIP):
            continue
        source = path.read_text(encoding='utf-8', errors='replace')
        blocks = re.findall(r'(?is)<style[^>]*>(.*?)</style>', source)
        css = re.sub(r'{%.*?%}|{{.*?}}', '', '\n'.join(blocks), flags=re.S)
        if not css.strip() or 'data-bs-theme' in css:
            continue
        out = _twins(css)
        if out:
            chunks.append(f'\n/* ---- templates/{rel} ---- */\n' + '\n'.join(out) + '\n')
    return ''.join(chunks)


class Command(BaseCommand):
    """``manage.py build_dark_twins [--check]``."""
    help = 'Write static/css/dark-twins.css (dark-theme twins for page stylesheets).'

    def add_arguments(self, parser):
        """Add ``--check``."""
        parser.add_argument('--check', action='store_true', help='Fail if the file is out of date.')

    def handle(self, *args, check=False, **opts):
        """Write the file, or with ``--check`` fail if the file on disk is stale."""
        css = build()
        if check:
            if not OUT.exists() or OUT.read_text(encoding='utf-8') != css:
                raise CommandError('static/css/dark-twins.css is stale: run manage.py build_dark_twins')
            self.stdout.write('dark-twins.css is up to date.')
            return
        OUT.write_text(css, encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(f'Wrote {OUT.relative_to(settings.BASE_DIR)} '
                                             f'({css.count("{")} rules).'))
