"""Shared bits for the Gmail import commands."""

import os

from django.core.management.base import CommandError

from apps.mailbox.models import MailAccount


def gmail_login(options):
    user = options.get('gmail_user') or os.getenv('GMAIL_IMAP_USER') or 'info@metanoiagroup.co.za'
    password = os.getenv('GMAIL_APP_PASSWORD', '').strip()
    if not password:
        raise CommandError('Set GMAIL_APP_PASSWORD to a Google app password for %s '
                           '(Google Account → Security → App passwords). It is read from the '
                           'environment only and never saved.' % user)
    return user, password


def mailbox(address):
    account = MailAccount.objects.filter(email_address__iexact=address.strip(), is_active=True).first()
    if account is None:
        raise CommandError(f'No connected Metanoia Mail mailbox {address}. Connect it under Mail settings first.')
    return account
