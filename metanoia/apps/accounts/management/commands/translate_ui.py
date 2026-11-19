"""Fill the French and Portuguese UI catalogs from the templates.

The runtime translator (:mod:`core.i18n_runtime`) swaps whole text nodes and
a few attributes for their entry in ``locale/runtime/<lang>.json``. A phrase
nobody added to the catalog stays English, so every new page shipped in
English until someone translated it by hand.

This command closes the loop:

1. **Harvest** — read every template under ``templates/``, drop Django tags,
   comments and protected blocks (``<script>``, ``<style>``, ``<pre>``,
   ``<code>``, ``<textarea>``, ``<svg>``), and collect each static text node
   and translatable attribute, normalised on whitespace exactly the way the
   runtime pass looks phrases up. A node with a ``{{ variable }}`` in it is
   skipped: the rendered text differs per page, so no catalog key could
   match it.
2. **Translate** — phrases missing from a catalog are sent to Claude in
   batches (structured JSON out) and merged in. Existing entries are never
   overwritten, so hand-corrected translations survive.
3. ``compile_locales`` is then run so ``{% translate %}`` and ``gettext()``
   read the same phrases.

    manage.py translate_ui --dry-run      # report what is missing, call nothing
    manage.py translate_ui                # translate the missing phrases
    manage.py translate_ui --lang fr      # one language
    manage.py translate_ui --limit 200    # a sample first

Needs ``ANTHROPIC_API_KEY``. The model is ``TRANSLATION_MODEL`` in settings
(default ``claude-opus-5-5``).
"""

import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

TEMPLATES = Path(settings.BASE_DIR) / 'templates'
CATALOGS = Path(settings.BASE_DIR) / 'locale' / 'runtime'
LANGUAGES = {'fr': 'French', 'pt': 'Portuguese (as used in Mozambique and Angola; neutral, not Brazilian slang)'}
BATCH = 150

_COMMENT_BLOCK = re.compile(r'(?is){%\s*comment\b.*?%}.*?{%\s*endcomment\s*%}')
_HTML_COMMENT = re.compile(r'(?s)<!--.*?-->')
_DJ_COMMENT = re.compile(r'(?s){#.*?#}')
_PROTECTED = re.compile(r'(?is)<(script|style|pre|code|textarea|svg)\b[^>]*>.*?</\1\s*>')
_TAG = re.compile(r'(?s){%.*?%}')
_TEXT = re.compile(r'>([^<>]+)<')
_ATTR = re.compile(r'(?i)\b(?:placeholder|title|alt|aria-label)\s*=\s*"([^"<>{}]+)"')
_LETTERS = re.compile(r'[A-Za-z]{2}')
_NOISE = re.compile(r'^(?:[\W\d_]+|&[a-z]+;|https?://\S+|\S+\.(?:png|jpg|svg|pdf|css|js)|[A-Z0-9_-]{1,6}'
                    r'|[a-z0-9]+(?:-[a-z0-9]+)+|bi-\S+|[\w-]+="[^"]*")$')
"""Text that is not words: CSS/JS fragments, lone entities, URLs, file names, codes."""
_LEGAL = ('_terms-body', '_tos-body', 'refund', 'shipping-policy', 'privacy-policy', 'terms')
"""Template-name fragments of the legal texts, which stay English: a machine
translation must never stand in for the binding wording (the Terms say English
governs)."""


def _phrases(source):
    """The set of translatable phrases in one template's source, normalised on whitespace.

    Comments and protected blocks are removed first. A Django tag splits a text
    node at render time (``{% if %}…{% endif %}``), so each tag is treated as a
    boundary. Phrases with ``{{ }}``, without two letters in a row, or matching
    :data:`_NOISE` are dropped.
    """
    source = _COMMENT_BLOCK.sub(' ', source)
    source = _DJ_COMMENT.sub(' ', _HTML_COMMENT.sub(' ', source))
    source = _PROTECTED.sub('<x>', source)
    out = set()
    for raw in _ATTR.findall(source):
        out.add(raw)
    for raw in _TEXT.findall(_TAG.sub('<x>', source)):
        out.add(raw)
    for raw in list(out):
        out.discard(raw)
        key = ' '.join(raw.split())
        if not key or '{{' in key or '}}' in key or not _LETTERS.search(key) or _NOISE.match(key):
            continue
        if len(key) > 400 or key.count('=') > 1 or key.startswith(('.', '#', '@', '//')):
            continue
        out.add(key)
    return out


def harvest():
    """Map every phrase in ``templates/`` to the first template it was found in.

    E-mail templates (they go out in English), the admin (staff-only) and the
    legal texts (:data:`_LEGAL`) are skipped.
    """
    phrases = {}
    for path in TEMPLATES.rglob('*.html'):
        rel = str(path.relative_to(TEMPLATES))
        if rel.startswith(('communication/email/', 'admin/')) or '/email/' in rel:
            continue
        if any(word in path.name for word in _LEGAL):
            continue
        for key in _phrases(path.read_text(encoding='utf-8', errors='replace')):
            phrases.setdefault(key, rel)
    return phrases


def _load(lang):
    """The ``locale/runtime/<lang>.json`` catalogue, or ``{}``."""
    path = CATALOGS / f'{lang}.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def _save(lang, data):
    """Write the catalogue for ``lang``, keys sorted."""
    path = CATALOGS / f'{lang}.json'
    path.write_text(json.dumps(dict(sorted(data.items())), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


SYSTEM = (
    'You translate the interface of a South African online learning platform for accounting '
    'students (Metanoia Group) from English into {language}. Each item is one UI string: a '
    'button, label, heading, sentence or help text. Translate naturally and concisely, as a '
    'native product team would write it. Keep the brand names "Metanoia", "Metanoia Group", '
    '"Metanoia Learning Hub", product names, module codes (MACF, FREP, TAX, CGAU, PGDA, CTA), '
    'institution names, e-mail addresses, numbers, currency (R) and punctuation such as "·", '
    '"→", HTML entities such as "&amp;" and trailing colons exactly as they are. Some items are '
    'fragments of a longer sentence; translate the fragment as it stands. Keep accounting terms standard for the '
    'language. Return every id exactly once.'
)

SCHEMA = {
    'type': 'object',
    'properties': {'items': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'id': {'type': 'integer'}, 'text': {'type': 'string'}},
        'required': ['id', 'text'], 'additionalProperties': False}}},
    'required': ['items'], 'additionalProperties': False,
}


def translate_batch(client, model, language, phrases):
    """Translate ``phrases`` into ``language`` in one structured-output call.

    Returns ``{english: translation}`` for the items that came back; a refusal or
    a truncated reply returns ``{}``. API errors become ``CommandError``.
    """
    import anthropic
    payload = json.dumps([{'id': i, 'en': p} for i, p in enumerate(phrases)], ensure_ascii=False)
    try:
        with client.messages.stream(
            model=model, max_tokens=32000,
            system=SYSTEM.format(language=language),
            output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': SCHEMA}},
            messages=[{'role': 'user', 'content': payload}],
        ) as stream:
            message = stream.get_final_message()
    except anthropic.APIStatusError as exc:
        raise CommandError(f'Claude API error {exc.status_code}: {exc.message}') from exc
    except anthropic.APIConnectionError as exc:
        raise CommandError(f'Could not reach the Claude API: {exc}') from exc
    if message.stop_reason in ('refusal', 'max_tokens'):
        return {}
    text = next((b.text for b in message.content if b.type == 'text'), '')
    out = {}
    for item in json.loads(text).get('items', []):
        i = item.get('id')
        if isinstance(i, int) and 0 <= i < len(phrases) and item.get('text', '').strip():
            out[phrases[i]] = item['text'].strip()
    return out


class Command(BaseCommand):
    """``manage.py translate_ui [--lang L] [--dry-run] [--limit N]``."""
    help = 'Harvest template text and machine-translate what the fr/pt catalogs are missing.'

    def add_arguments(self, parser):
        """Add ``--lang``, ``--dry-run`` and ``--limit``."""
        parser.add_argument('--lang', choices=sorted(LANGUAGES), help='Only this language.')
        parser.add_argument('--dry-run', action='store_true', help='Report only; no API calls.')
        parser.add_argument('--limit', type=int, default=0, help='Translate at most this many phrases.')

    def handle(self, *args, lang=None, dry_run=False, limit=0, **opts):
        """Harvest, translate what is missing per language, then run ``compile_locales``.

        The catalogue is saved after every batch, so an interruption keeps the work
        done so far.
        """
        phrases = harvest()
        en = _load('en')
        en.update({p: p for p in phrases if p not in en})
        langs = [lang] if lang else sorted(LANGUAGES)
        todo = {code: sorted(p for p in en if p not in _load(code)) for code in langs}
        self.stdout.write(f'{len(phrases)} phrases in templates; catalog keys {len(en)}.')
        for code, missing in todo.items():
            self.stdout.write(f'  {code}: {len(missing)} missing')
        if dry_run:
            return
        key = getattr(settings, 'ANTHROPIC_API_KEY', '')
        if not key:
            raise CommandError('ANTHROPIC_API_KEY is not set.')
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        model = getattr(settings, 'TRANSLATION_MODEL', '') or 'claude-opus-5-5'
        _save('en', en)
        for code, missing in todo.items():
            if limit:
                missing = missing[:limit]
            data = _load(code)
            done = 0
            for start in range(0, len(missing), BATCH):
                got = translate_batch(client, model, LANGUAGES[code], missing[start:start + BATCH])
                for k, v in got.items():
                    data.setdefault(k, v)
                done += len(got)
                _save(code, data)
                self.stdout.write(f'  {code}: {done}/{len(missing)}')
        call_command('compile_locales')
        self.stdout.write(self.style.SUCCESS('Catalogs updated and compiled.'))
