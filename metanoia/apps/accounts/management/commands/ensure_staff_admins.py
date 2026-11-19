"""Create (or repair) the administrator and sending-mailbox accounts in core.staff_accounts.

    python manage.py ensure_staff_admins                       # update existing ones
    python manage.py ensure_staff_admins --password '…'        # also create missing ones
    python manage.py ensure_staff_admins --password '…' --reset-password
    python manage.py ensure_staff_admins --refresh-pictures    # redraw the mailbox avatars

The password is never read from .env; it is only needed to create an account.
Non-destructive: nothing else in the database is touched.
"""

from django.core.management.base import BaseCommand, CommandError

from core.staff_accounts import ensure_staff_admins


class Command(BaseCommand):
    help = 'Create or repair the administrator superuser and the sending-mailbox accounts.'

    def add_arguments(self, parser):
        parser.add_argument('--password', default=None,
                            help='Password for accounts being created (and for --reset-password).')
        parser.add_argument('--reset-password', action='store_true',
                            help='Also set --password on accounts that already exist.')
        parser.add_argument('--refresh-pictures', action='store_true',
                            help="Redraw the sending mailboxes' initials avatars "
                                 "(the administrator's picture is kept).")

    def handle(self, *args, **opts):
        if opts['reset_password'] and not opts['password']:
            raise CommandError('--reset-password needs --password.')
        try:
            users = ensure_staff_admins(opts['password'], reset_password=opts['reset_password'],
                                        refresh_pictures=opts['refresh_pictures'],
                                        log=self.stdout.write)
        except ValueError as exc:
            raise CommandError(f'{exc} Re-run with --password.') from exc
        self.stdout.write(self.style.SUCCESS(f'{len(users)} account(s) ready.'))
