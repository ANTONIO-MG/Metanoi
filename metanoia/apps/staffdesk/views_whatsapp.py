"""The WhatsApp inbox — every conversation on the Metanoia number, for the admin team.

One page, two panes: conversations on the left, the open one on the right with
a reply box. Staff write the same way whether or not the person wrote in the
last 24 hours; :func:`apps.communication.whatsapp_inbox.send_staff_reply`
decides whether that is a message now or a template plus a queued reply, and
the page says which.

New messages arrive by polling (:func:`poll`) every few seconds while a
conversation is open, and every admin's bell rings when a message needs a
person (:func:`~apps.communication.whatsapp_inbox.alert_staff`).
"""
from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.communication import whatsapp_inbox as inbox
from apps.communication.models import WhatsAppMessage, WhatsAppThread
from core import whatsapp

from .access import staff_required

TABS = (('open', 'Open'), ('mine', 'Assigned to me'), ('all', 'All'))
PAGE = 60


def _threads(request, tab, search):
    """Conversations for ``tab``, newest inbound first (at most :data:`PAGE`).

    ``search`` matches contact name, message text, a number (4+ digits) or the
    number of a person whose name or e-mail matches.
    """
    threads = WhatsAppThread.objects.filter(last_message_at__isnull=False).select_related(
        'assigned_to', 'assigned_to__profile')
    if tab == 'open':
        threads = threads.filter(status=WhatsAppThread.OPEN)
    elif tab == 'mine':
        threads = threads.filter(assigned_to=request.user)
    if search:
        from apps.accounts.models import Person
        digits = ''.join(ch for ch in search if ch.isdigit())
        match = Q(contact_name__icontains=search) | Q(messages__body__icontains=search)
        if len(digits) >= 4:
            match |= Q(number__contains=digits.lstrip('0'))
        named = Person.objects.filter(
            Q(first_name__icontains=search) | Q(last_name__icontains=search)
            | Q(user__email__icontains=search)).select_related('contact')[:200]
        numbers = {whatsapp.number_for(p) for p in named} - {''}
        if numbers:
            match |= Q(number__in=numbers)
        threads = threads.filter(match).distinct()
    return threads.order_by('-last_inbound_at', '-last_message_at')[:PAGE]


def _rows(threads):
    """List rows: each thread with the people on its number and its title."""
    people = inbox.people_map(t.number for t in threads)
    return [{'thread': t, 'people': people.get(t.number, []),
             'title': inbox.thread_title(t, people.get(t.number, []))} for t in threads]


def _counts(request):
    """Conversation counts per tab."""
    base = WhatsAppThread.objects.filter(last_message_at__isnull=False)
    return {'open': base.filter(status=WhatsAppThread.OPEN).count(),
            'mine': base.filter(assigned_to=request.user).count(),
            'all': base.count()}


def _team():
    """Active admin and staff users a conversation can be assigned to."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return (User.objects.filter(is_active=True)
            .filter(Q(profile__user_type__in=('admin', 'staff')) | Q(is_staff=True) | Q(is_superuser=True))
            .filter(Q(profile__isnull=True) | Q(profile__preview_of__isnull=True))
            .select_related('profile').distinct().order_by('profile__first_name', 'email'))


@staff_required
def inbox_view(request, pk=None):
    """The inbox page, with conversation ``pk`` open when given.

    Opening an unread conversation clears its unread count and marks the last
    inbound message read on WhatsApp.
    """
    tab = request.GET.get('tab', 'open')
    if tab not in dict(TABS):
        tab = 'open'
    search = request.GET.get('q', '').strip()
    rows = _rows(_threads(request, tab, search))

    thread = conversation = None
    people = []
    if pk is not None:
        thread = get_object_or_404(WhatsAppThread.objects.select_related('assigned_to', 'active_person'), pk=pk)
        conversation = list(thread.messages.select_related('sent_by', 'sent_by__profile', 'person')
                            .order_by('-created_at')[:200])[::-1]
        people = thread.people()
        if thread.unread:
            WhatsAppThread.objects.filter(pk=thread.pk).update(unread=0)
            last_in = next((m for m in reversed(conversation) if m.direction == WhatsAppMessage.IN), None)
            if last_in is not None:
                whatsapp.mark_read(last_in.wa_id)
            for row in rows:
                if row['thread'].pk == thread.pk:
                    row['thread'].unread = 0

    return render(request, 'staffdesk/whatsapp/inbox.html', {
        'page_title': 'WhatsApp', 'tabs': TABS, 'tab': tab, 'search': search, 'rows': rows,
        'counts': _counts(request), 'thread': thread, 'conversation': conversation,
        'people': people, 'shared': len(people) > 1,
        'title': inbox.thread_title(thread, people) if thread else '',
        'number': whatsapp.display(thread.number) if thread else '',
        'team': _team() if thread else [],
        'last_id': conversation[-1].pk if conversation else 0,
        'configured': whatsapp.enabled(),
        'display_number': whatsapp.display(getattr(settings, 'WHATSAPP_DISPLAY_NUMBER', '')),
    })


@staff_required
def poll(request, pk):
    """New messages on a conversation since ``after``, as rendered bubbles.

    Delivery ticks move after a message is shown, so the statuses of the last
    20 sent messages are returned again on every poll.
    """
    thread = get_object_or_404(WhatsAppThread, pk=pk)
    try:
        after = int(request.GET.get('after', 0))
    except ValueError:
        after = 0
    new = list(thread.messages.filter(pk__gt=after).select_related('sent_by', 'sent_by__profile', 'person')
               .order_by('created_at')[:100])
    recent = list(thread.messages.filter(direction=WhatsAppMessage.OUT, pk__lte=after)
                  .order_by('-created_at').values('pk', 'status', 'error')[:20])
    if new and any(m.direction == WhatsAppMessage.IN for m in new):
        WhatsAppThread.objects.filter(pk=thread.pk).update(unread=0)
    html = render_to_string('staffdesk/whatsapp/_bubbles.html',
                            {'conversation': new, 'shared': len(thread.people()) > 1}, request=request)
    return JsonResponse({
        'html': html, 'last_id': new[-1].pk if new else after,
        'statuses': [{'id': r['pk'], 'status': r['status'],
                      'label': dict(WhatsAppMessage.STATUS_CHOICES).get(r['status'], ''),
                      'error': r['error']} for r in recent],
        'window_open': thread.window_open, 'status': thread.status,
        'open_total': WhatsAppThread.objects.filter(status=WhatsAppThread.OPEN).count(),
    })


@staff_required
@require_POST
def send(request, pk):
    """Send a staff reply (text and/or a file) and assign the thread to the sender if unassigned.

    Outside the 24-hour window WhatsApp allows only a template, so the reply is
    queued until the person answers; the page says so.
    """
    thread = get_object_or_404(WhatsAppThread, pk=pk)
    text = request.POST.get('text', '').strip()
    upload = request.FILES.get('file')
    if not text and upload is None:
        messages.error(request, 'Type a message or attach a file.')
        return redirect('staffdesk:whatsapp-thread', pk=thread.pk)
    if upload is not None:
        problem = inbox.check_upload(upload)
        if problem:
            messages.error(request, problem)
            return redirect('staffdesk:whatsapp-thread', pk=thread.pk)
    message = inbox.send_staff_reply(thread, request.user, text=text[:4000], upload=upload)
    if thread.assigned_to_id is None:
        thread.assigned_to = request.user
        thread.save(update_fields=['assigned_to'])
    if message.status == WhatsAppMessage.STATUS_QUEUED:
        messages.info(request, 'They have not written in the last 24 hours, so WhatsApp only allows a '
                               'template. They have been told the team replied, and your message goes '
                               'the moment they answer.')
    elif message.status == WhatsAppMessage.STATUS_FAILED:
        messages.error(request, f'Not delivered: {message.error or "WhatsApp refused it"}.')
    return redirect('staffdesk:whatsapp-thread', pk=thread.pk)


@staff_required
@require_POST
def action(request, pk):
    """Close, reopen or assign a conversation, or choose which person on a shared number it is about.

    Closing hands the number back to the bot.
    """
    thread = get_object_or_404(WhatsAppThread, pk=pk)
    what = request.POST.get('action', '')
    if what == 'close':
        thread.status = WhatsAppThread.CLOSED
        thread.save(update_fields=['status'])
        messages.success(request, 'Conversation closed. The bot answers this number again.')
        return redirect(f"{reverse('staffdesk:whatsapp')}?tab=open")
    if what == 'reopen':
        thread.status = WhatsAppThread.OPEN
        thread.save(update_fields=['status'])
    elif what == 'assign':
        user_id = request.POST.get('user') or ''
        thread.assigned_to = (get_object_or_404(_team(), pk=user_id) if user_id.isdigit() else None)
        thread.save(update_fields=['assigned_to'])
    elif what == 'person':
        person_id = request.POST.get('person') or ''
        chosen = next((p for p in thread.people() if str(p.pk) == person_id), None)
        thread.active_person = chosen
        thread.awaiting_choice = False
        thread.save(update_fields=['active_person', 'awaiting_choice'])
    return redirect('staffdesk:whatsapp-thread', pk=thread.pk)


@staff_required
def start(request, person_id):
    """Open (or start) the conversation with one person's number."""
    from apps.accounts.models import Person
    person = get_object_or_404(Person, pk=person_id)
    number = whatsapp.number_for(person)
    if not number:
        messages.error(request, 'There is no WhatsApp number on that account.')
        return redirect('staffdesk:whatsapp')
    thread = inbox.thread_for(number)
    if thread.last_message_at is None:
        thread.last_message_at = timezone.now()
    thread.active_person = person
    thread.awaiting_choice = False
    thread.save(update_fields=['active_person', 'awaiting_choice', 'last_message_at'])
    return redirect('staffdesk:whatsapp-thread', pk=thread.pk)


@staff_required
def media(request, message_id):
    """A file from a conversation: fetched from Meta on first open, then ours."""
    message = get_object_or_404(WhatsAppMessage, pk=message_id)
    if not message.media and not inbox.fetch_media(message):
        raise Http404('That file is no longer available from WhatsApp.')
    return redirect(message.media.url)
