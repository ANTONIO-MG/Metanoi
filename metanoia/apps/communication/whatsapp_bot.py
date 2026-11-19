"""The WhatsApp bot — what happens when a student messages us back.

A deliberately small, closed set of commands, not a conversational assistant.

======================  =====================================================
``help`` / ``menu``     everything below, which is also the reply to anything
                        it does not recognise
``dates`` / ``next``    the next assessment dates from their institution's
                        academic calendar
``modules``             their modules and whether each one is open
``invoice`` / ``pay``   what is outstanding, and the PDF sent through again
``notices``             their unread notifications
``agent`` / ``human``   hand the conversation to the staff inbox
``switch``              on a shared number, choose which account to answer for
``stop`` / ``mute``     turn WhatsApp notifications off for this number
``start``               turn them back on
======================  =====================================================

Anything else (free text, media) opens the conversation in the staff inbox and
the bot stays quiet on it until staff close it (commands still work).

* **Identify first, answer second.** A reply only ever contains data of an
  account on that number. With several accounts (parent and child) the bot asks
  which one, remembers it, and labels every answer. Unknown numbers go to staff.
* **``stop`` always works**, before anything else, with no account needed.
* **Never raise into the webhook.** Meta re-delivers anything that is not
  answered with 200, so an exception here would become an infinite retry loop.
  Failures are logged and turned into an apology.

Module constants:

``COMMANDS``
    Aliases → command. Kept generous: people type what they think of, and
    every one of these has been someone's first guess.
``PERSONAL``
    Commands that answer with one account's data, so need to know whose.
"""
import logging

from django.utils import timezone

from core import whatsapp

logger = logging.getLogger(__name__)

COMMANDS = {
    'help': 'help', 'menu': 'help', 'hi': 'help', 'hello': 'help', 'start help': 'help',
    '?': 'help', 'options': 'help', 'commands': 'help',
    'dates': 'dates', 'date': 'dates', 'next': 'dates', 'test': 'dates', 'tests': 'dates',
    'exam': 'dates', 'exams': 'dates', 'calendar': 'dates', 'when': 'dates',
    'modules': 'modules', 'module': 'modules', 'modules': 'modules', 'courses': 'modules',
    'invoice': 'invoice', 'invoices': 'invoice', 'pay': 'invoice', 'payment': 'invoice',
    'balance': 'invoice', 'fees': 'invoice', 'account': 'invoice',
    'notices': 'notices', 'notifications': 'notices', 'unread': 'notices', 'news': 'notices',
    'week': 'week', 'plan': 'week', 'due': 'week', 'todo': 'week', 'this week': 'week',
    'agent': 'agent', 'human': 'agent', 'person': 'agent', 'staff': 'agent', 'team': 'agent',
    'talk': 'agent', 'consultant': 'agent', 'call me': 'agent', 'speak to someone': 'agent',
    'switch': 'switch', 'who': 'switch', 'change': 'switch', 'switch account': 'switch',
    'show message': 'show', 'show': 'show',
    'stop': 'stop', 'mute': 'stop', 'unsubscribe': 'stop', 'off': 'stop',
    'start': 'start', 'resume': 'start', 'subscribe': 'start', 'on': 'start',
}

PERSONAL = ('dates', 'modules', 'invoice', 'notices', 'week')

HELP = (
    '*Metanoia Group*\n'
    'Reply with one of these:\n\n'
    '*WEEK* — your week ahead: what is due, open and on\n'
    '*DATES* — your next tests and exams\n'
    '*MODULES* — your modules and what is open\n'
    '*INVOICE* — what is outstanding, and the PDF again\n'
    '*NOTICES* — your unread notifications\n'
    '*AGENT* — talk to a person on the Metanoia team\n'
    '*STOP* — no more WhatsApp messages\n\n'
    'Or just type your question and the team will reply here.'
)

SWITCH_LINE = '\n*SWITCH* — answer for another account on this number'

WELCOME_UNKNOWN = (
    'Hi! Thanks for messaging *Metanoia Group*. A member of our team will reply '
    'to you here soon.\n\n'
    'Already registered? Verify this number on your profile and you can also use '
    'quick answers like *WEEK* and *DATES*.'
)

PASSED_ON = (
    'Thanks{name}! We have passed your message to the Metanoia team and someone will '
    'reply to you here.\n\nReply *MENU* any time for quick answers.'
)

FILE_RECEIVED = (
    'Thanks{name}, we have received your file. A member of the Metanoia team will look '
    'at it and reply here.'
)

AGENT = (
    'No problem{name}. A member of the Metanoia team will reply to you here as soon as '
    'they can. You can carry on typing in the meantime.'
)


def parse(text):
    """The command in a message, or ``''``.

    Matches the whole message first, then — for short messages only — its first
    word, so "dates please" and "DATES" land on the same place but "hi, I paid
    yesterday and my module is still locked" goes to a person, not the menu.
    """
    cleaned = (text or '').strip().lower().strip('.!?')
    if not cleaned:
        return ''
    if cleaned in COMMANDS:
        return COMMANDS[cleaned]
    words = cleaned.split()
    return COMMANDS.get(words[0].strip(',.!?'), '') if len(words) <= 3 else ''


def person_for_number(number):
    """``(person, error)`` for a number with exactly one account on it.

    Kept for callers that need a single answer; the bot itself works with
    :func:`~apps.communication.whatsapp_inbox.people_for_number`.
    """
    from .whatsapp_inbox import people_for_number

    people = people_for_number(number)
    if len(people) == 1:
        return people[0], ''
    return None, ('unknown' if not people else 'shared')


def _first(person):
    """Return ``', <first name>'`` for a greeting, or ``''`` when there is no name."""
    name = (getattr(person, 'first_name', '') or '').strip()
    return f', {name}' if name else ''


def _choice_prompt(people):
    """Return the numbered "Who is asking?" list for a number shared by several accounts."""
    from .whatsapp_inbox import name_of

    lines = ['This number is linked to more than one Metanoia Group account. '
             'Who is asking?\n']
    lines += [f'*{i}* — {name_of(p)}' for i, p in enumerate(people, start=1)]
    lines.append('\nReply with the number next to the name.')
    return '\n'.join(lines)


def _dates(person):
    """The next few dates from the calendars of the institutions they study at.

    Only their own programme and intake are listed, as the web calendar shows it.
    """
    from apps.learning.models import CalendarEvent

    institution_ids = set(person.programme_enrolments.values_list(
        'programme__institution_id', flat=True))
    if not institution_ids:
        return ('You are not registered for a programme yet, so there are no dates on your '
                'calendar. Finish registering on the platform and they will appear here.')

    from django.db.models import Q

    from apps.livesessions.audience import intake_scope

    events = (CalendarEvent.objects
              .filter(is_published=True, calendar__institution_id__in=institution_ids,
                      start__gte=timezone.now())
              .filter(Q(programme__isnull=True)
                      | Q(programme__in=person.programme_enrolments.values('programme')))
              .filter(intake_scope(person))
              .select_related('programme_module', 'programme', 'calendar__institution')
              .order_by('start')[:6])
    if not events:
        return 'Nothing is on your calendar yet. Your institution\'s dates go up as they are confirmed.'

    lines = ['*Your next dates*']
    for event in events:
        when = timezone.localtime(event.start).strftime('%a %-d %b')
        days = event.days_away
        countdown = 'today' if days == 0 else ('tomorrow' if days == 1 else f'in {days} days')
        what = event.programme_module.code + ' · ' if event.programme_module_id else ''
        lines.append(f'• {when} — {what}{event.title} ({countdown})')
    return '\n'.join(lines)


def _modules(person):
    """Their modules, and whether each one is open."""
    enrolments = list(person.module_enrolments.select_related(
        'programme_module__programme__institution'))
    if not enrolments:
        return 'You are not registered for any modules yet.'

    lines = ['*Your modules*']
    for enrolment in enrolments:
        module = enrolment.programme_module
        if enrolment.is_unlocked:
            if enrolment.status == enrolment.STATUS_TRIAL and enrolment.trial_ends_at:
                days = (timezone.localtime(enrolment.trial_ends_at).date()
                        - timezone.localdate()).days
                state = f'free week, {max(days, 0)} day(s) left'
            else:
                state = 'open'
        else:
            state = 'locked — payment outstanding'
        lines.append(f'• {module.code} — {state}')
    return '\n'.join(lines)


def _invoice(person):
    """What is outstanding, with the PDF sent through again.

    The PDF is best effort: if it cannot be produced or sent, they still get the
    numbers, which is the part they actually asked for.
    """
    from apps.finance.models import Invoice

    invoice = (Invoice.objects.filter(customer=person.user)
               .exclude(status=Invoice.STATUS_PAID)
               .order_by('-created_at').first())
    if invoice is None:
        return 'You have nothing outstanding — everything is paid up. 👍'

    reply = (f'*Invoice {invoice.number}*\n'
             f'Total: R{invoice.total:,.2f}\n'
             f'Outstanding: R{invoice.balance:,.2f}\n\n'
             'Sending the PDF now. Pay by EFT using the invoice number as your reference, '
             'or pay online from *Finance* on the platform.')

    try:
        from apps.finance.pdf import render_invoice_pdf
        pdf = render_invoice_pdf(invoice)
        if hasattr(pdf, 'read'):
            pdf = pdf.read()
        from django.core.files.base import ContentFile

        from .whatsapp_inbox import log_outbound
        number = whatsapp.number_for(person)
        caption = f'Invoice {invoice.number} — R{invoice.total:,.2f}'
        sent, reason, wamid = whatsapp.deliver_media(number, pdf, f'{invoice.number}.pdf',
                                                     caption=caption)
        log_outbound(number, caption, kind='document', person=person, sent=sent, reason=reason,
                     wamid=wamid, command='invoice', media=ContentFile(pdf),
                     mime='application/pdf', filename=f'{invoice.number}.pdf')
    except Exception:  # pragma: no cover - network / rendering
        logger.exception('whatsapp bot: could not send invoice PDF for %s', invoice.number)
    return reply


def _notices(person):
    """Their unread notifications, newest first."""
    from .models import Notification

    unread = (Notification.objects.filter(recipient=person.user, is_read=False)
              .order_by('-created_at')[:5])
    if not unread:
        return 'Nothing unread. 🎉'
    lines = ['*Unread notifications*']
    for note in unread:
        when = timezone.localtime(note.created_at).strftime('%-d %b')
        lines.append(f'• {when} — {(note.title or note.verb or "Notification")[:80]}')
    lines.append('\nRead them in full on the platform.')
    return '\n'.join(lines)


def _week(person):
    """The same list the Monday summary sends, on demand."""
    from .weekly import educator_lines, student_lines

    user = getattr(person, 'user', None)
    if user is None:
        return 'We could not find your account details.'
    now = timezone.now()
    lines = (educator_lines if person.user_type == 'educator' else student_lines)(user, now)
    if not lines:
        return 'Nothing is due in the next seven days. A good week to get ahead.'
    return '*Your week ahead*\n\n' + '\n'.join(lines)


def _set_notifications(person, on):
    """Turn WhatsApp notification copies on or off for this person."""
    from .models import NotificationPreference

    pref = NotificationPreference.for_user(person.user)
    pref.whatsapp_enabled = on
    pref.save(update_fields=['whatsapp_enabled', 'updated_at'])
    if on:
        return ('WhatsApp notifications are back on. Reply *STOP* at any time to turn '
                'them off again.')
    return ('Done — no more WhatsApp notifications from us. You will still see everything '
            'on the platform and by e-mail.\n\nReply *START* if you change your mind.')


def _answer(command, person):
    """The reply to a command for one known account."""
    try:
        if command == 'dates':
            return _dates(person)
        if command == 'modules':
            return _modules(person)
        if command == 'invoice':
            return _invoice(person)
        if command == 'notices':
            return _notices(person)
        if command == 'week':
            return _week(person)
    except Exception:  # pragma: no cover - defensive
        logger.exception('whatsapp bot: %r failed for person %s', command, person.pk)
        return ('Something went wrong on our side looking that up. Please try again, '
                'or check the platform.')
    return ''


def reply_to(number, text, *, person=None):
    """The answer for one account, without any conversation state.

    Returns ``(reply_text, command)``. Used directly when the account is already
    known; :func:`handle_inbound` is the full flow.
    """
    command = parse(text)
    if person is None:
        person, _error = person_for_number(number)
        if person is None:
            return WELCOME_UNKNOWN, command or 'unknown-number'
    if command == 'stop':
        return _set_notifications(person, False), command
    if command == 'start':
        return _set_notifications(person, True), command
    if command in PERSONAL:
        return _answer(command, person), command
    greeting = f'Hi {person.first_name}!\n\n' if getattr(person, 'first_name', '') else ''
    return greeting + HELP, command or 'help'


def route(thread, text, *, kind='text'):
    """Decide what to do with one inbound message on a conversation.

    Returns ``(reply, command, needs_staff)``. ``reply`` may be empty: on a
    conversation staff are handling, free text gets no automatic answer.

    The order matters:

    1. ``stop`` / ``start`` come first, and apply to everyone on the number: it
       is the phone that wants to be left alone.
    2. A digit while the bot is waiting is the answer to "who is asking?".
    3. ``show`` gets no reply: the queued staff reply is the answer.
    4. Media, ``agent`` and unknown numbers go to staff; personal commands on a
       shared number without a chosen account ask who is asking.
    5. Their own words are for a person to answer: an open conversation gets no
       automatic reply, a closed one is reopened for staff.
    """
    from .models import WhatsAppThread
    from .whatsapp_inbox import name_of, open_for_staff, people_for_number

    command = parse(text)
    people = people_for_number(thread.number)
    shared = len(people) > 1
    person = (thread.active_person if thread.active_person in people
              else (people[0] if len(people) == 1 else None))

    if command in ('stop', 'start'):
        for each in people:
            reply = _set_notifications(each, command == 'start')
        if not people:
            reply = ('Done. We will not message this number again.' if command == 'stop'
                     else 'Thanks. Register on the platform and verify this number to get updates here.')
        return reply, command, False

    if thread.awaiting_choice and shared and (text or '').strip().isdigit():
        index = int(text.strip()) - 1
        if 0 <= index < len(people):
            thread.active_person = people[index]
            thread.awaiting_choice = False
            thread.save(update_fields=['active_person', 'awaiting_choice'])
            return (f'Thanks. Answering for *{name_of(people[index])}* now.\n\n' + HELP + SWITCH_LINE,
                    'choose', False)
        return _choice_prompt(people), 'choose', False

    if command == 'show':
        return '', command, False

    if kind != 'text' and not (text or '').strip():
        opened = open_for_staff(thread)
        return (FILE_RECEIVED.format(name=_first(person)) if opened else ''), 'file', True

    if command == 'agent':
        open_for_staff(thread)
        return AGENT.format(name=_first(person)), command, True

    if not people:
        opened = open_for_staff(thread)
        return (WELCOME_UNKNOWN if opened else ''), command or 'unknown-number', True

    if command == 'switch' or (command in PERSONAL and person is None):
        if not shared:
            return HELP, command, False
        thread.awaiting_choice = True
        thread.save(update_fields=['awaiting_choice'])
        return _choice_prompt(people), 'switch', False

    if command in PERSONAL:
        answer = _answer(command, person)
        return (f'For *{name_of(person)}*\n\n{answer}' if shared else answer), command, False

    if command == 'help':
        greeting = f'Hi {person.first_name}!\n\n' if person and person.first_name else 'Hi!\n\n'
        return greeting + HELP + (SWITCH_LINE if shared else ''), command, False

    if thread.status == WhatsAppThread.OPEN:
        return '', '', True
    open_for_staff(thread)
    return PASSED_ON.format(name=_first(person)), '', True


def handle_inbound(number, text, *, wa_id='', payload=None, kind='text', media_id='',
                   mime='', filename='', contact_name=''):
    """Record an inbound message, deliver any queued staff replies, answer it.

    Idempotent on ``wa_id``: Meta re-delivers a webhook until it gets a 200, and
    a student should not be answered three times because our first 200 was slow.
    Returns the bot's stored reply, or ``None`` when there was nothing to say or
    the message had already been handled.

    Their message re-opened the 24-hour window, so anything staff wrote while it
    was shut is delivered first, before the bot says anything.
    """
    from . import whatsapp_inbox as inbox
    from .models import WhatsAppMessage

    message, thread = inbox.record_inbound(
        number, text=text, wa_id=wa_id, payload=payload, kind=kind, media_id=media_id,
        mime=mime, filename=filename, contact_name=contact_name)
    if message is None:
        return None

    inbox.flush_queued(thread)

    reply, command, needs_staff = route(thread, text, kind=kind)
    if command and message.command != command[:30]:
        message.command = command[:30]
        message.save(update_fields=['command'])
    if needs_staff:
        inbox.alert_staff(thread, message)
    if not reply:
        return None

    sent, reason, wamid = whatsapp.deliver_free_text(thread.number, reply)
    if not sent:
        logger.warning('whatsapp bot: reply to %s not delivered (%s)', thread.number, reason)
    return inbox.log_outbound(thread.number, reply, kind=WhatsAppMessage.KIND_TEXT,
                              person=message.person, sent=sent, reason=reason, wamid=wamid,
                              thread=thread, command=(command or '')[:30])
