"""The WhatsApp templates Meta has to approve — list, submit, check.

The set lives in :mod:`core.whatsapp_templates`::

    python manage.py whatsapp_templates            # what the platform uses
    python manage.py whatsapp_templates --json     # the exact bodies Meta receives
    python manage.py whatsapp_templates --submit   # create them in WhatsApp Manager
    python manage.py whatsapp_templates --status   # approved / pending / rejected

Submitting needs ``WHATSAPP_BUSINESS_ACCOUNT_ID`` and a token with
``whatsapp_business_management``. A template that already exists is reported,
not duplicated. Templates with a media header (the invoice) need a sample file,
which only WhatsApp Manager can take, so they are listed for creating by hand.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core import whatsapp
from core import whatsapp_templates as templates


class Command(BaseCommand):
    help = 'List, submit or check the WhatsApp message templates the platform uses.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Print the bodies sent to Meta.')
        parser.add_argument('--submit', action='store_true', help='Create them in WhatsApp Manager.')
        parser.add_argument('--status', action='store_true', help='Show their approval status.')

    def handle(self, *args, **options):
        if options['json']:
            self.stdout.write(json.dumps([templates.definition(t) for t in templates.TEMPLATES], indent=2))
            return
        if options['submit'] or options['status']:
            return self._remote(submit=options['submit'])
        for t in templates.TEMPLATES:
            self.stdout.write(f"{t['name']:<28} {t['category']:<15} {t['purpose']}")

    def _waba(self):
        waba = getattr(settings, 'WHATSAPP_BUSINESS_ACCOUNT_ID', '')
        if not (waba and getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')):
            raise CommandError('Set WHATSAPP_BUSINESS_ACCOUNT_ID and WHATSAPP_ACCESS_TOKEN first.')
        return waba

    def _call(self, path, payload=None):
        version = getattr(settings, 'WHATSAPP_API_VERSION', '') or 'v23.0'
        request = urllib.request.Request(
            f'{whatsapp.GRAPH}/{version}/{path}',
            data=json.dumps(payload).encode() if payload is not None else None,
            method='POST' if payload is not None else 'GET',
            headers={'Authorization': f'Bearer {settings.WHATSAPP_ACCESS_TOKEN}',
                     'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode() or '{}')
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors='ignore')
            try:
                return {'error': json.loads(body).get('error', {})}
            except ValueError:
                return {'error': {'message': body[:300]}}

    def _remote(self, *, submit):
        waba = self._waba()
        existing = self._call(f'{waba}/message_templates?fields=name,status,category,language&limit=200')
        if 'error' in existing:
            raise CommandError(existing['error'].get('message', 'Meta refused the request'))
        status = {row['name']: row for row in existing.get('data', [])}
        for t in templates.TEMPLATES:
            name = t['name']
            if name in status:
                row = status[name]
                self.stdout.write(f"{name:<28} {row.get('status', '?'):<10} {row.get('category', '')}")
                continue
            if not submit:
                self.stdout.write(f'{name:<28} MISSING')
                continue
            if t.get('header'):
                self.stdout.write(self.style.WARNING(
                    f'{name:<28} create by hand in WhatsApp Manager ({t["header"].lower()} header '
                    f'needs a sample file). Body: {t["body"]!r}'))
                continue
            reply = self._call(f'{waba}/message_templates', templates.definition(t))
            if 'error' in reply:
                message = reply['error'].get('error_user_msg') or reply['error'].get('message', '')
                self.stdout.write(self.style.ERROR(f'{name:<28} refused: {message}'))
            else:
                self.stdout.write(self.style.SUCCESS(f"{name:<28} submitted ({reply.get('status', 'PENDING')})"))
