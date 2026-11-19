"""The WhatsApp message templates the platform uses, as data.

Outside the 24-hour window Meta only delivers *templates*: messages written in
advance, approved by Meta, with ``{{1}}``-style blanks filled in at send time.
Everything the LMS starts a conversation with is one of these, so the whole set
lives here — one place to read what students can receive, and the source that
``manage.py whatsapp_templates --submit`` sends to Meta for approval.

Meta's rules the wording below already follows:

* a body may not start or end with a blank, so each one closes with fixed text;
* every blank needs an example, given in ``example``;
* *authentication* templates have fixed wording chosen by Meta — only the
  security line, the expiry and the copy-code button are configurable.

Each notification template takes the same four blanks — whose account it is
about (full name), the headline, the detail, and a link — so a shared number
always shows which person a message is for. :func:`for_notification` picks the
template for a notification category (see ``NotificationPreference.CATEGORIES``).

``BY_CATEGORY`` maps a notification category to a template name; anything
missing uses :data:`UPDATE`.
"""

OTP = 'metanoia_verification_code'
NEW_MESSAGE = 'metanoia_new_message'
UPDATE = 'metanoia_update'
INVOICE = 'metanoia_invoice'

_FOUR = ['Thabo Mokoena', 'FAC1501 Test 2 is on Friday',
         'Venue: Hall B, 09:00. Bring your student card.',
         'https://learn.metanoiagroup.co.za/myhub/']

TEMPLATES = [
    {
        'name': OTP, 'category': 'AUTHENTICATION',
        'purpose': 'Verification codes: registration and changing a number.',
        'components': [
            {'type': 'BODY', 'add_security_recommendation': True},
            {'type': 'FOOTER', 'code_expiration_minutes': 10},
            {'type': 'BUTTONS', 'buttons': [
                {'type': 'OTP', 'otp_type': 'COPY_CODE', 'text': 'Copy code'}]},
        ],
    },
    {
        'name': NEW_MESSAGE, 'category': 'UTILITY',
        'purpose': 'A staff reply written more than 24 hours after the person last wrote. '
                   'Their tap re-opens the window and the reply is delivered.',
        'body': 'Hi {{1}}, the Metanoia Group team has replied to you about your account. '
                'Tap *Show message* below to read it here.',
        'example': ['Thabo Mokoena'],
        'buttons': [{'type': 'QUICK_REPLY', 'text': 'Show message'}],
    },
    {
        'name': INVOICE, 'category': 'UTILITY',
        'purpose': 'The registration invoice, with the PDF attached.',
        'header': 'DOCUMENT',
        'body': 'Hi {{1}}, thank you for registering with Metanoia Group. Your invoice '
                '{{2}} for {{3}} is attached.\n\nYour free week has started. Pay by EFT with '
                'the invoice number as the reference before it ends and your modules stay open.',
        'example': ['Thabo Mokoena', 'INV-2026-0042', 'R1,250.00'],
    },
    {
        'name': UPDATE, 'category': 'UTILITY',
        'purpose': 'Any account notification without a more specific template.',
        'body': 'Hi {{1}}, there is an update on your Metanoia Group account.\n\n'
                '*{{2}}*\n{{3}}\n\nView it here: {{4}}\n\nReply MENU for quick answers.',
        'example': _FOUR,
    },
    {
        'name': 'metanoia_deadline', 'category': 'UTILITY',
        'purpose': 'Due dates and tests.',
        'body': 'Hi {{1}}, a reminder from Metanoia Group.\n\n*{{2}}*\n{{3}}\n\n'
                'Details: {{4}}\n\nReply DATES to see your next dates.',
        'example': _FOUR,
    },
    {
        'name': 'metanoia_session', 'category': 'UTILITY',
        'purpose': 'Classes, live sessions and meetings.',
        'body': 'Hi {{1}}, a class or session on your Metanoia Group timetable.\n\n'
                '*{{2}}*\n{{3}}\n\nJoin or view it here: {{4}}\n\nSee you there.',
        'example': _FOUR,
    },
    {
        'name': 'metanoia_result', 'category': 'UTILITY',
        'purpose': 'Marks, grades and results.',
        'body': 'Hi {{1}}, a result is ready on your Metanoia Group account.\n\n'
                '*{{2}}*\n{{3}}\n\nSee it here: {{4}}\n\nKeep going.',
        'example': _FOUR,
    },
    {
        'name': 'metanoia_notice', 'category': 'UTILITY',
        'purpose': 'Notices and new material for a module the person is registered for.',
        'body': 'Hi {{1}}, a new notice for your Metanoia Group modules.\n\n'
                '*{{2}}*\n{{3}}\n\nRead it in full: {{4}}\n\nReply MENU for quick answers.',
        'example': _FOUR,
    },
]

BY_NAME = {t['name']: t for t in TEMPLATES}

BY_CATEGORY = {
    'deadlines': 'metanoia_deadline', 'assessments': 'metanoia_deadline',
    'meetings': 'metanoia_session',
    'grades': 'metanoia_result',
    'announcements': 'metanoia_notice', 'content': 'metanoia_notice', 'weekly': 'metanoia_notice',
}


def for_notification(category):
    """The template name for notification ``category`` (:data:`UPDATE` when unmapped)."""
    return BY_CATEGORY.get(category or '', UPDATE)


def definition(template, language='en'):
    """The body Meta's ``POST /{waba-id}/message_templates`` expects.

    A template with a media ``header`` gets only the header format: a media
    header needs a sample file uploaded through Meta's resumable-upload API, so
    ``whatsapp_templates --submit`` leaves these to be created in WhatsApp
    Manager instead.
    """
    if 'components' in template:
        components = template['components']
    else:
        components = [{'type': 'BODY', 'text': template['body'],
                       'example': {'body_text': [template['example']]}},
                      {'type': 'FOOTER', 'text': 'Metanoia Group'}]
        if template.get('header'):
            components.insert(0, {'type': 'HEADER', 'format': template['header']})
        if template.get('buttons'):
            components.append({'type': 'BUTTONS', 'buttons': template['buttons']})
    return {'name': template['name'], 'language': language,
            'category': template['category'], 'components': components}
