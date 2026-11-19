"""Allowlist HTML sanitiser for author-written rich text.

Educators write lesson text and callouts in the block editor
(``static/js/lesson-editor.js``), which stores the ``contenteditable`` element's
``innerHTML`` verbatim. That HTML is later rendered with ``|safe`` — to learners
and to the admins who review lessons — so anything the browser would run
(``<script>``, ``onerror=``, ``javascript:`` links, ``<iframe srcdoc>`` …) must
never survive. The block editor's POST is just JSON, so the editor's own
toolbar is no guarantee: anyone with authoring rights can send any markup.

:func:`clean_html` is applied twice:

* **on save** — :meth:`apps.learning.models.LessonBlock.save` cleans the
  ``html`` keys of a block's ``data`` before they reach the database;
* **on render** — the ``|clean_html`` template filter
  (``core/templatetags/sanitize.py``) cleans again on the way out, so content
  stored before this existed is safe too.

The allowlist is what the editor's toolbar actually produces (``execCommand``
output: paragraphs, h2–h4, quote, pre, bold/italic/underline/strike,
``<font size color>``, highlight via ``style="background-color"``, alignment
via ``style="text-align"``, indents, lists, links) plus the tables, images and
emphasis that imported content packs and AI-drafted lessons use. Iframes are
allowed only from known video/document hosts.

Allowlists
----------
``STYLE_PROPERTIES``
    The inline CSS the editor writes (alignment, colour, highlight, indent,
    font size) plus harmless layout bits. Everything else in a ``style`` is
    dropped — notably ``background-image`` / ``url()`` and ``position``
    overlays.
``IFRAME_HOSTS``
    Embeds are allowed only from these hosts (and their subdomains), and only
    over https.
``HTML_DATA_KEYS`` / ``URL_DATA_KEYS``
    The keys of a LessonBlock's ``data`` that are rendered as HTML / as URLs,
    cleaned by :func:`clean_block_data`.
"""

from urllib.parse import urlsplit

import nh3

ALLOWED_TAGS = {
    'p', 'br', 'div', 'span', 'hr',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'ins', 'mark', 'small',
    'sub', 'sup', 'code', 'kbd', 'pre', 'blockquote', 'q', 'cite', 'abbr',
    'font',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd',
    'a', 'img', 'figure', 'figcaption',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption', 'colgroup', 'col',
    'iframe',
}

_GENERIC = {'class', 'style', 'title', 'dir', 'lang'}
ALLOWED_ATTRIBUTES = {
    '*': _GENERIC,
    'a': {'href', 'target', 'name'},
    'img': {'src', 'alt', 'width', 'height', 'loading'},
    'font': {'color', 'size', 'face'},
    'td': {'colspan', 'rowspan', 'align', 'valign'},
    'th': {'colspan', 'rowspan', 'scope', 'align', 'valign'},
    'col': {'span', 'width'},
    'colgroup': {'span'},
    'ol': {'start', 'type', 'reversed'},
    'li': {'value'},
    'blockquote': {'cite'},
    'p': {'align'},
    'div': {'align'},
    'iframe': {'src', 'width', 'height', 'allow', 'allowfullscreen', 'frameborder', 'loading'},
}

URL_SCHEMES = {'http', 'https', 'mailto', 'tel'}

STYLE_PROPERTIES = {
    'color', 'background-color', 'text-align', 'text-decoration', 'font-weight',
    'font-style', 'font-size', 'font-family', 'margin', 'margin-left', 'margin-right',
    'padding', 'padding-left', 'padding-right', 'list-style-type', 'vertical-align',
    'width', 'height', 'border', 'border-collapse', 'white-space',
}

IFRAME_HOSTS = (
    'youtube.com', 'youtube-nocookie.com', 'player.vimeo.com',
    'microsoftstream.com', 'web.microsoftstream.com', 'sharepoint.com',
    'office.com', 'officeapps.live.com',
)


def _iframe_src_ok(value):
    """True when ``value`` is an https URL on one of :data:`IFRAME_HOSTS`."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    return parts.scheme == 'https' and any(
        host == h or host.endswith('.' + h) for h in IFRAME_HOSTS)


def _attribute_filter(tag, attr, value):
    """nh3 attribute filter: drop iframe sources off the allowlist and any ``target`` but ``_blank``."""
    if tag == 'iframe' and attr == 'src':
        return value if _iframe_src_ok(value) else None
    if tag == 'a' and attr == 'target':
        return '_blank' if value == '_blank' else None
    return value


def clean_html(html):
    """Return ``html`` with everything outside the allowlist removed.

    Links keep ``href`` only for http(s)/mailto/tel or relative URLs and always
    get ``rel="noopener noreferrer"``. Disallowed tags are stripped (their text
    kept), except ``script``/``style`` whose contents are dropped entirely.
    """
    if not html:
        return ''
    if not isinstance(html, str):
        html = str(html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        clean_content_tags={'script', 'style'},
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        url_schemes=URL_SCHEMES,
        link_rel='noopener noreferrer',
        filter_style_properties=STYLE_PROPERTIES,
        strip_comments=True,
    )


def safe_url(url):
    """``url`` if it is http(s)/mailto/tel or relative, else ``''``.

    For author-supplied URLs dropped into ``href``/``src`` attributes — Django
    escapes quotes but not a ``javascript:`` scheme, which runs on click (or, in
    an ``<iframe src>``, immediately).

    Before the scheme is read, every character up to and including space is
    removed from a probe copy, because browsers ignore tabs, newlines and control
    characters inside a scheme (``java\tscript:``). A URL with no scheme before
    its first ``/``, ``?`` or ``#`` is relative and kept.
    """
    url = (url or '').strip() if isinstance(url, str) else ''
    if not url:
        return ''
    probe = ''.join(ch for ch in url if ch > ' ').lower()
    scheme, sep, _ = probe.partition(':')
    if not sep or '/' in scheme or '?' in scheme or '#' in scheme:
        return url
    return url if scheme in URL_SCHEMES else ''


HTML_DATA_KEYS = ('html',)
URL_DATA_KEYS = ('url', 'poster')


def clean_block_data(data):
    """Clean the HTML-bearing keys of a block's ``data`` dict (returns a copy)."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in HTML_DATA_KEYS:
        if isinstance(out.get(key), str):
            out[key] = clean_html(out[key])
    for key in URL_DATA_KEYS:
        if isinstance(out.get(key), str):
            out[key] = safe_url(out[key])
    return out


_JSON_SCRIPT_ESCAPES = {ord('<'): '\\u003C', ord('>'): '\\u003E', ord('&'): '\\u0026'}


def script_json(obj):
    """``json.dumps`` that is safe to drop inside an inline ``<script>`` with
    ``|safe`` — a ``</script>`` inside a string can no longer close the tag."""
    import json

    from django.core.serializers.json import DjangoJSONEncoder
    return json.dumps(obj, cls=DjangoJSONEncoder).translate(_JSON_SCRIPT_ESCAPES)
