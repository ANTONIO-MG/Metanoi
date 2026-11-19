"""``{{ text|whatsapp }}`` — a WhatsApp message as it looks on the phone.

WhatsApp marks *bold*, _italic_, ~strike~ and ```monospace``` with symbols. The
text is escaped and linkified first; the markers are then applied to the text
between tags only, so a link's address is never touched.
"""
import re

from django import template
from django.utils.html import urlize
from django.utils.safestring import mark_safe

register = template.Library()

_TAG = re.compile(r'(<[^>]+>)')
_RULES = [
    (re.compile(r'```(.+?)```', re.S), r'<code>\1</code>'),
    (re.compile(r'(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])'), r'<b>\1</b>'),
    (re.compile(r'(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])'), r'<i>\1</i>'),
    (re.compile(r'(?<![\w~])~(?!\s)([^~\n]+?)(?<!\s)~(?![\w~])'), r'<s>\1</s>'),
]


@register.filter
def whatsapp(text):
    html = urlize(text or '', nofollow=True, autoescape=True)
    parts = _TAG.split(html)
    for i, part in enumerate(parts):
        if part.startswith('<'):
            continue
        for pattern, replacement in _RULES:
            part = pattern.sub(replacement, part)
        parts[i] = part
    return mark_safe(''.join(parts).replace('\n', '<br>'))
