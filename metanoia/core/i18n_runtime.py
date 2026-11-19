"""Front-end translation for English / French / Portuguese.

Marking up every template with ``{% translate %}`` leaves the one forgotten
screen in English, so translation has two layers sharing one catalog:

1. **Catalog** — ``locale/runtime/<lang>.json``, a flat ``{english: translated}``
   map. ``manage.py compile_locales`` also writes it to ``.mo`` files (by hand —
   this deployment has no ``gettext`` binaries) so ``gettext()`` and Django's own
   strings use the same source.
2. **Runtime pass** — :class:`RuntimeTranslationMiddleware` rewrites rendered
   HTML against the catalog for non-English users. Already-translated text no
   longer matches an English key, so the layers compose.

The rewrite is deliberately conservative:

* ``<script>``, ``<style>``, ``<pre>``, ``<code>`` and ``<textarea>`` bodies are
  cut out first and put back untouched — translating a JS string literal or a
  code sample would break the page.
* Only whole text nodes are matched, normalised on whitespace, against exact
  catalog keys. There is no fuzzy or word-by-word substitution: a phrase is
  either in the catalog and translated as a unit, or left in English.
* Among attributes, only the human-readable ones are touched (``placeholder``,
  ``title``, ``alt``, ``aria-label``, and ``value`` on buttons).

Nothing here runs at all for English, which is the overwhelming majority of
requests: the middleware returns the response untouched.

Module constants
----------------
``CATALOG_DIR``
    Where the flat JSON catalogs live.
``_PROTECTED``
    Element bodies that must never be rewritten.
``_TEXT_NODE``
    A text node: everything between a ``>`` and the next ``<``.
``_ATTR``
    The human-readable attributes worth translating.
``_HAS_LETTERS``
    Only a node that actually contains letters is translated, so ``·``, ``3``
    and ``&nbsp;`` are skipped.
"""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils import translation

logger = logging.getLogger('apps')

CATALOG_DIR = Path(settings.BASE_DIR) / 'locale' / 'runtime'

_PROTECTED = re.compile(
    r'(?is)<(script|style|pre|code|textarea|svg)\b[^>]*>.*?</\1\s*>')

_TEXT_NODE = re.compile(r'>([^<>]+)<')

_ATTR = re.compile(
    r'(?i)\b(placeholder|title|alt|aria-label)\s*=\s*"([^"<>]+)"')

_HAS_LETTERS = re.compile(r'[A-Za-z]{2}')


@lru_cache(maxsize=8)
def catalog(language):
    """The ``{english: translated}`` map for ``language`` (empty for English or
    an unknown/missing catalog). Cached; call :func:`reload_catalogs` after
    editing a file in a long-running process.

    Besides the exact keys, the map carries a second, lower-cased index so
    "Save changes" also matches "SAVE CHANGES" coming out of a CSS-uppercased
    button whose markup is title-cased.
    """
    language = (language or 'en').split('-')[0]
    if language == 'en':
        return {}
    path = CATALOG_DIR / f'{language}.json'
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}
    for key, value in list(out.items()):
        out.setdefault(key.lower(), value)
    return out


def reload_catalogs():
    """Drop the cached catalogs so the next lookup re-reads the JSON files."""
    catalog.cache_clear()


def translate_html(html, language):
    """Return ``html`` with its visible text translated into ``language``.

    Protected regions (:data:`_PROTECTED`) are cut out and replaced by
    placeholders, the remaining text nodes and attributes are translated, and the
    regions are put back. Internal whitespace is collapsed for the lookup, because
    templates wrap lines mid-sentence, but a node keeps its own leading and
    trailing padding.
    """
    table = catalog(language)
    if not table:
        return html

    def lookup(value):
        """Exact catalog hit for ``value``, falling back to the lower-cased index."""
        return table.get(value) or table.get(value.lower())

    def do_text(match):
        """Translate one text-node match, keeping its surrounding whitespace."""
        raw = match.group(1)
        stripped = raw.strip()
        if not stripped or not _HAS_LETTERS.search(stripped):
            return match.group(0)
        key = ' '.join(stripped.split())
        hit = lookup(key)
        if hit is None:
            return match.group(0)
        lead = raw[:len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()):]
        return f'>{lead}{hit}{trail}<'

    def do_attr(match):
        """Translate one human-readable attribute match."""
        name, value = match.group(1), match.group(2)
        key = ' '.join(value.split())
        hit = lookup(key)
        return match.group(0) if hit is None else f'{name}="{hit}"'

    parts, guards = [], []

    def stash(match):
        r"""Swap a protected region for a ``\x00<n>\x00`` placeholder."""
        guards.append(match.group(0))
        return f'\x00{len(guards) - 1}\x00'

    body = _PROTECTED.sub(stash, html)
    body = _TEXT_NODE.sub(do_text, body)
    body = _ATTR.sub(do_attr, body)
    parts.append(body)
    out = ''.join(parts)
    return re.sub(r'\x00(\d+)\x00', lambda m: guards[int(m.group(1))], out)


class RuntimeTranslationMiddleware:
    """Translate rendered HTML for non-English users.

    Sits directly after
    :class:`~apps.accounts.middleware.UserPreferenceMiddleware`, which has
    already activated the user's language, so this only has to ask what the
    active language is.

    Streaming responses (file downloads, exports) and anything that is not
    ``text/html`` pass through untouched: they have no rendered body to rewrite,
    and a PDF or CSV is not front-end text. A ``TemplateResponse`` has not been
    rendered yet at this point in the chain, so its render is hooked through a
    post-render callback instead of reading an empty body.
    """

    def __init__(self, get_response):
        """Store the next handler in the middleware chain."""
        self.get_response = get_response

    def __call__(self, request):
        """Translate ``request``'s HTML response when the active language is not English."""
        response = self.get_response(request)
        if not getattr(settings, 'RUNTIME_TRANSLATION', True):
            return response

        language = translation.get_language() or 'en'
        if language.split('-')[0] == 'en':
            return response
        if getattr(response, 'streaming', False):
            return response
        content_type = response.headers.get('Content-Type', '')
        if 'text/html' not in content_type:
            return response
        if hasattr(response, 'render') and callable(response.render) and not response.is_rendered:
            response.add_post_render_callback(
                lambda r: self._rewrite(r, language))
            return response
        return self._rewrite(response, language) or response

    @staticmethod
    def _rewrite(response, language):
        """Translate ``response``'s body in place and fix ``Content-Length``.

        Any exception is logged and swallowed: a translation pass must never be the
        reason a page fails to load.
        """
        try:
            charset = response.charset or 'utf-8'
            html = response.content.decode(charset)
            translated = translate_html(html, language)
            if translated != html:
                response.content = translated.encode(charset)
                if response.has_header('Content-Length'):
                    response['Content-Length'] = str(len(response.content))
        except Exception:                                # pragma: no cover
            logger.exception('runtime translation failed (%s)', language)
        return response
