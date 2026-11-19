"""Editable JSON content packs — the FAQ and the review wall.

:mod:`core.branding` already owns the platform's *labels* (``.strings.json``).
This owns the platform's *content*: the lists that grow — a new question a
student keeps asking, a new review that came in — and that someone who is not a
developer should be able to extend without opening a template.

Each pack is one JSON file in :data:`CONTENT_DIR` (``metanoia/content/``).
Add an entry, save, restart (or call :func:`reload`) and it is on the page. The
directory is a plain, visible one, unlike ``.strings.json``, because these files
are meant to be edited often and found without being told.

Usage::

    from core import content
    content.faq_items()          # -> [{'q': ..., 'a': ..., 'tags': ...}, ...]
    content.reviews()            # -> {'items': [...], 'rating': ..., ...}
"""

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings

CONTENT_DIR = Path(settings.BASE_DIR) / 'content'


@lru_cache(maxsize=8)
def pack(name):
    """Return content pack ``name`` (``content/<name>.json``) as a dict.

    A missing or malformed file returns ``{}`` rather than raising: a typo in a
    content file must not take the landing page down with a 500.
    """
    try:
        with open(CONTENT_DIR / f'{name}.json', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reload():
    """Drop the cache so the next access re-reads the files on disk."""
    pack.cache_clear()


def faq_items():
    """The FAQ, as a list of ``{'q', 'a', 'tags'}`` dicts.

    Entries missing a question or an answer are skipped — a half-written draft
    left in the file shows as nothing rather than as an empty accordion row.
    """
    items = pack('faq').get('items') or []
    return [item for item in items
            if isinstance(item, dict) and item.get('q') and item.get('a')]


def reviews():
    """The review wall: ``{'items', 'rating', 'count', 'source', 'profile_url'}``.

    ``items`` are ``{'name', 'rating', 'text', 'when'}``. The file is written by
    hand or by ``manage.py sync_google_reviews`` — see that command.
    """
    data = pack('reviews')
    items = [item for item in (data.get('items') or [])
             if isinstance(item, dict) and item.get('text')]
    return {
        'items': items,
        'rating': data.get('rating'),
        'count': data.get('count'),
        'source': data.get('source') or 'Google',
        'profile_url': data.get('profile_url') or '',
        'synced_at': data.get('synced_at') or '',
    }
