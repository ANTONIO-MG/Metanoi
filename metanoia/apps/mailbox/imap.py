"""A thin wrapper over :mod:`imaplib`.

Only what Metanoia Mail needs, with imaplib's ``(typ, data)`` tuples and byte
soup turned into plain Python values. Everything talks to the server through
:class:`ImapClient`, so tests swap in a fake by patching :func:`connect`.

``TIMEOUT`` is the socket timeout, in seconds, for every connection.
``_ROLE_NAMES`` lists the folder names hosts use for each role when they do
not advertise SPECIAL-USE flags.
"""

import imaplib
import re
import socket
import ssl
import time

from .models import SECURITY_SSL, SECURITY_STARTTLS, MailFolder

TIMEOUT = 30


class MailError(Exception):
    """A mail server refused or could not be reached. The message is user-facing."""


def connect(account):
    """Open and log in to ``account``'s IMAP server; return an :class:`ImapClient`.

    The host comes from :meth:`~apps.mailbox.models.MailAccount.settings_for`
    and a per-mailbox override is vetted again before connecting
    (:func:`apps.mailbox.hosts.check_override`). Every failure is raised as a
    user-facing :class:`MailError`.
    """
    host, port, security, username = account.settings_for('imap')
    if not host:
        raise MailError('No IMAP server is set up yet. An administrator can add it under Mail settings.')
    from .hosts import UnsafeHost, check_override
    try:
        check_override(account, 'imap')
    except UnsafeHost as exc:
        raise MailError(str(exc)) from exc
    password = account.get_password()
    if not password:
        raise MailError('This mailbox has no password saved. Open Mail settings to enter it.')
    try:
        if security == SECURITY_SSL:
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT)
            if security == SECURITY_STARTTLS:
                conn.starttls(ssl_context=ssl.create_default_context())
    except (OSError, socket.timeout, imaplib.IMAP4.error, ssl.SSLError) as exc:
        raise MailError(f'Could not reach the mail server {host}:{port} ({exc}).') from exc
    try:
        conn.login(username, password)
    except imaplib.IMAP4.error as exc:
        try:
            conn.logout()
        except Exception:
            pass
        raise MailError('The mail server rejected the username or password.') from exc
    return ImapClient(conn)


def quote(name):
    """Quote an IMAP mailbox name."""
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def decode_folder_name(name):
    """Decode a modified UTF-7 folder name (RFC 3501 §5.1.3) for display."""
    out, i = [], 0
    while i < len(name):
        ch = name[i]
        if ch == '&':
            end = name.find('-', i)
            if end == -1:
                out.append(name[i:])
                break
            chunk = name[i + 1:end]
            if not chunk:
                out.append('&')
            else:
                import base64
                b64 = chunk.replace(',', '/')
                b64 += '=' * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode('utf-16-be'))
                except Exception:
                    out.append(name[i:end + 1])
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return ''.join(out)


def encode_folder_name(name):
    """Encode ``name`` as a modified UTF-7 folder name (RFC 3501 §5.1.3)."""
    import base64
    out, pending = [], ''

    def flush():
        """Append the pending non-ASCII run as one ``&…-`` chunk."""
        if pending:
            b64 = base64.b64encode(pending.encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            out.append('&' + b64 + '-')

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            pending = ''
            out.append('&-' if ch == '&' else ch)
        else:
            pending += ch
    flush()
    return ''.join(out)


_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$')
_SPECIAL_USE = {
    b'\\sent': MailFolder.ROLE_SENT, b'\\drafts': MailFolder.ROLE_DRAFTS,
    b'\\trash': MailFolder.ROLE_TRASH, b'\\junk': MailFolder.ROLE_JUNK,
    b'\\archive': MailFolder.ROLE_ARCHIVE, b'\\all': None,
}
_ROLE_NAMES = {
    MailFolder.ROLE_SENT: ('sent', 'sent items', 'sent mail', 'sent messages', 'inbox.sent'),
    MailFolder.ROLE_DRAFTS: ('drafts', 'draft', 'inbox.drafts'),
    MailFolder.ROLE_TRASH: ('trash', 'deleted items', 'deleted messages', 'bin', 'inbox.trash'),
    MailFolder.ROLE_JUNK: ('junk', 'junk email', 'junk e-mail', 'spam', 'bulk mail', 'inbox.junk', 'inbox.spam'),
    MailFolder.ROLE_ARCHIVE: ('archive', 'archives', 'inbox.archive'),
}


def guess_role(path, flags=()):
    """The folder role for ``path``: from its SPECIAL-USE ``flags``, else its name; ``''`` if none."""
    for flag in flags:
        role = _SPECIAL_USE.get(flag.lower())
        if role:
            return role
    low = path.lower().strip()
    if low == 'inbox':
        return MailFolder.ROLE_INBOX
    leaf = low.split('/')[-1]
    for role, names in _ROLE_NAMES.items():
        if low in names or leaf in names or low.replace('[gmail]/', '') in names:
            return role
    return ''


def _unquote(raw):
    """Decode a possibly quoted IMAP string from bytes."""
    raw = raw.strip()
    if raw.startswith(b'"') and raw.endswith(b'"'):
        raw = raw[1:-1].replace(b'\\"', b'"').replace(b'\\\\', b'\\')
    return raw.decode('utf-8', 'replace')


_FETCH_UID = re.compile(rb'UID (\d+)')
_FETCH_FLAGS = re.compile(rb'FLAGS \(([^)]*)\)')
_FETCH_SIZE = re.compile(rb'RFC822\.SIZE (\d+)')


def _flags(raw):
    """A FLAGS list as a set of lower-case strings."""
    return {f.decode('ascii', 'ignore').lower() for f in raw.split()}


class ImapClient:
    """A logged-in IMAP connection with plain-Python results; a context manager that logs out on exit."""

    def __init__(self, conn):
        """Wrap the logged-in :mod:`imaplib` connection ``conn``."""
        self.conn = conn
        self.selected = None

    def __enter__(self):
        """Return the client itself."""
        return self

    def __exit__(self, *exc):
        """Close the connection."""
        self.close()

    def close(self):
        """Close the selected folder and log out, ignoring errors."""
        try:
            if self.selected:
                self.conn.close()
        except Exception:
            pass
        try:
            self.conn.logout()
        except Exception:
            pass

    def _ok(self, result, what):
        """Return the data of an ``(typ, data)`` result, or raise :class:`MailError` saying the server would not ``what``."""
        typ, data = result
        if typ != 'OK':
            detail = b' '.join(d for d in data if isinstance(d, bytes)).decode('utf-8', 'replace')
            raise MailError(f'The mail server would not {what}: {detail or typ}')
        return data

    @property
    def capabilities(self):
        """The server's advertised capabilities, upper-cased."""
        return {c.upper() for c in getattr(self.conn, 'capabilities', ())}

    def list_folders(self):
        """``[(path, delimiter, flags, role)]`` for every selectable folder.

        A tuple entry is a folder whose name the server sent as a literal.
        """
        data = self._ok(self.conn.list(), 'list folders')
        folders = []
        for entry in data:
            if entry is None:
                continue
            if isinstance(entry, tuple):
                head, literal = entry
                match = _LIST_RE.match(head.rstrip() + b' x')
                name = literal.decode('utf-8', 'replace')
            else:
                match = _LIST_RE.match(entry)
                name = _unquote(match.group('name')) if match else ''
            if not match or not name:
                continue
            flags = match.group('flags').split()
            if any(f.lower() == b'\\noselect' or f.lower() == b'\\nonexistent' for f in flags):
                continue
            delim = match.group('delim')
            delim = '' if delim == b'NIL' else _unquote(delim)
            folders.append((name, delim, flags, guess_role(name, flags)))
        return folders

    def select(self, path, readonly=True):
        """Select folder ``path``; return ``(uidvalidity, message_count)``."""
        typ, data = self.conn.select(quote(path), readonly=readonly)
        if typ != 'OK':
            raise MailError(f'Could not open the folder "{decode_folder_name(path)}".')
        self.selected = path
        uidvalidity = None
        _, resp = self.conn.response('UIDVALIDITY')
        if resp and resp[0]:
            try:
                uidvalidity = int(resp[0])
            except (TypeError, ValueError):
                pass
        try:
            exists = int(data[0])
        except (TypeError, ValueError, IndexError):
            exists = 0
        return uidvalidity, exists

    def status(self, path):
        """``{'messages': n, 'unseen': n}`` without selecting the folder."""
        typ, data = self.conn.status(quote(path), '(MESSAGES UNSEEN)')
        out = {'messages': 0, 'unseen': 0}
        if typ == 'OK' and data and data[0]:
            for key in out:
                m = re.search(key.upper().encode() + rb' (\d+)', data[0])
                if m:
                    out[key] = int(m.group(1))
        return out

    def search_uids(self, criteria):
        """UIDs in the selected folder matching the IMAP SEARCH ``criteria``."""
        data = self._ok(self.conn.uid('SEARCH', None, criteria), 'search the folder')
        return [int(u) for u in (data[0] or b'').split()] if data else []

    def uids_after(self, last_uid):
        """UIDs greater than ``last_uid`` (``N:*`` can return the last message even when it is lower)."""
        return [u for u in self.search_uids(f'UID {last_uid + 1}:*') if u > last_uid]

    def uids_since(self, date):
        """UIDs of messages received on or after ``date``."""
        return self.search_uids('SINCE ' + date.strftime('%d-%b-%Y'))

    def fetch_messages(self, uids):
        """``[(uid, flags, size, raw_bytes)]``.

        Some servers (Gmail among them) send UID and FLAGS after the literal,
        in the closing ``b' UID 5 FLAGS (...))'`` bytes that follow it, so both
        places are searched.
        """
        if not uids:
            return []
        data = self._ok(self.conn.uid('FETCH', ','.join(map(str, uids)),
                                      '(UID FLAGS RFC822.SIZE BODY.PEEK[])'), 'fetch messages')
        out = []
        for i, entry in enumerate(data):
            if not isinstance(entry, tuple) or len(entry) < 2:
                continue
            head, raw = entry[0], entry[1]
            tail = data[i + 1] if i + 1 < len(data) and isinstance(data[i + 1], bytes) else b''
            uid = _FETCH_UID.search(head) or _FETCH_UID.search(tail)
            if not uid:
                continue
            flags = _FETCH_FLAGS.search(head) or _FETCH_FLAGS.search(tail)
            size = _FETCH_SIZE.search(head) or _FETCH_SIZE.search(tail)
            out.append((int(uid.group(1)), _flags(flags.group(1)) if flags else set(),
                        int(size.group(1)) if size else len(raw), raw))
        return out

    def fetch_flags(self, low_uid):
        """``{uid: flags}`` for every message from ``low_uid`` up."""
        data = self._ok(self.conn.uid('FETCH', f'{max(low_uid, 1)}:*', '(UID FLAGS)'), 'read message flags')
        out = {}
        for entry in data:
            line = entry[0] if isinstance(entry, tuple) else entry
            if not isinstance(line, bytes):
                continue
            uid = _FETCH_UID.search(line)
            flags = _FETCH_FLAGS.search(line)
            if uid:
                out[int(uid.group(1))] = _flags(flags.group(1)) if flags else set()
        return out

    def set_flag(self, uid, flag, on=True):
        """Add (or with ``on=False`` remove) ``flag`` on message ``uid``."""
        self._ok(self.conn.uid('STORE', str(uid), '+FLAGS' if on else '-FLAGS', f'({flag})'), 'update the message')

    def move(self, uid, dest_path):
        """Move message ``uid`` to ``dest_path``, with MOVE or else COPY plus delete."""
        if 'MOVE' in self.capabilities:
            self._ok(self.conn.uid('MOVE', str(uid), quote(dest_path)), 'move the message')
            return
        self._ok(self.conn.uid('COPY', str(uid), quote(dest_path)), 'copy the message')
        self.delete(uid)

    def delete(self, uid):
        """Delete permanently (the message is already in Deleted Items, or the caller means it)."""
        self._ok(self.conn.uid('STORE', str(uid), '+FLAGS', r'(\Deleted)'), 'delete the message')
        if 'UIDPLUS' in self.capabilities:
            self.conn.uid('EXPUNGE', str(uid))
        else:
            self.conn.expunge()

    def append(self, path, raw, flags=r'(\Seen)'):
        """Store ``raw`` in folder ``path`` (read, by default), e.g. a copy of a sent message."""
        self._ok(self.conn.append(quote(path), flags, imaplib.Time2Internaldate(time.time()), raw),
                 'save a copy of the message')

    def create_folder(self, path):
        """Create folder ``path`` on the server."""
        self._ok(self.conn.create(quote(path)), 'create the folder')
