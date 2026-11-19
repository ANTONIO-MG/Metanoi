# Metanoia Mail

An Outlook-style mailbox at `/mail/` for admin, staff and educator accounts
(`apps/mailbox`). Each person reads and sends from their own Metanoia-domain
mailbox over IMAP and SMTP; students and parents never see it.

## Setting it up

1. **Server settings (once).** Sign in as admin → sidebar **Email** →
   gear icon (**Mail settings**) → *Metanoia mail server*. Enter the domain, the
   IMAP host/port/security and the SMTP host/port/security from the mail host.
   Typical values:

   | Host | IMAP | SMTP | Notes |
   |---|---|---|---|
   | cPanel / Dovecot | `mail.<domain>` 993 SSL/TLS | `mail.<domain>` 465 SSL/TLS or 587 STARTTLS | leave *Server files sent mail itself* unticked |
   | Microsoft 365 | `outlook.office365.com` 993 SSL/TLS | `smtp.office365.com` 587 STARTTLS | tick *Server files sent mail*; SMTP AUTH must be enabled per mailbox |
   | Google Workspace | `imap.gmail.com` 993 SSL/TLS | `smtp.gmail.com` 587 STARTTLS | tick *Server files sent mail*; use an app password |

   The same values can be seeded from `.env` before first use:
   `MAILBOX_DOMAIN`, `MAILBOX_IMAP_HOST`, `MAILBOX_IMAP_PORT`,
   `MAILBOX_IMAP_SECURITY` (`ssl`/`starttls`/`none`), `MAILBOX_SMTP_HOST`,
   `MAILBOX_SMTP_PORT`, `MAILBOX_SMTP_SECURITY`. After the first read the
   settings page is the source of truth.

2. **Mailboxes.** Either each person opens **Email** and connects with their
   address and password, or an admin uses *Set up a mailbox for a team member*.
   Saving tests both IMAP and SMTP sign-in and imports the last
   `history_days` (default 60) of mail quietly — history is never announced as
   new.

3. **Encryption key (recommended).** Mailbox passwords are encrypted with
   `MAILBOX_SECRET_KEY`, falling back to `SECRET_KEY`. Set
   `MAILBOX_SECRET_KEY` once; changing whichever key is in use makes every
   saved mailbox password unreadable and each one must be re-entered.

## How new mail arrives

* While someone has any page open, `static/js/chrome.js` calls `/mail/poll/`
  every minute; the server checks their inbox in a background thread
  (throttled to once per 45 s per mailbox) and the sidebar **Email** badge
  updates.
* The open mailbox page also re-checks the folder it shows every minute.
* The `mail-sync` scheduler job (`apps/scheduler/jobs.py`) covers everyone
  else. It is listed in the quick tick (every 15 min, 06:00–22:45). After
  pulling this change, reinstall that unit so the job is picked up:

  ```
  sudo cp deploy/scheduler/production/metanoia-scheduler-quick.service /etc/systemd/system/
  sudo systemctl daemon-reload
  ```

* The `mail-inbox` job checks every inbox every 2 minutes, day and night,
  so new mail is announced even when nobody has a page open. Install its
  timer once:

  ```
  sudo cp deploy/scheduler/production/metanoia-mail.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now metanoia-mail.timer
  ```

Each new unread inbox message raises a platform notification linking straight
to the message. More than three in one check collapse into one "N new emails"
notification. Each mailbox can switch this off in Mail settings. On any open
page (static/js/chrome.js):

* the bell plays the **mail chime**, its green count goes up, and its list
  redraws in place, with no reload;
* a card pops up in the bottom-right corner ("New email from …" and the
  subject); clicking it opens the e-mail;
* browsers mute sound until the page has been clicked once, so the chime
  starts after the first click or key press.

**Desktop alerts.** In the bell dropdown, *Desktop alerts for new mail →
Turn on* asks the browser for permission and subscribes it to Web Push
(`apps/communication/webpush.py`, service worker `/sw.js`). New mail then shows
as a system notification even with no Metanoia tab open, as long as the browser
is running. Clicking it opens the e-mail. Switch it on once in each account you
sign in to on that computer (accounts@, info@ …), since alerts follow the
account that owns the mailbox. The VAPID key pair is in `.env`
(`VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT`). Changing it
means everyone switches desktop alerts on again. On iPhone and iPad, Web Push
only works for the site added to the Home Screen.

## Sending documents from the Learning Hub

Any page can open a ready-to-send message:
`/mail/compose/?attach=invoice:<id>` (also `receipt:<id>`, `estimate:<id>`).
The composer opens with the customer as recipient, a subject, a covering note
with the pay link and the PDF attached; sending it marks the invoice or
estimate as sent, exactly as the finance *Send* button does. Invoice and
estimate pages show an **Email from Metanoia Mail** button. New document types
are added in `apps/mailbox/attachables.py` with `@provider('kind')` and,
optionally, `@sent_hook('kind')`.

## Safety notes

* Incoming HTML is sanitised with nh3 every time it is shown and rendered in a
  sandboxed, script-less iframe with a strict CSP; remote images stay blocked
  until the reader clicks *Show pictures*.
* Attachments download with `Content-Disposition: attachment` and `nosniff`;
  only raster images are ever served inline (for `cid:` pictures).
* The original `.eml` of every synced message is kept in the files storage
  (`mail/<account>/…`) and deleted with the message.

## Signatures

Every mailbox carries the Metanoia signature, in the same layout as the
automated mail: the ringed picture, the name, the role line, the contact
details and the address, the logo, the four-colour strip and the e-mail banner
(`static/email/banner.jpg`)
(`apps/mailbox/signatures.py`, `templates/mailbox/signature.html`).

* Personal mailboxes (one per staff member, dev@, educators) show the owner's
  LMS profile picture, their full name and their role from
  `apps/communication/senders.PERSONAL_ROLES` (anyone not listed there:
  Metanoia team).
* accounts@, info@ and learninghub@ show the ringed Metanoia mark and the
  mailbox's name, exactly as their automated mail does.

Metanoia Mail adds it to new mail, replies and forwards. Changing *From* swaps in
that mailbox's signature. When a message is sent, the signature's pictures
travel inside it as inline parts, so recipients see them without clicking
"show pictures". New mailboxes get the signature when connected. To rebuild
them all after a change of photo or role, run `manage.py mail_signatures --force`.
Mail settings → *Edit* also shows a preview, a *Use the Metanoia signature*
reset and *Copy for other mail apps*.

### Signatures elsewhere (Roundcube, Outlook, phones)

A typical shared-hosting mail service (DirectAdmin or cPanel + Exim) has no
setting that stamps a signature on mail at the server, whichever app sent it.
Microsoft 365 and Google Workspace need paid add-ons such as Exclaimer or
CodeTwo for that. So each app keeps its own copy of the signature.
Press *Copy for other mail apps* in Mail settings and paste:

* **Webmail (Roundcube)**: Settings → Identities → the address →
  Signature, switch on the HTML editor, paste, and Save.
* **Outlook (desktop)**: File → Options → Mail → Signatures → New, paste, and
  pick it for new messages and replies.
* **Outlook on the web / new Outlook**: Settings → Accounts → Signatures.
* **iPhone Mail**: Settings → Mail → Signature. Paste, then shake to undo if
  iOS strips the formatting, or keep the plain text.
* **Gmail app sending as the address**: Settings → the account → Signature.

The pictures load from https://learn.metanoiagroup.co.za, so these pasted
copies work anywhere.

## Sending drafts in one go

**Send drafts** in the ribbon opens a list of every draft in your mailboxes,
showing To, Subject and From with a tick box each. Tick the ones to send (or
tick the box in the header for all), then press *Send selected*. They go one
after another, each as an ordinary Send, and each row shows a tick or the
reason it failed. Drafts without a recipient cannot be ticked.

## Bringing over the old Gmail mail

Both commands read info@metanoiagroup.co.za over IMAP with a Google app
password in `GMAIL_APP_PASSWORD`, which is read from the environment only.
Neither command sends anything.

* `manage.py copy_gmail_drafts --to info@… --to <name>@…` copies every Gmail
  draft into those mailboxes' Drafts, keeping recipients, subject, body,
  attachments and reply threading. The Gmail signature becomes the mailbox's
  Metanoia signature, and a line goes in after the greeting saying the sender is
  writing from its new accounts (`--no-notice` leaves it out). Re-running
  skips drafts already copied.
* `manage.py gmail_archive --to info@… --max-attachment-mb 5` copies all mail
  into **Gmail Archive** on that mailbox, in Inbox, Sent, PayHip,
  FNB inContact and Archived. The copy uses IMAP APPEND, so the original
  dates, senders and read state are kept and no one receives anything. To keep
  the archive small, a file already archived on an earlier e-mail becomes a
  one-line note naming that e-mail, and so does any file over the cap. Those
  files stay in Gmail, which is never changed. Gmail limits IMAP downloads to
  about 2.5 GB a day. If a run stops, start it again and it carries on from
  `~/metanoia-backups/gmail-archive/state.json`.
* `manage.py mail_backfill --account info@… --folder-prefix "Gmail Archive"`
  then brings the whole archive into Metanoia Mail's list and search. Normal sync
  only imports the last `history_days`.
