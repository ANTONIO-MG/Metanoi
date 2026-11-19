"""Link preview cards — the WhatsApp-style box under a message with a link.

:func:`preview_for` answers "what card should *this viewer* see for this URL":

* **an LMS page** — built from the database (session, module, lesson,
  announcement, study group, conversation), and only when the viewer may open
  that object. Anything else gets no card, so a title is never leaked to
  someone who could not have read it.
* **a Microsoft Teams link** — the session it belongs to (same permission rule),
  or a generic "Microsoft Teams meeting" card when it belongs to none.
* **anything else** — the page's OpenGraph title / description / image, fetched
  server-side by :func:`fetch_opengraph` and cached for a day.

The fetch is the dangerous part, so it is deliberately narrow (it runs on our
network, with whatever a user typed): http/https on the default ports only; the
host is resolved **once**, every address it resolves to must be public, and the
socket is opened to that vetted IP — never re-resolved, so a DNS answer that
changes between the check and the connect (the rebinding trick) cannot steer it
inside. TLS still verifies the certificate against the real host name. Private,
loopback, link-local (incl. the 169.254.169.254 metadata service), CGNAT,
multicast, reserved and the IPv6 equivalents / IPv4-embedding prefixes are all
refused. At most three redirects, each re-checked the same way; a 3-second
budget for the whole thing; at most 256 KB read; ``text/html`` only.

Nothing here runs while a page renders: ``static/js/link-preview.js`` asks the
``communication:link-preview`` endpoint once the link scrolls into view.
"""

import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from django.core.cache import cache
from django.urls import Resolver404, resolve
from django.utils import timezone

from . import links

logger = logging.getLogger('apps')

TIMEOUT = 3.0
MAX_BYTES = 256 * 1024
MAX_REDIRECTS = 3
CACHE_SECONDS = 24 * 60 * 60
NEGATIVE_CACHE_SECONDS = 60 * 60
USER_AGENT = 'Metanoia-LinkPreview/1.0 (+link previews in the LMS)'

_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
    '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16',
    '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4',
    '255.255.255.255/32',
    '::/128', '::1/128', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48', '100::/64',
    '2001::/23', '2001:db8::/32', '2002::/16', 'fc00::/7', 'fe80::/10', 'fec0::/10', 'ff00::/8',
)]


class UnsafeURL(Exception):
    """The URL may not be fetched (bad scheme, port, or a non-public address)."""


def ip_is_public(ip):
    """True only for a globally routable unicast address."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version == 6:
        for embedded in (addr.ipv4_mapped, addr.sixtofour,
                         addr.teredo[1] if addr.teredo else None):
            if embedded is not None and not ip_is_public(str(embedded)):
                return False
    if any(addr in net for net in _BLOCKED_NETS if net.version == addr.version):
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified or not addr.is_global)


def vet(url):
    """``(scheme, host, port, ip, path)`` for a URL that is safe to fetch.

    Resolves the host exactly once. Raises :class:`UnsafeURL` otherwise.

    Every address must be public: a name that resolves to one public and one
    private address is exactly what a rebinding attack looks like.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURL('unparseable') from exc
    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise UnsafeURL('scheme')
    if parts.username or parts.password:
        raise UnsafeURL('credentials in URL')
    host = (parts.hostname or '').rstrip('.').lower()
    if not host:
        raise UnsafeURL('no host')
    try:
        port = parts.port or (443 if scheme == 'https' else 80)
    except ValueError as exc:
        raise UnsafeURL('bad port') from exc
    if port not in (80, 443):
        raise UnsafeURL('port')
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnsafeURL('does not resolve') from exc
    ips = []
    for info in infos:
        ip = info[4][0].split('%', 1)[0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise UnsafeURL('does not resolve')
    if not all(ip_is_public(ip) for ip in ips):
        raise UnsafeURL('non-public address')
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    return scheme, host, port, ips[0], path


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Speaks HTTP to ``host`` over a socket opened to the vetted ``ip``."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        """Open the socket to the vetted IP instead of resolving ``host`` again."""
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to the vetted ``ip``, with SNI and certificate checks for ``host``."""

    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        """Open the socket to the vetted IP and wrap it in TLS for the real host name (SNI and certificate)."""
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _open(url, deadline):
    """Vet ``url`` and send a GET to its pinned address within the time ``deadline``.

    Returns ``(connection, response)``; raises :class:`UnsafeURL` or
    :class:`TimeoutError` when the URL is refused or the budget is spent.
    """
    scheme, host, port, ip, path = vet(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('preview budget spent')
    cls = _PinnedHTTPSConnection if scheme == 'https' else _PinnedHTTPConnection
    conn = cls(host, port, ip, timeout=min(TIMEOUT, remaining))
    conn.request('GET', path, headers={
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml;q=0.9',
        'Accept-Encoding': 'identity',
        'Accept-Language': 'en',
    })
    return conn, conn.getresponse()


def fetch_html(url):
    """``(final_url, html_text)`` or ``(url, '')`` — never raises.

    Network trouble of any kind is logged and gives no card.
    """
    deadline = time.monotonic() + TIMEOUT
    current, conn = url, None
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            conn, response = _open(current, deadline)
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location') or ''
                conn.close()
                if not location:
                    return current, ''
                current = urljoin(current, location)
                continue
            if response.status != 200:
                return current, ''
            ctype = (response.getheader('Content-Type') or '').lower()
            if not (ctype.startswith('text/html') or ctype.startswith('application/xhtml+xml')):
                return current, ''
            body = bytearray()
            while len(body) < MAX_BYTES:
                if time.monotonic() > deadline:
                    break
                chunk = response.read(min(16 * 1024, MAX_BYTES - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            charset = 'utf-8'
            match = re.search(r'charset=([\w\-]+)', ctype)
            if match:
                charset = match.group(1)
            try:
                return current, bytes(body).decode(charset, 'replace')
            except LookupError:
                return current, bytes(body).decode('utf-8', 'replace')
        return current, ''
    except UnsafeURL as exc:
        logger.info('link preview refused %s: %s', current, exc)
        return current, ''
    except Exception as exc:
        logger.info('link preview failed for %s: %s', current, exc)
        return current, ''
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class _MetaParser(HTMLParser):
    """Collects <title> and the OpenGraph / Twitter / description <meta> tags."""

    WANT = {'og:title', 'og:description', 'og:image', 'og:image:secure_url', 'og:site_name',
            'twitter:title', 'twitter:description', 'twitter:image', 'description'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.title, self._in_title, self.done = {}, '', False, False

    def handle_starttag(self, tag, attrs):
        """Note ``<title>`` and record the first value of each wanted ``<meta>``; stop at ``<body>``."""
        if self.done:
            return
        if tag == 'title':
            self._in_title = True
        elif tag == 'meta':
            attrs = {k.lower(): (v or '') for k, v in attrs if k}
            key = (attrs.get('property') or attrs.get('name') or '').strip().lower()
            if key in self.WANT and key not in self.meta:
                self.meta[key] = attrs.get('content', '')
        elif tag == 'body':
            self.done = True

    def handle_endtag(self, tag):
        """Close the title, and stop once ``</head>`` is reached."""
        if tag == 'title':
            self._in_title = False
        elif tag == 'head':
            self.done = True

    def handle_data(self, data):
        """Collect title text, capped at 500 characters."""
        if self._in_title and not self.done and len(self.title) < 500:
            self.title += data


def _clean(text, limit):
    """Unescape, collapse whitespace and truncate ``text`` to ``limit`` characters with an ellipsis."""
    text = re.sub(r'\s+', ' ', unescape(text or '')).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def parse_opengraph(html, base_url):
    """Build an external card from ``html``, or return ``None`` when the page has no title.

    Prefers OpenGraph, then Twitter, then plain ``<title>``/``description``. The
    image must be ``https`` (and at most 1000 characters) so the card never
    pulls mixed content into the page.
    """
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    meta = parser.meta
    title = _clean(meta.get('og:title') or meta.get('twitter:title') or parser.title, 200)
    description = _clean(meta.get('og:description') or meta.get('twitter:description')
                         or meta.get('description'), 300)
    image = (meta.get('og:image:secure_url') or meta.get('og:image')
             or meta.get('twitter:image') or '').strip()
    if image:
        image = urljoin(base_url, image)
        if urlsplit(image).scheme != 'https' or len(image) > 1000:
            image = ''
    site = _clean(meta.get('og:site_name'), 80) or (urlsplit(base_url).hostname or '')
    if not title:
        return None
    return {'title': title, 'description': description, 'image': image, 'site': site}


def fetch_opengraph(url):
    """The external card for ``url`` (cached a day; failures cached an hour)."""
    key = 'unfurl:og:' + hashlib.sha256(url.encode()).hexdigest()
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    final, html = fetch_html(url)
    card = parse_opengraph(html, final) if html else None
    if card:
        card.update({'kind': 'external', 'url': url, 'href': url, 'same_tab': False})
    cache.set(key, card or {}, CACHE_SECONDS if card else NEGATIVE_CACHE_SECONDS)
    return card


def _file_url(field):
    """Return a file field's URL, or ``''`` when it is empty or has no storage URL."""
    try:
        return field.url if field else ''
    except Exception:
        return ''


def _when(dt):
    """Format ``dt`` in local time for a card, or ``''`` when unset."""
    if not dt:
        return ''
    return timezone.localtime(dt).strftime('%a %d %b %Y, %H:%M')


def _meeting_card(meeting, user, href):
    """Return the session card for ``meeting``, or ``None`` when ``user`` may not see it.

    Like every LMS card it is permission-checked per viewer and never cached
    across people. A call in a chat is visible to that chat's members.
    """
    from apps.livesessions.audience import can_view

    allowed = can_view(user, meeting)
    if not allowed and meeting.group_id:
        allowed = meeting.group.memberships.filter(user=user).exists()
    if not allowed:
        return None
    bits = [b for b in (_when(meeting.scheduled_start),
                        getattr(meeting.module, 'display_name', '') if meeting.module_id else '') if b]
    return {'kind': 'session', 'title': meeting.title, 'description': ' · '.join(bits),
            'image': _file_url(meeting.thumbnail), 'site': 'Live session · Microsoft Teams',
            'href': href, 'same_tab': True}


def _internal_card(url, user):
    """Return the card for an LMS URL, resolved to its object and checked against ``user``.

    Returns ``None`` for an unknown page or an object the viewer may not open,
    so a title is never leaked. Never cached across people.
    """
    path = urlsplit(url).path or '/'
    try:
        match = resolve(path)
    except Resolver404:
        return None
    name, kw = match.view_name, match.kwargs
    href = path + (('?' + urlsplit(url).query) if urlsplit(url).query else '')

    if name in ('livesessions:session-detail', 'livesessions:session-join'):
        from .models import MeetingRoom
        meeting = MeetingRoom.objects.select_related('module', 'group').filter(pk=kw.get('pk')).first()
        return _meeting_card(meeting, user, href) if meeting else None

    if name == 'communication:meeting-room':
        from .models import MeetingRoom
        meeting = MeetingRoom.objects.select_related('module', 'group').filter(slug=kw.get('slug')).first()
        return _meeting_card(meeting, user, href) if meeting else None

    if name == 'communication:announcement-detail':
        from .models import Announcement, Notification
        announcement = Announcement.objects.filter(pk=kw.get('pk')).first()
        if announcement is None:
            return None
        if not (user.is_staff or Notification.objects.filter(
                announcement=announcement, recipient=user).exists()):
            return None
        return {'kind': 'announcement', 'title': announcement.title,
                'description': _clean(announcement.body, 200), 'image': '',
                'site': 'Announcement', 'href': href, 'same_tab': True}

    if name == 'communication:workspace-detail':
        from .models import Workspace
        group = Workspace.objects.filter(pk=kw.get('pk'), is_active=True).first()
        if group is None or not group.can_access(user):
            return None
        return {'kind': 'study-group', 'title': group.name,
                'description': _clean(group.description, 200), 'image': '',
                'site': 'Study group', 'href': href, 'same_tab': True}

    if name == 'communication:chat-room':
        from .models import ChatGroup
        group = ChatGroup.objects.filter(pk=kw.get('group_id')).first()
        if group is None or not group.memberships.filter(user=user).exists():
            return None
        if group.kind == ChatGroup.KIND_DIRECT:
            return None
        return {'kind': 'chat', 'title': group.display_name,
                'description': _clean(group.description, 200), 'image': '',
                'site': 'Group chat', 'href': href, 'same_tab': True}

    if name in ('learning:module-feed', 'learning:module-feed-tab'):
        from apps.learning.access import Gate
        from apps.learning.models import ProgrammeModule
        offering = (ProgrammeModule.objects.select_related('module', 'programme')
                    .filter(pk=kw.get('pk'), is_active=True).first())
        if offering is None:
            return None
        gate = Gate(user, offering, person=getattr(user, 'profile', None))
        if not (gate.can_author or gate.enrolment is not None or offering.is_free):
            return None
        return {'kind': 'module', 'title': offering.display_name,
                'description': _clean(getattr(offering.module, 'description', ''), 200),
                'image': '', 'site': f'Module · {offering.programme}', 'href': href,
                'same_tab': True}

    if name == 'learning:lesson-view':
        from apps.learning.models import Lesson
        from apps.learning.views import _can_view_lesson
        lesson = Lesson.objects.select_related('module').filter(pk=kw.get('lesson_id')).first()
        if lesson is None or not _can_view_lesson(user, lesson):
            return None
        return {'kind': 'lesson', 'title': lesson.title, 'description': _clean(lesson.subtitle, 200),
                'image': _file_url(lesson.cover_image), 'site': 'Lesson', 'href': href,
                'same_tab': True}
    return None


def _generic_teams_card(url):
    """Return the generic "Microsoft Teams meeting" card for a Teams link that belongs to no session."""
    meeting_link = '/meetup-join/' in url or '/meet/' in url
    return {'kind': 'teams', 'title': 'Microsoft Teams meeting' if meeting_link else 'Microsoft Teams',
            'description': 'Join the call in Microsoft Teams.' if meeting_link else '',
            'image': '', 'site': 'teams.microsoft.com', 'href': url, 'same_tab': False}


def preview_for(raw_url, user):
    """The card ``user`` should see for ``raw_url``, or None for a plain link."""
    url = links.normalise(raw_url)
    if not url or len(url) > 2000 or user is None or not user.is_authenticated:
        return None
    if links.is_teams(url):
        meeting = links.meeting_for_teams_url(url)
        if meeting is not None:
            return _meeting_card(meeting, user, links.lms_path_for_meeting(meeting))
        return _generic_teams_card(url)
    if links.is_internal(url):
        try:
            return _internal_card(url, user)
        except Exception:
            logger.exception('link preview: could not build the card for %s', url)
            return None
    return fetch_opengraph(url)
