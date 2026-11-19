"""Metanoia Mail data.

:class:`MailServer` holds the domain's IMAP/SMTP settings once, so connecting a
mailbox is just an address and a password. A :class:`MailAccount` is one
person's mailbox (someone may have several — their own and a shared one). Its
:class:`MailFolder` rows mirror the server's folders, and :class:`MailItem` is
a synced message with its original bytes kept in file storage, which is where
attachments are read from. :class:`MailDraft` is a message being written; it
lives here, not on the server, until it is sent.

Attachment previews are deliberately narrow. ``_PREVIEW_TYPES`` lists what the
reading pane can show in-page, keyed by the declared content type; each kind
maps to the single type the file is *served* as when previewed, so the browser
only ever renders bytes as one of a few harmless formats, whatever the sender
claimed. Anything not listed (Office documents, archives, HTML, SVG…) is
download-only: there is no safe local renderer for Office files and the owner
wants nothing sent to an outside viewer, and HTML or SVG would run as script in
our origin. Mail clients often label attachments ``application/octet-stream``,
so ``_PREVIEW_EXTENSIONS`` falls back to the file name, but only when the
declared type is not one of the above. ``_NEVER_PREVIEW_TYPES`` are never
previewed even when the name looks harmless: they render as live documents (or
script) if a browser is ever handed them inline.

Deleting a message or a draft attachment deletes its stored file too
(:func:`_delete_file`) — mail is the one thing here that grows without bound.
"""

import os
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.storage import files_storage
from core.validators import must_download, validate_mail_file

from . import crypto

SECURITY_SSL = 'ssl'
SECURITY_STARTTLS = 'starttls'
SECURITY_NONE = 'none'
SECURITY_CHOICES = [
    (SECURITY_SSL, 'SSL/TLS'),
    (SECURITY_STARTTLS, 'STARTTLS'),
    (SECURITY_NONE, 'None (unencrypted)'),
]


def _env(name, default=''):
    """Environment variable ``name``, or ``default`` when unset or empty."""
    return os.getenv(name, default) or default


def _env_int(name, default):
    """Environment variable ``name`` as an int, or ``default`` when unset or not a number."""
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


class MailServer(models.Model):
    """The Metanoia domain's mail server settings — a single row (pk=1).

    First read seeds it from ``MAILBOX_*`` environment variables, after which
    the admin settings page is the source of truth.
    """

    domain = models.CharField(max_length=120, blank=True,
                              help_text='The mail domain, e.g. metanoiagroup.co.za. Used to suggest addresses.')
    imap_host = models.CharField('IMAP server', max_length=255, blank=True)
    imap_port = models.PositiveIntegerField('IMAP port', default=993)
    imap_security = models.CharField('IMAP security', max_length=10, choices=SECURITY_CHOICES,
                                     default=SECURITY_SSL)
    smtp_host = models.CharField('SMTP server', max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField('SMTP port', default=587)
    smtp_security = models.CharField('SMTP security', max_length=10, choices=SECURITY_CHOICES,
                                     default=SECURITY_STARTTLS)
    username_is_email = models.BooleanField(
        'Sign in with the full e-mail address', default=True,
        help_text='Most hosts (cPanel, Microsoft 365, Google) expect the whole address as the username.')
    server_saves_sent = models.BooleanField(
        'Server files sent mail itself', default=False,
        help_text='Tick for Microsoft 365 or Google Workspace, which put a copy in Sent automatically. '
                  'Leave unticked for cPanel/Dovecot hosts so Metanoia Mail files the copy.')
    history_days = models.PositiveIntegerField(
        default=60, help_text='How far back to import when a mailbox is first connected.')
    max_message_mb = models.PositiveIntegerField(
        default=25, help_text='Largest total attachment size a message may carry when sending.')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Admin names for the settings row."""

        verbose_name = 'mail server settings'
        verbose_name_plural = 'mail server settings'

    def __str__(self):
        """The domain, or ``'Mail server'``."""
        return self.domain or 'Mail server'

    @classmethod
    def get(cls):
        """The settings row, created from the ``MAILBOX_*`` environment variables on first read."""
        row = cls.objects.filter(pk=1).first()
        if row is None:
            row = cls.objects.create(
                pk=1,
                domain=_env('MAILBOX_DOMAIN'),
                imap_host=_env('MAILBOX_IMAP_HOST'),
                imap_port=_env_int('MAILBOX_IMAP_PORT', 993),
                imap_security=_env('MAILBOX_IMAP_SECURITY', SECURITY_SSL),
                smtp_host=_env('MAILBOX_SMTP_HOST'),
                smtp_port=_env_int('MAILBOX_SMTP_PORT', 587),
                smtp_security=_env('MAILBOX_SMTP_SECURITY', SECURITY_STARTTLS),
            )
        return row

    @property
    def is_configured(self):
        """Whether both an IMAP and an SMTP server are set."""
        return bool(self.imap_host and self.smtp_host)


class MailAccount(models.Model):
    """One mailbox a person reads and sends from.

    The password is stored encrypted (:mod:`apps.mailbox.crypto`) and never
    shown. The ``imap_*`` and ``smtp_*`` fields are per-mailbox overrides;
    blank means "use the domain's :class:`MailServer`". ``is_default`` marks
    the mailbox new messages are sent from, and ``last_error`` holds the most
    recent sync failure. An address is unique per person.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='mail_accounts')
    email_address = models.EmailField(max_length=254)
    display_name = models.CharField(max_length=120, blank=True,
                                    help_text='The name recipients see, e.g. "Thandi Mokoena | Metanoia Group".')
    username = models.CharField(max_length=254, blank=True,
                                help_text='Leave blank to sign in with the e-mail address.')
    password_encrypted = models.TextField(blank=True, editable=False)
    signature_html = models.TextField(blank=True)

    imap_host = models.CharField(max_length=255, blank=True)
    imap_port = models.PositiveIntegerField(null=True, blank=True)
    imap_security = models.CharField(max_length=10, choices=SECURITY_CHOICES, blank=True)
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField(null=True, blank=True)
    smtp_security = models.CharField(max_length=10, choices=SECURITY_CHOICES, blank=True)

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=True, help_text='The mailbox new messages are sent from.')
    notify_new_mail = models.BooleanField('Notify me about new mail', default=True)

    connected_at = models.DateTimeField(default=timezone.now)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Default mailbox first; one row per person and address."""

        ordering = ['-is_default', 'email_address']
        constraints = [models.UniqueConstraint(fields=['user', 'email_address'], name='mailbox_unique_user_address')]

    def __str__(self):
        """The e-mail address."""
        return self.email_address

    def set_password(self, raw):
        """Encrypt and store ``raw`` (does not save the row)."""
        self.password_encrypted = crypto.encrypt(raw or '')

    def get_password(self):
        """The decrypted password."""
        return crypto.decrypt(self.password_encrypted)

    @property
    def has_password(self):
        """Whether a password is saved."""
        return bool(self.password_encrypted)

    def settings_for(self, kind):
        """``(host, port, security, username)`` for ``'imap'`` or ``'smtp'``."""
        server = MailServer.get()
        host = getattr(self, f'{kind}_host') or getattr(server, f'{kind}_host')
        port = getattr(self, f'{kind}_port') or getattr(server, f'{kind}_port')
        security = getattr(self, f'{kind}_security') or getattr(server, f'{kind}_security')
        username = self.username or (self.email_address if server.username_is_email
                                     else self.email_address.split('@')[0])
        return host, port, security, username

    @property
    def from_header(self):
        """A ``From:`` header value: the display name (or the owner's name) and the address."""
        from email.headerregistry import Address
        name = self.display_name or self.user.get_full_name() or ''
        local, _, domain = self.email_address.partition('@')
        return str(Address(display_name=name, username=local, domain=domain))

    def folder(self, role):
        """This mailbox's folder with ``role``, or ``None``."""
        return self.folders.filter(role=role).first()

    @property
    def inbox(self):
        """The Inbox folder, or ``None`` before the first sync."""
        return self.folder(MailFolder.ROLE_INBOX)


class MailFolder(models.Model):
    """A server folder mirrored for one mailbox.

    ``path`` is the folder name on the server (IMAP, modified UTF-7) and
    ``name`` its display form. ``role`` marks the well-known folders, which
    are listed in Outlook's order with its icons (``ROLE_ORDER`` and
    ``ROLE_ICONS``). ``uidvalidity`` and ``last_uid`` drive incremental sync;
    ``unread`` and ``total`` are cached counts.
    """

    ROLE_INBOX = 'inbox'
    ROLE_DRAFTS = 'drafts'
    ROLE_SENT = 'sent'
    ROLE_TRASH = 'trash'
    ROLE_JUNK = 'junk'
    ROLE_ARCHIVE = 'archive'
    ROLE_CHOICES = [
        (ROLE_INBOX, 'Inbox'), (ROLE_DRAFTS, 'Drafts'), (ROLE_SENT, 'Sent Items'),
        (ROLE_TRASH, 'Deleted Items'), (ROLE_JUNK, 'Junk Email'), (ROLE_ARCHIVE, 'Archive'),
    ]
    ROLE_ORDER = {ROLE_INBOX: 0, ROLE_DRAFTS: 1, ROLE_SENT: 2, ROLE_TRASH: 3, ROLE_JUNK: 4, ROLE_ARCHIVE: 5}
    ROLE_ICONS = {
        ROLE_INBOX: 'bi-inbox', ROLE_DRAFTS: 'bi-pencil', ROLE_SENT: 'bi-send',
        ROLE_TRASH: 'bi-trash3', ROLE_JUNK: 'bi-slash-circle', ROLE_ARCHIVE: 'bi-archive',
    }

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name='folders')
    path = models.CharField(max_length=500, help_text='The folder name on the server (IMAP, modified UTF-7).')
    name = models.CharField(max_length=255)
    delimiter = models.CharField(max_length=4, blank=True, default='/')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, blank=True)
    uidvalidity = models.BigIntegerField(null=True, blank=True)
    last_uid = models.BigIntegerField(default=0)
    unread = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """One row per mailbox and server path."""

        constraints = [models.UniqueConstraint(fields=['account', 'path'], name='mailbox_unique_folder_path')]

    def __str__(self):
        """Mailbox and folder name."""
        return f'{self.account} · {self.name}'

    @property
    def label(self):
        """The Outlook name for a well-known folder, else the folder's own name."""
        return dict(self.ROLE_CHOICES).get(self.role) or self.name

    @property
    def icon(self):
        """The Bootstrap icon class for the folder."""
        return self.ROLE_ICONS.get(self.role, 'bi-folder')

    @property
    def depth(self):
        """How deeply the folder is nested, for indenting the folder list."""
        return self.path.count(self.delimiter) if self.delimiter else 0

    @property
    def sort_key(self):
        """Well-known folders first in Outlook's order, then the rest by path."""
        return (self.ROLE_ORDER.get(self.role, 10), self.path.lower())


def _raw_upload_to(instance, filename):
    """Storage path for a message's original bytes: ``mail/<account>/<yyyy>/<mm>/<random>.eml``."""
    now = timezone.now()
    return f'mail/{instance.account_id}/{now:%Y/%m}/{uuid.uuid4().hex}.eml'


class MailItem(models.Model):
    """A message as synced from the server.

    ``to``, ``cc``, ``bcc`` and ``reply_to`` are lists of ``[name, address]``
    pairs. ``body_html`` is kept as received — sanitised when shown, never
    trusted. ``raw`` holds the original message bytes, which attachments are
    read from. A UID is unique within its folder.
    """

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name='items')
    folder = models.ForeignKey(MailFolder, on_delete=models.CASCADE, related_name='items')
    uid = models.BigIntegerField()

    message_id = models.CharField(max_length=500, blank=True, db_index=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    references = models.TextField(blank=True)

    subject = models.CharField(max_length=998, blank=True)
    from_name = models.CharField(max_length=255, blank=True)
    from_address = models.CharField(max_length=254, blank=True, db_index=True)
    to = models.JSONField(default=list, blank=True)
    cc = models.JSONField(default=list, blank=True)
    bcc = models.JSONField(default=list, blank=True)
    reply_to = models.JSONField(default=list, blank=True)
    date = models.DateTimeField(default=timezone.now, db_index=True)

    snippet = models.CharField(max_length=300, blank=True)
    body_text = models.TextField(blank=True)
    body_html = models.TextField(blank=True, help_text='As received — sanitised when shown, never trusted.')
    has_attachments = models.BooleanField(default=False)
    size = models.PositiveIntegerField(default=0)

    is_read = models.BooleanField(default=False, db_index=True)
    is_flagged = models.BooleanField(default=False)
    is_answered = models.BooleanField(default=False)
    is_draft = models.BooleanField(default=False)

    raw = models.FileField(upload_to=_raw_upload_to, storage=files_storage, blank=True, max_length=300,
                           validators=validate_mail_file)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Newest first; one row per folder and UID."""

        ordering = ['-date', '-uid']
        constraints = [models.UniqueConstraint(fields=['folder', 'uid'], name='mailbox_unique_folder_uid')]
        indexes = [models.Index(fields=['folder', '-date'], name='mailbox_item_folder_date')]

    def __str__(self):
        """The subject."""
        return self.subject or '(no subject)'

    def get_absolute_url(self):
        """The inbox with this message open in the reading pane."""
        return reverse('mailbox:inbox') + f'?folder={self.folder_id}&m={self.pk}'

    @property
    def sender_display(self):
        """The sender's name, else address."""
        return self.from_name or self.from_address or '(unknown sender)'

    @property
    def initials(self):
        """Up to two letters for the sender's avatar."""
        return _initials(self.from_name or self.from_address)

    @property
    def to_display(self):
        """The recipients' names (or addresses), comma-separated."""
        return ', '.join(n or a for n, a in self.to) if self.to else ''

    def read_raw(self):
        """The original message bytes, or ``b''`` when the file is missing."""
        if not self.raw:
            return b''
        try:
            with self.raw.open('rb') as fh:
                return fh.read()
        except (FileNotFoundError, OSError):
            return b''


class MailAttachment(models.Model):
    """An attachment's metadata; the bytes stay inside the message's raw file."""

    item = models.ForeignKey(MailItem, on_delete=models.CASCADE, related_name='attachments')
    part = models.PositiveIntegerField(help_text='Position of the part in email.message.walk().')
    filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveIntegerField(default=0)
    content_id = models.CharField(max_length=255, blank=True)
    is_inline = models.BooleanField(default=False)

    class Meta:
        """In message order."""

        ordering = ['part']

    def __str__(self):
        """The file name, or the part number."""
        return self.filename or f'part {self.part}'

    @property
    def icon(self):
        """The Bootstrap icon class for the file type."""
        return file_icon(self.filename, self.content_type)

    @property
    def preview_kind(self):
        """What the in-page preview can show the file as (see :func:`preview_type`), or ``None``."""
        return preview_type(self.filename, self.content_type)[0]


class MailDraft(models.Model):
    """A message being written. Sending it deletes the draft.

    ``source`` is the message being replied to or forwarded. ``context_label``
    records where the composer was opened from, e.g. "Invoice INV-0042" (shown
    as a chip), and ``context_ref`` the system document it carries, e.g.
    ``invoice:42`` (see :func:`apps.mailbox.attachables.after_send`).
    """

    MODE_NEW = 'new'
    MODE_REPLY = 'reply'
    MODE_REPLY_ALL = 'replyall'
    MODE_FORWARD = 'forward'
    MODE_CHOICES = [(MODE_NEW, 'New'), (MODE_REPLY, 'Reply'), (MODE_REPLY_ALL, 'Reply all'),
                    (MODE_FORWARD, 'Forward')]

    account = models.ForeignKey(MailAccount, on_delete=models.CASCADE, related_name='drafts')
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_NEW)
    source = models.ForeignKey(MailItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                               help_text='The message being replied to or forwarded.')
    to = models.TextField(blank=True)
    cc = models.TextField(blank=True)
    bcc = models.TextField(blank=True)
    subject = models.CharField(max_length=998, blank=True)
    body_html = models.TextField(blank=True)
    in_reply_to = models.CharField(max_length=500, blank=True)
    references = models.TextField(blank=True)
    context_label = models.CharField(max_length=200, blank=True)
    context_ref = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Most recently edited first."""

        ordering = ['-updated_at']

    def __str__(self):
        """The subject."""
        return self.subject or '(no subject)'

    @property
    def snippet(self):
        """The first 200 characters of the body as plain text."""
        from .parse import html_to_text
        return html_to_text(self.body_html)[:200]


def _draft_upload_to(instance, filename):
    """Storage path for a draft attachment, in its own random folder so the file name is kept."""
    now = timezone.now()
    return f'mail/drafts/{now:%Y/%m}/{uuid.uuid4().hex}/{filename}'


class MailDraftAttachment(models.Model):
    """A file attached to a :class:`MailDraft`."""

    draft = models.ForeignKey(MailDraft, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to=_draft_upload_to, storage=files_storage, max_length=300,
                            validators=validate_mail_file)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """In the order they were added."""

        ordering = ['created_at']

    def __str__(self):
        """The file name."""
        return self.filename

    @property
    def icon(self):
        """The Bootstrap icon class for the file type."""
        return file_icon(self.filename, self.content_type)

    @property
    def preview_kind(self):
        """What the in-page preview can show the file as (see :func:`preview_type`), or ``None``."""
        return preview_type(self.filename, self.content_type)[0]


def _initials(text):
    """Up to two upper-case initials from a name or e-mail address; ``'?'`` when there are none."""
    text = (text or '?').strip()
    if '@' in text and ' ' not in text:
        text = text.split('@')[0].replace('.', ' ').replace('_', ' ')
    parts = [p for p in text.replace('"', '').split() if p[:1].isalnum()]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def file_icon(filename, content_type=''):
    """The Bootstrap file icon class for a file name and content type."""
    name = (filename or '').lower()
    ct = (content_type or '').lower()
    if name.endswith('.pdf') or ct == 'application/pdf':
        return 'bi-file-earmark-pdf'
    if ct.startswith('image/'):
        return 'bi-file-earmark-image'
    if name.endswith(('.doc', '.docx', '.odt', '.rtf')):
        return 'bi-file-earmark-word'
    if name.endswith(('.xls', '.xlsx', '.csv', '.ods')):
        return 'bi-file-earmark-excel'
    if name.endswith(('.ppt', '.pptx', '.odp')):
        return 'bi-file-earmark-ppt'
    if name.endswith(('.zip', '.rar', '.7z', '.gz')):
        return 'bi-file-earmark-zip'
    return 'bi-file-earmark'


_PREVIEW_TYPES = {
    'image/png': 'image', 'image/jpeg': 'image', 'image/jpg': 'image', 'image/pjpeg': 'image',
    'image/gif': 'image', 'image/webp': 'image', 'image/bmp': 'image', 'image/avif': 'image',
    'image/x-icon': 'image', 'image/vnd.microsoft.icon': 'image',
    'application/pdf': 'pdf',
    'audio/mpeg': 'audio', 'audio/mp3': 'audio', 'audio/mp4': 'audio', 'audio/x-m4a': 'audio',
    'audio/ogg': 'audio', 'audio/wav': 'audio', 'audio/x-wav': 'audio', 'audio/wave': 'audio',
    'audio/webm': 'audio', 'audio/aac': 'audio', 'audio/flac': 'audio',
    'video/mp4': 'video', 'video/webm': 'video', 'video/ogg': 'video', 'video/quicktime': 'video',
    'text/plain': 'text', 'text/csv': 'text', 'text/tab-separated-values': 'text',
}
_PREVIEW_EXTENSIONS = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp',
    'bmp': 'image/bmp', 'avif': 'image/avif', 'ico': 'image/x-icon',
    'pdf': 'application/pdf',
    'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'wav': 'audio/wav', 'oga': 'audio/ogg', 'aac': 'audio/aac',
    'flac': 'audio/flac',
    'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm', 'ogv': 'video/ogg', 'mov': 'video/quicktime',
    'txt': 'text/plain', 'csv': 'text/csv', 'tsv': 'text/tab-separated-values', 'log': 'text/plain',
    'md': 'text/plain',
}
_NEVER_PREVIEW_TYPES = {'text/html', 'application/xhtml+xml', 'image/svg+xml', 'text/xml', 'application/xml',
                        'text/javascript', 'application/javascript'}


def preview_type(filename, content_type=''):
    """``(kind, served_type)`` for an attachment, or ``(None, None)``.

    ``kind`` is one of ``image``, ``pdf``, ``audio``, ``video`` or ``text`` and
    tells the preview which element to use; ``served_type`` is the
    Content-Type the inline response carries. Text is always served as UTF-8
    ``text/plain`` so a ``.csv`` (or anything else) can never be re-read as a
    page.
    """
    ct = (content_type or '').split(';')[0].strip().lower()
    if must_download(filename) or ct in _NEVER_PREVIEW_TYPES:
        return None, None
    if ct not in _PREVIEW_TYPES:
        ct = _PREVIEW_EXTENSIONS.get((filename or '').rpartition('.')[2].lower(), '')
    kind = _PREVIEW_TYPES.get(ct)
    if not kind:
        return None, None
    if kind == 'text':
        return kind, 'text/plain; charset=utf-8'
    return kind, {'image/jpg': 'image/jpeg', 'image/pjpeg': 'image/jpeg', 'audio/mp3': 'audio/mpeg'}.get(ct, ct)


from django.db.models.signals import post_delete  # noqa: E402
from django.dispatch import receiver  # noqa: E402


@receiver(post_delete, sender=MailItem)
@receiver(post_delete, sender=MailDraftAttachment)
def _delete_file(sender, instance, **kwargs):
    """Delete the stored file of a deleted message or draft attachment."""
    field = instance.raw if sender is MailItem else instance.file
    if field:
        try:
            field.storage.delete(field.name)
        except Exception:
            pass
