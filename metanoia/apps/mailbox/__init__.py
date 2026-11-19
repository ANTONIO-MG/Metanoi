"""Metanoia Mail — an Outlook-style mailbox for admin, staff and educators.

Each person connects their own Metanoia-domain mailbox (IMAP to receive, SMTP to
send). Messages are synced into the database so the inbox opens instantly, new
mail raises a platform notification, and any page in the system can open the
composer pre-filled with a generated document attached (an invoice PDF, say).

Layout:

* :mod:`.models`      — server settings, accounts, folders, messages, drafts
* :mod:`.crypto`      — mailbox passwords are stored encrypted
* :mod:`.imap`        — a thin, testable wrapper over :mod:`imaplib`
* :mod:`.parse`       — raw RFC 822 bytes → what the reading pane shows
* :mod:`.sync`        — pulls new mail and flag changes, raises notifications
* :mod:`.send`        — builds and sends over SMTP, files a copy in Sent
* :mod:`.attachables` — system documents a composer can attach by reference
"""
