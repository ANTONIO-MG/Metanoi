"""Metanoia Mail, end to end against a fake IMAP server.

:class:`FakeServer` answers imaplib's calls with the same ``(typ, data)``
shapes a real server produces, so the real :class:`apps.mailbox.imap.ImapClient`
parsing runs in every test. Sending uses Django's locmem backend
(``MAILBOX_EMAIL_BACKEND`` under test).
"""

from decimal import Decimal
from email.message import EmailMessage
from email.utils import formatdate
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.communication.models import Notification

from . import imap, sync
from .models import MailAccount, MailDraft, MailDraftAttachment, MailFolder, MailItem, MailServer

User = get_user_model()


class FakeServer:
    """An in-memory IMAP server: folders with UIDVALIDITY, next UID and messages."""

    def __init__(self):
        """Start with the usual Outlook-style folders, all empty."""
        self.folders = {}
        self.appended = []
        for path, flags in [('INBOX', r'\HasNoChildren'), ('Sent Items', r'\HasNoChildren \Sent'),
                            ('Deleted Items', r'\HasNoChildren \Trash'), ('Drafts', r'\HasNoChildren \Drafts'),
                            ('Junk Email', r'\HasNoChildren \Junk'), ('Clients', r'\HasNoChildren')]:
            self.folders[path] = {'flags': flags, 'uidvalidity': 7, 'next': 1, 'msgs': {}}

    def deliver(self, path, raw, flags=()):
        """Put ``raw`` into folder ``path`` with ``flags``; return its UID."""
        f = self.folders[path]
        uid = f['next']
        f['next'] += 1
        f['msgs'][uid] = {'flags': set(flags), 'raw': raw}
        return uid

    def conn(self):
        """A new :class:`FakeConn` to this server."""
        return FakeConn(self)


def _unquote(name):
    """Undo :func:`apps.mailbox.imap.quote`."""
    return name[1:-1].replace('\\"', '"').replace('\\\\', '\\') if name.startswith('"') else name


class FakeConn:
    """Stands in for an :class:`imaplib.IMAP4` connection, returning the same ``(typ, data)`` shapes."""

    capabilities = ('IMAP4REV1', 'MOVE', 'UIDPLUS')

    def __init__(self, server):
        """Connect to ``server`` with no folder selected."""
        self.server = server
        self.selected = None

    def list(self):
        """LIST: every folder with its flags."""
        return 'OK', [f'({f["flags"]}) "/" "{p}"'.encode() for p, f in self.server.folders.items()]

    def select(self, mailbox, readonly=False):
        """SELECT a folder; ``NO`` if it does not exist."""
        path = _unquote(mailbox)
        if path not in self.server.folders:
            return 'NO', [b'no such folder']
        self.selected = self.server.folders[path]
        return 'OK', [str(len(self.selected['msgs'])).encode()]

    def response(self, code):
        """The selected folder's UIDVALIDITY."""
        return code, [str(self.selected['uidvalidity']).encode()]

    def _uids(self, spec):
        """Expand a UID set such as ``3,5:*``.

        As RFC 3501 requires, ``n:*`` always matches the highest UID, even when it
        is below ``n``.
        """
        msgs = self.selected['msgs']
        out = []
        for part in spec.split(','):
            if ':' in part:
                lo, hi = part.split(':')
                hi = max(msgs) if hi == '*' and msgs else (int(hi) if hi != '*' else 0)
                out += [u for u in msgs if int(lo) <= u <= hi]
                if part.endswith('*') and msgs and not out:
                    out.append(max(msgs))
            elif int(part) in msgs:
                out.append(int(part))
        return sorted(set(out))

    def uid(self, command, *args):
        """UID SEARCH, FETCH, STORE, MOVE, COPY and EXPUNGE."""
        command = command.upper()
        msgs = self.selected['msgs']
        if command == 'SEARCH':
            criteria = args[-1]
            if criteria.startswith('UID '):
                return 'OK', [' '.join(map(str, self._uids(criteria[4:]))).encode()]
            return 'OK', [' '.join(map(str, sorted(msgs))).encode()]
        if command == 'FETCH':
            uids, what = self._uids(args[0]), args[1]
            data = []
            for i, uid in enumerate(uids, 1):
                m = msgs[uid]
                flags = ' '.join(sorted(m['flags']))
                if 'BODY.PEEK[]' in what:
                    data.append((f'{i} (UID {uid} RFC822.SIZE {len(m["raw"])} FLAGS ({flags}) BODY[] '
                                 f'{{{len(m["raw"])}}}'.encode(), m['raw']))
                    data.append(b')')
                else:
                    data.append(f'{i} (UID {uid} FLAGS ({flags}))'.encode())
            return 'OK', data
        if command == 'STORE':
            uid, op, flags = int(args[0]), args[1], args[2].strip('()').split()
            if uid in msgs:
                (msgs[uid]['flags'].update if op.startswith('+') else msgs[uid]['flags'].difference_update)(flags)
            return 'OK', [b'']
        if command in ('MOVE', 'COPY'):
            uid, dest = int(args[0]), self.server.folders[_unquote(args[1])]
            dest['msgs'][dest['next']] = {'flags': set(msgs[uid]['flags']), 'raw': msgs[uid]['raw']}
            dest['next'] += 1
            if command == 'MOVE':
                del msgs[uid]
            return 'OK', [b'']
        if command == 'EXPUNGE':
            for uid in [u for u, m in msgs.items() if '\\Deleted' in m['flags']]:
                del msgs[uid]
            return 'OK', [b'']
        raise AssertionError(command)

    def append(self, mailbox, flags, date, raw):
        """APPEND: deliver ``raw`` and record the folder it went to."""
        path = _unquote(mailbox)
        self.server.appended.append(path)
        self.server.deliver(path, raw, flags.strip('()').split())
        return 'OK', [b'']

    def create(self, mailbox):
        """CREATE an empty folder."""
        self.server.folders[_unquote(mailbox)] = {'flags': r'\HasNoChildren', 'uidvalidity': 9, 'next': 1, 'msgs': {}}
        return 'OK', [b'']

    def status(self, mailbox, what):
        """STATUS: always empty."""
        return 'OK', [b'(MESSAGES 0 UNSEEN 0)']

    def expunge(self):
        """EXPUNGE the selected folder."""
        return self.uid('EXPUNGE')

    def close(self):
        """CLOSE: nothing to do."""
        pass

    def logout(self):
        """LOGOUT: nothing to do."""
        pass


def message(subject='Hello', sender='Lerato Dube <lerato@example.com>', to='coach@metanoia.test',
            text='Plain body', html=None, attachment=None, message_id=None):
    """Raw bytes of a test e-mail, optionally with an HTML part and one attachment."""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = message_id or f'<{abs(hash(subject))}@example.com>'
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype='html')
    if attachment:
        name, data, maintype, subtype = attachment
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


def make_user(email, role, **extra):
    """A registered user with profile role ``role``."""
    user = User.objects.create_user(email, email, 'pw-Metanoia-123', first_name='Thandi', last_name='Mokoena', **extra)
    person = user.profile
    person.user_type, person.registered, person.profile_status = role, True, True
    person.save()
    return user


@override_settings(SECURE_SSL_REDIRECT=False)
class MailTestCase(TestCase):
    """Base case: a configured mail server, a :class:`FakeServer` behind ``imap.connect`` and a signed-in educator with one mailbox.

    Background work runs inline (``sync.RUN_IN_THREAD = False``).
    """

    def setUp(self):
        """Configure the server, patch the IMAP connection and sign in the mailbox owner."""
        sync.RUN_IN_THREAD = False
        self.addCleanup(setattr, sync, 'RUN_IN_THREAD', True)
        from django.core.cache import cache
        cache.clear()
        server = MailServer.get()
        server.domain, server.imap_host, server.smtp_host = 'metanoia.test', 'imap.metanoia.test', 'smtp.metanoia.test'
        server.save()
        self.server = FakeServer()
        patcher = mock.patch.object(imap, 'connect', side_effect=lambda account: imap.ImapClient(self.server.conn()))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user = make_user('coach@metanoia.test', 'educator')
        self.account = MailAccount(user=self.user, email_address='coach@metanoia.test', display_name='Thandi Mokoena')
        self.account.set_password('s3cret-mail')
        self.account.save()
        self.client.force_login(self.user)

    def sync(self, **kw):
        """Sync the test mailbox and refresh it; return the sync result."""
        result = sync.sync_account(MailAccount.objects.get(pk=self.account.pk), **kw)
        self.account.refresh_from_db()
        return result

    def folder(self, role):
        """The test mailbox's folder with ``role``."""
        return self.account.folders.get(role=role)


class SyncTests(MailTestCase):
    """Importing, incremental sync, flag mirroring and new-mail notifications."""

    def test_first_sync_imports_history_quietly_and_finds_special_folders(self):
        self.server.deliver('INBOX', message('Old news'))
        self.server.deliver('Sent Items', message('I sent this', sender='coach@metanoia.test'), [r'\Seen'])
        self.assertEqual(self.sync(), {'new': 2, 'error': ''})

        roles = dict(self.account.folders.values_list('path', 'role'))
        self.assertEqual(roles, {'INBOX': 'inbox', 'Sent Items': 'sent', 'Deleted Items': 'trash',
                                 'Drafts': 'drafts', 'Junk Email': 'junk', 'Clients': ''})
        item = MailItem.objects.get(subject='Old news')
        self.assertEqual((item.from_name, item.from_address), ('Lerato Dube', 'lerato@example.com'))
        self.assertEqual(self.folder('inbox').unread, 1)
        self.assertFalse(Notification.objects.exists(), 'history must not be announced as new mail')

    def test_new_mail_raises_a_notification_and_the_sidebar_badge(self):
        self.sync()
        self.server.deliver('INBOX', message('Test 2 marks are out', text='Please see the attached results.'))
        self.sync()
        note = Notification.objects.get(recipient=self.user)
        self.assertEqual(note.title, 'New email from Lerato Dube')
        self.assertIn('Test 2 marks are out', note.body)
        self.assertIn(f'm={MailItem.objects.get().pk}', note.url)

        page = self.client.get(reverse('mailbox:inbox'))
        self.assertContains(page, 'js-mail-badge')
        self.assertRegex(page.content.decode(), r'js-mail-badge"\s*>1<')
        self.assertContains(page, 'data-mail-poll=')

    def test_many_new_messages_collapse_into_one_notification(self):
        self.sync()
        for n in range(5):
            self.server.deliver('INBOX', message(f'Batch {n}'))
        self.sync()
        self.assertEqual(Notification.objects.filter(recipient=self.user).count(), 1)
        self.assertIn('5 new emails', Notification.objects.get().title)

    def test_flags_and_deletions_on_the_server_are_mirrored(self):
        keep = self.server.deliver('INBOX', message('Keep me'))
        gone = self.server.deliver('INBOX', message('Delete me elsewhere'))
        self.sync()
        self.server.folders['INBOX']['msgs'][keep]['flags'].add(r'\Seen')
        del self.server.folders['INBOX']['msgs'][gone]
        self.sync()
        self.assertEqual(list(MailItem.objects.values_list('subject', 'is_read')), [('Keep me', True)])

    def test_uidvalidity_change_resyncs_the_folder(self):
        self.server.deliver('INBOX', message('One'))
        self.sync()
        self.server.folders['INBOX']['uidvalidity'] = 99
        self.sync()
        self.assertEqual(MailItem.objects.count(), 1)
        self.assertEqual(self.folder('inbox').uidvalidity, 99)

    def test_bad_password_is_reported_not_raised(self):
        with mock.patch.object(imap, 'connect', side_effect=imap.MailError('The mail server rejected the username or password.')):
            result = self.sync()
        self.assertIn('rejected', result['error'])
        self.assertIn('rejected', self.account.last_error)

    def test_password_is_stored_encrypted(self):
        self.assertNotIn('s3cret-mail', self.account.password_encrypted)
        self.assertEqual(MailAccount.objects.get(pk=self.account.pk).get_password(), 's3cret-mail')


class ReadingTests(MailTestCase):
    """Showing a message: sanitising, remote images and the reading pane."""

    def test_html_is_sanitised_and_remote_images_held_back(self):
        self.server.deliver('INBOX', message('Newsletter', html=(
            '<p onclick="steal()">Hi</p><script>alert(1)</script>'
            '<img src="https://tracker.example/pixel.gif"><a href="javascript:alert(2)">x</a>')))
        self.sync()
        item = MailItem.objects.get()
        response = self.client.get(reverse('mailbox:body', args=[item.pk]))
        body = response.content.decode()
        self.assertNotIn('<script>alert', body)
        self.assertNotIn('onclick', body)
        self.assertNotIn('javascript:', body)
        self.assertNotIn('src="https://tracker.example', body)
        self.assertIn('data-blocked="1"', body)
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])
        self.assertNotIn('https:', response['Content-Security-Policy'].split('img-src')[1].split(';')[0])

        shown = self.client.get(reverse('mailbox:body', args=[item.pk]) + '?images=1').content.decode()
        self.assertIn('src="https://tracker.example/pixel.gif"', shown)

    def test_opening_a_message_marks_it_read_on_the_server(self):
        uid = self.server.deliver('INBOX', message('Read me'))
        self.sync()
        item = MailItem.objects.get()
        data = self.client.get(reverse('mailbox:read', args=[item.pk])).json()
        self.assertTrue(data['ok'])
        self.assertIn('Read me', data['html'])
        self.assertIn(r'\Seen', self.server.folders['INBOX']['msgs'][uid]['flags'])
        self.assertEqual(data['counts']['inbox_total'], 0)

    def test_attachments_are_listed_and_download(self):
        self.server.deliver('INBOX', message('Report', attachment=('report.pdf', b'%PDF-1.4 fake', 'application', 'pdf')))
        self.sync()
        item = MailItem.objects.get()
        self.assertTrue(item.has_attachments)
        att = item.attachments.get()
        self.assertEqual(att.filename, 'report.pdf')
        response = self.client.get(reverse('mailbox:attachment', args=[item.pk, att.pk]))
        self.assertEqual(response.content, b'%PDF-1.4 fake')
        self.assertIn('attachment;', response['Content-Disposition'])

    def test_someone_elses_message_is_404(self):
        self.server.deliver('INBOX', message('Private'))
        self.sync()
        other = make_user('other@metanoia.test', 'staff')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('mailbox:read', args=[MailItem.objects.get().pk])).status_code, 404)

    def test_list_search_and_filters(self):
        self.server.deliver('INBOX', message('FAC test results'))
        self.server.deliver('INBOX', message('Invoice query'), [r'\Seen', r'\Flagged'])
        self.sync()
        url = reverse('mailbox:list')
        inbox = self.folder('inbox').pk
        self.assertIn('Invoice query', self.client.get(url, {'folder': inbox, 'q': 'invoice'}).json()['html'])
        self.assertNotIn('FAC test', self.client.get(url, {'folder': inbox, 'q': 'invoice'}).json()['html'])
        flagged = self.client.get(url, {'folder': inbox, 'filter': 'flagged'}).json()['html']
        self.assertIn('Invoice query', flagged)
        self.assertNotIn('FAC test', flagged)


class AttachmentPreviewTests(MailTestCase):
    """Clicking an attachment previews it; only harmless types ever render inline."""

    def _att(self, name, data, maintype, subtype):
        """Deliver and sync a message carrying one attachment; return the item and attachment."""
        self.server.deliver('INBOX', message(f'File {name}', attachment=(name, data, maintype, subtype)))
        self.sync()
        item = MailItem.objects.get(subject=f'File {name}')
        return item, item.attachments.get()

    def _get(self, item, att, inline=True):
        """Fetch an attachment, inline (preview) by default."""
        url = reverse('mailbox:attachment', args=[item.pk, att.pk])
        return self.client.get(url + ('?inline=1' if inline else ''))

    def test_pdf_is_served_inline_and_framable_by_our_pages(self):
        """PDFs get no sandbox: it would switch off the browser's PDF viewer."""
        item, att = self._att('report.pdf', b'%PDF-1.4 fake', 'application', 'pdf')
        response = self._get(item, att)
        self.assertEqual(response.content, b'%PDF-1.4 fake')
        self.assertTrue(response['Content-Disposition'].startswith('inline;'))
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['X-Frame-Options'], 'SAMEORIGIN')
        self.assertNotIn('sandbox', response['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'self'", response['Content-Security-Policy'])

    def test_image_is_inline_with_a_sandboxed_csp(self):
        item, att = self._att('photo.png', b'\x89PNG fake', 'image', 'png')
        response = self._get(item, att)
        self.assertTrue(response['Content-Disposition'].startswith('inline;'))
        self.assertEqual(response['Content-Type'], 'image/png')
        csp = response['Content-Security-Policy']
        self.assertIn('sandbox', csp)
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_mislabelled_pdf_previews_by_its_name(self):
        item, att = self._att('scan.pdf', b'%PDF-1.4 fake', 'application', 'octet-stream')
        self.assertEqual(self._get(item, att)['Content-Type'], 'application/pdf')

    def test_csv_is_served_as_plain_text(self):
        item, att = self._att('marks.csv', b'name,mark\nLerato,82\n', 'text', 'csv')
        response = self._get(item, att)
        self.assertTrue(response['Content-Disposition'].startswith('inline;'))
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn('sandbox', response['Content-Security-Policy'])

    def test_risky_and_unknown_types_always_download(self):
        """Delivered together and synced once: the first sync files history quietly, so no notification is raised."""
        cases = [('page.html', b'<script>alert(1)</script>', 'text', 'html'),
                 ('logo.svg', b'<svg onload="alert(1)"/>', 'image', 'svg+xml'),
                 ('notes.docx', b'PK fake docx', 'application',
                  'vnd.openxmlformats-officedocument.wordprocessingml.document'),
                 ('blob.bin', b'\x00\x01', 'application', 'octet-stream')]
        for name, data, maintype, subtype in cases:
            self.server.deliver('INBOX', message(f'File {name}', attachment=(name, data, maintype, subtype)))
        self.sync()
        for name, data, maintype, subtype in cases:
            with self.subTest(name):
                item = MailItem.objects.get(subject=f'File {name}')
                att = item.attachments.get()
                self.assertIsNone(att.preview_kind)
                response = self._get(item, att)
                self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
                self.assertEqual(response['Content-Type'], 'application/octet-stream')
                self.assertIn('sandbox', response['Content-Security-Policy'])
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_without_the_query_it_is_still_a_download(self):
        item, att = self._att('report.pdf', b'%PDF-1.4 fake', 'application', 'pdf')
        response = self._get(item, att, inline=False)
        self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
        self.assertIn('report.pdf', response['Content-Disposition'])

    def test_someone_elses_attachment_is_404(self):
        item, att = self._att('report.pdf', b'%PDF-1.4 fake', 'application', 'pdf')
        other = make_user('other@metanoia.test', 'staff')
        other_account = MailAccount(user=other, email_address='other@metanoia.test')
        other_account.set_password('pw')
        other_account.save()
        self.client.force_login(other)
        self.assertEqual(self._get(item, att).status_code, 404)
        self.assertEqual(self._get(item, att, inline=False).status_code, 404)

    def test_reading_pane_offers_preview_and_direct_download(self):
        item, att = self._att('report.pdf', b'%PDF-1.4 fake', 'application', 'pdf')
        url = reverse('mailbox:attachment', args=[item.pk, att.pk])
        html = self.client.get(reverse('mailbox:read', args=[item.pk])).json()['html']
        self.assertIn('data-mb="preview"', html)
        self.assertIn('data-kind="pdf"', html)
        self.assertIn(f'href="{url}?inline=1"', html)
        self.assertRegex(html, rf'class="mb-att__dl" href="{url}" download')
        page = self.client.get(reverse('mailbox:inbox') + f'?m={item.pk}')
        self.assertContains(page, 'id="mbPreview"')
        self.assertContains(page, 'data-preview-download')
        self.assertContains(page, 'Open in new tab')

    def test_draft_attachments_share_the_rules(self):
        draft = MailDraft.objects.create(account=self.account)
        meta = MailDraftAttachment(draft=draft, filename='page.html', content_type='text/html', size=5)
        meta.file.save('page.html', ContentFile(b'<b>x</b>'), save=True)
        response = self.client.get(reverse('mailbox:draft-attachment', args=[draft.pk, meta.pk]) + '?inline=1')
        self.assertTrue(response['Content-Disposition'].startswith('attachment;'))


class ActionTests(MailTestCase):
    """Toolbar actions: read, flag, delete, archive and move."""

    def setUp(self):
        """One synced message in the inbox."""
        super().setUp()
        self.uid = self.server.deliver('INBOX', message('Act on me'))
        self.sync()
        self.item = MailItem.objects.get()

    def post(self, action, **extra):
        """POST a toolbar action for the test message; return the JSON reply."""
        return self.client.post(reverse('mailbox:action'), {'action': action, 'ids': [self.item.pk], **extra}).json()

    def test_delete_goes_to_deleted_items_then_for_good(self):
        self.assertTrue(self.post('delete')['ok'])
        self.assertFalse(self.server.folders['INBOX']['msgs'])
        self.assertEqual(len(self.server.folders['Deleted Items']['msgs']), 1)
        trashed = MailItem.objects.get()
        self.assertEqual(trashed.folder.role, 'trash')
        self.item = trashed
        self.assertTrue(self.post('delete')['ok'])
        self.assertFalse(self.server.folders['Deleted Items']['msgs'])
        self.assertFalse(MailItem.objects.exists())

    def test_flag_and_unread(self):
        self.post('flag')
        self.assertIn(r'\Flagged', self.server.folders['INBOX']['msgs'][self.uid]['flags'])
        self.post('read')
        self.post('unread')
        self.assertNotIn(r'\Seen', self.server.folders['INBOX']['msgs'][self.uid]['flags'])
        self.assertFalse(MailItem.objects.get().is_read)

    def test_move_to_a_custom_folder(self):
        clients = self.account.folders.get(path='Clients')
        self.assertTrue(self.post('move', folder=clients.pk)['ok'])
        self.assertEqual(MailItem.objects.get().folder, clients)


class SendTests(MailTestCase):
    """Sending drafts, replies and forwards over SMTP."""

    def test_new_message_with_attachment_is_sent_and_filed_in_sent(self):
        self.sync()
        draft = MailDraft.objects.create(account=self.account, to='Lerato <lerato@example.com>; tutor@example.com',
                                         subject='Timetable', body_html='<p>See <b>attached</b>.</p><script>x</script>')
        upload = self.client.post(reverse('mailbox:draft-attach', args=[draft.pk]),
                                  {'files': [_upload('timetable.pdf', b'%PDF timetable')]}).json()
        self.assertTrue(upload['ok'])
        result = self.client.post(reverse('mailbox:draft-send', args=[draft.pk])).json()
        self.assertTrue(result['ok'], result)

        sent = mail.outbox[-1]
        self.assertEqual(sent.to, ['Lerato <lerato@example.com>', 'tutor@example.com'])
        self.assertIn('Thandi Mokoena', sent.from_email)
        self.assertEqual(sent.attachments[0][0], 'timetable.pdf')
        html = sent.alternatives[0][0]
        self.assertIn('<b>attached</b>', html)
        self.assertNotIn('<script>', html)
        self.assertEqual(self.server.appended, ['Sent Items'])
        self.assertFalse(MailDraft.objects.exists())

    def test_reply_threads_and_marks_the_original_answered(self):
        uid = self.server.deliver('INBOX', message('Question', message_id='<q1@example.com>'))
        self.sync()
        item = MailItem.objects.get()
        data = self.client.post(reverse('mailbox:respond', args=[item.pk, 'reply'])).json()
        draft = MailDraft.objects.get(pk=data['id'])
        self.assertEqual(draft.subject, 'RE: Question')
        self.assertIn('lerato@example.com', draft.to)
        self.assertIn('<b>From:</b>', draft.body_html)
        self.client.post(reverse('mailbox:draft-send', args=[draft.pk]), {'body_html': '<p>Answer</p>'})
        self.assertEqual(mail.outbox[-1].extra_headers['In-Reply-To'], '<q1@example.com>')
        self.assertIn(r'\Answered', self.server.folders['INBOX']['msgs'][uid]['flags'])

    def test_reply_all_excludes_me_and_keeps_others_in_cc(self):
        self.server.deliver('INBOX', message('Group', to='coach@metanoia.test, peer@example.com'))
        self.sync()
        data = self.client.post(reverse('mailbox:respond', args=[MailItem.objects.get().pk, 'replyall'])).json()
        draft = MailDraft.objects.get(pk=data['id'])
        self.assertNotIn('coach@metanoia.test', draft.to + draft.cc)
        self.assertIn('peer@example.com', draft.cc)

    def test_forward_carries_the_attachments(self):
        self.server.deliver('INBOX', message('Docs', attachment=('notes.txt', b'hello', 'text', 'plain')))
        self.sync()
        data = self.client.post(reverse('mailbox:respond', args=[MailItem.objects.get().pk, 'forward'])).json()
        draft = MailDraft.objects.get(pk=data['id'])
        self.assertEqual(draft.subject, 'FW: Docs')
        self.assertEqual(list(draft.attachments.values_list('filename', flat=True)), ['notes.txt'])

    def test_bad_address_is_refused_with_a_reason(self):
        draft = MailDraft.objects.create(account=self.account, to='not-an-address', subject='x')
        result = self.client.post(reverse('mailbox:draft-send', args=[draft.pk])).json()
        self.assertFalse(result['ok'])
        self.assertIn('not-an-address', result['error'])
        self.assertFalse(mail.outbox)


class InvoiceIntegrationTests(MailTestCase):
    """Composing with ``?attach=invoice:…`` and marking the invoice sent afterwards."""

    def setUp(self):
        """An admin with the accounts mailbox and an invoice to send."""
        super().setUp()
        from apps.finance.models import Invoice, InvoiceItem
        self.admin = make_user('admin@metanoia.test', 'admin', is_staff=True)
        admin_box = MailAccount(user=self.admin, email_address='accounts@metanoia.test')
        admin_box.set_password('pw')
        admin_box.save()
        self.customer = make_user('student@example.com', 'student')
        self.invoice = Invoice.objects.create(customer=self.customer)
        InvoiceItem.objects.create(invoice=self.invoice, description='Module fee', unit_price=Decimal('1500'))
        self.client.force_login(self.admin)

    def test_invoice_opens_a_ready_message_and_sending_marks_it_sent(self):
        detail = self.client.get(reverse('finance:invoice-detail', args=[self.invoice.pk]))
        self.assertContains(detail, f'?attach=invoice:{self.invoice.pk}')

        with mock.patch('apps.finance.pdf.render_invoice_pdf', return_value=b'%PDF invoice'):
            response = self.client.get(reverse('mailbox:compose') + f'?attach=invoice:{self.invoice.pk}')
        draft = MailDraft.objects.get()
        self.assertRedirects(response, reverse('mailbox:inbox') + f'?draft={draft.pk}', fetch_redirect_response=False)
        self.assertIn('student@example.com', draft.to)
        self.assertIn(self.invoice.number, draft.subject)
        self.assertIn('/finance/pay/', draft.body_html)
        self.assertEqual(draft.attachments.get().filename, f'Invoice-{self.invoice.number}.pdf')

        page = self.client.get(reverse('mailbox:inbox') + f'?draft={draft.pk}')
        self.assertContains(page, f'Invoice {self.invoice.number}')

        self.assertTrue(self.client.post(reverse('mailbox:draft-send', args=[draft.pk])).json()['ok'])
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, 'sent')
        self.assertIsNotNone(self.invoice.sent_at)
        self.assertEqual(mail.outbox[-1].attachments[0][2], 'application/pdf')

    def test_educators_cannot_attach_invoices(self):
        self.client.force_login(self.user)
        self.client.get(reverse('mailbox:compose') + f'?attach=invoice:{self.invoice.pk}')
        self.assertFalse(MailDraft.objects.get().attachments.exists())


class AccessTests(MailTestCase):
    """Who may use Metanoia Mail, mailbox setup and administration."""

    def test_students_are_kept_out(self):
        student = make_user('learner@example.com', 'student')
        self.client.force_login(student)
        self.assertEqual(self.client.get(reverse('mailbox:inbox')).status_code, 302)
        page = self.client.get(reverse('pages:dashboard'))
        self.assertNotContains(page, 'js-mail-badge')

    def test_setup_page_when_no_mailbox(self):
        self.account.delete()
        page = self.client.get(reverse('mailbox:inbox'))
        self.assertContains(page, 'Connect your Metanoia mailbox')

    def test_connecting_a_mailbox_tests_and_imports(self):
        self.account.delete()
        self.server.deliver('INBOX', message('Welcome'))
        response = self.client.post(reverse('mailbox:settings'), {
            'form': 'account', 'acct-email_address': 'Coach@Metanoia.test', 'acct-password': 'pw',
            'acct-display_name': 'Thandi', 'acct-notify_new_mail': 'on', 'acct-is_default': 'on'})
        self.assertRedirects(response, reverse('mailbox:inbox'), fetch_redirect_response=False)
        account = MailAccount.objects.get(user=self.user)
        self.assertEqual(account.email_address, 'coach@metanoia.test')
        self.assertTrue(MailItem.objects.filter(subject='Welcome').exists())
        self.assertFalse(Notification.objects.exists())

    def test_admin_saves_server_settings_and_assigns_a_mailbox(self):
        admin = make_user('boss@metanoia.test', 'admin', is_staff=True)
        self.client.force_login(admin)
        public = mock.patch('apps.mailbox.hosts.socket.getaddrinfo', return_value=_PUBLIC_ANSWER)
        public.start()
        self.addCleanup(public.stop)
        self.client.post(reverse('mailbox:settings'), {
            'form': 'server', 'srv-domain': 'metanoiagroup.co.za', 'srv-imap_host': 'mail.metanoiagroup.co.za',
            'srv-imap_port': 993, 'srv-imap_security': 'ssl', 'srv-smtp_host': 'mail.metanoiagroup.co.za',
            'srv-smtp_port': 465, 'srv-smtp_security': 'ssl', 'srv-username_is_email': 'on',
            'srv-history_days': 30, 'srv-max_message_mb': 20})
        self.assertEqual(MailServer.get().smtp_port, 465)
        educator = make_user('edu@metanoia.test', 'educator')
        self.client.post(reverse('mailbox:settings'), {
            'form': 'assign', 'assign-person': educator.pk, 'assign-email_address': 'edu@metanoiagroup.co.za',
            'assign-password': 'pw', 'assign-notify_new_mail': 'on', 'assign-is_default': 'on'})
        self.assertTrue(MailAccount.objects.filter(user=educator, email_address='edu@metanoiagroup.co.za').exists())

    def test_old_mail_links_redirect(self):
        self.assertRedirects(self.client.get('/communication/mail/'), reverse('mailbox:inbox'),
                             fetch_redirect_response=False)


_PUBLIC_ANSWER = [(2, 1, 6, '', ('8.8.8.8', 993))]


def _answer(ip):
    """A ``getaddrinfo`` answer resolving to ``ip``."""
    return [(2, 1, 6, '', (ip, 993))]


class MailHostSSRFTests(MailTestCase):
    """Mail hosts are where the server opens sockets: never the internal network."""

    def test_internal_addresses_are_refused(self):
        from .hosts import UnsafeHost, vet_host
        for ip in ('127.0.0.1', '10.0.0.5', '172.16.3.4', '192.168.1.1', '169.254.169.254', '::1', 'fd00::1'):
            with self.subTest(ip=ip), mock.patch('apps.mailbox.hosts.socket.getaddrinfo',
                                                 return_value=_answer(ip)):
                with self.assertRaises(UnsafeHost):
                    vet_host('mail.evil.test')
        with mock.patch('apps.mailbox.hosts.socket.getaddrinfo',
                        return_value=_answer('8.8.8.8') + _answer('127.0.0.1')):
            with self.assertRaises(UnsafeHost, msg='one private answer is enough to refuse'):
                vet_host('rebind.evil.test')
        with self.assertRaises(UnsafeHost):
            vet_host('169.254.169.254')
        with mock.patch('apps.mailbox.hosts.socket.getaddrinfo', return_value=_PUBLIC_ANSWER):
            self.assertEqual(vet_host(' mail.example.com '), 'mail.example.com')

    def test_an_educator_cannot_point_their_mailbox_at_another_server(self):
        self.client.post(reverse('mailbox:settings') + f'?edit={self.account.pk}', {
            'form': 'account', 'acct-email_address': 'coach@metanoia.test', 'acct-display_name': 'Thandi',
            'acct-imap_host': '169.254.169.254', 'acct-imap_port': 80,
            'acct-smtp_host': '127.0.0.1', 'acct-smtp_port': 6379})
        self.account.refresh_from_db()
        self.assertEqual((self.account.imap_host, self.account.smtp_host), ('', ''))
        self.assertIsNone(self.account.imap_port)
        page = self.client.get(reverse('mailbox:settings'))
        self.assertNotContains(page, 'acct-imap_host')

    def test_an_educator_cannot_change_the_domain_server(self):
        self.client.post(reverse('mailbox:settings'), {
            'form': 'server', 'srv-domain': 'metanoia.test', 'srv-imap_host': '127.0.0.1',
            'srv-imap_port': 993, 'srv-imap_security': 'ssl', 'srv-smtp_host': '127.0.0.1',
            'srv-smtp_port': 25, 'srv-smtp_security': 'none', 'srv-history_days': 30,
            'srv-max_message_mb': 20})
        self.assertEqual(MailServer.get().imap_host, 'imap.metanoia.test')

    def test_an_admin_cannot_save_an_internal_server_either(self):
        admin = make_user('root@metanoia.test', 'admin', is_staff=True)
        self.client.force_login(admin)
        response = self.client.post(reverse('mailbox:settings'), {
            'form': 'server', 'srv-domain': 'metanoia.test', 'srv-imap_host': '169.254.169.254',
            'srv-imap_port': 80, 'srv-imap_security': 'none', 'srv-smtp_host': 'localhost',
            'srv-smtp_port': 25, 'srv-smtp_security': 'none', 'srv-history_days': 30,
            'srv-max_message_mb': 20})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not a public mail server')
        self.assertEqual(MailServer.get().imap_host, 'imap.metanoia.test')

    def test_an_old_internal_override_is_refused_when_connecting(self):
        from . import send
        from .hosts import UnsafeHost, check_override
        MailAccount.objects.filter(pk=self.account.pk).update(imap_host='127.0.0.1', smtp_host='10.1.2.3')
        account = MailAccount.objects.get(pk=self.account.pk)
        with self.assertRaises(send.SendError):
            send.smtp_connection(account)
        with self.assertRaises(UnsafeHost):
            check_override(account, 'imap')


def _upload(name, data):
    """An uploaded PDF called ``name``."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, data, content_type='application/pdf')


class ParseTests(TestCase):
    """IMAP parsing: folder names, literals and role guessing."""

    def test_folder_names_round_trip(self):
        for name in ['Entwürfe', 'R&D', 'Gesendete Objekte']:
            self.assertEqual(imap.decode_folder_name(imap.encode_folder_name(name)), name)

    def test_gmail_style_flags_after_the_literal(self):
        conn = mock.Mock()
        conn.uid.return_value = ('OK', [(b'1 (BODY[] {5}', b'hello'), b' UID 42 FLAGS (\\Seen))'])
        self.assertEqual(imap.ImapClient(conn).fetch_messages([42]), [(42, {'\\seen'}, 5, b'hello')])

    def test_list_with_literal_folder_name(self):
        conn = mock.Mock()
        conn.list.return_value = ('OK', [(b'(\\HasNoChildren) "/" {8}', b'Projects'), b'(\\Noselect) "/" "[Gmail]"'])
        self.assertEqual([f[0] for f in imap.ImapClient(conn).list_folders()], ['Projects'])

    def test_role_guess_without_special_use(self):
        self.assertEqual(imap.guess_role('INBOX.Sent'), 'sent')
        self.assertEqual(imap.guess_role('Deleted Messages'), 'trash')
        self.assertEqual(imap.guess_role('[Gmail]/Spam'), 'junk')
        self.assertEqual(imap.guess_role('Clients'), '')


@override_settings(SITE_URL='https://learn.metanoia.test')
class SignatureTests(MailTestCase):
    """The Metanoia signature, its public avatar and embedding its pictures when sending."""

    def test_shared_mailbox_signs_as_the_mailbox_with_the_tick(self):
        """The default signature is applied once and kept unless forced."""
        from . import signatures
        owner = make_user('info@metanoiagroup.co.za', 'staff')
        box = MailAccount(user=owner, email_address='info@metanoiagroup.co.za')
        box.set_password('x')
        box.save()
        self.assertTrue(signatures.apply_default(box))
        self.assertIn('Metanoia Info', box.signature_html)
        self.assertIn('https://learn.metanoia.test/static/email/sig-avatar.png', box.signature_html)
        self.assertIn('https://learn.metanoia.test/static/email/banner.jpg', box.signature_html)
        self.assertFalse(signatures.apply_default(box))

    def test_person_signs_with_name_role_and_their_own_picture(self):
        """The avatar link is public: mail readers are not signed in."""
        from django.core.files.base import ContentFile
        from PIL import Image
        import io
        from . import signatures
        buf = io.BytesIO()
        Image.new('RGB', (40, 40), (200, 50, 50)).save(buf, format='PNG')
        self.user.profile.profile_picture.save('me.png', ContentFile(buf.getvalue()), save=True)
        html = signatures.build(self.account)
        self.assertIn('coach@metanoia.test', html)
        self.assertIn('/mail/sig/', html)
        token = signatures.avatar_token(self.account)
        self.client.logout()
        r = self.client.get(reverse('mailbox:signature-avatar', args=[token]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'image/png')
        self.assertEqual(self.client.get('/mail/sig/forged.png').status_code, 404)

    def test_new_mail_starts_with_the_signature_and_sending_embeds_its_pictures(self):
        from . import signatures
        signatures.apply_default(self.account)
        self.sync()
        r = self.client.post(reverse('mailbox:draft-new')).json()
        draft = MailDraft.objects.get(pk=r['id'])
        self.assertIn('mb-signature', draft.body_html)
        draft.to = 'lerato@example.com'
        draft.save()
        self.assertTrue(self.client.post(reverse('mailbox:draft-send', args=[draft.pk])).json()['ok'])
        sent = mail.outbox[-1]
        html = sent.alternatives[0][0]
        self.assertIn('cid:metanoia-sig-', html)
        self.assertNotIn('https://learn.metanoia.test/static/email/sig-logo.png', html)
        cids = [p['Content-ID'] for p in sent.message().walk() if p['Content-ID']]
        self.assertIn('<metanoia-sig-1>', cids)

    def test_settings_can_restore_the_metanoia_signature(self):
        self.account.signature_html = 'Old'
        self.account.save()
        self.client.post(reverse('mailbox:settings'), {'form': 'signature', 'account': self.account.pk})
        self.account.refresh_from_db()
        self.assertIn('EMPOWERING', self.account.signature_html)


class SendDraftsTests(MailTestCase):
    """The "Send drafts" outbox list."""

    def test_outbox_lists_my_drafts_and_flags_ones_without_recipients(self):
        MailDraft.objects.create(account=self.account, to='a@example.com', subject='One')
        MailDraft.objects.create(account=self.account, subject='No one')
        other = make_user('other@metanoia.test', 'educator')
        theirs = MailAccount.objects.create(user=other, email_address='other@metanoia.test')
        MailDraft.objects.create(account=theirs, to='b@example.com', subject='Not mine')
        rows = self.client.get(reverse('mailbox:drafts-outbox')).json()['drafts']
        self.assertEqual([r['subject'] for r in rows], ['One', 'No one'])
        self.assertEqual(rows[0]['problem'], '')
        self.assertEqual(rows[1]['problem'], 'No recipient')
        self.assertTrue(rows[0]['send_url'].endswith('/send/'))


class GmailImportTests(MailTestCase):
    """Copying Gmail drafts and slimming archived messages."""

    def _gmail_draft(self, body_html, attach=True):
        """Raw bytes of a Gmail-style draft with ``body_html`` and, optionally, a PDF."""
        msg = EmailMessage()
        msg['From'] = 'Metanoia Group <info@metanoiagroup.co.za>'
        msg['To'] = 'Masiphile <masi@example.com>'
        msg['Subject'] = 'Invoice for November'
        msg['In-Reply-To'] = '<earlier@mail.gmail.com>'
        msg.set_content('Hi Masiphile,\n\nPlease find attached your invoice.')
        msg.add_alternative(body_html, subtype='html')
        if attach:
            msg.add_attachment(b'%PDF invoice', maintype='application', subtype='pdf', filename='Invoice.pdf')
        return msg.as_bytes()

    def test_draft_copy_keeps_everything_swaps_the_signature_and_adds_the_notice(self):
        from . import gmail_import, signatures
        signatures.apply_default(self.account)
        raw = self._gmail_draft(
            '<div dir="ltr"><div><span>Hi Masiphile,</span></div><div><p>Hope you are well.</p>'
            '<p>Please find attached your invoice.</p></div><div class="gmail_signature">Kind Regards'
            '<div>Metanoia Group</div><div>info@metanoiagroup.co.za</div></div></div>')
        draft = gmail_import.draft_from_raw(raw, self.account)
        self.assertEqual(draft.to, 'Masiphile <masi@example.com>')
        self.assertEqual(draft.subject, 'Invoice for November')
        self.assertEqual(draft.in_reply_to, '<earlier@mail.gmail.com>')
        self.assertEqual([a.filename for a in draft.attachments.all()], ['Invoice.pdf'])
        body = draft.body_html
        self.assertNotIn('info@metanoiagroup.co.za', body)
        self.assertIn('Kind Regards', body)
        self.assertIn('EMPOWERING', body)
        greeting, notice, rest = body.index('Hi Masiphile'), body.index('new email accounts'), body.index('Hope you')
        self.assertLess(greeting, notice)
        self.assertLess(notice, rest)

    def test_notice_goes_on_top_when_there_is_no_greeting(self):
        from . import gmail_import
        draft = gmail_import.draft_from_raw(self._gmail_draft('<p>See attached.</p>', attach=False), self.account)
        self.assertLess(draft.body_html.index('new email accounts'), draft.body_html.index('See attached'))

    def test_labels_pick_the_archive_folder(self):
        from .gmail_import import folder_for, parse_labels
        self.assertEqual(parse_labels(r'"\\Inbox" "INBOX/PayHip" \Important'),
                         ['\\Inbox', 'INBOX/PayHip', '\\Important'])
        self.assertEqual(folder_for(['\\Sent', '\\Inbox']), 'Sent')
        self.assertEqual(folder_for(['\\Inbox', 'INBOX/PayHip']), 'PayHip')
        self.assertEqual(folder_for(['\\Inbox']), 'Inbox')
        self.assertEqual(folder_for([]), 'Archived')
        self.assertIsNone(folder_for(['\\Draft']))

    def test_repeated_and_oversize_files_become_notes(self):
        """The first copy of a file is kept whole; repeats and over-size files become notes."""
        from email import message_from_bytes
        from .gmail_import import slim_message

        def mail_with(name, data, subject):
            """Raw bytes of a message carrying one PDF."""
            msg = EmailMessage()
            msg['Subject'], msg['Date'] = subject, formatdate()
            msg.set_content('Here it is.')
            msg.add_attachment(data, maintype='application', subtype='pdf', filename=name)
            return msg.as_bytes()

        pack = b'P' * (200 * 1024)
        seen = {}
        first, saved = slim_message(mail_with('Pack.pdf', pack, 'Pack for Ann'), seen)
        self.assertEqual(saved, 0)
        again, saved = slim_message(mail_with('Pack.pdf', pack, 'Pack for Ben'), seen)
        self.assertGreater(saved, 150 * 1024)
        text = message_from_bytes(again).as_string()
        self.assertIn('Pack for Ann', text)
        self.assertNotIn('PPPPPPPP', text)
        big, saved = slim_message(mail_with('Video.pdf', b'V' * (3 * 1048576), 'Big'), seen, max_bytes=1048576)
        self.assertGreater(saved, 2 * 1048576)
        self.assertIn('info@metanoiagroup.co.za', message_from_bytes(big).get_payload()[1].get_payload(decode=True).decode())
