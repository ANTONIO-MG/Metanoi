"""``{% load rich_text %}`` — render author-written HTML safely.

``{{ block.data.html|clean_html }}`` runs the value through the allowlist
sanitiser in :mod:`core.sanitize` and marks the result safe. Use it instead of
``|safe`` for any HTML a person (rather than the code) wrote.
"""

from django import template
from django.utils.safestring import mark_safe

from core.sanitize import clean_html as _clean_html
from core.sanitize import safe_url as _safe_url

register = template.Library()


@register.filter(name='clean_html', is_safe=True)
def clean_html(value):
    """Sanitise author-written HTML through the :mod:`core.sanitize` allowlist and mark
    it safe.
    """
    return mark_safe(_clean_html(value or ''))  # noqa: S308 - sanitised above


@register.filter(name='safe_url')
def safe_url(value):
    """Blank out a ``javascript:``/``data:``/… URL; keep http(s), mailto, tel, relative."""
    return _safe_url(value)
