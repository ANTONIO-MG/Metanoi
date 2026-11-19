"""Which mailbox each outbound e-mail is sent from.

The platform sends from three mailboxes on ``mail.metanoiagroup.co.za``, split
by what the reader is dealing with:

    accounts@     money and the account itself — invoices, receipts, estimates,
                  orders, registration, invites, e-mail verification, password
                  resets and changes
    learninghub@  learning — new and upcoming live sessions, course welcomes,
                  and notifications about content, assessments, deadlines,
                  grades, tasks and the weekly summary
    info@         everything else — announcements, general messages, mail
                  composed in the hub, and any notification not listed above

Each mailbox signs in to SMTP as itself (``EMAIL_<NAME>_USER`` /
``EMAIL_<NAME>_PASSWORD`` in ``.env``) rather than one login sending "as" the
others, so the mail server never rewrites or refuses the From: address. A
mailbox with no credentials falls back to the default connection and
``DEFAULT_FROM_EMAIL``, which keeps development and the test runner working
with nothing configured.

Lookup tables:

- ``TEMPLATE_MAILBOX`` — template name (``communication/email/<name>.html``) → mailbox.
  Anything not listed goes out from info@.
- ``NOTIFICATION_MAILBOX`` — notification category
  (``NotificationPreference.CATEGORIES``) → mailbox. Mentions, messages, announcements
  and uncategorised notes stay on info@.
- ``SIGNATURES`` — the signature each mailbox signs with, in the Metanoia Brand Studio
  layout: ringed tick, name with a green full stop, role line, contact line, logo and
  the three pillars. The address shown is the mailbox's own.
- ``PERSONAL_ROLES`` — the role line on a person's own signature, by e-mail. Only the
  administrator mailbox is listed; anyone not listed signs as part of the Metanoia
  team. Add a person's own role line here when they join.
- ``BRAND_ACCOUNTS`` — accounts that are the business itself rather than a person:
  content they publish is signed by the mailbox it belongs with, not by "Metanoia
  Coach".

Who an in-app notification is "from": the list, the bell and the reading page show the
sender's picture — a member of staff or an educator who caused it (an announcement they
sent, a mention) is shown as that person; anything the platform made is shown as the
Metanoia mailbox it belongs with (accounts@ for money and the account, learninghub@ for
study, info@ for everything else — the same split the e-mail copies use). Older
notifications carry no category, so their mailbox is read from what they point at (a
live session, the page they open, see ``_ACCOUNTS_PATHS``) and their title.
"""

from django.conf import settings
from django.core.mail import get_connection

ACCOUNTS = 'accounts'
INFO = 'info'
LEARNING = 'learninghub'

TEMPLATE_MAILBOX = {
    'invoice': ACCOUNTS,
    'invoice-reminder': ACCOUNTS,
    'estimate': ACCOUNTS,
    'purchase': ACCOUNTS,
    'payment-made': ACCOUNTS,
    'registration-thank-you': ACCOUNTS,
    'registration-document': ACCOUNTS,
    'trial-ending': ACCOUNTS,
    'password-changed': ACCOUNTS,
    'invite-parent': ACCOUNTS,
    'invite-educator': ACCOUNTS,
    'event-upcoming': LEARNING,
    'session-scheduled': LEARNING,
    'session-invite': LEARNING,
    'welcome-class': LEARNING,
}

NOTIFICATION_MAILBOX = {
    'meetings': LEARNING,
    'content': LEARNING,
    'assessments': LEARNING,
    'deadlines': LEARNING,
    'grades': LEARNING,
    'weekly': LEARNING,
}


SIGNATURES = {
    ACCOUNTS: {'name': 'Metanoia Accounts', 'role': 'Accounts · Metanoia Group',
               'email': 'accounts@metanoiagroup.co.za'},
    INFO: {'name': 'Metanoia Info', 'role': 'Info · Metanoia Group',
           'email': 'info@metanoiagroup.co.za'},
    LEARNING: {'name': 'Metanoia Learning Hub', 'role': 'Learning Hub · Metanoia Group',
               'email': 'learninghub@metanoiagroup.co.za'},
}


PERSONAL_ROLES = {
    'admin@metanoiagroup.co.za': 'Administrator · Metanoia Group',
}


BRAND_ACCOUNTS = {'info@metanoiagroup.co.za'}


def _is_person(user):
    """A real member of the team, not one of the sending-mailbox or brand accounts."""
    from core.staff_accounts import SYSTEM_ACCOUNTS
    email = (getattr(user, 'email', '') or '').lower()
    return bool(email) and email not in {row[0] for row in SYSTEM_ACCOUNTS} | BRAND_ACCOUNTS


def signature(mailbox, author=None):
    """The signature block for an e-mail.

    Written by a person (``author`` — hub mail, an announcement someone sent):
    their name, role, address and their own LMS profile picture in the ring.
    Otherwise the sending mailbox's signature (info@'s when unknown) with the
    ringed Metanoia tick. Both carry the WhatsApp number from the catalogue.
    """
    from core.branding import strings
    contact = (strings().get('brand') or {}).get('contact') or {}
    if author is not None and _is_person(author):
        from core.utils import display_name
        sig = {'name': display_name(author),
               'role': PERSONAL_ROLES.get(author.email.lower(), 'Metanoia team · Metanoia Group'),
               'email': author.email, 'author': author}
    else:
        sig = dict(SIGNATURES.get(mailbox) or SIGNATURES[INFO])
    sig['whatsapp'] = contact.get('whatsapp') or contact.get('phone') or ''
    sig['whatsapp_url'] = contact.get('whatsapp_url') or ''
    return sig


def mailbox_for_template(template):
    """The mailbox an e-mail rendered from ``template`` is sent from (info@ by default).
    """
    return TEMPLATE_MAILBOX.get(template, INFO)


def mailbox_for_category(category):
    """The mailbox for a notification ``category`` (info@ by default)."""
    return NOTIFICATION_MAILBOX.get(category or '', INFO)


def _credentials(mailbox):
    """The SMTP credentials configured for ``mailbox`` in ``EMAIL_MAILBOXES`` (or ``{}``).
    """
    return (getattr(settings, 'EMAIL_MAILBOXES', None) or {}).get(mailbox) or {}


def from_address(mailbox):
    """``"Metanoia Group <accounts@…>"``, or DEFAULT_FROM_EMAIL if unset."""
    user = _credentials(mailbox).get('user')
    if not user:
        return settings.DEFAULT_FROM_EMAIL
    name = getattr(settings, 'EMAIL_FROM_NAME', '')
    return f'{name} <{user}>' if name else user


def connection(mailbox):
    """An SMTP connection signed in as ``mailbox``; ``None`` means "use the default"."""
    creds = _credentials(mailbox)
    if not creds.get('user'):
        return None
    return get_connection(
        host=settings.EMAIL_HOST, port=settings.EMAIL_PORT,
        username=creds['user'], password=creds.get('password', ''),
        use_tls=settings.EMAIL_USE_TLS, use_ssl=settings.EMAIL_USE_SSL,
        timeout=settings.EMAIL_TIMEOUT,
    )


def apply(msg, mailbox):
    """Point an already-built message at ``mailbox``: its From: and its login."""
    if _credentials(mailbox).get('user'):
        msg.from_email = from_address(mailbox)
        msg.connection = connection(mailbox)
    return msg


_ACCOUNTS_PATHS = ('/finance/', '/shop/', '/pay', '/accounts/', '/registration', '/invoice', '/orders')
_LEARNING_PATHS = ('/learning/', '/livesessions/', '/assessments/', '/reports/', '/revision/',
                   '/calendar', '/modules/', '/communication/meetings/')
_ACCOUNTS_WORDS = ('invoice', 'payment', 'receipt', 'order', 'estimate', 'trial', 'registration',
                   'account', 'password', 'refund', 'subscription', 'paid', 'debit', 'booking')
_LEARNING_WORDS = ('new item', 'new lesson', 'mock', 'past paper', 'week ', 'topic', 'session', 'class', 'module', 'material', 'recording', 'transcript', 'assessment',
                   'test', 'quiz', 'exam', 'deadline', 'grade', 'mark', 'lesson', 'study', 'timetable',
                   'week ahead', 'learning hub', 'summary', 'agenda', 'task', 'revision', 'progress')


def _mailbox_for_notification(n):
    """The mailbox a notification belongs with: by category, else by meeting, URL and
    title.
    """
    if getattr(n, 'category', ''):
        return mailbox_for_category(n.category)
    if n.meeting_id:
        return LEARNING
    url = (n.url or '').lower()
    if url.startswith(_ACCOUNTS_PATHS):
        return ACCOUNTS
    if url.startswith(_LEARNING_PATHS):
        return LEARNING
    title = f'{n.title} {n.verb}'.lower()
    if title.startswith('new email'):
        return INFO
    if any(w in title for w in _ACCOUNTS_WORDS):
        return ACCOUNTS
    if any(w in title for w in _LEARNING_WORDS):
        return LEARNING
    return INFO


_mailbox_users = {}


def mailbox_user(mailbox):
    """The hub account behind a sending mailbox (accounts@ / info@ / learninghub@), cached."""
    email = (SIGNATURES.get(mailbox) or SIGNATURES[INFO])['email']
    if email not in _mailbox_users:
        from django.contrib.auth import get_user_model
        _mailbox_users[email] = (get_user_model().objects.select_related('profile')
                                 .filter(email__iexact=email).first())
    return _mailbox_users[email]


def notification_sender(n):
    """``{'person', 'mailbox', 'name', 'role', 'avatar', 'user'}`` for a notification.

    ``person`` is true when a real member of the team sent it; otherwise
    ``mailbox`` names the Metanoia mailbox and ``user`` is that mailbox's own
    hub account (its picture is the ringed Metanoia tick).

    When the author is one of the mailbox accounts itself, the matching mailbox is used.
    """
    from core.utils import avatar_url, display_name
    author = n.actor or (n.announcement.sender if n.announcement_id and n.announcement else None)
    if author is not None and _is_person(author):
        return {'person': True, 'mailbox': '', 'user': author, 'name': display_name(author),
                'role': PERSONAL_ROLES.get((author.email or '').lower(), ''), 'avatar': avatar_url(author)}
    mailbox = _mailbox_for_notification(n)
    if author is not None:
        email = (author.email or '').lower()
        mailbox = next((k for k, v in SIGNATURES.items() if v['email'] == email), mailbox)
    sig = SIGNATURES.get(mailbox) or SIGNATURES[INFO]
    user = mailbox_user(mailbox)
    from django.templatetags.static import static
    return {'person': False, 'mailbox': mailbox, 'user': user, 'name': sig['name'], 'role': sig['role'],
            'avatar': avatar_url(user) if user else static('email/sig-avatar.png')}
