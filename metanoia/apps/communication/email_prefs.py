"""Unsubscribe and e-mail preferences, reached from a link in the e-mail itself.

Every automated e-mail ends with an Unsubscribe button and an "E-mail
preferences" link. Both carry a signed token naming the person, so they work
from any phone or mail app without signing in — that is the point of an
unsubscribe link — and nobody can change somebody else's settings by editing
the address.

Two details carry the design:

* **A GET never unsubscribes.** Mail security scanners open every link in a
  message before the person does; if opening the link were enough, every
  scanned inbox would unsubscribe itself. The page asks, and the button posts.
* **One-click for the mail apps.** Gmail and Apple Mail show their own
  "Unsubscribe" next to the sender when a message carries ``List-Unsubscribe``
  with ``List-Unsubscribe-Post`` (RFC 8058). They POST to :func:`one_click`
  with no cookie and no CSRF token, so that one view is CSRF-exempt and takes
  nothing but the signed token.

What can be switched off is the optional mail only: reminders, updates and the
e-mail copies of team messages. Invoices, receipts, sign-in and account e-mails
are not optional and the page says so.

``SWITCHES`` lists the switches the preferences page offers, in the order it shows them.
"""

from django.contrib.auth import get_user_model
from django.core import signing
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import NotificationPreference

SALT = 'communication.email-preferences'


def token_for(user):
    """The signed, non-expiring token in a person's unsubscribe links."""
    return signing.dumps({'u': user.pk}, salt=SALT, compress=True)


def user_for(token):
    """The person a token names, or ``None`` for a forged or garbled one."""
    try:
        data = signing.loads(token, salt=SALT)
    except signing.BadSignature:
        return None
    pk = data.get('u') if isinstance(data, dict) else None
    return get_user_model().objects.filter(pk=pk, is_active=True).first() if pk else None


def links_for(user):
    """``{'unsubscribe_url', 'preferences_url', 'one_click_url'}`` for ``user`` — absolute."""
    from core.utils import absolute_url
    token = token_for(user)
    return {
        'preferences_url': absolute_url(reverse('communication:email-preferences', args=[token])),
        'unsubscribe_url': absolute_url(reverse('communication:email-preferences', args=[token]) + '?unsubscribe=1'),
        'one_click_url': absolute_url(reverse('communication:email-unsubscribe-one-click', args=[token])),
    }


def headers_for(user):
    """``List-Unsubscribe`` headers, so mail apps offer their own one-click button."""
    links = links_for(user)
    return {
        'List-Unsubscribe': f'<{links["one_click_url"]}>',
        'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
    }


SWITCHES = (
    ('email_reminders', 'Reminders',
     'Upcoming live sessions, due dates and "where you left off" nudges.'),
    ('email_updates', 'Updates',
     'New material on your modules, the weekly summary, class summaries and recordings.'),
    ('email_enabled', 'Messages from the Metanoia team',
     'Announcements and notes your coaches and the team send you.'),
)


def preferences(request, token):
    """The page behind every Unsubscribe / E-mail preferences link."""
    user = user_for(token)
    if user is None:
        raise Http404('This link is not valid.')
    pref = NotificationPreference.for_user(user)
    done = ''
    if request.method == 'POST':
        if request.POST.get('action') == 'unsubscribe':
            pref.unsubscribe()
            done = 'unsubscribed'
        else:
            for field, _label, _help in SWITCHES:
                setattr(pref, field, request.POST.get(field) == 'on')
            pref.save(update_fields=[f for f, _l, _h in SWITCHES] + ['updated_at'])
            done = 'saved'
    return render(request, 'communication/email-preferences.html', {
        'page_title': 'E-mail preferences',
        'person': user,
        'token': token,
        'switches': [(field, label, help_text, getattr(pref, field)) for field, label, help_text in SWITCHES],
        'asking_to_unsubscribe': request.GET.get('unsubscribe') == '1' and not done,
        'done': done,
        'all_off': not (pref.email_enabled or pref.email_reminders or pref.email_updates),
    })


@csrf_exempt
@require_POST
def one_click(request, token):
    """RFC 8058 one-click unsubscribe, posted by the mail app itself."""
    user = user_for(token)
    if user is None:
        raise Http404('This link is not valid.')
    NotificationPreference.for_user(user).unsubscribe()
    return HttpResponse('Unsubscribed.', content_type='text/plain')
