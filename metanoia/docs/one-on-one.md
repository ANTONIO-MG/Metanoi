# One-on-one track

Decided by the owner on 2026-11-10. Code: `apps/shop/one_on_one.py`.

## Who and what

- A student is on **one** track for everything (`Person.learning_track`): group
  classes (per module per month) or one-on-one. Staff switch a student on
  *One-on-one students → student → Move to …*.
- One-on-one costs **R300 per module per hour** (the price of catalogue item
  `SVC-1ON1-HOUR`, else R300). Hours are bought per module; a session is for
  one module and uses its hours.
- One-on-one modules are **always open**: schedule (every week), documents,
  mocks, and recordings of the student's **own** sessions. Group classes and
  class recordings are hidden.
- Feed: a private chat per student (the student, their module educators and
  the Metanoia team) plus the general Metanoia Community. They are left out of the
  cohort module chats and the programme community.

## Registration

Step 2 → *One-on-one coaching* → the same institution / programme / cohort /
module picker, with hours per module (0 = buy later; compulsory-module rules
do not apply). Step 3 → pay now or e-mail the EFT invoice. The modules open at
once; the hours arrive when the invoice is paid. Institution or programme not
listed → the old free-text request (*Staff desk → 1-on-1 requests*).

## Booking and scheduling

- **Student**: *My one-on-one* → Buy hours → once paid, Book → the educator's
  week grid (`manage.py ensure_one_on_one_rates` sets up the default educator's rate and hours).
- **Staff / educator**: *One-on-one students* → student → Schedule a session.
  Uses the student's hours if they have enough; otherwise books **on account**
  and e-mails an invoice. The student can still attend; the invoice is sent
  again after the session.
- **Account must be settled**: while a session or overtime invoice is unpaid,
  nobody can book or schedule the next session. An admin can override.
- The general *New session* form refuses one-on-one students as invitees.

## After the session (scheduler, every 5 minutes)

- An unpaid on-account invoice is e-mailed again.
- **Overtime**: once Teams reports attendance (we wait up to 48 h), if the call
  ran more than 30 minutes past the booked length (1 h → past 1 h 30), an extra
  **R100** invoice is sent. Staff can also add it by hand.

## Cancelling

Hours never expire. Student cancels **24 h or more** before the start → the hour
comes back (an on-account invoice is cancelled). Later, or a no-show → the hour
is used / the invoice stands. Staff or the educator cancelling → always back.
