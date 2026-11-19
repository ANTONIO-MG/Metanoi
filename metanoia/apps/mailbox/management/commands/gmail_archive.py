"""Copy all Gmail mail into a "Gmail Archive" folder tree on one Metanoia mailbox.

    GMAIL_APP_PASSWORD=... manage.py gmail_archive --to info@metanoiagroup.co.za \\
        --max-attachment-mb 5 --state ~/metanoia-backups/gmail-archive/state.json

Messages are copied with IMAP APPEND (original bytes, dates and read/flag
state), so nothing is re-sent and nobody is copied in. Sub-folders: Inbox,
Sent, PayHip, FNB inContact and Archived (everything else); Drafts, Spam and
Trash are left out. A file already archived on an earlier e-mail, or one over
--max-attachment-mb, becomes a short note saying where the file is; the Gmail
account itself is never changed. Gmail lets IMAP download about 2.5 GB a day:
if it stops the run, start it again later and it carries on from the state file.
"""

import os

from django.core.management.base import BaseCommand

from apps.mailbox import gmail_import

from ._gmail import gmail_login, mailbox


class Command(BaseCommand):
    help = 'Copy the Gmail mailbox into a "Gmail Archive" folder on a Metanoia mailbox (nothing is sent).'

    def add_arguments(self, parser):
        parser.add_argument('--to', required=True, help='The Metanoia mailbox that holds the archive.')
        parser.add_argument('--gmail-user', default='')
        parser.add_argument('--state', default=os.path.expanduser('~/metanoia-backups/gmail-archive/state.json'))
        parser.add_argument('--max-attachment-mb', type=float, default=None,
                            help='Leave files bigger than this in Gmail (a note takes their place).')
        parser.add_argument('--no-dedupe', action='store_true', help='Keep repeated files on every e-mail.')
        parser.add_argument('--limit', type=int, default=None, help='Copy at most this many (for a trial run).')

    def handle(self, *args, **opts):
        account = mailbox(opts['to'])
        user, password = gmail_login(opts)
        os.makedirs(os.path.dirname(opts['state']), exist_ok=True)
        copier = gmail_import.ArchiveCopier(user, password, account, opts['state'],
                                            max_attachment_mb=opts['max_attachment_mb'],
                                            dedupe=not opts['no_dedupe'], log=self.stdout.write)
        stats = copier.run(limit=opts['limit'])
        self.stdout.write(self.style.SUCCESS(
            f'Done: {stats["copied"]} copied in all; {stats["bytes_in"] / 1e6:.0f} MB read, '
            f'{stats["bytes_out"] / 1e6:.0f} MB in the archive.'))
