"""Turning shared text into safe, clickable links — one rule set for every page.

Chat messages, community posts and comments, announcements, notifications and
the e-mails that carry them all go through :func:`linkify`, so a link behaves
the same wherever it is read:

* Everything that is not a link is HTML-escaped; a link's address and its
  visible text are escaped too. Only ``http``/``https`` (and bare ``www.``)
  addresses are recognised — ``javascript:`` and friends stay plain text.
* A link to **this LMS** (``SITE_URL`` / ``ALLOWED_HOSTS``) opens in the same
  tab, because it is part of the site.
* A **Microsoft Teams** link that belongs to a session on the platform
  (``MeetingRoom.teams_join_url``) is pointed at the LMS join hand-off for that
  session instead of raw Teams, so the attendance roster knows who went in.
* Anything else opens in a new tab with ``rel="noopener noreferrer nofollow ugc"``.

The first link of a message is marked ``data-preview-url`` so
``static/js/link-preview.js`` can ask :mod:`apps.communication.unfurl` for a
WhatsApp-style card — lazily, after the page has rendered, and per viewer.

``_URL_RE`` matches ``http(s)://`` or a bare ``www.`` address, up to whitespace or a
character that cannot be part of a URL in running text.
"""

import hashlib
import re
from urllib.parse import quote, unquote, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.utils.html import escape
from django.utils.safestring import mark_safe

_URL_RE = re.compile(r'(?i)\b((?:https?://|www\.)[^\s<>"\'` ]+)')
_TRAILING = '.,;:!?\'"*_~'
_MAX_LABEL = 70

TEAMS_HOSTS = {'teams.microsoft.com', 'teams.live.com', 'gov.teams.microsoft.us',
               'dod.teams.microsoft.us', 'teams.microsoft.us'}

EXTERNAL_REL = 'noopener noreferrer nofollow ugc'


def _trim(candidate):
    """Strip punctuation that ends a sentence rather than the URL."""
    url = candidate
    while url:
        last = url[-1]
        if last in _TRAILING:
            url = url[:-1]
        elif last == ')' and url.count('(') < url.count(')'):
            url = url[:-1]
        elif last == ']' and url.count('[') < url.count(']'):
            url = url[:-1]
        elif last == '}' and url.count('{') < url.count('}'):
            url = url[:-1]
        else:
            break
    return url


def normalise(raw):
    """``raw`` as an absolute http(s) URL, or '' when it is not one."""
    url = (raw or '').strip()
    if url.lower().startswith('www.'):
        url = 'https://' + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return ''
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        return ''
    return url


def site_hosts():
    """``(exact_hosts, suffixes)`` that are this LMS."""
    exact, suffixes = set(), set()
    site = urlsplit(getattr(settings, 'SITE_URL', '') or '').hostname
    if site:
        exact.add(site.lower())
    for host in getattr(settings, 'ALLOWED_HOSTS', []) or []:
        host = (host or '').strip().lower()
        if not host or host == '*':
            continue
        if host.startswith('.'):
            suffixes.add(host)
            exact.add(host[1:])
        else:
            exact.add(host)
    return exact, suffixes


def is_internal(url):
    """True when ``url`` points at this LMS (see :func:`site_hosts`)."""
    host = (urlsplit(url).hostname or '').lower()
    if not host:
        return False
    exact, suffixes = site_hosts()
    return host in exact or any(host.endswith(s) for s in suffixes)


def is_teams(url):
    """True when ``url`` is a Microsoft Teams link."""
    host = (urlsplit(url).hostname or '').lower()
    return host in TEAMS_HOSTS or host.endswith('.teams.microsoft.com')


def _teams_candidates(url):
    """Spellings of a Teams URL (as given, unquoted, re-quoted) to match against stored
    links.
    """
    variants = {url, unquote(url), quote(unquote(url), safe=':/?&=%#@+,;~')}
    return [v for v in variants if v][:4]


def meeting_for_teams_url(url):
    """The :class:`MeetingRoom` whose Teams join link is ``url`` (or None).

    Cached for five minutes either way, so a busy thread full of the same link
    costs one query, not one per message per poll.
    """
    from .models import MeetingRoom

    if not is_teams(url):
        return None
    key = 'links:teams:' + hashlib.sha1(url.encode()).hexdigest()
    pk = cache.get(key)
    if pk is None:
        room = (MeetingRoom.objects.filter(teams_join_url__in=_teams_candidates(url))
                .order_by('-pk').only('pk').first())
        pk = room.pk if room else 0
        cache.set(key, pk, 300)
    if not pk:
        return None
    return MeetingRoom.objects.select_related('group').filter(pk=pk).first()


def lms_path_for_meeting(meeting):
    """Where a session's Teams link should go inside the LMS.

    A call started in a group goes through the group's join route (which adds a
    member who came later to the call); every other session through the
    livesessions hand-off that records who went in.
    """
    from django.urls import reverse
    from .models import ChatGroup

    group = getattr(meeting, 'group', None)
    if group is not None and group.kind != ChatGroup.KIND_DIRECT:
        return reverse('communication:chat-call-join', args=[group.pk, meeting.pk])
    return reverse('livesessions:session-join', args=[meeting.pk])


def absolute(path):
    """``path`` prefixed with ``SITE_URL``."""
    return (getattr(settings, 'SITE_URL', '') or '').rstrip('/') + path


def _label(url):
    """The visible text for a link: the URL without its scheme, shortened to
    ``_MAX_LABEL``.
    """
    text = re.sub(r'^https?://', '', url, flags=re.I)
    return text if len(text) <= _MAX_LABEL else text[:_MAX_LABEL - 1] + '…'


def anchor(raw, *, absolute_urls=False, preview=False):
    """The ``<a>`` for one URL found in text (already trimmed), escaped."""
    url = normalise(raw)
    if not url:
        return escape(raw)
    href, internal = url, False
    meeting = meeting_for_teams_url(url) if is_teams(url) else None
    if meeting is not None:
        path = lms_path_for_meeting(meeting)
        href, internal = (absolute(path) if absolute_urls else path), True
    elif is_internal(url):
        internal = True
    attrs = [f'href="{escape(href)}"', 'class="lms-link"']
    if not internal:
        attrs.append('target="_blank"')
        attrs.append(f'rel="{EXTERNAL_REL}"')
    if preview:
        attrs.append(f'data-preview-url="{escape(url)}"')
    return f'<a {" ".join(attrs)}>{escape(_label(raw))}</a>'


def _plain(text, mentions, linebreaks):
    """Escape non-link ``text``, highlight tagged ``mentions`` and optionally keep line
    breaks.
    """
    html = escape(text)
    for label in mentions:
        safe = escape(label)
        html = re.sub(re.escape(safe), f'<span class="feed-mention">{safe}</span>', html,
                      flags=re.IGNORECASE)
    if linebreaks:
        html = html.replace('\r\n', '\n').replace('\n', '<br>')
    return html


def linkify(text, *, mentions=(), linebreaks=True, absolute_urls=False, previews=1):
    """``text`` as safe HTML with every URL made into a link.

    ``mentions`` are ``@Name`` labels to highlight (community posts);
    ``absolute_urls`` is for e-mail, where a relative LMS path goes nowhere;
    ``previews`` is how many links get a ``data-preview-url`` (0 for none).
    """
    text = text or ''
    labels = sorted({m for m in mentions if m}, key=len, reverse=True)
    out, last, marked = [], 0, 0
    for match in _URL_RE.finditer(text):
        raw = _trim(match.group(1))
        if not normalise(raw):
            continue
        start, end = match.start(1), match.start(1) + len(raw)
        out.append(_plain(text[last:start], labels, linebreaks))
        want = marked < previews
        out.append(anchor(raw, absolute_urls=absolute_urls, preview=want))
        marked += 1 if want else 0
        last = end
    out.append(_plain(text[last:], labels, linebreaks))
    return mark_safe(''.join(out))
