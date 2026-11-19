"""Raw RFC 822 bytes → what Metanoia Mail stores and shows.

HTML mail is hostile input. It is sanitised with nh3 every time it is shown
(never at sync, so a stricter rule applies to old mail too), remote images are
held back until the reader asks for them — as Outlook does, since a remote
image tells the sender when and where the mail was opened — and the result is
displayed in a sandboxed, script-less iframe.

``_TAGS``, ``_ATTRS`` and ``_SCHEMES`` are the allow-list shared by incoming
mail (:func:`sanitize_html`) and outgoing mail (:func:`clean_compose_html`).
"""

import email
import email.policy
import html as html_lib
import re
from email.utils import getaddresses, parsedate_to_datetime

import nh3
from bs4 import BeautifulSoup
from django.utils import timezone


def _header(msg, name):
    """Header ``name`` unfolded onto one line, or ``''``.

    A malformed header the parser cannot fold is read raw instead.
    """
    try:
        value = msg.get(name)
    except Exception:
        value = msg.get_all(name, [None])[0] if hasattr(msg, 'get_all') else None
    if value is None:
        return ''
    return ' '.join(str(value).split())


def _addresses(msg, name):
    """``[[name, address], ...]`` for header ``name``, skipping entries without an address."""
    try:
        values = msg.get_all(name, [])
    except Exception:
        return []
    pairs = getaddresses([str(v) for v in values])
    return [[n.strip(), a.strip()] for n, a in pairs if a and '@' in a]


def _date(msg):
    """The ``Date`` header as an aware datetime, or now when it is missing or unreadable."""
    raw = _header(msg, 'Date')
    try:
        value = parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, IndexError):
        value = None
    if value is None:
        return timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    return value


def _text(part):
    """A text part's content as ``str``, decoding by hand when the parser cannot."""
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b''
        return payload.decode(part.get_content_charset() or 'utf-8', 'replace')


def load(raw):
    """Parse raw message bytes with the modern :mod:`email` policy."""
    return email.message_from_bytes(raw, policy=email.policy.default)


def parse_message(raw):
    """A dict of everything :class:`~apps.mailbox.models.MailItem` stores.

    Attachments are listed by their position in ``msg.walk()``; a part with no
    file name, Content-ID or attachment disposition is an alternative body that
    was not chosen and is skipped.
    """
    msg = load(raw)
    html_part = text_part = None
    try:
        html_part = msg.get_body(preferencelist=('html',))
        text_part = msg.get_body(preferencelist=('plain',))
    except Exception:
        pass
    body_html = _text(html_part) if html_part is not None else ''
    body_text = _text(text_part) if text_part is not None else ''
    if not body_text and body_html:
        body_text = html_to_text(body_html)

    attachments = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart() or part is html_part or part is text_part:
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ''
        cid = (part.get('Content-ID') or '').strip().strip('<>')
        ctype = part.get_content_type()
        if disposition != 'attachment' and not filename and not cid:
            continue
        payload = part.get_payload(decode=True) or b''
        if not filename:
            ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
                   'application/pdf': 'pdf', 'message/rfc822': 'eml'}.get(ctype, 'bin')
            filename = f'attachment-{index}.{ext}'
        attachments.append({
            'part': index, 'filename': filename[:255], 'content_type': ctype[:120],
            'size': len(payload), 'content_id': cid[:255],
            'is_inline': bool(cid) and disposition != 'attachment' and ctype.startswith('image/'),
        })

    from_pairs = _addresses(msg, 'From')
    from_name, from_address = from_pairs[0] if from_pairs else ['', '']
    snippet = ' '.join(body_text.split())[:300]
    return {
        'message_id': _header(msg, 'Message-ID')[:500],
        'in_reply_to': _header(msg, 'In-Reply-To')[:500],
        'references': _header(msg, 'References'),
        'subject': _header(msg, 'Subject')[:998],
        'from_name': from_name[:255], 'from_address': from_address[:254],
        'to': _addresses(msg, 'To'), 'cc': _addresses(msg, 'Cc'),
        'bcc': _addresses(msg, 'Bcc'), 'reply_to': _addresses(msg, 'Reply-To'),
        'date': _date(msg),
        'body_text': body_text, 'body_html': body_html,
        'snippet': snippet,
        'attachments': attachments,
        'has_attachments': any(not a['is_inline'] for a in attachments),
    }


def get_part(raw, index):
    """``(filename, content_type, bytes)`` of part ``index`` of a raw message."""
    msg = load(raw)
    for i, part in enumerate(msg.walk()):
        if i == index and not part.is_multipart():
            return (part.get_filename() or f'attachment-{i}', part.get_content_type(),
                    part.get_payload(decode=True) or b'')
    return None


_TAGS = {
    'a', 'abbr', 'b', 'big', 'blockquote', 'br', 'caption', 'center', 'cite', 'code', 'col', 'colgroup',
    'dd', 'del', 'div', 'dl', 'dt', 'em', 'font', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img',
    'ins', 'kbd', 'li', 'mark', 'ol', 'p', 'pre', 'q', 's', 'small', 'span', 'strike', 'strong', 'sub',
    'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
}
_GENERIC_ATTRS = {'style', 'class', 'align', 'valign', 'dir', 'title', 'width', 'height', 'bgcolor', 'border',
                  'lang'}
_ATTRS = {
    'a': {'href', 'name', 'target'},
    'img': {'src', 'alt'},
    'font': {'color', 'face', 'size'},
    'table': {'cellpadding', 'cellspacing'},
    'td': {'colspan', 'rowspan', 'nowrap'},
    'th': {'colspan', 'rowspan', 'nowrap', 'scope'},
    'col': {'span'}, 'colgroup': {'span'},
    'ol': {'start', 'type'}, 'li': {'value'},
}
_ATTRS = {tag: _ATTRS.get(tag, set()) | _GENERIC_ATTRS for tag in _TAGS}
_SCHEMES = {'http', 'https', 'mailto', 'tel', 'cid', 'data'}


def sanitize_html(raw_html, *, cid_urls=None, allow_remote=False):
    """``(safe_html, blocked_remote_count)``.

    ``cid_urls`` maps Content-IDs to URLs that serve the inline part, so
    pictures embedded in a signature show without a network fetch.

    ``<body>`` content is lifted out of a full document and ``<style>`` blocks
    are dropped whole: their rules could restyle nothing outside the iframe,
    but they can carry remote ``url()`` values that would bypass the image
    gate. Unless ``allow_remote``, remote images and inline-style ``url()``
    values are blanked and counted.
    """
    if not raw_html:
        return '', 0
    soup = BeautifulSoup(raw_html, 'lxml')
    for bad in soup(['script', 'style', 'head', 'title', 'meta', 'link', 'iframe', 'object', 'embed', 'form']):
        bad.decompose()
    body = soup.body or soup
    clean = nh3.clean(''.join(str(c) for c in body.contents), tags=_TAGS, attributes=_ATTRS,
                      url_schemes=_SCHEMES, link_rel='noopener noreferrer nofollow',
                      clean_content_tags={'script', 'style'})

    soup = BeautifulSoup(clean, 'html.parser')
    blocked = 0
    cid_urls = cid_urls or {}
    for img in soup.find_all('img'):
        src = (img.get('src') or '').strip()
        if src.lower().startswith('cid:'):
            url = cid_urls.get(src[4:].strip('<>'))
            if url:
                img['src'] = url
            else:
                img.decompose()
        elif src.lower().startswith(('http:', 'https:', '//')):
            if not allow_remote:
                img['src'] = ''
                img['alt'] = img.get('alt') or ''
                blocked += 1
        elif src.lower().startswith('data:') and not src.lower().startswith('data:image/'):
            img.decompose()
    for el in soup.find_all(style=True):
        style = el['style']
        if 'url(' in style.lower():
            if not allow_remote:
                el['style'] = re.sub(r'url\([^)]*\)', 'none', style, flags=re.I)
                blocked += 1
    for a in soup.find_all('a', href=True):
        a['target'] = '_blank'
    return str(soup), blocked


def text_to_html(text):
    """Plain text mail as HTML: escaped, links clickable, line breaks kept."""
    escaped = html_lib.escape(text or '')
    escaped = re.sub(r'(https?://[^\s<>"]+)', r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>',
                     escaped)
    return '<div style="white-space:pre-wrap;font-family:inherit">' + escaped + '</div>'


def html_to_text(raw_html):
    """HTML as plain text, with line breaks for ``<br>`` and block elements."""
    if not raw_html:
        return ''
    soup = BeautifulSoup(raw_html, 'html.parser')
    for br in soup.find_all('br'):
        br.replace_with('\n')
    for block in soup.find_all(['p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'blockquote']):
        block.insert_after('\n')
    text = soup.get_text()
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def clean_compose_html(raw_html):
    """Clean a composed body before it is sent.

    The body is the composer's toolbar output plus any quoted original, and is
    cleaned to the same allow-list as incoming mail.
    """
    return nh3.clean(raw_html or '', tags=_TAGS, attributes=_ATTRS, url_schemes=_SCHEMES,
                     link_rel='noopener noreferrer')
