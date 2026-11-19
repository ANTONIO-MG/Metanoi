"""Bring the old info@metanoiagroup.co.za mail into the Metanoia mailboxes.

Two jobs, both reading Gmail over IMAP with an app password and never sending
anything to anyone:

* :func:`draft_from_raw` turns a Gmail draft into a Metanoia Mail
  :class:`~apps.mailbox.models.MailDraft`, swapping in the Metanoia signature and
  adding the ``NOTICE`` line (``manage.py copy_gmail_drafts``).
* :class:`ArchiveCopier` copies every message into a "Gmail Archive" folder tree
  with IMAP APPEND (original bytes, dates, flags — nothing is re-sent); repeated
  or over-size files become a note (``manage.py gmail_archive``; resumable).

Archive settings: ``ARCHIVE_ROOT`` is the top folder of the archive tree.
``LABEL_FOLDERS`` maps a Gmail label to an archive sub-folder — the first match
wins, and anything else goes to ``OTHER_FOLDER`` ("Archived"); messages with a
label in ``SKIP_LABELS`` (drafts, spam, trash) are not copied.
``DEDUPE_MIN_BYTES`` is the size below which a file is always kept, repeated
or not: for a logo or a tiny file a note would not save anything.
"""

import email
import email.header
import email.policy
import hashlib
import imaplib
import io
import json
import logging
import re
import time
from email.generator import BytesGenerator
from email.utils import getaddresses, parsedate_to_datetime

from bs4 import BeautifulSoup, NavigableString

from . import parse

logger = logging.getLogger('apps')

GMAIL_HOST = 'imap.gmail.com'
GMAIL_ALL = '[Gmail]/All Mail'
GMAIL_DRAFTS = '[Gmail]/Drafts'

NOTICE = ('This is Metanoia Group, using our new email accounts and system for the first time. '
          'This note is only so that you can be sure it is really us.')
NOTICE_HTML = f'<p style="color:#0B5FC6;font-weight:600">{NOTICE}</p>'

_GREETING = re.compile(r'^\s*(hi|hello|hey|dear|good\s+(morning|afternoon|evening|day)|greetings|morning|'
                       r'afternoon|evening|hola|molo|sawubona|dumela|howzit)\b', re.I)
_SIGN_OFF = re.compile(r'^\s*((kind|warm|best)\s+regards|regards|best( wishes)?|thanks?( you)?|many thanks|'
                       r'cheers|sincerely|yours (sincerely|faithfully))\b[,!.]?', re.I)


def gmail_connect(user, password):
    """Log in to Gmail's IMAP server with an app password and return the connection."""
    conn = imaplib.IMAP4_SSL(GMAIL_HOST, 993, timeout=120)
    conn.login(user, password)
    return conn


def _q(name):
    """Quote an IMAP mailbox name."""
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def fetch_drafts(conn):
    """``[(uid, raw)]`` for every Gmail draft, oldest first."""
    conn.select(_q(GMAIL_DRAFTS), readonly=True)
    typ, data = conn.uid('SEARCH', None, 'ALL')
    uids = (data[0] or b'').split()
    out = []
    for uid in uids:
        typ, data = conn.uid('FETCH', uid, '(BODY.PEEK[])')
        raw = next((p[1] for p in data if isinstance(p, tuple)), None)
        if raw:
            out.append((int(uid), raw))
    return out


def _addresses(msg, name):
    """The addresses in header ``name`` as a ``'; '``-separated string."""
    pairs = getaddresses([str(v) for v in msg.get_all(name, [])])
    return '; '.join(f'{n} <{a}>' if n else a for n, a in pairs if a and '@' in a)


def _is_quoted(el):
    """Whether ``el`` sits inside a ``<blockquote>`` (a quoted reply)."""
    return any(getattr(p, 'name', None) == 'blockquote' for p in el.parents)


def _short(text):
    """``text`` with its whitespace collapsed."""
    return ' '.join((text or '').split())


def _insert_notice(soup):
    """Put :data:`NOTICE_HTML` right after the greeting (or at the top).

    The greeting is first looked for as the outermost element holding only the
    greeting (``<div><span>Hi Masiphile,</span></div>``), then as a plain-text
    line followed by line breaks (``Hi Masiphile,<br><br>Hope you are well.``).
    Returns True when a greeting was found.
    """
    notice = BeautifulSoup(NOTICE_HTML, 'html.parser').p
    for el in soup.find_all(['p', 'div', 'span', 'td', 'h1', 'h2', 'h3', 'b', 'strong', 'font']):
        text = _short(el.get_text(' '))
        if text and len(text) <= 80 and _GREETING.match(text) and not _is_quoted(el):
            el.insert_after(notice)
            return True
    for s in soup.find_all(string=True):
        if isinstance(s, NavigableString) and _GREETING.match(s) and len(_short(s)) <= 80 and not _is_quoted(s):
            anchor = s
            nxt = s.next_sibling
            while getattr(nxt, 'name', None) == 'br':
                anchor, nxt = nxt, nxt.next_sibling
            anchor.insert_after(notice)
            return True
    root = soup.body or soup
    root.insert(0, notice)
    return False


def _swap_signature(soup, signature_html):
    """Replace the Gmail signature with the Metanoia one (keeping "Kind Regards").

    Returns True if a Gmail signature was found. Without one, the Metanoia
    signature goes before any quoted reply, else at the end.
    """
    block = BeautifulSoup(signature_html, 'html.parser') if signature_html else None
    old = next((el for el in soup.select('.gmail_signature') if not _is_quoted(el)), None)
    if old is not None:
        first = _short(old.get_text('\n').strip().split('\n')[0]) if old.get_text(strip=True) else ''
        sign_off = _SIGN_OFF.match(first)
        wrapper = soup.new_tag('div')
        if sign_off:
            p = soup.new_tag('p')
            p.string = first.rstrip()
            wrapper.append(p)
        if block is not None:
            wrapper.append(block)
        old.replace_with(wrapper)
        return True
    if block is None:
        return False
    quote = next((el for el in soup.select('.gmail_quote') if el.find('blockquote')), None)
    if quote is not None:
        quote.insert_before(block)
    else:
        (soup.body or soup).append(block)
    return False


def draft_from_raw(raw, account, *, notice=True):
    """A saved :class:`MailDraft` in ``account`` copied from a Gmail draft's raw bytes.

    Inline pictures (``cid:``) become ``data:`` images in the body; Metanoia Mail
    sends them as inline parts again (:func:`apps.mailbox.signatures.embed_images`).
    Other attachments are copied onto the draft.
    """
    from django.core.files.base import ContentFile

    from .models import MailDraft, MailDraftAttachment
    from .views import _signature

    msg = email.message_from_bytes(raw, policy=email.policy.default)
    data = parse.parse_message(raw)
    body_html = data.get('body_html') or parse.text_to_html(data.get('body_text') or '')

    import base64
    inline = {}
    for meta in data.get('attachments', []):
        if meta.get('content_id') and (meta.get('content_type') or '').startswith('image/'):
            part = parse.get_part(raw, meta['part'])
            if part:
                inline[meta['content_id']] = (part[1], part[2])
    soup = BeautifulSoup(body_html, 'html.parser')
    used_cids = set()
    for img in soup.find_all('img'):
        src = (img.get('src') or '').strip()
        if src.lower().startswith('cid:'):
            cid = src[4:].strip('<>')
            if cid in inline:
                ctype, blob = inline[cid]
                img['src'] = f'data:{ctype};base64,' + base64.b64encode(blob).decode()
                used_cids.add(cid)
            else:
                img.decompose()

    sig = _signature(account).replace('<p><br></p>', '', 1)
    _swap_signature(soup, sig)
    if notice:
        _insert_notice(soup)

    draft = MailDraft.objects.create(
        account=account,
        to=_addresses(msg, 'To'), cc=_addresses(msg, 'Cc'), bcc=_addresses(msg, 'Bcc'),
        subject=data.get('subject') or '',
        body_html=str(soup),
        in_reply_to=data.get('in_reply_to') or '', references=data.get('references') or '',
    )
    for meta in data.get('attachments', []):
        if meta.get('content_id') in used_cids:
            continue
        part = parse.get_part(raw, meta['part'])
        if not part:
            continue
        filename, ctype, blob = part
        filename = (meta.get('filename') or filename or 'attachment')[:255]
        att = MailDraftAttachment(draft=draft, filename=filename, content_type=ctype or '', size=len(blob))
        att.file.save(filename, ContentFile(blob), save=False)
        att.save()
    return draft


ARCHIVE_ROOT = 'Gmail Archive'
LABEL_FOLDERS = [
    ('\\Sent', 'Sent'),
    ('INBOX/PayHip', 'PayHip'),
    ('INBOX/inContact FNB', 'FNB inContact'),
    ('\\Inbox', 'Inbox'),
]
OTHER_FOLDER = 'Archived'
SKIP_LABELS = {'\\Draft', '\\Spam', '\\Trash'}

_LABELS = re.compile(rb'X-GM-LABELS \(((?:[^()"]|"(?:[^"\\]|\\.)*")*)\)')
_MSGID = re.compile(rb'X-GM-MSGID (\d+)')
_UID = re.compile(rb'UID (\d+)')
_FLAGS = re.compile(rb'FLAGS \(([^)]*)\)')
_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')
_LABEL_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


def parse_labels(raw):
    """Split an ``X-GM-LABELS`` list into label names, unquoting quoted ones."""
    out = []
    for quoted, bare in _LABEL_TOKEN.findall(raw):
        out.append(quoted.replace('\\"', '"').replace('\\\\', '\\') if quoted else bare)
    return out


def folder_for(labels):
    """The archive sub-folder for a message with ``labels``, or ``None`` to skip it."""
    if SKIP_LABELS & set(labels):
        return None
    for label, folder in LABEL_FOLDERS:
        if label in labels:
            return folder
    return OTHER_FOLDER


def _human(size):
    """``size`` in bytes as ``'1.2 MB'`` or ``'12 KB'``."""
    return f'{size / 1048576:.1f} MB' if size >= 1048576 else f'{max(size // 1024, 1)} KB'


def _note_part(text):
    """An inline text/plain part holding ``text``, quoted-printable so it stays readable in the raw message."""
    from email.charset import QP, Charset
    from email.mime.text import MIMEText
    charset = Charset('utf-8')
    charset.body_encoding = QP
    note = MIMEText(text, 'plain', charset)
    note['Content-Disposition'] = 'inline'
    return note


DEDUPE_MIN_BYTES = 50 * 1024


def slim_message(raw, seen, *, max_bytes=None, gmail_address='info@metanoiagroup.co.za'):
    """``(bytes, saved)``: ``raw`` with repeated or over-size attachments swapped for notes.

    ``seen`` maps a file's SHA-256 to ``"<subject>" (<date>)`` of the first
    e-mail it was archived with, and is updated. Inline pictures and the
    message text are never touched.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    subject = _short(str(email.header.make_header(email.header.decode_header(msg.get('Subject', '')))))[:120]
    try:
        when = parsedate_to_datetime(msg.get('Date')).strftime('%d %b %Y')
    except Exception:
        when = ''
    changed = False
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get('Content-Disposition') or '').lower()
        filename = part.get_filename()
        if not filename or (disposition.startswith('inline') and part.get('Content-ID')
                            and part.get_content_maintype() == 'image'):
            continue
        payload = part.get_payload(decode=True) or b''
        if len(payload) < DEDUPE_MIN_BYTES:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        if digest in seen:
            note = (f'[Attachment "{filename}" ({_human(size)}) left out of this archive copy: '
                    f'the same file is attached to the archived e-mail {seen[digest]}.]')
        elif max_bytes and size > max_bytes:
            note = (f'[Attachment "{filename}" ({_human(size)}) left out of this archive copy because of its size. '
                    f'The original e-mail, with the file, is still in {gmail_address}.]')
        else:
            seen[digest] = f'"{subject or "(no subject)"}"' + (f' of {when}' if when else '')
            continue
        replacement = _note_part(note)
        for key in list(part.keys()):
            del part[key]
        for key, value in replacement.items():
            part[key] = value
        part.set_payload(replacement.get_payload())
        changed = True
    if not changed:
        return raw, 0
    out = io.BytesIO()
    BytesGenerator(out, mangle_from_=False, policy=email.policy.compat32.clone(linesep='\r\n')).flatten(msg)
    data = out.getvalue()
    return data, max(len(raw) - len(data), 0)


class ArchiveCopier:
    """Copy Gmail's All Mail into ``<ARCHIVE_ROOT>.<folder>`` on a Metanoia mailbox.

    ``state`` (a JSON file) remembers each copied Gmail message id and the
    file hashes already archived, so a run stopped by Gmail's daily download
    limit carries on where it left off.
    """

    BATCH = 15

    def __init__(self, gmail_user, gmail_password, account, state_path, *, max_attachment_mb=None,
                 dedupe=True, log=print):
        """Set up the copier and load any saved ``state``.

        ``max_attachment_mb`` turns larger files into notes; ``dedupe`` turns
        repeated files into notes; ``log`` receives progress lines.
        """
        self.gmail_user, self.gmail_password = gmail_user, gmail_password
        self.account = account
        self.state_path = state_path
        self.max_bytes = int(max_attachment_mb * 1048576) if max_attachment_mb else None
        self.dedupe = dedupe
        self.log = log
        self.state = {'done': [], 'seen': {}, 'stats': {'copied': 0, 'bytes_in': 0, 'bytes_out': 0}}
        try:
            with open(state_path) as fh:
                self.state.update(json.load(fh))
        except FileNotFoundError:
            pass
        self.done = set(self.state['done'])

    def save(self):
        """Write ``state`` atomically (to a temporary file, then renamed over the old one)."""
        self.state['done'] = sorted(self.done)
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(self.state, fh)
        import os
        os.replace(tmp, self.state_path)

    def _target(self):
        """Connect to the Metanoia mailbox; return ``(client, hierarchy_delimiter)``."""
        from . import imap
        client = imap.connect(self.account)
        delim = next((d for _p, d, _f, _r in client.list_folders() if d), '.') or '.'
        return client, delim

    def _ensure_folders(self, client, delim):
        """Create and subscribe the archive folders; return ``{sub_folder: full_path}``."""
        existing = {p for p, _d, _f, _r in client.list_folders()}
        paths = [ARCHIVE_ROOT] + [f'{ARCHIVE_ROOT}{delim}{f}' for f in
                                  [f for _l, f in LABEL_FOLDERS] + [OTHER_FOLDER]]
        for path in paths:
            if path not in existing:
                client.create_folder(path)
                client.conn.subscribe(_q(path))
        return {p.split(delim)[-1]: p for p in paths[1:]}

    def run(self, limit=None):
        """Copy the messages not yet done (at most ``limit``), in batches of :attr:`BATCH`.

        State is saved after every batch and on the way out, so an interrupted
        run resumes. Returns the running totals (copied, bytes in and out).
        """
        gmail = gmail_connect(self.gmail_user, self.gmail_password)
        gmail.select(_q(GMAIL_ALL), readonly=True)
        typ, data = gmail.uid('FETCH', '1:*', '(UID X-GM-MSGID X-GM-LABELS)')
        todo = []
        for entry in data:
            line = entry[0] if isinstance(entry, tuple) else entry
            if not isinstance(line, bytes):
                continue
            uid, mid, labels = _UID.search(line), _MSGID.search(line), _LABELS.search(line)
            if not (uid and mid):
                continue
            mid = mid.group(1).decode()
            folder = folder_for(parse_labels(labels.group(1).decode('utf-8', 'replace')) if labels else [])
            if folder and mid not in self.done:
                todo.append((int(uid.group(1)), mid, folder))
        todo.sort()
        if limit:
            todo = todo[:limit]
        self.log(f'{len(todo)} message(s) to copy, {len(self.done)} already done.')

        client, delim = self._target()
        folders = self._ensure_folders(client, delim)
        stats = self.state['stats']
        try:
            for start in range(0, len(todo), self.BATCH):
                chunk = todo[start:start + self.BATCH]
                by_uid = {u: (m, f) for u, m, f in chunk}
                typ, data = gmail.uid('FETCH', ','.join(str(u) for u, _m, _f in chunk),
                                      '(UID FLAGS INTERNALDATE BODY.PEEK[])')
                if typ != 'OK':
                    raise imaplib.IMAP4.error(f'Gmail refused the fetch: {data}')
                for i, entry in enumerate(data):
                    if not isinstance(entry, tuple):
                        continue
                    head, raw = entry[0], entry[1]
                    tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b''
                    uid = _UID.search(head) or _UID.search(tail)
                    if not uid or int(uid.group(1)) not in by_uid:
                        continue
                    mid, folder = by_uid[int(uid.group(1))]
                    flags_m = _FLAGS.search(head) or _FLAGS.search(tail)
                    date_m = _DATE.search(head) or _DATE.search(tail)
                    flags = ' '.join(f for f in (flags_m.group(1).decode().split() if flags_m else [])
                                     if f in ('\\Seen', '\\Flagged', '\\Answered'))
                    when = imaplib.Internaldate2tuple(b'INTERNALDATE "' + date_m.group(1) + b'"') if date_m else None
                    body = raw
                    if self.dedupe or self.max_bytes:
                        seen = self.state['seen'] if self.dedupe else {}
                        try:
                            body, _saved = slim_message(raw, seen, max_bytes=self.max_bytes,
                                                        gmail_address=self.gmail_user)
                        except Exception:
                            logger.exception('gmail archive: could not slim %s; copying it whole', mid)
                            body = raw
                    typ, resp = client.conn.append(_q(folders[folder]), f'({flags})',
                                                   time.mktime(when) if when else time.time(), body)
                    if typ != 'OK':
                        raise imaplib.IMAP4.error(f'The Metanoia mail server refused a message: {resp}')
                    self.done.add(mid)
                    stats['copied'] += 1
                    stats['bytes_in'] += len(raw)
                    stats['bytes_out'] += len(body)
                self.save()
                if (start // self.BATCH) % 20 == 0:
                    self.log(f'  {stats["copied"]} copied · {stats["bytes_in"] / 1e6:.0f} MB read from Gmail · '
                             f'{stats["bytes_out"] / 1e6:.0f} MB written to the archive')
        finally:
            self.save()
            client.close()
            try:
                gmail.logout()
            except Exception:
                pass
        return stats
