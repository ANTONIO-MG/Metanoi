"""Bring the local copy of a mailbox up to date with the server.

Three ways in:

* :func:`sync_all` — the ``mail-sync`` scheduler job, every mailbox, every folder;
* :func:`sync_account` — the mailbox page's "Send/Receive" and its refresh;
* :func:`poll` — called while someone has any page open (see chrome.js), it
  kicks off a throttled inbox check in a background thread so new mail is
  noticed within a minute or so without the page waiting on the mail server.

A per-mailbox cache lock keeps two syncs of one mailbox from racing to insert
the same message.

Tuning constants:

``INITIAL_LIMIT``
    Most messages imported from one folder the first time it is synced (going
    back ``MailServer.history_days``).
``FETCH_BATCH``
    Messages fetched per IMAP FETCH.
``FLAG_WINDOW``
    Flag and deletion changes are mirrored for this many of a folder's newest
    messages.
``POLL_EVERY``
    Seconds before a page-open poll may re-check one mailbox.
``RUN_IN_THREAD``
    Tests turn this off so background work happens inline, inside the test
    transaction.
"""

import logging
import threading
from datetime import timedelta

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.utils import timezone

from . import imap, parse
from .models import MailAccount, MailAttachment, MailFolder, MailItem, MailServer

logger = logging.getLogger('apps')

INITIAL_LIMIT = 500
FETCH_BATCH = 20
FLAG_WINDOW = 2000
POLL_EVERY = 45

RUN_IN_THREAD = True


def _lock_key(account):
    """Cache key of the per-mailbox sync lock."""
    return f'mailbox:sync:{account.pk}'


def run_in_background(fn, *args, **kwargs):
    """Run ``fn`` off the request thread, with its own database connection."""
    if not RUN_IN_THREAD:
        return fn(*args, **kwargs)

    def target():
        """Run ``fn`` with fresh database connections, logging any failure."""
        close_old_connections()
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception('mailbox: background task failed')
        finally:
            connections.close_all()

    threading.Thread(target=target, daemon=True, name='metanoia-mail').start()
    return None


def sync_all():
    """The ``mail-sync`` job. Returns a one-line summary for the job log."""
    done, new, failed = 0, 0, 0
    for account in MailAccount.objects.filter(is_active=True).exclude(password_encrypted='').select_related('user'):
        result = sync_account(account)
        if result is None:
            continue
        if result.get('error'):
            failed += 1
        else:
            done += 1
            new += result['new']
    return f'{done} mailbox(es) synced, {new} new message(s), {failed} failed'


def sync_inboxes():
    """The ``mail-inbox`` job: every mailbox's inbox only, so new mail is announced
    (bell, sound, desktop alert) within minutes even when nobody has a page open."""
    done, new = 0, 0
    for account in MailAccount.objects.filter(is_active=True).exclude(password_encrypted=''):
        result = sync_account(account, only=[MailFolder.ROLE_INBOX])
        if result and not result.get('error'):
            done += 1
            new += result['new']
    return f'{done} inbox(es) checked, {new} new message(s)'


def poll(user):
    """Start a background inbox check for each of ``user``'s mailboxes, if due."""
    for account in MailAccount.objects.filter(user=user, is_active=True).exclude(password_encrypted=''):
        if cache.add(f'mailbox:poll:{account.pk}', 1, POLL_EVERY):
            run_in_background(sync_account, account, only=[MailFolder.ROLE_INBOX])


def sync_account(account, *, only=None, folder=None, notify=True):
    """Sync one mailbox. ``only`` limits it to folder roles; ``folder`` to one folder.

    Returns ``{'new': n, 'error': ''}``, or ``None`` if another sync of this
    mailbox is already running.

    A full sync re-lists folders (one cheap LIST); a targeted one only does so
    when there are none yet. Any unexpected error is logged and recorded on
    the mailbox rather than raised, so a server quirk cannot take the
    scheduler job down.
    """
    if not cache.add(_lock_key(account), 1, 300):
        return None
    new = 0
    try:
        with imap.connect(account) as client:
            if (only is None and folder is None) or not account.folders.exists():
                refresh_folders(account, client)
            folders = account.folders.all()
            if folder is not None:
                folders = folders.filter(pk=folder.pk)
            elif only:
                folders = folders.filter(role__in=only)
            for f in sorted(folders, key=lambda f: f.sort_key):
                new += sync_folder(account, client, f, notify=notify)
        account.last_sync_at = timezone.now()
        account.last_error = ''
        account.save(update_fields=['last_sync_at', 'last_error'])
        return {'new': new, 'error': ''}
    except imap.MailError as exc:
        _record_error(account, str(exc))
        return {'new': new, 'error': str(exc)}
    except Exception as exc:
        logger.exception('mailbox: sync of %s failed', account)
        _record_error(account, f'Sync failed: {exc}')
        return {'new': new, 'error': str(exc)}
    finally:
        cache.delete(_lock_key(account))


def _record_error(account, message):
    """Store ``message`` as the mailbox's last sync error."""
    account.last_error = message[:1000]
    account.last_error_at = timezone.now()
    account.save(update_fields=['last_error', 'last_error_at'])


def refresh_folders(account, client):
    """Mirror the server's folder list: add new folders, update roles, drop vanished ones.

    Each role is held by one folder only: a host advertising two "Sent"
    folders keeps the first (the inbox wins ties).
    """
    seen = set()
    for path, delim, _flags, role in client.list_folders():
        seen.add(path)
        folder, _ = MailFolder.objects.get_or_create(
            account=account, path=path,
            defaults={'name': imap.decode_folder_name(path.split(delim)[-1] if delim else path),
                      'delimiter': delim or '', 'role': role})
        changed = False
        if folder.role != role and role:
            folder.role, changed = role, True
        if folder.delimiter != (delim or ''):
            folder.delimiter, changed = delim or '', True
        if changed:
            folder.save(update_fields=['role', 'delimiter'])
    account.folders.exclude(path__in=seen).delete()
    claimed = set()
    for folder in sorted(account.folders.exclude(role=''), key=lambda f: (f.role != 'inbox', f.path)):
        if folder.role in claimed:
            folder.role = ''
            folder.save(update_fields=['role'])
        claimed.add(folder.role)


def sync_folder(account, client, folder, *, notify=True):
    """Fetch what is new in ``folder`` and mirror flags/deletions. Returns the new count.

    When the server's UIDVALIDITY changes it has renumbered the folder, so
    every UID held locally is meaningless: the local copy is dropped and the
    folder is imported afresh. A first import is history, not news, so it
    never notifies.
    """
    uidvalidity, _exists = client.select(folder.path, readonly=True)
    first = folder.synced_at is None
    if folder.uidvalidity and uidvalidity and uidvalidity != folder.uidvalidity:
        folder.items.all().delete()
        folder.last_uid = 0
        first = True
    folder.uidvalidity = uidvalidity

    if first:
        days = MailServer.get().history_days
        uids = client.uids_since(timezone.now() - timedelta(days=days))[-INITIAL_LIMIT:]
        notify = False
    else:
        uids = client.uids_after(folder.last_uid)

    fresh = []
    for start in range(0, len(uids), FETCH_BATCH):
        for uid, flags, size, raw in client.fetch_messages(uids[start:start + FETCH_BATCH]):
            item = store_message(account, folder, uid, flags, size, raw)
            if item is not None:
                fresh.append(item)
            folder.last_uid = max(folder.last_uid, uid)

    _mirror_flags(client, folder)

    folder.unread = folder.items.filter(is_read=False).count()
    folder.total = folder.items.count()
    folder.synced_at = timezone.now()
    folder.save(update_fields=['uidvalidity', 'last_uid', 'unread', 'total', 'synced_at'])

    if notify and folder.role == MailFolder.ROLE_INBOX:
        announce(account, [i for i in fresh if not i.is_read])
    return len(fresh)


def store_message(account, folder, uid, flags, size, raw):
    """Save one fetched message with its attachments; ``None`` if it is already stored.

    A message that cannot be parsed is still stored, with a placeholder
    subject. If a concurrent sync stored it first (unique constraint), the
    file just written is deleted and ``None`` returned.
    """
    if MailItem.objects.filter(folder=folder, uid=uid).exists():
        return None
    try:
        data = parse.parse_message(raw)
    except Exception:
        logger.exception('mailbox: could not parse uid %s in %s', uid, folder)
        data = {'subject': '(message could not be read)', 'attachments': [], 'date': timezone.now()}
    attachments = data.pop('attachments', [])
    item = MailItem(account=account, folder=folder, uid=uid, size=size or len(raw),
                    is_read='\\seen' in flags, is_flagged='\\flagged' in flags,
                    is_answered='\\answered' in flags, is_draft='\\draft' in flags, **data)
    item.raw.save('message.eml', ContentFile(raw), save=False)
    try:
        with transaction.atomic():
            item.save()
            MailAttachment.objects.bulk_create(MailAttachment(item=item, **a) for a in attachments)
    except IntegrityError:
        item.raw.storage.delete(item.raw.name)
        return None
    return item


def _mirror_flags(client, folder):
    """Copy server flags onto the newest :data:`FLAG_WINDOW` local messages and drop ones gone from the server."""
    local = dict(folder.items.order_by('-uid').values_list('uid', 'id')[:FLAG_WINDOW])
    if not local:
        return
    server = client.fetch_flags(min(local))
    gone = [pk for uid, pk in local.items() if uid not in server]
    if gone:
        MailItem.objects.filter(pk__in=gone).delete()
    for item in folder.items.filter(uid__in=[u for u in local if u in server]).only(
            'id', 'uid', 'is_read', 'is_flagged', 'is_answered'):
        flags = server[item.uid]
        read, flagged, answered = '\\seen' in flags, '\\flagged' in flags, '\\answered' in flags
        if (read, flagged, answered) != (item.is_read, item.is_flagged, item.is_answered):
            MailItem.objects.filter(pk=item.pk).update(is_read=read, is_flagged=flagged, is_answered=answered)


def announce(account, items):
    """Raise a platform notification (bell + Notifications badge) for new mail.

    Up to three messages are announced one by one, more as a single summary.
    ``alert_kind='mail'`` makes the bell play the mail chime, and
    ``desktop=True`` raises a system notification in browsers that switched
    desktop alerts on.
    """
    if not items or not account.notify_new_mail:
        return
    from apps.communication.services import notify
    try:
        if len(items) <= 3:
            for item in items:
                notify(account.user, verb='sent you an email', level='info',
                       title=f'New email from {item.sender_display}'[:200],
                       body=(item.subject or '(no subject)') + (f' — {item.snippet[:140]}' if item.snippet else ''),
                       url=item.get_absolute_url(), alert_kind='mail', desktop=True)
        else:
            newest = max(items, key=lambda i: i.date)
            notify(account.user, verb='new email', level='info',
                   title=f'{len(items)} new emails in {account.email_address}',
                   body='Latest: ' + (newest.subject or '(no subject)') + f' — from {newest.sender_display}',
                   url=newest.get_absolute_url(), alert_kind='mail', desktop=True)
    except Exception:
        logger.exception('mailbox: could not raise new-mail notification')
