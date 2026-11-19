"""Copy the Gmail drafts into Metanoia Mail mailboxes' Drafts.

    GMAIL_APP_PASSWORD=... manage.py copy_gmail_drafts \\
        --to info@metanoiagroup.co.za --to admin@metanoiagroup.co.za

Each copy keeps the recipients, subject, body, attachments and reply
threading; the Gmail signature becomes the mailbox's Metanoia signature, and the
"this is Metanoia Group on our new email accounts" line goes in after the
greeting (``--no-notice`` leaves it out). Nothing is sent. A draft already
copied to a mailbox (same subject and recipients) is skipped, so re-running is
safe.
"""

from django.core.management.base import BaseCommand

from apps.mailbox import gmail_import
from apps.mailbox.models import MailDraft

from ._gmail import gmail_login, mailbox


class Command(BaseCommand):
    help = 'Copy every Gmail draft into the given Metanoia Mail mailboxes (nothing is sent).'

    def add_arguments(self, parser):
        parser.add_argument('--to', action='append', required=True, help='Target mailbox address (repeatable).')
        parser.add_argument('--gmail-user', default='')
        parser.add_argument('--no-notice', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        import email
        import email.policy
        targets = [mailbox(a) for a in opts['to']]
        user, password = gmail_login(opts)
        conn = gmail_import.gmail_connect(user, password)
        try:
            drafts = gmail_import.fetch_drafts(conn)
        finally:
            conn.logout()
        self.stdout.write(f'{len(drafts)} Gmail draft(s) found.')
        made = skipped = 0
        for _uid, raw in drafts:
            head = email.message_from_bytes(raw, policy=email.policy.default)
            subject = str(head.get('Subject') or '')
            to = gmail_import._addresses(head, 'To')
            for account in targets:
                if MailDraft.objects.filter(account=account, subject=subject, to=to).exists():
                    skipped += 1
                    continue
                if opts['dry_run']:
                    self.stdout.write(f'  would copy to {account.email_address}: {subject or "(no subject)"} → {to}')
                    continue
                draft = gmail_import.draft_from_raw(raw, account, notice=not opts['no_notice'])
                made += 1
                self.stdout.write(f'  {account.email_address}: {draft.subject or "(no subject)"} '
                                  f'→ {draft.to or "(no recipient)"} · {draft.attachments.count()} file(s)')
        self.stdout.write(self.style.SUCCESS(f'{made} draft(s) created, {skipped} already there.'))
