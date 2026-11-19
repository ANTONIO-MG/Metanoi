"""Clickable links in shared text — see :mod:`apps.communication.links`.

``{{ text|linkify }}``        escaped, links made clickable, line breaks kept,
                              first link marked for a preview card.
``{{ text|linkify_inline }}`` the same without ``<br>`` (for ``pre-wrap`` text).
``{{ text|linkify_email }}``  the same for e-mail: absolute LMS links, no card.
"""
from django import template

from apps.communication import links

register = template.Library()


@register.filter
def linkify(text):
    return links.linkify(text or '')


@register.filter
def linkify_inline(text):
    """For text shown with ``white-space: pre-wrap`` — newlines are left as they are."""
    return links.linkify(text or '', linebreaks=False)


@register.filter
def linkify_email(text):
    return links.linkify(text or '', absolute_urls=True, previews=0)
