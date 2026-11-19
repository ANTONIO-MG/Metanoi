"""Per-block presentation for the lesson builder.

Styling is a small JSON dict on :attr:`~apps.learning.models.LessonBlock.style`,
rendered as an inline ``style=""`` attribute.

**Author-supplied CSS never reaches the page.** Every key must be in
:data:`FIELDS` and every value must match that key's whitelist (fixed options or
a tight regex); anything else is dropped, not escaped, so nothing like
``expression(...)``, ``url(javascript:…)`` or a stray ``}`` can survive.

The editor, player and export all read this one table.

``FIELDS``
    One spec per key: either ``values`` (a set of allowed literal values) or
    ``regex`` (a compiled pattern the value must match in full); ``css`` is the
    property it feeds.
``BORDERS`` / ``SHADOWS``
    Presets that expand to more than one declaration.
``ALLOWED_KEYS``
    Everything the editor may send; used to reject unknown keys on save.
"""

import re

FIELDS = {
    'align': {'values': {'left', 'center', 'right', 'justify'}, 'css': 'text-align'},
    'width': {'regex': re.compile(r'^(100|[1-9]\d?)%$|^auto$'), 'css': 'width'},
    'float': {'values': {'none', 'left', 'right'}, 'css': 'float'},
    'display': {'values': {'block', 'inline-block'}, 'css': 'display'},

    'font_family': {
        'values': {
            'inherit', 'system-ui, sans-serif', 'Georgia, serif',
            '"Times New Roman", serif', '"Courier New", monospace',
            'Verdana, sans-serif', '"Trebuchet MS", sans-serif',
            'Garamond, serif', '"Palatino Linotype", serif',
        },
        'css': 'font-family',
    },
    'font_size': {'regex': re.compile(r'^(?:[8-9]|[1-9]\d|1\d\d)px$'), 'css': 'font-size'},
    'font_weight': {'values': {'300', '400', '500', '600', '700', '800'}, 'css': 'font-weight'},
    'font_style': {'values': {'normal', 'italic'}, 'css': 'font-style'},
    'text_transform': {'values': {'none', 'uppercase', 'lowercase', 'capitalize'},
                       'css': 'text-transform'},
    'line_height': {'regex': re.compile(r'^[0-3](?:\.\d{1,2})?$'), 'css': 'line-height'},
    'letter_spacing': {'regex': re.compile(r'^-?[0-9](?:\.\d)?px$'), 'css': 'letter-spacing'},

    'color': {'regex': re.compile(r'^#[0-9a-fA-F]{6}$'), 'css': 'color'},
    'background': {'regex': re.compile(r'^#[0-9a-fA-F]{6}$'), 'css': 'background-color'},

    'padding': {'regex': re.compile(r'^(?:\d|[1-9]\d)px$'), 'css': 'padding'},
    'margin_top': {'regex': re.compile(r'^(?:\d|[1-9]\d)px$'), 'css': 'margin-top'},
    'margin_bottom': {'regex': re.compile(r'^(?:\d|[1-9]\d)px$'), 'css': 'margin-bottom'},
    'radius': {'regex': re.compile(r'^(?:\d|[1-9]\d)px$'), 'css': 'border-radius'},
}

BORDERS = {
    'none': '',
    'thin': 'border:1px solid rgba(0,0,0,.12)',
    'medium': 'border:2px solid rgba(0,0,0,.18)',
    'thick': 'border:4px solid rgba(0,0,0,.22)',
    'left-accent': 'border-left:4px solid currentColor; padding-left:14px',
}
SHADOWS = {
    'none': '',
    'sm': 'box-shadow:0 1px 3px rgba(0,0,0,.10)',
    'md': 'box-shadow:0 4px 14px rgba(0,0,0,.12)',
    'lg': 'box-shadow:0 12px 32px rgba(0,0,0,.16)',
}

ALLOWED_KEYS = set(FIELDS) | {'border', 'shadow'}


def clean(raw):
    """Return the subset of ``raw`` that is safe and recognised.

    Silently drops unknown keys and invalid values, so a malformed payload
    degrades to plain styling instead of erroring the save.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        if key not in ALLOWED_KEYS:
            continue
        value = str(value).strip()
        if not value:
            continue
        if key == 'border':
            if value in BORDERS:
                out[key] = value
            continue
        if key == 'shadow':
            if value in SHADOWS:
                out[key] = value
            continue
        spec = FIELDS[key]
        if 'values' in spec:
            if value in spec['values']:
                out[key] = value
        elif spec['regex'].match(value):
            out[key] = value
    return out


def to_css(style):
    """Render a cleaned style dict as an inline ``style`` attribute value.

    Re-cleans on the way out: rows written before a control was tightened, or
    edited straight in the database, still cannot inject anything.
    """
    style = clean(style)
    parts = []
    for key, value in style.items():
        if key == 'border':
            declaration = BORDERS.get(value, '')
        elif key == 'shadow':
            declaration = SHADOWS.get(value, '')
        else:
            declaration = f'{FIELDS[key]["css"]}:{value}'
        if declaration:
            parts.append(declaration)
    return '; '.join(parts)
