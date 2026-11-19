# Automated messages: complete catalogue

Every message the platform sends **by itself**: e-mails, in-app notifications
(the bell), WhatsApp messages, browser alerts and scheduled reminders. It is
meant for rewording. Each entry gives the exact wording as it is today and the
file it lives in, so the new wording can be put straight back in.

*Compiled 2026-11-07 from the code in this repository. Line numbers can drift
as the code changes. If one no longer matches, search the file for the quoted
title.*

**How to read the entries**

- `{{ name }}` is a blank filled in when the message is sent. In Django
  templates (`.html` / `.txt`) it is written exactly like that. In Python
  strings it is written `{name}` inside an `f'...'` string. Keep the blank
  when rewording; you can move it around the sentence.
- `[BUTTON: LABEL → link]` is the branded button in an HTML e-mail.
- *(only if …)* marks a line that appears only under that condition.
- **Mailbox** is the From: address: `accounts@`, `info@` or `learninghub@`
  (all `@metanoiagroup.co.za`). See [Shared parts](#0-shared-parts-of-every-e-mail)
  for how it is chosen.
- **E-mail copy** (for in-app notifications) says whether the bell notice is
  also e-mailed. Many automated notices are e-mailed **only** to people who
  switched on *Reminders by e-mail* or *Updates by e-mail* under Settings →
  Notifications. Both are **off by default**.
- **WhatsApp copy.** *Every* in-app notification created through `notify()` is
  also sent to WhatsApp for people who switched WhatsApp on (off by default) and
  have a number, outside their quiet hours. The format is in
  [WA-NOTIFY-COPY](#wa-notify-copy--whatsapp-copy-of-any-notification). The
  only exception is NT-TRIAL-MARK.

---

## Table of contents

0. [Shared parts of every e-mail](#0-shared-parts-of-every-e-mail)
1. [Summary table](#1-summary-table)
2. [Sign-up and accounts](#2-sign-up-and-accounts)
3. [Parents and invited educators](#3-parents-and-invited-educators)
4. [Enrolment and billing (registration, invoices, receipts)](#4-enrolment-and-billing)
5. [Shop and 1-on-1 bookings](#5-shop-and-1-on-1-bookings)
6. [Live sessions](#6-live-sessions)
7. [Learning and assessments](#7-learning-and-assessments)
8. [Community and messaging](#8-community-and-messaging)
9. [Staff and admin notices](#9-staff-and-admin-notices)
10. [Broadcasts (announcements)](#10-broadcasts-announcements)
11. [WhatsApp](#11-whatsapp)
12. [Templates that exist but are never sent (dormant)](#12-dormant-templates)
13. [Important on-screen (flash) messages](#13-important-on-screen-flash-messages)
14. [Things noticed while cataloguing](#14-things-noticed-while-cataloguing)
15. [How to update a message](#15-how-to-update-a-message)

---

## 0. Shared parts of every e-mail

All branded e-mails use one layout,
`templates/communication/email/base_email.html`. You only reword the middle of
each e-mail; the parts below are shared.

**Layout, top to bottom**

1. *Hidden preview text* (`{% block preheader %}`). It shows in the inbox list
   and defaults to the subject.
2. *Big heading* (`{% block heading %}`).
3. *Body* (`{% block content %}`).
4. *Button* (`{% block cta %}`, drawn by `communication/email/_button.html`).
5. *Small print* (`{% block disclaimer %}` and the grey `{% block expiry_note %}`).
6. **Signature**, chosen by sending mailbox (from `apps/communication/senders.py`, `SIGNATURES`):
   - `Metanoia Accounts.` / `Accounts · Metanoia Group`
   - `Metanoia Info.` / `Info · Metanoia Group`
   - `Metanoia Learning Hub.` / `Learning Hub · Metanoia Group`
   - A broadcast sent by a person is signed with **their** name and role
     (`PERSONAL_ROLES`, e.g. `Founder · Metanoia Group`; anyone else
     `Metanoia team · Metanoia Group`).
   - Each signature then has the line `WhatsApp <brand.contact.whatsapp> · <mailbox address>`
     and the tagline `EMPOWERING THROUGH TRANSFORMATION.`
7. **Banner image.** Alt text: *"Metanoia Group. Empowering through transformation."*
8. **Unsubscribe block.** It is added to every e-mail to one known account
   unless a person signed it (so it also appears on invoices and receipts):
   > [Unsubscribe]
   > This is an automated e-mail from your Metanoia Group account. Choose which e-mails you get. Invoices, receipts and account e-mails are always sent.
9. **Small print:** `Copyright © 2026 Metanoia Group. All rights reserved.` · Terms & conditions · Privacy policy.

Brand values (name, support e-mail `info@metanoiagroup.co.za`, WhatsApp
number, copyright) come from `metanoia/.strings.json` (`brand`
section). Greetings use the `greeting_name` filter: the person's first name,
or "there".

**Which mailbox sends it** (`apps/communication/senders.py`)

| Mailbox | Sends |
|---|---|
| `accounts@` | invoice, invoice-reminder, estimate, purchase, payment-made, registration-thank-you, registration-document, trial-ending, password-changed, invite-parent, invite-educator, **all allauth e-mails** (verify, reset, …), shop download e-mail |
| `learninghub@` | event-upcoming, session-scheduled, session-invite, welcome-class, and notification e-mails in categories `meetings`, `content`, `assessments`, `deadlines`, `grades`, `weekly` |
| `info@` | everything else: announcements, the `general` template (staff account invitation), and notification e-mails with category `mentions`, `messages`, `announcements` or none |

**HTML vs plain-text part.** Each e-mail has an HTML part and a plain-text
part. The plain-text part comes from a matching `.txt` template **only where
one exists**:

- `communication/email/announcement.txt`
- `communication/email/notification.txt`
- `communication/email/session-invite.txt`
- every allauth e-mail in `templates/account/email/*.txt`

For **every other template** there is no `.txt` file.
`send_branded_email` (`apps/communication/emails.py:203-206`) then builds
the plain-text part with `strip_tags()` on the HTML. That removes the tags but
**keeps the text inside the `<style>` block** of `base_email.html`. This is why
the plain-text part of those e-mails starts with raw CSS. The affected
templates are: `estimate`, `event-upcoming`, `general`, `invite-educator`,
`invite-parent`, `invoice`, `invoice-reminder`, `password-changed`,
`payment-made`, `purchase`, `registration-document`, `registration-thank-you`,
`session-scheduled`, `trial-ending`, `welcome-class`. To fix it, add a `.txt`
twin for each (see [§15](#15-how-to-update-a-message)).

**The notification e-mail (EM-NOTIFICATION).** Any in-app notification that is
e-mailed uses one wrapper, so its wording is the notification's title and body:

- Subject: `{{ notification.title }}` (or its verb if it has no title)
- Template: `templates/communication/email/notification.html` + `notification.txt`
- Sent by `send_notification_email`, `apps/communication/emails.py:230`

```
[Heading] {{ notification.title }}
Hi {{ first_name }},
From {{ actor name }}            (only if a person sent it)
{{ notification.body }}
[BUTTON: OPEN → {{ notification.url }}]
```

Plain-text part:

```
{{ notification.title }}
From: {{ actor }}                (only if a person sent it)

{{ notification.body }}
Open: {{ url }}
--
Copyright © 2026 Metanoia Group. All rights reserved.
Unsubscribe: … / E-mail preferences: … / Terms: … · Privacy: …
```

**Scheduler cadence.** Jobs are listed in `apps/scheduler/jobs.py`. The
production timers in `deploy/scheduler/production/` decide when they actually
run:

- **Main tick:** hourly at :05, from 06:05 to 22:05 SAST.
- **Quick tick:** every 15 minutes, 06:00–22:45 SAST. It runs `session-reminders`,
  `send-broadcasts`, `shop-bookings`, `auto-notices` and `mail-sync`.
- **Sessions tick:** 06:00 and 22:00 SAST.
- **Recordings tick:** every 5 minutes, day and night, for classes that ended in the last 36 hours.
- **Mail tick:** every 2 minutes.

Nothing on the main or quick tick runs overnight.

---

## 1. Summary table

Channels: **EM** e-mail · **NT** in-app notification (bell, sound, plus an
e-mail and WhatsApp copy where noted) · **WA** WhatsApp · **RT** real-time
pop-up only (not stored) · **MS** Microsoft Outlook invite.

| ID | Ch. | Title / subject | Trigger |
|---|---|---|---|
| EM-ACC-VERIFY-SIGNUP | EM | Welcome to Metanoia Group — confirm your e-mail | Sign-up |
| EM-ACC-VERIFY | EM | Confirm your e-mail address — Metanoia Group | Unverified sign-in attempt; "Become a student" |
| EM-ACC-EXISTS | EM | You already have a Metanoia Group account | Sign-up with an address already registered |
| EM-ACC-RESET | EM | Reset your Metanoia Group password | Forgot password (known address) |
| EM-ACC-UNKNOWN | EM | Password reset attempted — Metanoia Group | Forgot password (unknown address) |
| EM-ACC-PW-CHANGED | EM | Your password was changed | Password changed or reset |
| EM-ACC-STAFF-INVITE | EM | Your Metanoia Group account | Superuser creates a team account |
| NT-WELCOME-1…4 | NT | Welcome to the Metanoia family, … / Quick guide … / Your study timetable … / Who to talk to | Onboarding completed (once) |
| NT-COMMUNITY-WELCOME | NT | Welcome to the Metanoia Community | Community sign-up |
| EM-ACC-EDU-INVITE | EM | You have been invited to teach on Metanoia | Staff invite an educator / resend |
| EM-REG-THANKYOU | EM | Thank you for registering — your Metanoia Group summary | Registration finished (student, parent, educator) |
| EM-REG-DOC (invoice) | EM | Your invoice — Metanoia Group | Student takes the free week (EFT) |
| EM-REG-DOC (receipt) | EM | Your receipt — Metanoia Group | Registration invoice paid in full |
| WA-REG-INVOICE | WA | (invoice PDF + caption / `metanoia_invoice` template) | Student takes the free week (EFT) |
| EM-TRIAL-ENDING | EM | Your free week ends tomorrow — invoice … is still open | Hourly job, < 24 h before free week ends, unpaid |
| NT-TRIAL-MARK | NT | Free week ending — invoice reminder sent | Same job (bell record) |
| EM-INVOICE | EM | Invoice … — payment requested | Staff "send"; single-module EFT unlock |
| EM-INVOICE-REMINDER ×4 | EM | Reminder: invoice … is due in 3 days / … is due today / … is overdue / Reminder: invoice … | Daily finance job (−3, 0, +7 days) or staff button |
| EM-ESTIMATE | EM | Estimate … from us | Staff send an estimate |
| NT-ESTIMATE-RESPONSE | NT | Estimate … accepted / declined | Customer responds to an estimate |
| EM-PURCHASE | EM | Order confirmed — … | Shop checkout with balance to pay |
| EM-RECEIPT | EM | Receipt — Invoice … paid | Any non-registration invoice paid in full |
| EM-SHOP-DOWNLOADS | EM | Your download — order … | Paid order with digital products |
| NT-SHOP-PAID | NT | Payment received — order … | Shop order paid |
| NT-SHOP-SALE | NT | New sale — R… | Shop order paid (to staff) |
| NT-SHOP-SHIPPED | NT | Shipped — order … | Courier booked / manual dispatch |
| NT-SHOP-DELIVERED | NT | Delivered — order … | Courier tracking job / staff mark delivered |
| NT-BOOKING-CONFIRMED | NT | Your 1-on-1 is booked | 1-on-1 booking paid |
| NT-BOOKING-NEW | NT | New 1-on-1 booked | 1-on-1 booking paid (to educator) |
| NT-BOOKING-NEWTIME | NT | Please choose a new time | Payment after the held slot was lost |
| NT-BOOKING-MOVED | NT | 1-on-1 moved | Booking rescheduled |
| NT-BOOKING-CANCELLED | NT | 1-on-1 cancelled | Staff or educator cancels a booking |
| NT-SESSION-NEW | NT | New session: … | Live session scheduled |
| EM-SESSION-INVITE | EM | Live session scheduled — … | Live session scheduled (opt-in) / manager re-shares |
| MS-OUTLOOK-INVITE | MS | `{{ meeting.title }}` | Teams session created (licensed staff only) |
| NT-SESSION-REMINDER | NT | Session in 1 day / Session in 30 minutes: … | 24 h and 30 min before a session |
| NT-SESSION-CANCELLED | NT | Cancelled: … | Session cancelled |
| NT-SESSION-RECORDING | NT | Recording (and transcript) available — … | Recording found after class |
| NT-SESSION-SUMMARY-READY | NT | Session summary ready — … / Now available: summary of … | Summary published (after Teams Copilot's insight, or 48 h without it) |
| NT-SESSION-PACK | NT | Session summary, next agenda & action items — … / Session material — … | AI follow-up run after class |
| NT-SESSION-ATTENDANCE | NT | Your session record — … | 1-on-1 session processed |
| NT-CALENDAR-DISCONNECTED | NT | Calendar disconnected — … | Connected calendar keeps failing |
| NT-MEETING-INVITE | NT | Meeting: … | Someone invites people to a meeting (API) |
| EM-SESSION-SCHEDULED | EM | Session scheduled: … | Host creates a meeting in Communication → Meetings |
| NT-AUTO-MATERIAL | NT | New {{ kind }}: … | Material published on a module |
| NT-AUTO-LESSON | NT | New lesson: … | Lesson published / scheduled publish time |
| NT-AUTO-ASSESSMENT-OPEN | NT | … is open | Assessment opened / scheduled |
| NT-AUTO-SOLUTIONS | NT | Solutions released: … | Assessment closed with release-on-close |
| NT-AUTO-RESULT | NT | Your … has been marked — …% | Coach finishes hand-marking |
| NT-AUTO-VIDEO | NT | New video: … | New YouTube video in a mapped playlist |
| NT-AUTO-FOLDED | NT | N new items in … | Several content items on one module at once |
| NT-DEADLINE-ASSESSMENT | NT | Closing soon: … | Assessment closes within the lead |
| NT-WEEKLY-STUDENT | NT | Your week ahead | Monday 06:05 |
| NT-WEEKLY-EDUCATOR | NT | Your teaching week ahead | Monday 06:05 |
| NT-REENGAGE | NT | It's been N days. Here's where you left off | Daily; student inactive 7+ days |
| NT-NUDGE | NT | … misses you | Educator clicks "Nudge" on a module |
| NT-ATRISK-STUDENT | NT | Checking in on your progress | Staff "notify student" on an at-risk flag |
| NT-ATRISK-EDUCATOR | NT | … may need support | Staff "notify educator" on an at-risk flag |
| NT-CONNECT-REQUEST | NT | New connection request | Student asks to message another student |
| NT-CONNECT-ACCEPTED | NT | Connection accepted | Request accepted |
| NT-CHAT-MENTION | NT | … mentioned you in … | @mention in chat |
| NT-COMMUNITY-MENTION | NT | … mentioned you in … | @tag in a community post or comment |
| NT-STUDYGROUP-ADDED | NT | You were added to … | Added to a study group |
| NT-APPEAL-DECIDED | NT | Your appeal was approved / not upheld | Staff decide a moderation appeal |
| RT-CHAT-MESSAGE | RT | `{{ sender name }}` | New chat message |
| NT-ONE-ON-ONE-REQUEST | NT | New one-on-one request | Student requests 1-on-1 coaching (to staff) |
| NT-ONE-ON-ONE-SETUP | NT | Your one-on-one coaching is set up | Staff mark the request enrolled |
| NT-MOD-REPORTED | NT | A community post was reported | First report on a post (to staff) |
| NT-MOD-HIDDEN | NT | A community post was hidden after N reports | 3rd report (to staff) |
| NT-MAIL-NEW | NT | New email from … / N new emails in … | Metanoia Mail sync finds new inbox mail |
| RT-WHATSAPP-STAFF | RT | WhatsApp · … | Inbound WhatsApp needs a person |
| NT-BROADCAST | NT | `{{ announcement.title }}` | Staff broadcast sent (now or scheduled) |
| EM-ANNOUNCEMENT | EM | `{{ announcement.title }}` | Same, if "also e-mail" ticked |
| WA-OTP | WA | (verification code) | Phone verification |
| WA-NOTIFY-COPY | WA | (copy of any notification) | Any notification, if WhatsApp on |
| WA-NEW-MESSAGE | WA | (template "the team has replied") | Staff reply after the 24 h window closed |
| WA-BOT-* (14) | WA | (bot replies) | Inbound WhatsApp message |

---

## 2. Sign-up and accounts

### EM-ACC-VERIFY-SIGNUP: confirm e-mail after sign-up

- **Channel:** e-mail (allauth)
- **Subject:** `Welcome to Metanoia Group — confirm your e-mail`. Any `ACCOUNT_EMAIL_SUBJECT_PREFIX` (env, empty by default) is put in front.
- **When:**
  - Immediately after sign-up on `/myhub/page-register/` (`apps/myhub/views.py:411`).
  - When a parent creates an account from a student's parent link (`apps/accounts/views.py:954`).
- **Who:** the new account's e-mail address
- **Mailbox:** accounts@
- **Source:**
  - Subject: `templates/account/email/email_confirmation_signup_subject.txt`
  - HTML: `templates/account/email/email_confirmation_signup_message.html`
  - Text: `templates/account/email/email_confirmation_signup_message.txt`
- **Message (HTML):**
```
Preview: Welcome to Metanoia Group — confirm your e-mail to finish.
[Heading] Welcome to Metanoia Group!
Hi,
Thanks for signing up. One last step — confirm your e-mail address to finish setting up your account.
[BUTTON: CONFIRM MY EMAIL → {{ activate_url }}]
If you didn't deliberately sign up for Metanoia Group, please ignore this e-mail — no account will be activated and nothing else will happen.
This confirmation link will expire soon. Questions? info@metanoiagroup.co.za.
```
- **Message (plain text):**
```
Welcome to Metanoia Group!

Hi,

Please confirm your e-mail address to finish setting up your account:

{{ activate_url }}

If you didn't deliberately sign up, please ignore this e-mail — no account
will be activated and nothing else will happen.

--
{{ brand.copyright }}
```

### EM-ACC-VERIFY: confirm e-mail (not at sign-up)

- **Channel:** e-mail (allauth)
- **Subject:** `Confirm your e-mail address — Metanoia Group`
- **When:**
  - An unverified student tries to sign in. A fresh link is sent and sign-in is refused (`apps/myhub/views.py:299`).
  - A community member clicks "Become a student" (`apps/accounts/community_views.py:132`).
  - Any other allauth confirmation of an added address.
- **Who:** the account's address
- **Mailbox:** accounts@
- **Source:** `templates/account/email/email_confirmation_subject.txt`, `email_confirmation_message.html`, `email_confirmation_message.txt`
- **Message (HTML):**
```
Preview: Confirm your e-mail address for Metanoia Group.
[Heading] Confirm your e-mail
Hi,
Please confirm your e-mail address for your Metanoia Group account. Just tap the button below.
[BUTTON: CONFIRM MY EMAIL → {{ activate_url }}]
If you didn't deliberately request this, please ignore this e-mail — your address won't be confirmed and nothing will change.
This confirmation link will expire soon. Questions? info@metanoiagroup.co.za.
```
- **Message (plain text):**
```
Hi,

Please confirm your e-mail address for Metanoia Group by visiting:

{{ activate_url }}

If you didn't deliberately request this, please ignore this e-mail — your
address won't be confirmed and nothing will change.

--
{{ brand.copyright }}
```

### EM-ACC-EXISTS: someone signed up with your address

- **Channel:** e-mail (allauth, anti-enumeration)
- **Subject:** `You already have a Metanoia Group account`
- **When:** a sign-up uses an address that is already registered. The page looks the same either way; the owner gets this instead.
- **Who:** the existing account owner
- **Mailbox:** accounts@
- **Source:** `templates/account/email/account_already_exists_subject.txt`, `account_already_exists_message.html`, `account_already_exists_message.txt`
- **Message (HTML):**
```
Preview: Someone tried to sign up using your e-mail address.
[Heading] You already have an account
Hi,
Someone — possibly you — tried to create a Metanoia Group account using {{ email }}. An account with that address already exists, so we didn't create a second one.
If that was you and you've forgotten your password, you can set a new one:
[BUTTON: RESET MY PASSWORD → {{ password_reset_url }}]
Wasn't you? You can safely ignore this message — nothing has changed and your account is untouched.
Need help? info@metanoiagroup.co.za.
```
- **Message (plain text):**
```
Hi,

Someone — possibly you — tried to create a Metanoia Group account using {{ email }}.
An account with that address already exists, so we didn't create a second one.

If that was you and you've forgotten your password, set a new one here:

{{ password_reset_url }}

Wasn't you? You can safely ignore this e-mail — nothing has changed.

--
{{ brand.copyright }}
```

### EM-ACC-RESET: password reset link

- **Channel:** e-mail (allauth)
- **Subject:** `Reset your Metanoia Group password`
- **When:** "Forgot password" submitted for a registered address (`apps/myhub/views.py:483`)
- **Who:** the account owner
- **Mailbox:** accounts@
- **Source:** `templates/account/email/password_reset_key_subject.txt`, `password_reset_key_message.html`, `password_reset_key_message.txt`
- **Message (HTML):**
```
Preview: Reset your Metanoia Group password.
[Heading] Forgot your password?
Hi {{ first_name }},
We received a request to reset the password for your Metanoia Group account.
Let's get you a new one!
[BUTTON: RESET MY PASSWORD → {{ password_reset_url }}]
Didn't request a password reset? You can safely ignore this message — your password won't change.
This link will expire soon for your security. Need help? info@metanoiagroup.co.za.
```
- **Message (plain text):**
```
Hi {{ first_name }},

We received a request to reset the password for your Metanoia Group account.
Open this link to choose a new password:

{{ password_reset_url }}

If you didn't ask for this, you can ignore this e-mail — your password won't change.

--
{{ brand.copyright }}
```

### EM-ACC-UNKNOWN: reset requested for an unknown address

- **Channel:** e-mail (allauth, anti-enumeration)
- **Subject:** `Password reset attempted — Metanoia Group`
- **When:** "Forgot password" submitted for an address with no account
- **Who:** that address
- **Mailbox:** accounts@
- **Source:** `templates/account/email/unknown_account_subject.txt`, `unknown_account_message.html`, `unknown_account_message.txt`
- **Message (HTML):**
```
Preview: No Metanoia Group account uses this address.
[Heading] We couldn't find that account
Hi,
Someone — possibly you — asked to reset the password for {{ email }} at Metanoia Group. We don't have an account registered with that address, so there's no password to reset.
If that was you, you may have signed up with a different e-mail address — or you can create an account:
[BUTTON: CREATE AN ACCOUNT → {{ signup_url }}]
Wasn't you? You can safely ignore this message — no account exists and nothing has changed.
Need help? info@metanoiagroup.co.za.
```
- **Message (plain text):**
```
Hi,

Someone — possibly you — asked to reset the password for {{ email }} at Metanoia Group.
We don't have an account registered with that address, so there's no password to reset.

If that was you, you may have signed up with a different e-mail address — or you can
create an account here:

{{ signup_url }}

Wasn't you? You can safely ignore this e-mail — no account exists and nothing has changed.

--
{{ brand.copyright }}
```

### EM-ACC-PW-CHANGED: password changed confirmation

- **Channel:** e-mail
- **Subject:** `Your password was changed` (Python string, `apps/communication/emails.py:415`)
- **When:** right after a password is changed in settings, or set through a reset link (allauth `password_changed` / `password_reset` signals, `apps/accounts/signals.py:205-215`)
- **Who:** the account owner
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/password-changed.html` (no `.txt`, so the plain text includes raw CSS)
- **Message:**
```
[Heading] Your password was changed
Hi {{ first_name }},
This is a confirmation that the password for your Metanoia Group account was just changed.
If this was you, no action is needed. If it wasn't, reset your password immediately and contact us.
[BUTTON: SIGN IN → {{ SITE_URL }}/myhub/page-login/]
Didn't change your password? Your account may be at risk — contact us right away.
```

### EM-ACC-STAFF-INVITE: team account created

- **Channel:** e-mail
- **Subject:** `Your Metanoia Group account`
- **When:** a superuser creates an admin, staff or educator account (Team accounts form, `apps/accounts/views.py:367`)
- **Who:** the new team member
- **Mailbox:** info@ (uses the `general` template, which is not mapped to accounts@)
- **Source:**
  - Body text: Python, `apps/accounts/emails.py:11-40`
  - Layout: `templates/communication/email/general.html` (no `.txt`, so raw CSS in the plain text)
- **Message:**
```
[Heading] Your Metanoia Group account
Hi {{ first_name }},
An account has been created for you on Metanoia Group.

Log in with these details:
    E-mail: {{ user.email }}
    Temporary password: {{ password }}

On your first login, please:
  1. Set your own password — use "Forgot password?" on the login page.
  2. Complete your profile (a few personal details and consent).

Welcome to the team.
[BUTTON: Log in → {{ login_url }}]
```

### NT-WELCOME-1 … NT-WELCOME-4: the welcome pack (4 notifications)

- **Channel:** in-app notification. No e-mail copy (`email` not set). WhatsApp copy if on.
- **When:** once per user, the first time onboarding completes (guarded on verb `welcome`):
  - A student finishes registration step 3 (`apps/accounts/views.py:603`).
  - A student finishes with a one-on-one request only (`:521`).
  - An admin, staff member or educator completes their first-login profile (`:322`).
- **Who:** the new user
- **Source:** `apps/communication/welcome.py:35-70`
- **Messages:**

**NT-WELCOME-1**: title `Welcome to the Metanoia family, {{ first_name }}! 🌱` (link `/myhub/`)
```
You've joined Metanoia Group — a home built to help you grow through your learning, not just get through it. Every step of your programme is supported, and we're genuinely glad you're here. Let's make this year count.
```
**NT-WELCOME-2**: title `Quick guide — where to find everything` (link `/myhub/`)
```
A one-minute tour:
• Dashboard (home) — your feed and what's due next.
• My Modules — everything you're registered for; open a module for its Schedule.
• Schedule — the blueprint & study guide on top, then week 1–4, the mock exam and final details, each with its documents on the side.
• Messages & Notifications — stay in the loop with your coaches and cohort.
• Support (bottom of the sidebar) — how-to guides and answers to common questions.
```
**NT-WELCOME-3**: title `Your study timetable — what to look out for` (link `/calendar/`)
```
Each module runs to a calendar: four tests, coaching intensives, a mock assessment and the final examination. Open your module's Schedule to see the dates for your cohort, and watch Notifications — we'll remind you as each test and intensive approaches so nothing sneaks up on you.
```
**NT-WELCOME-4**: title `Who to talk to` (link `/communication/chat/`)
```
We're one message away:
• Academics — a topic, a mark, a lesson or the schedule — message your educator or the academic staff from Messages.
• Anything technical — logging in, payments, or the platform itself — contact an administrator.
Never stuck in silence: reach out early and often.
```

### NT-COMMUNITY-WELCOME

- **Channel:** in-app notification (no e-mail)
- **Title:** `Welcome to the Metanoia Community`
- **When:** right after someone joins the free community at `/join/`
- **Who:** the new community member
- **Source:** `apps/accounts/community_views.py:101`
- **Message:**
```
Say hello in the community, follow what is happening in your programme, and use Message Metanoia to talk to our team privately. When you are ready for modules, live classes and assessments, choose "Become a student".
```

---

## 3. Parents and invited educators

### EM-ACC-EDU-INVITE: invitation to teach

- **Channel:** e-mail
- **Subject:** `You have been invited to teach on Metanoia` (`apps/accounts/emails.py:76`)
- **When:**
  - Admin or staff use "Add educator" (`apps/accounts/views.py:866`).
  - Staff click **Resend** on Staff desk → Invitations (`apps/staffdesk/views_ops.py:479`).
- **Who:** the invited address
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/invite-educator.html` (no `.txt`)
- **Message:**
```
[Heading] You've been invited to teach
Hello,
You've been invited to teach {{ course.title | "a course" }} on Metanoia Group.
Click the button below to create your educator account and confirm the course.
[BUTTON: ACCEPT INVITE → {{ accept_url }}]
```
Note: the view passes `programme`, but the template reads `course`. In
practice it always says "a course".

### EM-REG-THANKYOU (parent / educator variant)

The same e-mail as [EM-REG-THANKYOU](#em-reg-thankyou-thank-you-for-registering)
below. It is sent when an invited parent (`apps/accounts/views.py:806`) or an
invited educator (`apps/accounts/views.py:842`) completes their one-page
registration. Parents get it with no programme; educators get their
programme. **No invoice e-mail follows**, but the e-mail's small print still
says one does (see §14).

The parent invitation template (`invite-parent.html`) is dormant; see §12.
Parents who sign up from a student's parent link get EM-ACC-VERIFY-SIGNUP.

---

## 4. Enrolment and billing

Registration sends **two e-mails**, plus a WhatsApp on the free-week path. The
single entry point is `deliver_registration`,
`apps/accounts/registration_billing.py:209`.

### EM-REG-THANKYOU: thank you for registering

- **Channel:** e-mail
- **Subject:** `Thank you for registering — your Metanoia Group summary` (`apps/communication/emails.py:364`)
- **When:**
  - A student clicks **Start free week / pay by EFT** at registration step 3 (`apps/accounts/views.py:640`, `paid=False`).
  - A registration invoice becomes fully paid by any method: PayFast, a staff-recorded EFT, or free (`apps/accounts/signals.py:119`, `paid=True`). A free-week student therefore gets this e-mail **a second time** when they pay (see §14).
  - An invited parent or educator finishes registration (§3).
- **Who:** the student, parent or educator
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/registration-thank-you.html` (no `.txt`)
- **Message:**
```
Preview: Thank you for registering for {{ programme }} — here's how to get started.
[Heading] Thank you for registering
Hi {{ first_name | "there" }},
You're registered, and we're glad you're here. Here's what you signed up for:
  Institution   {{ institution }}
  Programme     {{ programme }}
  Module(s)     {{ module names, one per line }}

Getting started — four steps
1. Sign in with the button below.
2. Finish your profile — it takes a minute, and it is what your coach sees.
3. Open My Modules and pick one, then open its Schedule — the blueprint and study guide sit at the top, then weeks 1–4, the mock exam and the finals.
4. Check Calendar for your live classes and coaching intensives. We'll remind you before each one.

Where to find things
Dashboard — your feed and what's due next.
Messages & Notifications — your coaches, your cohort, and every reminder.
Support (bottom of the sidebar) — how-to guides and answers to common questions.

Academic questions go to your educator or the academic staff from Messages; anything technical goes to an administrator. Reach out early and often — nobody here should be stuck in silence.

Understand it. Apply it. Pass it. Let's make this year count.
[BUTTON: GO TO YOUR DASHBOARD → {{ SITE_URL }}/myhub/]
Small print: Your invoice (and receipt, if paid) comes in a separate e-mail, with the PDF attached. Questions? info@metanoiagroup.co.za.
```

### EM-REG-DOC: the invoice or the receipt (registration)

- **Channel:** e-mail with a PDF attached (`invoice-{{ number }}.pdf` or `receipt-{{ number }}.pdf`)
- **Subject:**
  - Unpaid: `Your invoice — Metanoia Group`
  - Paid: `Your receipt — Metanoia Group`
  - Source: `apps/communication/emails.py:386-387`
- **When:** together with EM-REG-THANKYOU (student registration only)
- **Who:** the student
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/registration-document.html` (no `.txt`)
- **Message, unpaid (free-week) branch:**
```
Preview: Your invoice is attached — your free week has started.
[Heading] Your invoice
Hi {{ first_name | "there" }},
Your free week has started. The whole platform is open to you until {{ trial_ends, e.g. "Friday, 14 November 2026" }} — every module, every live class, every document, with nothing held back.
Your invoice is attached to this e-mail as a PDF. Settle it by EFT before {{ trial_ends, e.g. "14 November" }} and your modules stay open without interruption. If it is not settled by then, your modules lock until it is — nothing is lost, and everything reopens the moment payment reflects.
The banking details are on the attached PDF. You can also pay by card using the button below, which settles it immediately.
[BUTTON: PAY NOW BY CARD → {{ pay_url }}]
Small print: Keep the attached invoice for your records. Questions about this invoice? info@metanoiagroup.co.za.
```
- **Message, paid branch:**
```
Preview: Your receipt is attached.
[Heading] Your receipt
Hi {{ first_name | "there" }},
Thank you — your payment has gone through and every module you registered for is unlocked. Nothing else is needed from you.
Your receipt is attached to this e-mail as a PDF. Keep it for your records; the same document is always available in the platform under Finance.
Small print: Keep the attached receipt for your records. Questions about this receipt? info@metanoiagroup.co.za.
```

### WA-REG-INVOICE: the registration invoice on WhatsApp

- **Channel:** WhatsApp, with the PDF
- **When:** with EM-REG-DOC, unpaid branch only, if WhatsApp is configured and the student has a number
- **Who:** the student's WhatsApp number
- **Source:** `apps/accounts/registration_billing.py:84` (caption); template `metanoia_invoice` in `core/whatsapp_templates.py:51-59`
- **Message, inside the 24 h window** (the PDF with a caption):
```
Your Metanoia Group invoice {{ invoice.number }} — {{ amount, e.g. R1,250.00 }}. Your free week has started; pay by EFT before it ends and your modules stay open.
```
- **Message, outside the window** (Meta template `metanoia_invoice`, PDF as header, footer `Metanoia Group`):
```
Hi {{1 full name}}, thank you for registering with Metanoia Group. Your invoice {{2 invoice number}} for {{3 amount}} is attached.

Your free week has started. Pay by EFT with the invoice number as the reference before it ends and your modules stay open.
```

### EM-TRIAL-ENDING: free week ends tomorrow

- **Channel:** e-mail with the invoice PDF attached
- **Subject:** `Your free week ends tomorrow — invoice {{ invoice.number }} is still open` (`apps/communication/emails.py:405`)
- **When:** job `trial-ending-reminders` (hourly, main tick). It sends once per invoice, when **all** of these hold:
  - a trial enrolment ends within the next 24 h;
  - the invoice still has a balance;
  - no reminder has been sent for it before.
  - Source: `apps/accounts/trial_reminders.py:77`.
- **Who:** the student
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/trial-ending.html` (no `.txt`)
- **Message:**
```
Preview: Your free week ends tomorrow — settle invoice {{ invoice.number }} to keep your modules open.
[Heading] Your free week ends tomorrow
Hi {{ first_name | "there" }},
A quick heads-up: your seven-day access ends on {{ trial_ends, e.g. "Friday, 14 November" }}, and invoice {{ invoice.number }} is still open.
  Amount outstanding      R {{ invoice.balance }}
  Module(s) at risk       {{ module names }}
Settle it and nothing changes — your modules stay open and you carry on where you left off. Leave it, and they lock until the payment comes through; your progress is kept either way, so nothing is lost.
Pay by card using the button below, or by EFT using the banking details on the attached invoice. Already paid or arranged it with us? Then please ignore this — it can cross in the post with your payment.
[BUTTON: PAY MY INVOICE → {{ pay_url }}]
Small print: The invoice is attached, and it is always in your account under Finance. Need more time? Talk to us at info@metanoiagroup.co.za.
```

### NT-TRIAL-MARK: bell record of the trial reminder

- **Channel:** in-app notification. It is created directly, so it has no push, e-mail or WhatsApp copy. It **does** show in the student's bell.
- **Title:** `Free week ending — invoice reminder sent`
- **When:** right after EM-TRIAL-ENDING is sent. It is also the "already sent" marker for that reminder.
- **Source:** `apps/accounts/trial_reminders.py:44-52`
- **Message:** `Reminder e-mail sent for invoice {{ invoice.number }}.` (links to the pay page)

### EM-INVOICE: invoice with pay link

- **Channel:** e-mail
- **Subject:** `Invoice {{ invoice.number }} — payment requested` (`apps/communication/emails.py:277`)
- **When** (all staff- or student-initiated):
  - Staff click **Send** on an invoice (`apps/finance/views.py:595`, `apps/finance/views_books.py:120`).
  - Staff create invoices with "e-mail now" ticked (`apps/finance/views.py:638`).
  - A student unlocks a single module and chooses EFT (`apps/learning/views.py:246`).
- **Who:** the invoice customer
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/invoice.html` (no `.txt`)
- **Message:**
```
[Heading] Invoice {{ invoice.number }}
Hi {{ first_name }},
You have an invoice awaiting payment.
  Issued        {{ invoice.issue_date }}
  Due           {{ invoice.due_date }}        (if set)
  Amount due    R{{ invoice.balance }}
[BUTTON: PAY NOW → {{ pay_url }}]
```

### EM-INVOICE-REMINDER: unpaid invoice reminders (4 variants)

- **Channel:** e-mail
- **Subject** (`apps/communication/emails.py:282-293`):
  - `before_3`: `Reminder: invoice {{ number }} is due in 3 days`
  - `due`: `Invoice {{ number }} is due today`
  - `after_7`: `Invoice {{ number }} is overdue`
  - `manual`: `Reminder: invoice {{ number }}`
- **When:** job `finance-daily` (every 6 h on the main tick) → `run_reminders`, `apps/finance/documents.py:96`. Stages are 3 days before the due date, on it, and 7 days after.
  - Each stage is sent once, and only the latest stage that applies.
  - **Registration invoices are skipped**; EM-TRIAL-ENDING covers them.
  - The `manual` variant is sent when staff click **Send reminder** (`apps/finance/views_books.py:165`).
- **Who:** the invoice customer
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/invoice-reminder.html` (no `.txt`)
- **Message:**
```
[Heading] (after_7) Invoice {{ number }} is overdue | (due) Invoice {{ number }} is due today | (other) A friendly reminder
Hi {{ first_name }},
(after_7) Our records show invoice {{ number }} is still unpaid, {{ days_overdue }} days after it was due. If you've already paid, thank you — please reply with your proof of payment and we'll match it.
(due)     Invoice {{ number }} is due today.
(other)   Invoice {{ number }} is due on {{ due_date, e.g. "14 November 2026" }}.
  For             {{ invoice.summary }}   (if set)
  Due             {{ due_date }}
  Amount due      R{{ balance }}
  EFT reference   {{ payment_reference }}
[BUTTON: PAY NOW → {{ pay_url }}]
```

### EM-ESTIMATE

- **Channel:** e-mail
- **Subject:** `Estimate {{ estimate.number }} from us` (`apps/communication/emails.py:297`)
- **When:** staff click **Send** on an estimate (`apps/finance/views_books.py:125`)
- **Who:** the estimate customer
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/estimate.html` (no `.txt`)
- **Message:**
```
[Heading] Estimate {{ number }}
Hi {{ first_name }},
Here is the estimate you asked for: {{ summary }}. You can view it and accept it online.
  Total         R{{ total }}
  Valid until   {{ valid_until, e.g. "14 November 2026" }}
[BUTTON: VIEW ESTIMATE → {{ view_url }}]
```

### NT-ESTIMATE-RESPONSE (to staff)

- **Channel:** in-app notification (no e-mail)
- **Title:** `Estimate {{ number }} accepted` / `Estimate {{ number }} declined`
- **When:** a customer accepts or declines an estimate on its public page
- **Who:** all admin and staff accounts
- **Source:** `apps/finance/documents.py:169`
- **Message:** `{{ bill_to_name }} accepted|declined {{ number }} (R{{ total }}).`

### EM-PURCHASE: order confirmed

- **Channel:** e-mail
- **Subject:** `Order confirmed — {{ invoice.number }}` (`apps/communication/emails.py:303`)
- **When:** shop checkout placed with a balance still to pay, just before the buyer goes to PayFast (`apps/finance/views.py:332`)
- **Who:** the buyer
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/purchase.html` (no `.txt`)
- **Message:**
```
[Heading] Order confirmed
Hi {{ first_name }},
Thanks for your order. Here's what you purchased — invoice {{ invoice.number }}:
  Item | Qty | Amount   (one row per line)
  Total  R{{ invoice.total }}
[BUTTON: COMPLETE PAYMENT → {{ pay_url }}]   (only if a balance remains)
```

### EM-RECEIPT: payment received (non-registration invoices)

- **Channel:** e-mail with `Receipt-{{ number }}.pdf` attached
- **Subject:** `Receipt — Invoice {{ invoice.number }} paid` (`apps/communication/emails.py:322`)
- **When:** any invoice becomes fully paid (PayFast, staff-recorded payment, or a free order), **unless** it is a registration invoice (`apps/finance/services.py:122`)
- **Who:** the invoice customer
- **Mailbox:** accounts@
- **Source:** `templates/communication/email/payment-made.html` (no `.txt`)
- **Message:**
```
[Heading] Payment received ✓
Hi {{ first_name }},
Thank you — your payment for invoice {{ invoice.number }} has been received in full. This e-mail is your receipt.
  Item | Amount     ({{ description }} × {{ quantity }} | R{{ amount }})
  Total paid  R{{ invoice.total }}
```

---

## 5. Shop and 1-on-1 bookings

### EM-SHOP-DOWNLOADS: your downloads

- **Channel:** e-mail, **plain text only** (not branded). Files are attached, or linked if too large.
- **Subject:** `Your download — order {{ order.order_no }}`
- **When:** an order containing digital products is paid (`invoice_paid` signal, `apps/shop/signals.py:123`)
- **Who:** the buyer
- **Mailbox:** accounts@
- **Source:** Python string, `apps/shop/entitlements.py:110-122`
- **Message:**
```
Hello {{ buyer full name }},

Payment received for order {{ order_no }}. Here is what you bought:

• {{ product }} — {{ file label }} (attached)
• {{ product }} — {{ file label }}: {{ download link }}

You can also download these any time from Shop → My orders while you are signed in.

Metanoia Group        ← brand.invoice_name from .strings.json
```

### NT-SHOP-PAID (buyer)

- **Channel:** in-app (no e-mail)
- **Title:** `Payment received — order {{ order_no }}`
- **When:** shop order paid
- **Source:** `apps/shop/fulfilment.py:102`
- **Message:**
```
We received R{{ total }} for {{ up to 3 item names }}[ and N more].
[If digital:] Your downloads are ready on the order page.
```

### NT-SHOP-SALE (staff)

- **Channel:** in-app (no e-mail)
- **Title:** `New sale — R{{ total }}`
- **When:** shop order paid
- **Who:** all admin and staff accounts
- **Source:** `apps/shop/fulfilment.py:110`
- **Message:** `{{ buyer }} bought {{ items }} ({{ order_no }}).`

### NT-SHOP-SHIPPED

- **Channel:** in-app, **plus e-mail** if the buyer has e-mail copies on (default on)
- **Mailbox:** info@
- **Title:** `Shipped — order {{ order_no }}`
- **When:** staff book the courier, or record a manual dispatch
- **Source:** `apps/shop/fulfilment.py:173-184`
- **Message:**
```
Your parcel is on its way to your locker|your door. Tracking number: {{ tracking_reference }}.
```

### NT-SHOP-DELIVERED

- **Channel:** in-app (no e-mail)
- **Title:** `Delivered — order {{ order_no }}`
- **When:** job `shop-tracking` (every 2 h) sees the parcel delivered, or staff mark it delivered
- **Source:** `apps/shop/fulfilment.py:165`
- **Message:** `Your parcel has been delivered. Enjoy, and good luck with your studies!`

### NT-BOOKING-CONFIRMED (student) and NT-BOOKING-NEW (educator)

- **Channel:** in-app (no e-mail)
- **When:** a 1-on-1 booking is paid and confirmed
- **Source:** `apps/shop/booking.py:372` and `:376`
- **Messages:**
  - Student, title `Your 1-on-1 is booked`: `{{ booking.label }} with {{ educator }}. The join link is under Online sessions.`
  - Educator, title `New 1-on-1 booked`: `{{ student name }} booked {{ product name }} · {{ booking.label }}.`

### NT-BOOKING-NEWTIME

- **Channel:** in-app, **plus e-mail** (default on)
- **Mailbox:** info@
- **Title:** `Please choose a new time`
- **When:** payment arrives after the held slot was taken by someone else. The money becomes account credit.
- **Source:** `apps/shop/booking.py:403`
- **Message:**
```
Your payment for {{ product name }} arrived after {{ booking.label }} was booked by someone else. R{{ price }} is on your account as credit — book any free time and it will be used automatically.
```

### NT-BOOKING-MOVED

- **Channel:** in-app (no e-mail)
- **Title:** `1-on-1 moved`
- **When:** a booking is rescheduled
- **Who:** both the student and the educator
- **Source:** `apps/shop/booking.py:448`
- **Message:** `{{ old label }} → {{ new label }}.`

### NT-BOOKING-CANCELLED

- **Channel:** in-app, **plus e-mail** (default on)
- **Mailbox:** info@
- **Title:** `1-on-1 cancelled`
- **When:** staff or the educator cancel a booking. NT-SESSION-CANCELLED is also sent for its session.
- **Source:** `apps/shop/booking.py:482-490`
- **Message:**
```
{{ booking.label }} with {{ educator }} has been cancelled.[ R{{ price }} has been added to your account credit for your next purchase.][ Reason: {{ reason }}]
```

---

## 6. Live sessions

Times in session messages are written in **each recipient's own time zone**,
with the zone abbreviation, e.g. `Tuesday 11 November, 18:00 (SAST)`. The
format is set in `local_when`, `apps/livesessions/reminders.py:180`.

### NT-SESSION-NEW: new session scheduled

- **Channel:** in-app (no e-mail; the e-mail is EM-SESSION-INVITE)
- **Title:** `New session: {{ meeting.title }}`
- **When:** a session is created in Live sessions (`apps/livesessions/services.py:74-77`), or a manager clicks re-share (`apps/livesessions/views.py:392`). Not sent for 1-on-1 bookings.
- **Who:** the session's audience
- **Source:** `apps/livesessions/reminders.py:114-119`
- **Message:** `{{ session kind }} on {{ when }}. {{ audience label }}.`

### EM-SESSION-INVITE: live session scheduled

- **Channel:** e-mail
- **Subject:** `Live session scheduled — {{ meeting.title }}` (`apps/livesessions/reminders.py:136`)
- **When:**
  - On creation: only to people who switched on *Reminders by e-mail* (off by default).
  - On a manager's re-share: to everyone in the audience, unless the manager unticks e-mail.
- **Who:** the session's audience, one e-mail each (in their own time zone)
- **Mailbox:** learninghub@
- **Source:** `templates/communication/email/session-invite.html` + `session-invite.txt`
- **Message (HTML):**
```
[Heading] {{ meeting.title }}
Hi {{ first_name }},
A {{ session kind, lower case }} has been scheduled for {{ when_local }}, running about {{ duration_minutes }} minutes.
That is the time in your own zone ({{ timezone_label }}). If it looks wrong, update your time zone under Settings and every session time will follow it.
  For            {{ audience_label }}
  Module         {{ module }}            (if set)
  Covers         {{ week }}              (if set)
  Prepares for   {{ calendar event }}    (if set)
{{ meeting.description }}
(if recorded) This session is recorded automatically. If you cannot make it, the recording, transcript and summary appear on the session page afterwards.
[BUTTON: OPEN THE SESSION → {{ session_url }}]
```
- **Message (plain text):** the same content; see `session-invite.txt`.

### MS-OUTLOOK-INVITE: Microsoft calendar invite

- **Channel:** Outlook / Teams calendar invitation. Microsoft sends it, not the platform.
- **Subject:** `{{ meeting.title }}`
- **When:** a Teams session is created and "create calendar events" is on
- **Who:** the organiser, plus invited participants who have a Teams licence (**never students**)
- **Source:** `apps/livesessions/services.py:191` (`ensure_calendar_event`); body built by `_event_body`, `:434`
- **Message:**
```
{{ meeting.description }}
Audience: {{ audience_label }}
Open this session on Metanoia Group   (link to the session)
```

### NT-SESSION-REMINDER: 24 h and 30 min reminders

- **Channel:** in-app. **E-mail** only to people who switched on *Reminders by e-mail* (and only while the live-session setting "Also send reminders by e-mail" is on).
- **Mailbox:** learninghub@
- **Title:** `{{ headline }}: {{ meeting.title }}`. With the default leads (`1440,30`) the headline is `Session in 1 day` or `Session in 30 minutes`. Other leads give `Session in N hours/days/minutes` (`_headline`, `apps/livesessions/reminders.py:194`).
- **When:** job `session-reminders` on the quick tick (every 15 min, 06:00–22:45 SAST). It sends once per person per lead, as soon as the session is within that lead. Leads are set in Live-session settings.
- **Who:** the session's audience
- **Source:** `apps/livesessions/reminders.py:75-82`
- **Message:**
```
"{{ meeting.title }}" starts at {{ when }}. {{ audience label }}. Join from the session page.
```

### NT-SESSION-CANCELLED

- **Channel:** in-app, **plus e-mail** (default on, because a person cancelled it)
- **Mailbox:** learninghub@
- **Title:** `Cancelled: {{ meeting.title }}`
- **When:** a manager cancels a session (`apps/livesessions/views.py:498`), or a 1-on-1 booking is cancelled
- **Who:** the session's audience
- **Source:** `apps/livesessions/services.py:133-140`
- **Message:** `"{{ meeting.title }}" has been cancelled.[ {{ reason }}]`

### NT-SESSION-RECORDING: recording available

- **Channel:** in-app only (no e-mail by design; NT-SESSION-PACK carries the links)
- **Title:**
  - `Recording and transcript available — {{ meeting.title }}`
  - `Recording available — {{ meeting.title }}` when there is no transcript
- **When:** session pipeline finds the recording: the recordings tick (every 5 minutes, classes that ended in the last 36 hours) or, for older classes, the sessions tick (06:00 and 22:00). Only for sessions held in the last 7 days.
- **Who:** the class's students, plus the host
- **Source:** `apps/livesessions/pipeline.py:661-692`
- **Message:**
```
The recording[ and the transcript] of this session are on its page. You can watch it in full whenever suits you; the summary, next agenda and action items follow shortly.
```

### The session summary: how it is made, and its layout

Every summary a class receives (NT-SESSION-SUMMARY-READY, NT-SESSION-PACK, the
summary lesson on the module, the session page and the `-summary.md` in OneDrive)
has the same sections, in this order (`apps/livesessions/brief.py`,
`to_markdown` :94). A section with nothing in it is left out, never invented.

```
OVERVIEW
{{ a brief description of what was discussed, said and stated in the session }}

KEY HIGHLIGHTS
• {{ highlight }} …

AGENDA FOR THE NEXT SESSION
1. {{ item }} …

ACTION ITEMS — STUDENTS
• Must do: {{ task }} ({{ named student, when Copilot names one }}) …
• Should do: … / Will do: …

ACTION ITEMS — EDUCATOR
• Will do: {{ task }} ({{ educator }}) …

ACTION ITEMS — EVERYONE
• Should do: {{ task }} …
```

**Where it comes from.** The summary **waits for Teams Copilot**: after a class
ends, the job asks Graph for Copilot's meeting insight
(`/copilot/users/{id}/onlineMeetings/{id}/aiInsights`) and, until it is there,
writes nothing — it asks again on later runs (every 30 minutes on the
recordings tick, plus the 06:00/22:00 sessions tick) for up to **48 hours after
the class ended** (`COPILOT_WAIT_HOURS`, `apps/livesessions/summaries.py`). The
session page shows "Waiting for the Teams Copilot summary (until …)" on the
Processing card meanwhile. The recording is published and announced
(NT-SESSION-RECORDING) straight away; it does not wait.

- **Copilot insight found:**
  - with the admin AI configured (`ANTHROPIC_API_KEY` + `ADMIN_AI_ENABLED`), Claude
    is given Copilot's notes and action items plus the transcript and fills the
    sections above (prompt: `brief.PROMPT` :309, headings fixed in code);
  - without it (production today), the sections are mapped from Copilot directly
    (`brief.from_copilot` :147): the overview from Copilot's note titles and the
    first sentence of its first notes; key highlights from the note titles and
    their sub-points; action items from Copilot's action items, grouped by owner
    (the host/educator → *educator*; "class"/"students" or a named student →
    *students*, with the name; no owner/"everyone" → *everyone*), each labelled
    *Must do* / *Should do* / *Will do* from its wording; the next agenda from
    sentences that mention the next session/week/class (not "before the next
    session" deadlines).
- **No insight 48 hours after the class** (or Graph refuses it with 403): the
  summary is written from the transcript instead — by Claude when configured,
  otherwise extracted from it — in the same layout. If Copilot's insight turns
  up later (within 7 days) it replaces that summary in place, without a second
  message.

### NT-SESSION-SUMMARY-READY

- **Channel:** in-app. **E-mail** only to people with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **When:** the first time a session summary is published — by the session
  pipeline once Copilot's insight is in (or the 48-hour wait is over), or by job
  `session-summaries` / its daily backfill. Not sent when the AI follow-up is on
  (NT-SESSION-PACK is the one message then).
- **Who:** the class's students, plus the host (the educator has action items too)
- **Source:** `apps/livesessions/summaries.py:416` (`announce`)
- **Messages:**
  - Normal: title `Session summary ready — {{ meeting.title }}`
  - Two or more days late: title `Now available: summary of {{ meeting.title }} ({{ date, e.g. 03 Nov }})`
  - Body:
    ```
    The summary of the live session is ready: overview, key highlights, the next agenda and action items.
      (late: The summary of the live session on {{ e.g. Monday 03 November }} is now on your module.)

    {{ the summary, in the layout above }}

    Watch or download the recording: {{ link }}
    Read the transcript: {{ link }}
    ```

### NT-SESSION-PACK: summary, next agenda and action items

- **Channel:** in-app. **E-mail** only to people with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **When:** job `session-ai-followup` (sessions tick) after a class, once per session,
  for sessions in the last 7 days. Only with the AI follow-up switched on and the
  admin AI configured. It waits for the session summary (so for Copilot) first.
- **Who:** the class's students, plus the host
- **Source:**
  - Titles: `apps/livesessions/ai_followup.py:517` and `:525`
  - Body skeleton: `student_markdown`, `:399`
  - Fallback text: `SUMMARY_PENDING`, `:444`
- **Messages:**
  - With an AI pack: title `Session summary, next agenda & action items — {{ meeting.title }}`. The body is mostly AI-written; the fixed parts are:
    ```
    {{ meeting.title }}
    {{ module }} · {{ cohort }} · {{ e.g. Monday 03 November 2026, 18:00 }} SAST

    OVERVIEW / KEY HIGHLIGHTS / AGENDA FOR THE NEXT SESSION /
    ACTION ITEMS — STUDENTS / ACTION ITEMS — EDUCATOR
    {{ the layout above; each action "Must do: / Should do: / Will do: {{ task }} ({{ due }})" }}

    RECORDING AND TRANSCRIPT
    Watch the recording: {{ link }}
    Read the transcript: {{ link }}
    ```
    The attendance roster, the long staff write-up and any concerns about
    candidates stay in the staff pack.
  - Without a pack: title `Session material — {{ meeting.title }}`. The body is the published summary plus the two links above.
  - Both links are platform pages (the session page's player and the transcript page). No message to students or parents carries a download link or a YouTube / OneDrive address: recordings are watched on the platform, and only staff and educators can download them (`audience.can_download_recording`).
  - If the AI failed, its error text is replaced by: `The written summary of this session is not ready yet. The recording and the transcript are on the session page.`

### NT-SESSION-ATTENDANCE: your 1-on-1 record

- **Channel:** in-app. **E-mail** only with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **Title:** `Your session record — {{ meeting.title }}`
- **When:** the session pipeline processes a finished **one-on-one** session
- **Who:** the student in the 1-on-1
- **Source:** `apps/livesessions/pipeline.py:403-411`
- **Message:**
```
You attended {{ minutes }} minute(s) of "{{ meeting.title }}" ({{ status }}). The recording and notes are on the session page.
```

### NT-CALENDAR-DISCONNECTED

- **Channel:** in-app. **E-mail** only with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **Title:** `Calendar disconnected — {{ subscription.name }}`
- **When:** job `calendar-subscriptions` (every 30 min) fails too many times in a row on a connected Google, Outlook or Apple calendar, and switches it off
- **Who:** the person who connected it
- **Source:** `apps/livesessions/subscriptions.py:107-115`
- **Message:**
```
Metanoia could not read "{{ name }}" after several attempts ({{ last_error }}). Its events have stopped updating. Reconnect it under Settings → Calendar with a fresh link.
```

### NT-MEETING-INVITE: invited to a meeting (API)

- **Channel:** in-app, **plus e-mail** if the meeting has a start time and the person has e-mail copies on
- **Mailbox:** info@
- **Title:** `Meeting: {{ meeting.title }}`
- **When:** someone adds people to a meeting through the meetings API invite action
- **Source:** `apps/communication/api.py:287-294`
- **Message:** `{{ meeting.description }}` (links to the join page)

### EM-SESSION-SCHEDULED: copy to the host

- **Channel:** e-mail
- **Subject:** `Session scheduled: {{ meeting.title }}` (`apps/communication/emails.py:269`)
- **When:** a meeting is created from Communication → Meetings → New meeting (`apps/communication/views.py:302`)
- **Who:** **the person who created it** (not the attendees)
- **Mailbox:** learninghub@
- **Source:** `templates/communication/email/session-scheduled.html` (no `.txt`)
- **Message:**
```
[Heading] {{ session_title }}
Hi {{ first_name }},
A class / live session has been scheduled for {{ session_when }}.
[BUTTON: JOIN THE SESSION → {{ join_url }}]
```

---

## 7. Learning and assessments

### Automatic "new on Metanoia" notices (NT-AUTO-*)

These are queued when something is saved, then sent by job `auto-notices` (quick
tick, every 15 min 06:00–22:45). Nobody hears about their own action.

- **Channel:** in-app. **E-mail** only to people with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **Source:**
  - Queueing and wording: `apps/communication/auto.py`
  - Triggers: `apps/communication/auto_signals.py`

| ID | Title | Body | Trigger | Who | Source |
|---|---|---|---|---|---|
| NT-AUTO-MATERIAL | `New {{ kind, lower }}: {{ material.title }}` (e.g. "New blueprint: …") | `{{ material.description }}` or `A new {{ kind }} is available on {{ module }}.` | Module material published (or its "available from" time arrives) | Students and educators on the module | `auto.py:64-77` |
| NT-AUTO-LESSON | `New lesson: {{ lesson.title }}` | `{{ lesson.subtitle }}` or `About {{ estimated_minutes }} minutes of study.` | Lesson published, or its scheduled publish time arrives | The lesson's target audience | `auto.py:98-116` |
| NT-AUTO-ASSESSMENT-OPEN | `{{ assessment.title }} is open` | `A new {{ kind }} on {{ module }} is ready for you.[ It closes {{ e.g. Fri 14 Nov at 17:00 }}.]` | Assessment set to open or scheduled (sent at "available from") | Students on the module | `auto.py:119-135` |
| NT-AUTO-SOLUTIONS | `Solutions released: {{ assessment.title }}` | `The memo is open. Compare it with your answers while the paper is fresh.` | Assessment closes with "release solution on close" | Students who attempted it | `auto.py:138-151` |
| NT-AUTO-RESULT | `Your {{ assessment.title }} has been marked — {{ pct }}%` | `Open it to see your marks and the feedback on each answer.` | Coach saves the last hand-marked answer (`apps/assessments/authoring.py:311`) | The student | `auto.py:154-164` |
| NT-AUTO-VIDEO | `New video: {{ video.title }}` | `{{ video.description }}` | New (≤ 3 days old) video in a YouTube playlist mapped to a module, cohort, programme or institution | Students and educators in that scope | `auto.py:187-216` |
| NT-AUTO-FOLDED | `{{ N }} new items in {{ module }}` | One line per item: `• {{ item title }}` | Two or more material, lesson or video notices for the same module and audience due in the same run | As above | `auto.py:282-289`, `:329-338` |

### NT-DEADLINE-ASSESSMENT: closing soon

- **Channel:** in-app. **E-mail** only to people with *Reminders by e-mail* on.
- **Mailbox:** learninghub@
- **When:** job `deadline-reminders` (nominally every 30 min; runs hourly on the main tick). The notice is created once per item, when the deadline is within the person's **reminder lead** (Settings, default 60 min).
- **Who:** students enrolled on the module
- **Source:** `apps/communication/services.py` `scan_deadline_reminders` (assessments)
- **Messages:**
  - Assessment, title `Closing soon: {{ assessment.title }}`: `"{{ assessment.title }}" closes {{ e.g. Nov 14, 17:00 }}.`

### NT-WEEKLY-STUDENT and NT-WEEKLY-EDUCATOR: Monday summary

- **Channel:** in-app. **E-mail** only to people with *Updates by e-mail* on.
- **Mailbox:** learninghub@
- **Title:** `Your week ahead` (students), `Your teaching week ahead` (educators)
- **When:** job `weekly-summary`. It acts on Monday from 06:00 SAST, which is the 06:05 tick in practice. Once per person per ISO week, and skipped if the person has nothing on.
- **Who:** active students and educators
- **Source:**
  - Lines: `apps/communication/weekly.py:31-107`
  - Send: `:140-142`
- **Message** (one line per item):
  - Student lines:
    ```
    ⏳ {{ days }} days to {{ exam title }}[ (date provisional)]     (if within 120 days)
    ✅ {{ assessment title }} is open[ — closes {{ when }}]
    🗓 {{ assessment title }} opens {{ when }}
    🎥 {{ session title }} — {{ when }}
    📚 {{ N }} new item(s) of material arrived last week
    ```
  - Educator lines:
    ```
    🖊 {{ N }} attempt(s) waiting to be marked
    🎥 {{ session title }} — {{ when }}
    ```

### NT-REENGAGE: where you left off

- **Channel:** in-app. **E-mail** only to people with *Reminders by e-mail* on.
- **Mailbox:** learninghub@
- **Title:** `It's been {{ days }} days. Here's where you left off`
- **When:** job `re-engagement` (every 24 h). It picks students who have not signed in for 7+ days, still have something on, and have not been nudged in the last 14 days.
- **Who:** students
- **Source:** `apps/communication/weekly.py:188-190`
- **Message:** the same lines as the student weekly summary.

### NT-NUDGE: module misses you (educator button)

- **Channel:** in-app, **plus e-mail** (default on, because the coach sent it)
- **Mailbox:** learninghub@
- **Title:** `{{ module name }} misses you`
- **When:** an educator clicks **Nudge** on the teaching dashboard (at most once per module per 24 h)
- **Who:** students on that module inactive for 7+ days
- **Source:** `apps/staffdesk/dashboards.py:594-599`
- **Message:**
```
It's been a little while since you studied {{ module name }}. Pick up where you left off — even 20 minutes today keeps you on track. — {{ coach name }}
```

### NT-ATRISK-STUDENT and NT-ATRISK-EDUCATOR (staff action, pre-filled text)

The `risk-scan` job (daily) only flags students; it sends nothing. Staff then
send these from Staff desk → At-risk. The text below is the **pre-filled
default**, which staff can edit before sending.

- **Channel:** in-app, **plus e-mail** (default on)
- **Mailbox:**
  - Student message: learninghub@ (category grades)
  - Educator message: info@
- **Source:**
  - Defaults: `apps/staffdesk/views_academic.py:534-545`
  - Send: `:605`, `:616`
- **Messages:**
  - Student, title `Checking in on your progress`:
    ```
    Hi {{ student name }}, we noticed you might be falling behind[ in {{ module }}]. If anything is getting in the way, reply or book a session — we are here to help.
    ```
  - Educator, title `{{ student name }} may need support`:
    ```
    {{ student name }} has been flagged as at risk[ in {{ module }}] ({{ reasons }}). Could you check in with them?
    ```

---

## 8. Community and messaging

| ID | Title | Body | When | Who | E-mail | Source |
|---|---|---|---|---|---|---|
| NT-CONNECT-REQUEST | `New connection request` | `{{ name }} would like to message you. Approve the request to start chatting.` | A student asks to message another student | Recipient | No | `apps/communication/services.py:295-302` |
| NT-CONNECT-ACCEPTED | `Connection accepted` | `{{ name }} accepted your request — you can now message each other.` | Request accepted | The requester | No | `services.py:323-330` |
| NT-CHAT-MENTION | `{{ sender }} mentioned you in {{ group }}` | First 300 chars of the message | @mention in a chat group | The person tagged | No | `services.py:504-512` |
| NT-COMMUNITY-MENTION | `{{ author }} mentioned you in {{ community name }}` | First 280 chars of the post or comment | @tag in a community post or comment | The person tagged | No | `apps/communication/community.py:235-238` |
| NT-STUDYGROUP-ADDED | `You were added to {{ workspace }}` | `{{ name }} added you to the study group {{ workspace }}.` | Added to a study group | New members | No | `apps/communication/views.py:1183-1186` |
| NT-APPEAL-DECIDED | `Your appeal was approved` / `Your appeal was not upheld` | `The {{ penalty kind }} has been lifted.` / `The {{ penalty kind }} stays in place.` + staff note | Staff decide a moderation appeal | The user | Yes (default on), info@ | `apps/staffdesk/views_ops.py:358-364` |

### RT-CHAT-MESSAGE: new chat message pop-up

- **Channel:** real-time bell sound and pop-up only (not stored, no e-mail or WhatsApp)
- **Title:** `{{ sender name }}` (or `New message`)
- **When:** any chat message
- **Who:** every other member of the conversation
- **Source:** `apps/communication/realtime.py:84`
- **Message:** first 80 characters of the message, or `Sent an attachment`.

The chat moderation ladder (warn → mute → suspend) records penalties
silently; it sends **no** message to the user (`apps/communication/services.py:592`).

---

## 9. Staff and admin notices

| ID | Title | Body | When | Who | Source |
|---|---|---|---|---|---|
| NT-ONE-ON-ONE-REQUEST | `New one-on-one request` | `{{ student }} · {{ institution }} · {{ programme }}` | Student submits a one-on-one coaching request at registration | All admin and staff | `apps/accounts/views.py:540-543` |
| NT-ONE-ON-ONE-SETUP | `Your one-on-one coaching is set up` | `{{ educator }} will coach you for {{ programme }}.` | Staff mark the request "enrolled" (sent to the **student**) | Student | `apps/staffdesk/views_ops.py:617-620` |
| NT-MOD-REPORTED | `A community post was reported` | First 200 chars of the post | First report on a post | All admin and staff | `apps/communication/community.py:287` |
| NT-MOD-HIDDEN | `A community post was hidden after {{ N }} reports` | First 200 chars of the post | 3rd open report; the post is auto-hidden | All admin and staff | `community.py:285` |
| NT-MAIL-NEW (1–3 mails) | `New email from {{ sender }}` | `{{ subject }} — {{ snippet }}` | Mail sync (every 2 min) finds new inbox mail, if "notify new mail" is on for the mailbox. Also shows a desktop alert. | Mailbox owner | `apps/mailbox/sync.py:253-256` |
| NT-MAIL-NEW (4+ mails) | `{{ N }} new emails in {{ address }}` | `Latest: {{ subject }} — from {{ sender }}` | Same | Mailbox owner | `sync.py:259-262` |
| NT-SHOP-SALE, NT-ESTIMATE-RESPONSE | see §4 and §5 | | | | |

None of these is e-mailed.

### RT-WHATSAPP-STAFF

- **Channel:** real-time pop-up only
- **Title:** `WhatsApp · {{ contact name or number }}`
- **When:** an inbound WhatsApp needs a person: free text, a file, AGENT, or an unknown number
- **Who:** every admin and staff member
- **Source:** `apps/communication/whatsapp_inbox.py:225-243`
- **Message:** first 80 characters of the message, or the message kind.

---

## 10. Broadcasts (announcements)

Staff write the words; the platform adds the wrapper text below.

### NT-BROADCAST and EM-ANNOUNCEMENT

- **Channel:** in-app for every recipient, **plus e-mail** if "also send by e-mail" is ticked and the person has e-mail copies on. Important broadcasts skip the "announcements" opt-out.
- **When:**
  - Immediately on **Send**.
  - At the scheduled time, via job `send-broadcasts` (quick tick, every 15 min 06:00–22:45). A broadcast scheduled overnight goes out at 06:00.
- **Who:** the audience chosen in the composer
- **Mailbox:** info@. It is signed by the sender personally and has **no** unsubscribe footer.
- **Source:**
  - Send: `apps/communication/broadcast.py:191`
  - E-mail: `apps/communication/emails.py:469`
  - Templates: `templates/communication/email/announcement.html` + `announcement.txt`
- **E-mail subject:** `{{ announcement.title }}`
- **E-mail wrapper text:**
```
[Heading] {{ announcement.title }}
Hi {{ first_name }},
[Important · ]Announcement from {{ sender first name }}
{{ announcement.body }}
▶ Watch the video                          (if a video link is set)
🔗 {{ url_label | "Open the link" }}        (if a link is set)
{{ N }} file(s) attached | {{ N }} file(s) — open the notification to view or download
  📎 {{ file name }} ({{ size }}) …
[BUTTON: OPEN IN METANOIA → their notification]
```

---

## 11. WhatsApp

WhatsApp only sends when it is configured. People must opt in to WhatsApp
copies (off by default) and have a number. Outside the 24-hour window after the
person last wrote, only Meta-approved **templates** can be sent. All templates
are defined in `core/whatsapp_templates.py`; after rewording a template it must
be re-approved by Meta (`python manage.py whatsapp_templates --submit`, or in
WhatsApp Manager). Every template has the footer `Metanoia Group`.

### WA-OTP: verification code

- **Template:** `metanoia_verification_code` (AUTHENTICATION category)
- **When:** a person clicks "send code" next to their phone number at registration or in their profile (`apps/accounts/phone_verify.py:56`). At most once a minute, and limited per hour.
- **Message:** Meta's fixed authentication wording. It says the code is a verification code and not to share it. Footer: "This code expires in 10 minutes." Button: **Copy code**.
- **Rewordable?** Only the expiry minutes and the security line can be configured (`core/whatsapp_templates.py:32-41`); Meta fixes the rest.
- **On-screen replies** (`phone_verify.py`):
  - `We sent a 6-digit code to {{ number }} on WhatsApp.`
  - `A code was just sent. You can ask for another in {{ N }} seconds.`
  - `Too many codes were requested. Please try again in an hour.`
  - `We could not send a WhatsApp message to that number. Check it is your WhatsApp number, with the right country code.`

### WA-NOTIFY-COPY: WhatsApp copy of any notification

- **When:** every in-app notification created via `notify()` (all NT-* above except NT-TRIAL-MARK), if the person has WhatsApp on, the category is allowed, and it is outside quiet hours
- **Source:** `apps/communication/whatsapp_inbox.py:357-394`
- **Inside the 24 h window** (free text):
```
For *{{ full name }}*          (only when several accounts share the number)

*{{ notification title }}*

{{ notification body, first 600 chars }}

{{ link }}
```
- **Outside the window:** the template is chosen by category (`BY_CATEGORY`, `core/whatsapp_templates.py:100`). Blanks: {{1}} full name, {{2}} title, {{3}} body, {{4}} link.

| Template | Used for categories | Body |
|---|---|---|
| `metanoia_update` | none / mentions / messages (anything not listed) | `Hi {{1}}, there is an update on your Metanoia Group account.`<br>`*{{2}}*` `{{3}}`<br>`View it here: {{4}}`<br>`Reply MENU for quick answers.` |
| `metanoia_deadline` | deadlines, assessments | `Hi {{1}}, a reminder from Metanoia Group.`<br>`*{{2}}*` `{{3}}`<br>`Details: {{4}}`<br>`Reply DATES to see your next dates.` |
| `metanoia_session` | meetings | `Hi {{1}}, a class or session on your Metanoia Group timetable.`<br>`*{{2}}*` `{{3}}`<br>`Join or view it here: {{4}}`<br>`See you there.` |
| `metanoia_result` | grades | `Hi {{1}}, a result is ready on your Metanoia Group account.`<br>`*{{2}}*` `{{3}}`<br>`See it here: {{4}}`<br>`Keep going.` |
| `metanoia_notice` | announcements, content, weekly | `Hi {{1}}, a new notice for your Metanoia Group modules.`<br>`*{{2}}*` `{{3}}`<br>`Read it in full: {{4}}`<br>`Reply MENU for quick answers.` |

### WA-NEW-MESSAGE: "the team has replied"

- **Template:** `metanoia_new_message`, with a quick-reply button **Show message**
- **When:** staff reply in the WhatsApp inbox more than 24 h after the person last wrote. Sent once per silence; the staff reply is queued until the person taps the button.
- **Source:**
  - Template: `core/whatsapp_templates.py:42-50`
  - Send: `apps/communication/whatsapp_inbox.py:311`
- **Message:**
```
Hi {{1 name}}, the Metanoia Group team has replied to you about your account. Tap *Show message* below to read it here.
```
- Staff replies themselves are signed automatically with a closing line: `_{{ staff first name }}, Metanoia Group_` (`_signed`, `whatsapp_inbox.py:257`).

### WA-BOT-*: automatic replies to inbound WhatsApp

- **Source:** all in `apps/communication/whatsapp_bot.py`
- **When:** someone messages the WhatsApp number. Commands are matched on the whole message, or on the first word of a message of up to 3 words.

| ID | Trigger | Reply | Source |
|---|---|---|---|
| WA-BOT-HELP | `help`, `menu`, `hi`, `hello`, `?` … (also the default for unrecognised short commands) | `Hi {{ first_name }}!` + blank line, then:<br>`*Metanoia Group*`<br>`Reply with one of these:`<br>`*WEEK* — your week ahead: what is due, open and on`<br>`*DATES* — your next tests and exams`<br>`*MODULES* — your modules and what is open`<br>`*INVOICE* — what is outstanding, and the PDF again`<br>`*NOTICES* — your unread notifications`<br>`*AGENT* — talk to a person on the Metanoia team`<br>`*STOP* — no more WhatsApp messages`<br>`Or just type your question and the team will reply here.`<br>On a shared number it adds `*SWITCH* — answer for another account on this number` | `HELP` :75, `SWITCH_LINE` :88 |
| WA-BOT-UNKNOWN | Number not linked to any account | `Hi! Thanks for messaging *Metanoia Group*. A member of our team will reply to you here soon.`<br>`Already registered? Verify this number on your profile and you can also use quick answers like *WEEK* and *DATES*.` | `WELCOME_UNKNOWN` :90 |
| WA-BOT-PASSED-ON | Free-text question | `Thanks{{, first_name}}! We have passed your message to the Metanoia team and someone will reply to you here.`<br>`Reply *MENU* any time for quick answers.` | `PASSED_ON` :97 |
| WA-BOT-FILE | Photo, voice note or file | `Thanks{{, first_name}}, we have received your file. A member of the Metanoia team will look at it and reply here.` | `FILE_RECEIVED` :102 |
| WA-BOT-AGENT | `agent`, `human`, `staff` … | `No problem{{, first_name}}. A member of the Metanoia team will reply to you here as soon as they can. You can carry on typing in the meantime.` | `AGENT` :107 |
| WA-BOT-CHOOSE | Shared number and a personal command, or `switch` | `This number is linked to more than one Metanoia Group account. Who is asking?`<br>`*1* — {{ name }}` …<br>`Reply with the number next to the name.` | `_choice_prompt` :148 |
| WA-BOT-CHOSEN | Reply to the above with a number | `Thanks. Answering for *{{ name }}* now.` + the HELP menu + SWITCH line | :370 |
| WA-BOT-DATES | `dates`, `next`, `exam` … | `*Your next dates*` then `• {{ e.g. Mon 3 Nov }} — {{ module code }} · {{ event }} (today / tomorrow / in N days)`<br>No programme: `You are not registered for a programme yet, so there are no dates on your calendar. Finish registering on the platform and they will appear here.`<br>Nothing upcoming: `Nothing is on your calendar yet. Your institution's dates go up as they are confirmed.` | `_dates` :161 |
| WA-BOT-MODULES | `modules`, `courses` | `*Your modules*` then `• {{ code }} — open` / `free week, N day(s) left` / `locked — payment outstanding`<br>None: `You are not registered for any modules yet.` | `_modules` :189 |
| WA-BOT-INVOICE | `invoice`, `pay`, `balance` … | `*Invoice {{ number }}*`<br>`Total: R{{ total }}`<br>`Outstanding: R{{ balance }}`<br>`Sending the PDF now. Pay by EFT using the invoice number as your reference, or pay online from *Finance* on the platform.`<br>Then the PDF, captioned `Invoice {{ number }} — R{{ total }}`.<br>Nothing owed: `You have nothing outstanding — everything is paid up. 👍` | `_invoice` :212 |
| WA-BOT-NOTICES | `notices`, `unread` … | `*Unread notifications*` then `• {{ e.g. 3 Nov }} — {{ title }}`, then `Read them in full on the platform.`<br>None: `Nothing unread. 🎉` | `_notices` :250 |
| WA-BOT-WEEK | `week`, `due`, `plan` … | `*Your week ahead*` + the weekly-summary lines.<br>Nothing due: `Nothing is due in the next seven days. A good week to get ahead.` | `_week` :266 |
| WA-BOT-STOP / START | `stop`, `mute`, `unsubscribe` / `start`, `resume` | STOP: `Done — no more WhatsApp notifications from us. You will still see everything on the platform and by e-mail.`<br>`Reply *START* if you change your mind.`<br>START: `WhatsApp notifications are back on. Reply *STOP* at any time to turn them off again.`<br>Unknown number, STOP: `Done. We will not message this number again.`<br>Unknown number, START: `Thanks. Register on the platform and verify this number to get updates here.` | `_set_notifications` :280, `route` :359 |
| WA-BOT-ERROR | Internal error | `Something went wrong on our side looking that up. Please try again, or check the platform.` | :312 |

On a shared number, personal answers are prefixed `For *{{ name }}*`.

---

## 12. Dormant templates

These templates and functions exist but **nothing calls them today**. Reword
them only if they are going to be used.

| Template / function | Subject | Note |
|---|---|---|
| `communication/email/invite-parent.html` via `apps/accounts/emails.py:66` | `You have been invited to join as a parent / guardian / sponsor` | No code creates a parent `Invitation`, so it is never sent. Body: "Hello, {{ student }} has invited you to join Metanoia Group as their parent / guardian / sponsor. Click the button below to create your account and complete a short registration — it only takes a minute." [ACCEPT INVITE] |
| `communication/email/event-upcoming.html`, `send_event_reminder` (`emails.py:261`) | `Upcoming: {{ event_title }}` | No callers. Body: "This is a reminder about an upcoming event." When / Where table, [VIEW ON CALENDAR]. |
| `communication/email/welcome-class.html`, `send_welcome_to_class` (`emails.py:327`) | `Welcome to your course` | No callers. Body: "Welcome aboard! … You've been enrolled. Here's where you'll be learning: … Your lessons, assessments, group chat and calendar are ready in your dashboard." [GO TO MY DASHBOARD] |
| `email_announcement` (`emails.py:419`) | `{{ announcement.title }}` | Old broadcast path; replaced by `email_announcement_copy`. |
| `invoice_for_modules(send_to_student=True)` (`apps/learning/enrolment.py:119`) | EM-INVOICE | Every caller passes `False`. |
| `core.whatsapp.send_text` legacy catch-all template (`WHATSAPP_TEMPLATE_NAME` setting) | | Only used if that setting is set. It replaces free text with one generic template. |

---

## 13. Important on-screen (flash) messages

These are the user-facing confirmations shown after automatic events (mostly
sign-up, registration and payment). Generic "saved", "deleted" and form-error
messages are left out. Strings marked *catalog* are read from
`.strings.json` → `messages`; edit them there.

| Where / when | Message | Source |
|---|---|---|
| Sign-in (after any successful login) | `Welcome, {{ name }}.` | catalog `messages.welcome`; `apps/accounts/adapters.py:67` |
| Sign-in with unverified e-mail | `Please verify your e-mail before signing in. We've sent a fresh link to {{ email }}.` | catalog `messages.verify_email_required`; `apps/myhub/views.py:300` |
| Sign-in failure | `Invalid e-mail or password.` | catalog `messages.invalid_credentials` |
| Forgot password submitted | `If that e-mail exists, a reset link has been sent.` | catalog `messages.reset_link_sent`; `apps/myhub/views.py:485` |
| Sign-out | `You have been signed out.` | catalog `messages.signed_out` |
| Sign-up done | Page "Check your e-mail" (`templates/myhub/pages/page-register-done.html`) | `apps/myhub/views.py:433` |
| Team member finishes first-login profile | `Welcome aboard — your profile is set up.` | `apps/accounts/views.py:325` |
| Registration, free week chosen | `Your free week has started — everything is open until {{ e.g. Friday, 14 November }}. Your invoice has been sent to {{ e-mail and WhatsApp }}; pay it by EFT before the week is out and your modules stay open.` | `apps/accounts/views.py:643-647` |
| …if a channel failed | `We could not reach you on {{ channel }} — the invoice is in your account under Finance either way.` | `apps/accounts/views.py:649-651` |
| Registration, everything free | `You are enrolled — enjoy your modules!` | `apps/accounts/views.py:621` |
| Registration hand-off screen (paid) | **Payment completed** — `Thank you — your payment came through and every module you registered for is unlocked.` / `A receipt and your registration summary are on their way to your inbox.` | `REGISTRATION_OUTCOMES`, `apps/accounts/views.py:671` |
| Registration hand-off screen (EFT / free week) | **Registration completed** — `Setting up your account — your free week starts now and the whole platform is open.` / `Your invoice and registration summary are on their way to your inbox. Pay it by EFT before the week is out and your modules stay open.` | same, `:686` |
| Registration hand-off screen (1-on-1) | **Request received** — `Thank you — your one-on-one request is with our team. We will match you with an educator and set up your enrolment.` / `We will be in touch by e-mail or WhatsApp to confirm your coach and your first session.` | same, `:678` |
| One-on-one request (already onboarded) | `Thank you — your one-on-one request is with our team.` | `apps/accounts/views.py:525` |
| Invited parent or educator registration complete | `Registration complete — welcome aboard!` | `apps/accounts/views.py:809`, `:845` |
| Parent opens a student's parent link | `You are now linked as a guardian of {{ student }}.` / `Account created — please verify your e-mail, then sign in.` | `apps/accounts/views.py:964`, `:966` |
| Enrolment confirmed after paying a pending registration | `Enrolment confirmed — welcome aboard!` | `apps/accounts/views.py:891`, `:907` |
| Community join | `Welcome to the Metanoia Community, {{ first_name }}!` | `apps/accounts/community_views.py:63` |
| Become a student | `We have sent a verification link to {{ email }}.` / `First confirm your e-mail address with the link we send you.` / `Your e-mail is verified and your password is set. …` | `community_views.py:138`, `:151`, `:159` |
| Unlock a module by EFT | `Invoice e-mailed. Pay by EFT and the module unlocks the moment it clears.` | `apps/learning/views.py:250` |
| Shop checkout fully paid by credit | `Order placed and paid. Your items are ready.` | `apps/finance/views.py:330` |
| Staff sends invoice | `Pay-link e-mailed to {{ email }}.` / `Could not e-mail the invoice (no address or mail error).` | `apps/finance/views.py:597-599` |
| Staff creates a team account | (success message naming the role) | `apps/accounts/views.py:369` |

---

## 14. Things noticed while cataloguing

These are observations only; no behaviour was changed. They are worth deciding
on while rewording.

1. **Raw CSS in plain-text e-mails.** See §0. Fifteen templates have no `.txt`
   twin, so their plain-text part is `strip_tags(html)`, which keeps the CSS
   from the `<style>` block of `base_email.html`.
2. **Free-week students get the thank-you twice.** EM-REG-THANKYOU (+ invoice)
   goes out at registration. When the invoice is later paid by any method, the
   `invoice_paid` receiver sends EM-REG-THANKYOU again, with the receipt
   (`apps/accounts/signals.py:70-98`). Its docstring says "paid by card", but
   the code does not check the payment method.
3. **Card payers who never complete PayFast get no registration e-mail.** The
   "pay now" path sends nothing until the payment clears, although the free
   week starts anyway.
4. **Parents and educators are told an invoice is coming.**
   EM-REG-THANKYOU's small print always says "Your invoice comes in a separate
   e-mail…" and its "four steps" mention My Modules. Neither applies to
   parents, and no invoice follows.
5. **The welcome pack speaks to students only, but staff get it too.** It
   mentions "becoming a CA(SA)" and the study timetable, yet it is also sent to
   admins, staff and educators when they finish their first-login profile.
6. **EM-SESSION-SCHEDULED goes to the host, not the attendees**
   (`apps/communication/views.py:302`).
7. **EM-ACC-EDU-INVITE always says "a course".** The template reads `course`,
   but the code passes `programme`.
8. **Invoices and receipts carry an "Unsubscribe" button.** The footer text
   right under it says those e-mails are always sent. The footer is added to
   any automated e-mail to a known account.
9. **The staff account e-mail goes from info@ and contains a temporary
   password.** It uses the generic `general` template, which is not mapped to
   accounts@.
10. **Nothing runs overnight.** The scheduler timers stop between about 23:00
    and 06:00 SAST. A 30-minute reminder for an early-morning session, or a
    broadcast scheduled overnight, therefore goes out from 06:00.

**Triggers that could not be fully determined from the code**

- NT-MEETING-INVITE: it is raised by the meetings API `invite` action
  (`apps/communication/api.py`). No template or JS caller of that endpoint was
  confirmed, so whether any screen uses it today is unclear.
- EM-ACC-VERIFY: it also covers allauth's own "add / re-send e-mail" flows, if
  allauth's e-mail management page is reachable. No hub screen linking to it
  was confirmed.
- WA-OTP: the template body is fixed by Meta and is not in the repository, so
  the wording above describes Meta's standard authentication message rather
  than quoting it.

---

## 15. How to update a message

**1. E-mail written in a template** (most e-mails)

Edit the file under `metanoia/templates/`. Change only the wording
inside the blocks:

- `{% block preheader %}` — inbox preview
- `{% block heading %}` — big title
- `{% block content %}` — body
- `{% block cta %}` — button: change `label="…"`
- `{% block disclaimer %}` and `{% block expiry_note %}` — small print

Keep every `{{ … }}` and `{% … %}`. Keep the inline `style="…"` attributes as
they are, because e-mail clients need them.

**Both versions.** Where a `.txt` twin exists (`announcement`, `notification`,
`session-invite`, and every `templates/account/email/*_message`), update the
`.html` **and** the `.txt`: the plain-text part is sent alongside the HTML.

**Templates with no `.txt` twin.** Their plain text is generated from the HTML
and currently includes raw CSS. When rewording, consider adding a
`communication/email/<name>.txt` next to the `.html`.
`send_branded_email` picks it up automatically, with no code change. Copy the
structure of `notification.txt`.

**2. E-mail subjects**

- Platform e-mails: the subject is a Python string in
  `apps/communication/emails.py`, in the `send_*` function named in each
  entry. Exceptions:
  - `apps/accounts/emails.py`: staff invite, educator invite
  - `apps/livesessions/reminders.py:136`: session invite
- allauth e-mails: the subject is its own template, `templates/account/email/*_subject.txt`.
  Keep it on one line.

**3. In-app notification titles and bodies**

These are Python strings at the file:line given, inside a `notify(...)` call:
`title='…'` and `body='…'`. In `f'…'` strings the blanks are written
`{name}`. Keep the `f` prefix and the braces.

The same text is used for:

- the bell;
- the e-mail copy, through the `notification.html` / `.txt` wrapper;
- the WhatsApp copy.

So one edit updates all three. The "Hi {{ name }}," greeting and the OPEN
button are added by the wrapper.

**4. Scheduled and automatic notices**

Same as 3; the strings live in these files:

- `apps/communication/auto.py`: new material, lessons, tests, results, videos
- `apps/communication/weekly.py`: weekly summary, re-engagement
- `apps/communication/services.py`: deadlines, connections, mentions
- `apps/livesessions/*.py`: session reminders, summaries, recordings
- `apps/accounts/trial_reminders.py`
- `apps/finance/documents.py`

Timing is set in `apps/scheduler/jobs.py` and the timers in
`deploy/scheduler/production/`.

**5. WhatsApp**

- Notification templates and the invoice and "new message" templates are in
  `core/whatsapp_templates.py`. After any change:
  1. Run `python manage.py whatsapp_templates --json` to check it.
  2. Submit it for Meta approval with `--submit`, or in WhatsApp Manager. The
     document-header `metanoia_invoice` template must be created in WhatsApp
     Manager.
  3. Until Meta approves, the old approved wording keeps being used.
- Free-text pieces (the in-window notification format, the invoice caption, bot
  replies) are plain Python strings:
  - `apps/communication/whatsapp_inbox.py`
  - `apps/accounts/registration_billing.py:84`
  - `apps/communication/whatsapp_bot.py` (constants `HELP`, `WELCOME_UNKNOWN`,
    `PASSED_ON`, `FILE_RECEIVED`, `AGENT` at the top, then the `_dates` /
    `_modules` / … functions)
- WhatsApp formatting: `*bold*`, `_italic_`.

**6. Brand wording used everywhere**

The brand name, support e-mail, WhatsApp number and copyright line come from
`metanoia/.strings.json`:

- `brand` section;
- `messages` section, for the catalog flash messages in §13.

Signatures and role lines are in `apps/communication/senders.py`
(`SIGNATURES`, `PERSONAL_ROLES`). The footer, unsubscribe text and banner are
in `templates/communication/email/base_email.html`.

**7. After editing**

Send yourself a test of each changed e-mail and check both the HTML and the
plain-text view. In Gmail use "Show original"; Outlook shows the plain text
when HTML is blocked. Run the test suite: some tests assert on subjects, for
example `apps/accounts/test_registration_emails.py`.
