"""What the toolbar does to messages: read/unread, flag, move, delete.

Each call changes the server first and the local copy after, so the two never
disagree for long. Moves delete the local row and re-sync the destination
folder, which is where the message's new UID comes from.
"""

from collections import defaultdict

from . import imap, sync
from .models import MailFolder, MailItem

FLAG_SEEN = r'\Seen'
FLAG_FLAGGED = r'\Flagged'


def _by_folder(items):
    """Group ``items`` into a ``{folder: [items]}`` mapping."""
    groups = defaultdict(list)
    for item in items:
        groups[item.folder].append(item)
    return groups


def set_read(account, items, read=True):
    """Mark ``items`` read (or unread) locally and on the server, then recount their folders."""
    MailItem.objects.filter(pk__in=[i.pk for i in items]).update(is_read=read)
    with imap.connect(account) as client:
        for folder, group in _by_folder(items).items():
            client.select(folder.path, readonly=False)
            for item in group:
                client.set_flag(item.uid, FLAG_SEEN, read)
            _recount(folder)


def set_read_in_background(account, item):
    """Opening a message marks it read locally now and on the server soon."""
    if item.is_read:
        return
    MailItem.objects.filter(pk=item.pk).update(is_read=True)
    _recount(item.folder)
    sync.run_in_background(_store_seen, account.pk, item.pk)


def _store_seen(account_id, item_id):
    """Background task: set ``\\Seen`` on the server for one message, if it still exists."""
    item = MailItem.objects.select_related('folder', 'account').filter(pk=item_id).first()
    if item is None:
        return
    with imap.connect(item.account) as client:
        client.select(item.folder.path, readonly=False)
        client.set_flag(item.uid, FLAG_SEEN, True)


def set_flagged(account, items, flagged=True):
    """Flag (or unflag) ``items`` locally and on the server."""
    MailItem.objects.filter(pk__in=[i.pk for i in items]).update(is_flagged=flagged)
    with imap.connect(account) as client:
        for folder, group in _by_folder(items).items():
            client.select(folder.path, readonly=False)
            for item in group:
                client.set_flag(item.uid, FLAG_FLAGGED, flagged)


def move(account, items, dest):
    """Move ``items`` to the folder ``dest`` on the server.

    The local rows are deleted and ``dest`` is re-synced, which is where the
    messages' new UIDs come from. Raises :class:`~apps.mailbox.imap.MailError`
    when ``dest`` is ``None``.
    """
    if dest is None:
        raise imap.MailError('That folder does not exist on this mailbox.')
    with imap.connect(account) as client:
        for folder, group in _by_folder(items).items():
            if folder.pk == dest.pk:
                continue
            client.select(folder.path, readonly=False)
            for item in group:
                client.move(item.uid, dest.path)
            MailItem.objects.filter(pk__in=[i.pk for i in group]).delete()
            _recount(folder)
        sync.sync_folder(account, client, dest, notify=False)


def delete(account, items):
    """Outlook semantics: to Deleted Items, or for good if already there."""
    trash = account.folder(MailFolder.ROLE_TRASH)
    in_trash = [i for i in items if trash is None or i.folder_id == trash.pk]
    to_trash = [i for i in items if i not in in_trash]
    if to_trash:
        move(account, to_trash, trash)
    if in_trash:
        with imap.connect(account) as client:
            for folder, group in _by_folder(in_trash).items():
                client.select(folder.path, readonly=False)
                for item in group:
                    client.delete(item.uid)
                MailItem.objects.filter(pk__in=[i.pk for i in group]).delete()
                _recount(folder)


def create_folder(account, name):
    """Create a folder called ``name`` on the server and refresh the folder list.

    On Courier/Dovecot servers with an ``INBOX.`` namespace every folder lives
    under INBOX, so the new folder is created there too when the existing
    folders show that layout. Raises :class:`~apps.mailbox.imap.MailError` for
    an empty name.
    """
    name = imap.encode_folder_name(name.strip().replace('/', '-'))
    if not name:
        raise imap.MailError('Give the folder a name.')
    inbox = account.inbox
    if inbox and inbox.delimiter and account.folders.filter(
            path__startswith=inbox.path + inbox.delimiter).exists():
        name = inbox.path + inbox.delimiter + name
    with imap.connect(account) as client:
        client.create_folder(name)
        sync.refresh_folders(account, client)


def _recount(folder):
    """Refresh ``folder``'s cached unread and total counts from its local items."""
    MailFolder.objects.filter(pk=folder.pk).update(
        unread=folder.items.filter(is_read=False).count(), total=folder.items.count())
