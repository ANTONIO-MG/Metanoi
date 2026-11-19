"""Give Metanoia Mail mailboxes the Metanoia signature (apps/mailbox/signatures.py).

    manage.py mail_signatures              # only mailboxes with no signature yet
    manage.py mail_signatures --force      # rebuild every one
    manage.py mail_signatures --account admin@metanoiagroup.co.za --force
"""

from django.core.management.base import BaseCommand

from apps.mailbox import signatures
from apps.mailbox.models import MailAccount


class Command(BaseCommand):
    help = 'Fill in (or, with --force, rebuild) the Metanoia signature on Metanoia Mail mailboxes.'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Replace signatures that are already set.')
        parser.add_argument('--account', action='append', default=[], help='Only this address (repeatable).')

    def handle(self, *args, **opts):
        accounts = MailAccount.objects.select_related('user').order_by('email_address')
        if opts['account']:
            accounts = accounts.filter(email_address__in=[a.lower() for a in opts['account']])
        for account in accounts:
            changed = signatures.apply_default(account, force=opts['force'])
            sig = signatures.context(account)['signature']
            self.stdout.write(f'{"updated" if changed else "kept   "}  {account.email_address:38} '
                              f'{sig["name"]} — {sig["role"]}')
