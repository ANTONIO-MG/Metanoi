"""Import a folder's whole history into Metanoia Mail, not just the recent part.

A folder's first sync brings in only the last ``history_days`` (and at most
500 messages); this fetches everything still missing, without notifications.

    manage.py mail_backfill --account info@metanoiagroup.co.za --folder-prefix "Gmail Archive"
"""

from django.core.management.base import BaseCommand

from apps.mailbox import imap, sync
from apps.mailbox.models import MailItem

from ._gmail import mailbox


class Command(BaseCommand):
    help = "Import every message in a mailbox's folders (or those under --folder-prefix) into Metanoia Mail."

    def add_arguments(self, parser):
        parser.add_argument('--account', required=True)
        parser.add_argument('--folder-prefix', default='')

    def handle(self, *args, **opts):
        account = mailbox(opts['account'])
        with imap.connect(account) as client:
            sync.refresh_folders(account, client)
            folders = account.folders.all()
            if opts['folder_prefix']:
                folders = folders.filter(path__startswith=opts['folder_prefix'])
            for folder in folders:
                uidvalidity, _ = client.select(folder.path, readonly=True)
                have = set(MailItem.objects.filter(folder=folder).values_list('uid', flat=True))
                missing = [u for u in client.search_uids('ALL') if u not in have]
                for start in range(0, len(missing), sync.FETCH_BATCH):
                    for uid, flags, size, raw in client.fetch_messages(missing[start:start + sync.FETCH_BATCH]):
                        sync.store_message(account, folder, uid, flags, size, raw)
                        folder.last_uid = max(folder.last_uid, uid)
                folder.uidvalidity = folder.uidvalidity or uidvalidity
                folder.unread = folder.items.filter(is_read=False).count()
                folder.total = folder.items.count()
                if folder.synced_at is None:
                    from django.utils import timezone
                    folder.synced_at = timezone.now()
                folder.save(update_fields=['uidvalidity', 'last_uid', 'unread', 'total', 'synced_at'])
                self.stdout.write(f'{folder.path}: {len(missing)} imported, {folder.total} in all')
