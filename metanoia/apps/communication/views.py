"""HTML pages for the communication app.

These are thin shells around the REST API (:mod:`apps.communication.api`):

* ``chat_home`` / ``chat_room`` — chat UI (vanilla-JS, polls the API)
* ``meetings_list`` / ``meeting_create`` — schedule meetings; ``meeting_room``
  is the launch page into the Microsoft Teams meeting
* ``chat_call`` / ``chat_call_join`` — a group's Teams video call
* ``link_preview`` — preview cards for links in shared text (JSON)
* ``notifications_list`` — the notification inbox
* ``announcement_compose`` / ``announcement_list`` — staff/admin bulk
  notifications (also delivered by e-mail)

The rest of the module, in order:

* **Chat.** ``_decorate_group`` is :func:`services.decorate_group`: it lives
  in services now that the module feed embeds a thread too, because a chat
  header has to be decorated the same way wherever it is drawn.
* **Meetings** (Teams video calls and class sessions).
* **Notifications.** ``_LEVEL_THEME`` holds the presentational themes derived
  from a notification's ``level``, used only when it is not tied to a richer
  related object. :data:`NOTIFICATIONS_PER_PAGE` is the page size of
  ``/communication/notifications/``; :data:`NOTIFICATION_TONE_LABELS` holds
  the tone names the Notification centre composer uses (``AnnouncementForm``).
* **Announcements — the broadcast composer** (admin / staff). Staff write a
  message, attach pictures / video / documents (at most
  :data:`MAX_BROADCAST_FILES`), choose who gets it (everyone, or any mix of
  institutions, programmes, cohorts, modules and people, narrowed by role) and
  send it now, schedule it or keep it as a draft. Educators reach their own
  classes through Messages and the module feed.
* **Discussions** (the Q&A-style forum), the attendance heartbeat,
  notification preferences and **study groups**.
* Redirects for the old in-system e-mail, which moved to Metanoia Mail.
* **The unified activity feed** (dashboard and social page), live alerts and
  Web Push, and the **Metanoia Community** actions (share, report, moderate,
  tag, Message Metanoia).
* **The wall** — the social feed rendered on every profile (person / course /
  module). It is backed by :class:`Discussion` + :class:`DiscussionReply`,
  which already carry likes, threaded replies and attachments and can be
  scoped to a course or a module. Each action returns to the page it was
  fired from, so the wall works inline on whichever profile you are looking
  at.
"""

import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.branding import t
from core.errors import note, report
from core.redirects import redirect_back, safe_target
from core.scoping import PARENT_CHAT_KINDS
from core.roles import role_flags

from . import forms, models, services

logger = logging.getLogger('apps')
staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


def _may_attend(user, session):
    """Is ``user`` in the audience of ``session`` — someone it is *for*?

    A session tied to a live meeting follows that meeting's audience (the same
    rule that puts the class on their calendar, :func:`apps.livesessions.audience.can_view`);
    a free-standing session is for the students on its module (everyone on the
    programme, for its General module).

    Nobody holds an enrolment on a programme's General module; being on the
    programme is what makes it theirs (:mod:`apps.learning.general`).
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if session.meeting_id:
        from apps.livesessions import audience as aud
        return aud.can_view(user, session.meeting)
    person = getattr(user, 'profile', None)
    if person is None:
        return False
    if person.selected_modules.filter(pk=session.module_id).exists():
        return True
    from apps.learning.general import general_offerings_for
    return general_offerings_for(person).filter(pk=session.module_id).exists()


_decorate_group = services.decorate_group


def _chat_context(request, active_group=None):
    """Conversations the user belongs to (General ensured) + people to DM.

    * A **parent** is not part of the school's group conversations: the
      platform-wide "General" room and any class group would put them in a chat
      with other families' children, so they get their own direct threads only.
    * Members are prefetched with their login and profile in one query, so every
      row's name and picture is already loaded.
    * Joining General is a no-op for anyone already in it, so the get-or-create
      (and a re-read) only runs for people who are not.
    * The active group reuses the decorated instance from the list, so the header
      and the row agree; the fallback is only decorated when needed (as a
      ``next()`` default it was evaluated, at a query per member, on every view).
    * ``people`` — who this person may start a conversation with — is scoped to
      their own spine by :func:`core.scoping.directory_users`: people who share
      their institution, programme or intake, plus every admin, staff member and
      educator, who must stay reachable by everybody. A parent gets the narrower
      ``parent_contacts`` rule, so the account never becomes a directory of other
      families.
    * ``dm_gate`` — for a 1:1 direct chat, whether a student ↔ student connection
      is needed (and its state), so the template can gate the compose box.
    """
    from core.scoping import is_parent

    parent = is_parent(request.user)
    groups = models.ChatGroup.objects.filter(memberships__user=request.user, is_active=True)
    if parent:
        groups = groups.filter(kind__in=PARENT_CHAT_KINDS)
    groups = groups.prefetch_related(Prefetch(
        'memberships',
        queryset=models.ChatMembership.objects.select_related('user__profile'))).distinct()
    listed = list(groups)
    if not parent and not any(g.kind == models.ChatGroup.KIND_CUSTOM and g.name == 'General'
                              for g in listed):
        services.ensure_general_group(request.user)
        listed = list(groups.all())
    groups = [_decorate_group(g, request.user) for g in listed]
    if active_group is not None:
        match = next((g for g in groups if g.pk == active_group.pk), None)
        active_group = match if match is not None else _decorate_group(active_group, request.user)
    from core.scoping import directory_users
    people = directory_users(request.user).select_related('profile')
    people = people.order_by('first_name', 'last_name', 'email')[:100]
    dm_gate = None
    if active_group is not None and active_group.kind == models.ChatGroup.KIND_DIRECT:
        other = services.other_member(active_group, request.user)
        if other is not None:
            dm_gate = services.direct_gate_state(request.user, other)
    return {'groups': groups, 'active_group': active_group, 'people': people,
            'dm_gate': dm_gate}


@login_required
def chat_home(request):
    """The Messages page with no conversation open."""
    ctx = _chat_context(request)
    ctx['page_title'] = 'Messages'
    return render(request, 'communication/chat.html', ctx)


@login_required
def chat_room(request, group_id):
    """The Messages page with one conversation open.

    Members only (staff may read any). A parent may be a legacy member of a group
    room, but parents use direct threads only and are sent back to the list.
    """
    from core.scoping import is_parent

    group = get_object_or_404(models.ChatGroup, pk=group_id)
    if not (request.user.is_staff or group.memberships.filter(user=request.user).exists()):
        note('CHAT-2001', request, group=group_id)
        messages.error(request, t('messages.form_errors', 'You are not a member of that chat.'))
        return redirect('communication:chat-home')
    if is_parent(request.user) and group.kind not in PARENT_CHAT_KINDS:
        messages.info(request, 'Parent accounts use direct messages rather than group chats.')
        return redirect('communication:chat-home')
    ctx = _chat_context(request, active_group=group)
    ctx['page_title'] = f"Messages · {getattr(ctx['active_group'], 'chat_title', group.display_name)}"
    return render(request, 'communication/chat.html', ctx)


@login_required
def chat_direct(request, user_id):
    """Open (or start) a 1:1 direct chat with another user.

    The lookup runs against :func:`core.scoping.directory_users` — your
    institution, programme or intake, plus every admin, staff member and
    educator (and, for a parent, the narrower
    :func:`core.scoping.parent_contacts` rule).

    The pool is the control, not the contact list a template renders: a
    hand-typed id for somebody at another institution gets a 404, not a chat.
    The other person's "Let other members start a direct chat with me" setting
    (Settings → Privacy) stops a new chat; one that already exists stays open.
    """
    from core.scoping import directory_users

    other = get_object_or_404(directory_users(request.user), pk=user_id, is_active=True)
    if not services.accepts_direct_messages(request.user, other) and not services.can_direct_message(request.user, other):
        from core.utils import display_name
        messages.info(request, f'{display_name(other)} is not taking new direct messages.')
        return redirect('communication:chat-home')
    group = services.get_or_create_direct_chat(request.user, other)
    return redirect('communication:chat-room', group_id=group.pk)


@login_required
@require_POST
def connection_request(request, user_id):
    """Send a request to open 1:1 messaging with another student.

    Only meaningful between two students (``students_need_connection``); for any
    other pair direct messaging is already open, so we just say so and move on.
    """
    User = get_user_model()
    other = get_object_or_404(User, pk=user_id, is_active=True)
    if other.pk == request.user.pk:
        messages.error(request, 'You cannot connect with yourself.')
    elif services.are_connected(request.user, other):
        messages.info(request, f'You are already connected with {other.get_full_name() or other.get_username()}.')
    elif not services.students_need_connection(request.user, other):
        messages.info(request, 'You can message this person directly — no request needed.')
    else:
        services.request_connection(request.user, other)
        messages.success(
            request,
            f'Connection request sent to {other.get_full_name() or other.get_username()}. '
            f'You can chat once they accept.')
    return redirect(safe_target(request, request.POST.get('next'), reverse('communication:chat-direct', args=[other.pk])))


@login_required
@require_POST
def connection_respond(request, pk):
    """Accept or decline a pending connection request addressed to you."""
    conn = get_object_or_404(models.ConnectionRequest, pk=pk, to_user=request.user)
    accept = request.POST.get('action') == 'accept'
    if services.respond_connection(conn, accept=accept, by_user=request.user):
        who = conn.from_user.get_full_name() or conn.from_user.get_username()
        if accept:
            messages.success(request, f'You are now connected with {who}. Say hello!')
        else:
            messages.info(request, f"Declined {who}'s connection request.")
    else:
        messages.error(request, 'That request could not be updated.')
    if accept:
        return redirect('communication:chat-direct', user_id=conn.from_user_id)
    return redirect(safe_target(request, request.POST.get('next'), reverse('communication:chat-home')))


def _visible_meetings(user):
    """Active meetings ``user`` may see — the same rule as the API
    (``api.MeetingRoomViewSet``): the calendar's visibility
    (``livesessions.audience.visible_sessions``; admin/staff see all) plus calls
    started in a conversation they belong to (parents: direct chats only)."""
    from apps.livesessions import audience
    from core.scoping import is_parent

    qs = models.MeetingRoom.objects.select_related('host', 'group').filter(is_active=True)
    if audience.sees_whole_calendar(user):
        return qs
    visible = audience.visible_sessions(user).values('pk')
    chat_calls = qs.filter(group__memberships__user=user)
    if is_parent(user):
        chat_calls = chat_calls.filter(group__kind__in=PARENT_CHAT_KINDS)
    return qs.filter(Q(pk__in=visible) | Q(pk__in=chat_calls.values('pk')))


def _limit_meeting_groups(form, user):
    """A meeting may only be attached to a conversation the user is in
    (Django staff/superusers excepted — the same rule as the API's _check_group)."""
    field = form.fields.get('group')
    if field is not None and not (user.is_staff or user.is_superuser):
        field.queryset = field.queryset.filter(memberships__user=user).distinct()


@login_required
def meetings_list(request):
    """The "Upcoming" half of Online sessions (``livesessions/_sessions_switch.html``).

    Lists what is live or still to come, soonest first, from the meetings the
    user may see. Finished classes and their recordings are on Past sessions. A
    meeting with no end time stays listed until a day after it started, then
    counts as past; a repeating meeting is always ahead.
    """
    from apps.livesessions import audience
    now = timezone.now()
    upcoming = (_visible_meetings(request.user)
                .filter(Q(scheduled_end__gte=now)
                        | Q(scheduled_end__isnull=True, scheduled_start__gte=now - timedelta(days=1))
                        | Q(scheduled_start__isnull=True)
                        | ~Q(recurrence=''))
                .order_by(F('scheduled_start').asc(nulls_last=True)))
    return render(request, 'communication/meetings.html', {
        'page_title': 'Upcoming sessions',
        'meetings': upcoming,
        'can_schedule': audience.can_manage(request.user),
    })


@login_required
def meeting_create(request):
    """Schedule a standalone meeting — for the roles that may schedule on the
    calendar (``livesessions.audience.can_manage``: admin, staff, educators).
    Anyone can still start a call in a chat they belong to (``chat_call``).

    The session is assigned to the module behind the chosen group. It is created
    through the Teams service, which saves the :class:`MeetingRoom`; when Teams
    cannot make the meeting now it is made on the first join. The form fields the
    service does not take (recurrence) are applied afterwards.
    """
    from apps.livesessions import audience
    if not audience.can_manage(request.user):
        messages.error(request, 'Only educators and staff can schedule meetings. '
                                'You can start a call from any of your chats.')
        return redirect('communication:meetings')
    if request.method == 'POST':
        form = forms.MeetingRoomForm(request.POST)
        _limit_meeting_groups(form, request.user)
        if form.is_valid():
            cd = form.cleaned_data
            group = cd.get('group')
            module = getattr(group, 'module', None) if group else None

            from apps.msteams import services as teams_services
            meeting = teams_services.create_class_meeting(
                host=request.user,
                title=cd['title'],
                start=cd.get('scheduled_start'),
                end=cd.get('scheduled_end'),
                module=module,
                group=group,
                description=cd.get('description', ''),
            )
            meeting.is_recurring = cd.get('is_recurring', False)
            meeting.recurrence = cd.get('recurrence', '')
            meeting.save(update_fields=['is_recurring', 'recurrence'])

            try:
                from .emails import send_session_scheduled
                send_session_scheduled(
                    request.user, session_title=meeting.title,
                    session_when=meeting.scheduled_start or '',
                    join_url=request.build_absolute_uri(meeting.get_join_url()))
            except Exception:
                pass
            messages.success(request, t('messages.created', '{name} created.', name='Teams meeting'))
            if not meeting.teams_join_url:
                messages.warning(request, 'Microsoft Teams could not create the meeting yet. It will '
                                          'be created when someone joins — if that fails too, people '
                                          'are told and the organiser is notified.')
            return redirect('communication:meeting-room', slug=meeting.slug)
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        form = forms.MeetingRoomForm()
        _limit_meeting_groups(form, request.user)
    return render(request, 'communication/meeting-form.html', {'page_title': 'New Meeting', 'form': form})


def _call_return(group):
    """Where a group's call buttons send people back to: the study group's page
    when the chat belongs to one, otherwise the conversation."""
    study_group = group.linked_study_group
    if study_group is not None:
        return reverse('communication:workspace-detail', args=[study_group.pk])
    return reverse('communication:chat-room', args=[group.pk])


@login_required
@require_POST
def chat_call(request, group_id):
    """Start (or join the running) Microsoft Teams video call of a group.

    Groups only — there are no calls in one-on-one chats, and no audio-only
    calls. The meeting is created by the platform's Microsoft account with the
    lobby open to everyone, so members can go in before whoever started it.
    If Teams cannot create it the person is told; nothing else is tried.
    """
    from . import group_calls
    from apps.msteams.services import MeetingNotCreated

    group = get_object_or_404(models.ChatGroup, pk=group_id)
    if not group.memberships.filter(user=request.user).exists():
        note('CHAT-2001', request, group=group_id, action='call')
        messages.error(request, 'You are not a member of that conversation.')
        return redirect('communication:chat-home')
    back = _call_return(group)
    if not group_calls.can_call(group):
        messages.error(request, 'Video calls are for groups. Start a group to call more than one person.')
        return redirect(back)
    if getattr(request, 'student_view', None) is not None:
        messages.info(request, 'Calls are not available in student view.')
        return redirect(back)
    try:
        meeting, _created = group_calls.start_call(group, request.user)
    except MeetingNotCreated as exc:
        logger.warning('group call: Teams refused a call for group %s: %s', group.pk, exc)
        messages.error(request, f'The video call could not be started: {exc} '
                                'Please try again in a few minutes, or tell the Metanoia team '
                                'if it keeps happening.')
        return redirect(back)
    return redirect('livesessions:session-join', pk=meeting.pk)


@login_required
def chat_call_join(request, group_id, meeting_id):
    """A group member joins the group's call through the LMS hand-off.

    Members who joined the group after the call started are added to it here,
    so the session page and the hand-off let them in.
    """
    from . import group_calls

    group = get_object_or_404(models.ChatGroup, pk=group_id)
    meeting = get_object_or_404(models.MeetingRoom, pk=meeting_id, group=group)
    if not group.memberships.filter(user=request.user).exists():
        note('CHAT-2001', request, group=group_id, action='join-call')
        messages.error(request, 'You are not a member of that group.')
        return redirect('communication:chat-home')
    group_calls.add_participant(meeting, request.user)
    return redirect('livesessions:session-join', pk=meeting.pk)


@login_required
def link_preview(request):
    """The preview card for one link, as JSON — for ``static/js/link-preview.js``.

    Per viewer: an LMS object gets a card only when this person may open it.
    External pages are fetched by :mod:`apps.communication.unfurl` (SSRF-safe,
    cached a day). Throttled per person so the endpoint cannot be used as a
    crawler.
    """
    from django.core.cache import cache
    from . import unfurl

    url = (request.GET.get('url') or '').strip()
    key = f'unfurl:rate:{request.user.pk}'
    count = cache.get(key, 0)
    if count >= 120:
        return JsonResponse({'card': None, 'throttled': True}, status=429)
    cache.set(key, count + 1, 60)
    card = unfurl.preview_for(url, request.user) if url else None
    if card:
        card = {k: card.get(k, '') for k in ('kind', 'title', 'description', 'image', 'site',
                                             'href', 'same_tab')}
    response = JsonResponse({'card': card})
    response['Cache-Control'] = 'private, max-age=300'
    return response


@login_required
def meeting_room(request, slug):
    """Launch page for a live session (this URL *is* the shareable link).

    The authoritative attendance comes from the Graph attendance report after
    the class (students join anonymously), so nothing is marked on page-open.

    Paid-up students only: the invite e-mail and the calendar entry go to the
    whole audience on purpose, but the room itself does not. That is checked
    before anything else, so a blocked student is never recorded at a class they
    never got into.

    The page also carries where to go when the call ends (``?return=``, only if
    it stays on this site — otherwise the chat list), the after-class recap if
    the sync has filed one, and the shareable link with a short human message
    copied together with it by "Share link", so the paste explains itself.
    """
    meeting = get_object_or_404(models.MeetingRoom, slug=slug)

    from apps.livesessions.audience import can_join
    allowed, _reason = can_join(request.user, meeting)
    if not allowed:
        person = getattr(request.user, 'profile', None)
        invoice = person.get_pending_invoice() if person else None
        return render(request, 'communication/meeting-locked.html', {
            'page_title': meeting.title,
            'meeting': meeting,
            'invoice': invoice,
        }, status=403)

    from django.utils.http import url_has_allowed_host_and_scheme
    return_url = request.GET.get('return', '')
    if not (return_url and url_has_allowed_host_and_scheme(
            return_url, allowed_hosts={request.get_host()}, require_https=request.is_secure())):
        return_url = reverse('communication:chat-home')

    recap = None
    try:
        from apps.ai_assistant.models import AiReport
        recap = (AiReport.objects.filter(linked_ref=f'meeting:{meeting.pk}')
                 .order_by('-created_at').first())
    except Exception:
        pass

    share_url = request.build_absolute_uri(meeting.get_join_url())
    when = meeting.scheduled_start.strftime('%a %d %b, %H:%M') if meeting.scheduled_start else 'now'
    share_message = (
        f'Join me on “{meeting.title}” ({when}) — {share_url}'
    )

    recap_sections = []
    if recap is not None:
        try:
            from apps.livesessions.summaries import display_sections
            recap_sections = display_sections(recap.content)
        except Exception:
            recap_sections = []

    return render(request, 'communication/meeting-room.html', {
        'page_title': meeting.title, 'meeting': meeting, 'recap': recap,
        'recap_sections': recap_sections,
        'return_url': return_url,
        'share_url': share_url, 'share_message': share_message,
        'can_manage': _can_manage_session(request.user, meeting),
    })


def _can_manage_session(user, meeting):
    """Return whether ``user`` may manage ``meeting``; ``False`` on any error."""
    from apps.livesessions.audience import can_manage
    try:
        return can_manage(user, meeting)
    except Exception:
        return False


_LEVEL_THEME = {
    'info': ('Update', 'info-circle', 'info'),
    'success': ('Notification', 'bell-fill', 'success'),
    'warning': ('Reminder', 'exclamation-triangle', 'warning'),
    'error': ('Alert', 'exclamation-octagon', 'danger'),
}


def _decorate_notification(n):
    """Attach display-only ``category_label`` / ``icon`` / ``color`` and a
    ``related_url`` + ``related_label`` to a :class:`Notification`.

    The category is derived from which related object the notification points at
    (announcement / meeting / mention) falling back to its ``level``. Nothing is
    persisted — these are purely for theming the list & detail templates.

    Without a related object the theme comes from ``_LEVEL_THEME`` (by
    ``level``); ``success`` shows as a plain "Notification" bell, because
    "Success" named the severity, not the thing. The sender — the person who sent
    it, or the Metanoia mailbox it belongs with (Accounts / Learning Hub / Info) —
    is attached with their picture, and ``related_url`` is where a "view the
    related item" button should go.
    """
    if n.announcement_id:
        n.category_label, n.icon, n.color = 'Announcement', 'megaphone', 'primary'
    elif n.meeting_id:
        n.category_label, n.icon, n.color = 'Meeting', 'camera-video', 'info'
    elif n.message_id:
        n.category_label, n.icon, n.color = 'Mention', 'at', 'success'
    else:
        n.category_label, n.icon, n.color = _LEVEL_THEME.get(n.level, _LEVEL_THEME['info'])

    from core.utils import initials as _initials
    from .senders import notification_sender
    sender = n.sender = notification_sender(n)
    n.actor_name = sender['name']
    n.actor_avatar = sender['avatar']
    n.actor_initial = _initials(sender['user']) if sender['user'] else 'T'

    related_url, related_label = '', ''
    if n.url:
        related_url, related_label = n.url, 'Open'
    elif n.meeting_id and n.meeting:
        related_url = reverse('communication:meeting-room', args=[n.meeting.slug])
        related_label = 'Join meeting'
    elif n.message_id and n.message and n.message.group_id:
        related_url = reverse('communication:chat-room', args=[n.message.group_id])
        related_label = 'Open conversation'
    n.related_url, n.related_label = related_url, related_label
    return n


NOTIFICATIONS_PER_PAGE = 30

NOTIFICATION_TONE_LABELS = {'info': 'Update', 'success': 'Good news',
                            'warning': 'Reminder', 'error': 'Urgent'}


@login_required
def notifications_list(request):
    """The notification inbox (``/communication/notifications/``), paginated.

    POST handles "mark all read" and favourite toggles, then redirects back. The
    ``?tone=`` filter (``info`` / ``success`` / ``warning`` / ``error`` — the tone
    picked in the Notification centre, labelled as in
    :data:`NOTIFICATION_TONE_LABELS`) narrows the list and carries across pages.
    The Notification centre button follows the same rule as the centre itself
    (:func:`can_announce`).
    """
    qs = (
        models.Notification.objects
        .filter(recipient=request.user)
        .select_related('actor', 'actor__profile', 'message', 'message__group', 'meeting',
                        'announcement', 'announcement__sender', 'announcement__sender__profile')
    )

    unread_notifications = qs.filter(is_read=False)
    unread_count = unread_notifications.count()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'mark_all_read':
            unread_notifications.update(is_read=True)
        elif action == 'toggle_favourite':
            note = qs.filter(pk=request.POST.get('pk')).first()
            if note:
                note.is_favourite = not note.is_favourite
                note.save(update_fields=['is_favourite'])
        return redirect('communication:notifications')

    tones = [(value, NOTIFICATION_TONE_LABELS.get(value, label))
             for value, label in models.Notification.LEVEL_CHOICES]
    tone = request.GET.get('tone', '')
    if tone not in dict(tones):
        tone = ''
    listed = qs.filter(level=tone) if tone else qs
    from django.core.paginator import Paginator
    page = Paginator(listed, NOTIFICATIONS_PER_PAGE).get_page(request.GET.get('page'))
    notifications = [_decorate_notification(n) for n in page.object_list]
    favourites = [_decorate_notification(n) for n in qs.filter(is_favourite=True)[:20]]

    return render(request, 'communication/notifications.html', {
        'page_title': 'Notifications',
        'notifications': notifications,
        'page_obj': page,
        'tones': tones,
        'tone': tone,
        'tone_query': f'tone={tone}&' if tone else '',
        'can_send_notifications': can_announce(request.user),
        'favourites': favourites,
        'unread': unread_count,
        'unread_notifications': unread_notifications[:10],
        'unread_notifications_count': unread_count,
        'recent_notifications': qs[:10],
    })


@login_required
def notification_detail(request, pk):
    """A single notification in full detail (blog-details-style page).

    Only the recipient may view it (404 otherwise). Opening it marks it read;
    the recipient can also toggle read/unread via a POST action.

    "Mark unread" leaves the page as well as flipping the flag: staying here
    would immediately re-read it on the redirect and undo the click. ``?go=1``
    (desktop alerts, the bell's new-mail rows) goes straight on to the
    notification's own on-site URL.
    """
    notification = get_object_or_404(
        models.Notification.objects.select_related(
            'actor', 'message', 'message__group', 'message__sender',
            'meeting', 'announcement', 'announcement__sender',
        ),
        pk=pk, recipient=request.user,
    )

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'mark_unread':
            notification.is_read = False
            notification.save(update_fields=['is_read'])
            return redirect('communication:notifications')
        if action == 'toggle_favourite':
            notification.is_favourite = not notification.is_favourite
            notification.save(update_fields=['is_favourite'])
        elif action == 'mark_read':
            notification.mark_read()
        return redirect('communication:notification-detail', pk=notification.pk)

    notification.mark_read()
    if request.GET.get('go') and (notification.url or '').startswith('/') and not notification.url.startswith('//'):
        return redirect(notification.url)
    _decorate_notification(notification)

    return render(request, 'communication/notification-detail.html', {
        'page_title': notification.title or notification.verb or 'Notification',
        'notification': notification,
        'letter': _notification_letter(notification),
    })


def _notification_letter(n):
    """Who a notification is "from", and the signature it ends with.

    The page reads like the e-mail it would have been (base_email.html): a
    sender, a body, the Metanoia Brand Studio signature. A person who caused it
    (an educator's announcement, a mention) signs with their own picture and
    role; anything system-made signs from the mailbox it belongs with — Metanoia
    Accounts, Metanoia Learning Hub or Metanoia Info (senders.notification_sender).
    An e-mail address is only shown for those shared mailboxes.
    """
    from . import senders
    sender = getattr(n, 'sender', None) or senders.notification_sender(n)
    sig = senders.signature(sender['mailbox'] or senders.INFO,
                            author=sender['user'] if sender['person'] else None)
    personal = sender['person']
    return {
        'sig': sig,
        'personal': personal,
        'person': sender['user'] if personal else None,
        'avatar': sender['avatar'],
        'address': '' if personal else sig['email'],
    }

def can_announce(user):
    """Who may use the Notification centre (the broadcast composer)."""
    return bool(user and getattr(user, 'is_authenticated', False) and user.is_active and (
        user.is_staff or getattr(getattr(user, 'profile', None), 'user_type', '') in ('staff', 'admin')))


announce_required = user_passes_test(can_announce)

MAX_BROADCAST_FILES = 10


def _broadcast_files(request):
    """Validated uploads from the composer, or ``(files, errors)``."""
    from django.core.exceptions import ValidationError
    from core import validators as v

    files, errors = request.FILES.getlist('attachments'), []
    for f in files:
        for validator in v.validate_broadcast:
            try:
                validator(f)
            except ValidationError as exc:
                errors.append(f'{f.name}: {" ".join(exc.messages)}')
                break
    return files, errors


@login_required
@announce_required
def announcement_list(request):
    """The Notification centre: every broadcast by tab (all, sent, scheduled, drafts) with this month's KPIs.

    Admin and staff only (:func:`can_announce`); a ``?q=`` search narrows by
    title and body.
    """
    from django.db.models import Count, Q as _Q
    from . import broadcast

    A = models.Announcement
    base = A.objects.select_related('sender').annotate(
        n_read=Count('notifications', filter=_Q(notifications__is_read=True), distinct=True),
        n_attachments=Count('attachments', distinct=True))
    tab = request.GET.get('tab', 'all')
    tabs = {
        'all': base,
        'sent': base.filter(status=A.STATUS_SENT),
        'scheduled': base.filter(status__in=(A.STATUS_SCHEDULED, A.STATUS_SENDING)),
        'drafts': base.filter(status__in=(A.STATUS_DRAFT, A.STATUS_FAILED, A.STATUS_CANCELLED)),
    }
    qs = tabs.get(tab, base)
    search = (request.GET.get('q') or '').strip()
    if search:
        qs = qs.filter(_Q(title__icontains=search) | _Q(body__icontains=search))

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sent_month = A.objects.filter(status=A.STATUS_SENT, sent_at__gte=month_start)
    reached = sum(sent_month.values_list('recipient_count', flat=True))
    notes = models.Notification.objects.filter(announcement__in=sent_month)
    total_notes = notes.count()
    kpis = {
        'sent_month': sent_month.count(),
        'reached_month': reached,
        'read_rate': round(100 * notes.filter(is_read=True).count() / total_notes) if total_notes else None,
        'scheduled': A.objects.filter(status=A.STATUS_SCHEDULED).count(),
    }
    rows = list(qs[:200])
    for a in rows:
        a.read_pct = round(100 * a.n_read / a.recipient_count) if a.recipient_count else 0
    return render(request, 'communication/announcements.html', {
        'page_title': 'Notifications centre', 'announcements': rows, 'tab': tab, 'search': search,
        'counts': {k: v.count() for k, v in tabs.items()}, 'kpis': kpis,
        'scheduler_ok': broadcast.scheduler_is_running(),
    })


@login_required
@announce_required
def announcement_compose(request, pk=None):
    """Write a new broadcast, or edit a draft / scheduled one.

    ``?copy=<pk>`` starts from an earlier broadcast; with ``&unread=1`` it is
    "Remind unread": the same message, to the people who have not opened it.
    Otherwise deep links such as ``?module=12`` or ``?programme=3`` pre-select
    the audience. At most :data:`MAX_BROADCAST_FILES` files are kept.
    """
    from . import broadcast

    A = models.Announcement
    instance = None
    if pk is not None:
        instance = get_object_or_404(A, pk=pk)
        if not instance.is_editable:
            messages.info(request, 'That notification has already gone out — duplicate it to send again.')
            return redirect('communication:announcement-detail', pk=pk)
    elif request.GET.get('copy'):
        source = A.objects.filter(pk=request.GET.get('copy')).first()
    else:
        source = None

    if request.method == 'POST':
        form = forms.AnnouncementForm(request.POST, instance=instance, user=request.user)
        files, file_errors = _broadcast_files(request)
        kept = instance.attachments.count() if instance else 0
        removed = [x for x in request.POST.getlist('remove_attachment') if x.isdigit()]
        if kept - len(removed) + len(files) > MAX_BROADCAST_FILES:
            file_errors.append(f'A notification can carry up to {MAX_BROADCAST_FILES} files.')
        if form.is_valid() and not file_errors:
            with transaction.atomic():
                announcement = form.save(commit=False)
                if announcement.sender_id is None:
                    announcement.sender = request.user
                when = form.cleaned_data['when']
                announcement.status = (A.STATUS_SCHEDULED if when == form.WHEN_LATER else A.STATUS_DRAFT)
                if when != form.WHEN_LATER:
                    announcement.scheduled_for = None
                announcement.save()
                form.save_m2m()
                if instance and removed:
                    for att in instance.attachments.filter(pk__in=removed):
                        att.file.delete(save=False)
                        att.delete()
                for f in files:
                    models.AnnouncementAttachment.objects.create(
                        announcement=announcement, file=f, original_name=f.name)
                for att in (source.attachments.all() if source and not instance else []):
                    if request.POST.get(f'keep_copy_{att.pk}'):
                        models.AnnouncementAttachment.objects.create(
                            announcement=announcement, file=att.file.name, original_name=att.original_name,
                            size=att.size)

            if when == form.WHEN_NOW:
                inline, count = broadcast.dispatch(announcement)
                if inline:
                    messages.success(request, f'Sent to {count} {"person" if count == 1 else "people"}.')
                else:
                    messages.success(request, f'Sending to about {count} people in the background — '
                                              'the numbers on this page fill in as it goes.')
            elif when == form.WHEN_LATER:
                messages.success(request, 'Scheduled for '
                                 + timezone.localtime(announcement.scheduled_for).strftime('%a %d %b, %H:%M') + '.')
            else:
                messages.success(request, 'Saved as a draft.')
            return redirect('communication:announcement-detail', pk=announcement.pk)
        for err in file_errors:
            messages.error(request, err)
        if file_errors:
            note('NOTF-1002', request, problems=file_errors[:5])
        if not form.is_valid():
            messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        initial = {}
        if source is not None:
            initial = {f: getattr(source, f) for f in ('title', 'body', 'level', 'url', 'url_label', 'media_url',
                                                       'audience', 'roles', 'include_parents', 'is_important',
                                                       'send_email')}
            for f in ('institutions', 'programmes', 'cohorts', 'modules', 'users'):
                initial[f] = list(getattr(source, f).values_list('pk', flat=True))
            if request.GET.get('unread'):
                initial.update(audience=A.AUDIENCE_TARGETED, roles=[], include_parents=False,
                               institutions=[], programmes=[], cohorts=[], modules=[],
                               title=f'Reminder: {source.title}'[:200],
                               users=list(source.notifications.filter(is_read=False)
                                          .values_list('recipient', flat=True)))
        else:
            for key, field in (('institution', 'institutions'), ('programme', 'programmes'),
                               ('cohort', 'cohorts'), ('module', 'modules'), ('user', 'users')):
                ids = [x for x in request.GET.getlist(key) if x.isdigit()]
                if ids:
                    initial[field] = ids
                    initial['audience'] = A.AUDIENCE_TARGETED
        form = forms.AnnouncementForm(instance=instance, initial=initial, user=request.user)
        if source is not None and initial.get('users'):
            from django.contrib.auth import get_user_model
            form.fields['users'].widget.choices = [
                (u.pk, broadcast.person_label(u))
                for u in get_user_model().objects.filter(pk__in=initial['users']).select_related('profile')]

    return render(request, 'communication/announcement-form.html', {
        'page_title': 'Edit notification' if instance else 'Send a notification',
        'form': form, 'announcement': instance,
        'existing_attachments': instance.attachments.all() if instance else [],
        'copied_attachments': source.attachments.all() if source and not instance else [],
        'max_files': MAX_BROADCAST_FILES,
        'scheduler_ok': broadcast.scheduler_is_running(),
    })


@login_required
@announce_required
def announcement_detail(request, pk):
    """The sender's view: what went out, to whom, and who has read it."""
    from . import broadcast

    announcement = get_object_or_404(
        models.Announcement.objects.select_related('sender').prefetch_related(
            'attachments', 'institutions', 'programmes', 'cohorts', 'modules', 'users'), pk=pk)
    notes = (models.Notification.objects.filter(announcement=announcement)
             .select_related('recipient', 'recipient__profile').order_by('is_read', 'recipient__first_name'))
    show = request.GET.get('show', 'all')
    if show == 'unread':
        notes = notes.filter(is_read=False)
    elif show == 'read':
        notes = notes.filter(is_read=True)
    audience = None
    if announcement.status != models.Announcement.STATUS_SENT:
        audience = broadcast.audience_summary(broadcast.recipients_for_announcement(announcement))
    return render(request, 'communication/announcement-detail.html', {
        'page_title': announcement.title, 'a': announcement,
        'stats': broadcast.read_stats(announcement), 'notes': notes[:500], 'show': show,
        'audience': audience, 'scheduler_ok': broadcast.scheduler_is_running(),
    })


@login_required
@announce_required
@require_POST
def announcement_action(request, pk):
    """Send now, cancel a scheduled send, or delete a draft."""
    from . import broadcast

    A = models.Announcement
    announcement = get_object_or_404(A, pk=pk)
    action = request.POST.get('action')
    if action == 'send' and announcement.status in (A.STATUS_DRAFT, A.STATUS_SCHEDULED, A.STATUS_FAILED,
                                                    A.STATUS_CANCELLED):
        if announcement.status == A.STATUS_CANCELLED:
            A.objects.filter(pk=pk).update(status=A.STATUS_DRAFT)
            announcement.refresh_from_db()
        inline, count = broadcast.dispatch(announcement)
        messages.success(request, f'Sent to {count} people.' if inline else
                         f'Sending to about {count} people in the background.')
    elif action == 'cancel' and announcement.status == A.STATUS_SCHEDULED:
        A.objects.filter(pk=pk, status=A.STATUS_SCHEDULED).update(status=A.STATUS_CANCELLED)
        messages.success(request, 'Scheduled send cancelled. It is kept as a draft you can send later.')
    elif action == 'delete' and announcement.status in (A.STATUS_DRAFT, A.STATUS_CANCELLED, A.STATUS_FAILED) \
            and not announcement.recipient_count:
        for att in announcement.attachments.all():
            att.file.delete(save=False)
        announcement.delete()
        messages.success(request, 'Draft deleted.')
        return redirect('communication:announcements')
    else:
        note('NOTF-4001', request, announcement=pk, action=action, status=announcement.status)
        messages.error(request, 'That action is not available for this notification.')
    return redirect('communication:announcement-detail', pk=pk)


@login_required
@announce_required
@require_POST
def announcement_audience_preview(request):
    """Live "who will get this" count for the composer (JSON)."""
    from . import broadcast

    A = models.Announcement

    def ids(name):
        """Return the numeric ids posted under ``name``."""
        return [x for x in request.POST.getlist(name) if x.isdigit()]

    roles = [r for r in request.POST.getlist('roles') if r in dict(A.ROLE_CHOICES)]
    if request.POST.get('audience') == A.AUDIENCE_ALL:
        users = broadcast.audience_users(everyone=True, roles=roles)
    else:
        users = broadcast.audience_users(
            institutions=ids('institutions'), programmes=ids('programmes'), cohorts=ids('cohorts'),
            modules=ids('modules'), users=ids('users'), roles=roles,
            include_parents=bool(request.POST.get('include_parents')))
    return JsonResponse(broadcast.audience_summary(users))


@login_required
@announce_required
def announcement_people_search(request):
    """Search active accounts by name, username or e-mail for the people picker."""
    from django.contrib.auth import get_user_model

    from core.utils import avatar_url
    from . import broadcast

    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'results': []})
    users = get_user_model().objects.filter(is_active=True)
    for word in q.split()[:3]:
        users = users.filter(Q(first_name__icontains=word) | Q(last_name__icontains=word)
                             | Q(profile__first_name__icontains=word) | Q(profile__last_name__icontains=word)
                             | Q(email__icontains=word))
    results = [{
        'id': u.pk, 'name': broadcast.person_label(u), 'avatar': avatar_url(u),
        'role': getattr(getattr(u, 'profile', None), 'get_user_type_display', lambda: '')(),
    } for u in users.select_related('profile').distinct().order_by('first_name', 'last_name')[:20]]
    return JsonResponse({'results': results})


def _save_attachments(request, *, discussion=None, reply=None):
    """Persist any uploaded media against a discussion or reply.

    ``objects.create()`` skips the field's validators, so each file is checked
    here; a wrong-type or oversized file is left off with a message.
    """
    from core import validators as v
    for f in request.FILES.getlist('attachments'):
        errors = v.upload_errors(f, v.validate_attachment)
        if errors:
            messages.error(request, f'{f.name} was not attached: {" ".join(errors)}')
            continue
        models.DiscussionAttachment.objects.create(
            discussion=discussion, reply=reply, file=f,
            original_name=getattr(f, 'name', ''),
        )


@login_required
def discussions(request):
    """The discussion forum list, optionally filtered by ``?module=`` and a ``?q=`` search."""
    qs = (models.Discussion.objects
          .select_related('author', 'module')
          .prefetch_related('replies', 'likes'))

    module_id = request.GET.get('module')
    search = request.GET.get('q')
    if module_id:
        qs = qs.filter(module_id=module_id)
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(body__icontains=search) | Q(tags__icontains=search))

    return render(request, 'communication/discussions.html', {
        'page_title': 'Discussions', 'discussions': qs[:200],
        'search': search or '',
    })


@login_required
def discussion_detail(request, pk):
    """One discussion with its replies.

    Each view is counted (cheap, best effort). POST ``action=reply`` adds a reply
    with attachments; ``action=like`` toggles the viewer's like.
    """
    discussion = get_object_or_404(
        models.Discussion.objects.select_related('author', 'module')
        .prefetch_related('attachments', 'replies__author', 'replies__attachments'),
        pk=pk,
    )
    models.Discussion.objects.filter(pk=pk).update(view_count=discussion.view_count + 1)

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reply':
            form = forms.DiscussionReplyForm(request.POST)
            if form.is_valid():
                reply = form.save(commit=False)
                reply.discussion = discussion
                reply.author = request.user
                reply.save()
                _save_attachments(request, reply=reply)
                messages.success(request, t('messages.saved', '{name} saved successfully.', name='Reply'))
            else:
                messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
            return redirect('communication:discussion-detail', pk=discussion.pk)
        if action == 'like':
            if discussion.likes.filter(pk=request.user.pk).exists():
                discussion.likes.remove(request.user)
            else:
                discussion.likes.add(request.user)
            return redirect('communication:discussion-detail', pk=discussion.pk)

    return render(request, 'communication/discussion-detail.html', {
        'page_title': discussion.title, 'discussion': discussion,
        'reply_form': forms.DiscussionReplyForm(),
        'liked': discussion.likes.filter(pk=request.user.pk).exists(),
    })


@login_required
def discussion_create(request):
    """Start a discussion, optionally pre-set to a ``?course=`` or ``?module=``."""
    if request.method == 'POST':
        form = forms.DiscussionForm(request.POST)
        if form.is_valid():
            discussion = form.save(commit=False)
            discussion.author = request.user
            discussion.save()
            form.save_m2m()
            _save_attachments(request, discussion=discussion)
            messages.success(request, t('messages.created', '{name} created.', name='Discussion'))
            return redirect('communication:discussion-detail', pk=discussion.pk)
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        initial = {}
        if request.GET.get('course'):
            initial['course'] = request.GET['course']
        if request.GET.get('module'):
            initial['module'] = request.GET['module']
        form = forms.DiscussionForm(initial=initial)
    return render(request, 'communication/discussion-form.html', {
        'page_title': 'Start a Discussion', 'form': form,
    })


@login_required
@require_POST
def attendance_ping(request, pk):
    """Heartbeat from a student who has the class session open.

    Called every ~60s by the meeting-room / session page while the tab is
    visible. Each ping credits the elapsed time (capped, see
    :data:`apps.communication.services.PRESENCE_MAX_GAP_SECONDS`) and re-derives
    the status, so attendance reflects time actually spent rather than a single
    click on the join link.

    Attendance is automatic only. The manual register
    (``/communication/attendance/``) was removed at the client's request
    (2026-11-09); presence is measured by this heartbeat and by the Teams
    attendance-report import.
    """
    session = get_object_or_404(models.ClassSession, pk=pk)
    if not _may_attend(request.user, session):
        raise Http404('No such session')
    if not session.is_open:
        note('MEET-4001', request, session=pk)
        return JsonResponse({'ok': False, 'reason': 'closed'}, status=409)

    record = services.record_presence_ping(session, request.user)
    if record is None:
        return JsonResponse({'ok': False}, status=400)
    return JsonResponse({
        'ok': True,
        'seconds': record.seconds_attended,
        'minutes': record.minutes_attended,
        'pct': record.attended_pct,
        'required_pct': session.min_attendance_pct,
        'status': record.status,
        'status_display': record.get_status_display(),
        'manual': record.is_manual,
    })


@login_required
def notification_preferences(request):
    """The signed-in user's notification preferences form."""
    pref = models.NotificationPreference.for_user(request.user)
    if request.method == 'POST':
        form = forms.NotificationPreferenceForm(request.POST, instance=pref)
        if form.is_valid():
            form.save()
            messages.success(request, t('messages.saved', '{name} saved successfully.', name='Preferences'))
            return redirect('communication:notification-preferences')
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        form = forms.NotificationPreferenceForm(instance=pref)
    return render(request, 'communication/notification-preferences.html', {
        'page_title': 'Notification preferences', 'form': form,
    })


def _visible_workspaces(request):
    """Study groups the viewer may see.

    Admin and staff see every active group; anyone else sees groups they created
    or belong to, plus groups on a module they are enrolled in or teach. Study
    groups are :class:`~apps.communication.models.Workspace` rows: they were
    called "workspaces" until 2026-11, and the model, table and URL names kept the
    old word so nothing that links here had to change. Who may be put in a group,
    and which modules it may be about, is :mod:`apps.communication.study_groups`.
    """
    qs = models.Workspace.objects.filter(is_active=True).select_related(
        'module__programme__institution', 'module__module', 'created_by__profile')
    flags = role_flags(request)
    if flags['is_admin_staff']:
        return qs
    person = getattr(request.user, 'profile', None)
    cond = Q(created_by=request.user) | Q(members=request.user)
    if person:
        cond |= Q(module__enrolments__person=person) | Q(module__educators=person)
    return qs.filter(cond).distinct()


def _study_group_cards(queryset):
    """Groups with their member pictures and file / note counts for the cards."""
    from django.db.models import Count
    from django.contrib.auth import get_user_model
    people = get_user_model().objects.select_related('profile').order_by('profile__first_name')
    return (queryset
            .annotate(file_count=Count('files', distinct=True), note_count=Count('notes', distinct=True))
            .prefetch_related(Prefetch('members', queryset=people)))


def _count_people(groups):
    """Each group's head count — its members and its creator, once each."""
    groups = list(groups)
    for ws in groups:
        ids = {m.pk for m in ws.members.all()}
        if ws.created_by_id:
            ids.add(ws.created_by_id)
        ws.people_count = len(ids)
    return groups


def _may_manage_group(request, workspace):
    """Return whether the viewer created the group or is admin/staff."""
    return workspace.created_by_id == request.user.id or role_flags(request)['is_admin_staff']


def _is_preview(request):
    """Return whether the viewer is a preview (student view) account, for which study groups are read-only."""
    person = getattr(request.user, 'profile', None)
    return bool(person and person.is_preview)


def _picker_context(request, form, workspace=None):
    """What the searchable member picker needs: the rows and who is ticked.

    Someone already in the group who is no longer in the user's scope is still
    listed, so the editor can see — and untick — them.
    """
    from . import study_groups
    rows = study_groups.member_rows(request)
    if form.is_bound:
        chosen = {str(v) for v in form.data.getlist('members')}
    elif workspace is not None:
        chosen = {str(pk) for pk in workspace.members.values_list('pk', flat=True)}
    else:
        chosen = set()
    if workspace is not None:
        listed = {row['user'].pk for row in rows}
        for user in workspace.members.select_related('profile'):
            if user.pk not in listed and user.pk != request.user.pk:
                rows.append({'user': user, 'programmes': []})
    for row in rows:
        row['checked'] = str(row['user'].pk) in chosen
    return {'member_rows': rows, 'chosen_count': sum(1 for r in rows if r['checked'])}


def _tell_new_members(workspace, before, by):
    """A notification for everyone just added to ``workspace`` (not the person adding them)."""
    from core.utils import display_name
    for member in workspace.members.exclude(pk__in=before | {by.pk}):
        services.notify(member, actor=by, title=f'You were added to {workspace.name}',
                        body=f'{display_name(by)} added you to the study group {workspace.name}.',
                        verb='added you to a study group',
                        url=reverse('communication:workspace-detail', args=[workspace.pk]))


@login_required
def workspaces(request):
    """The study groups page: the viewer's groups, the others they may see, and the "new group" form.

    A POST creates a group (not in student view) together with its own chat —
    talking happens on its page — and tells the people added.
    """
    if request.method == 'POST':
        if _is_preview(request):
            messages.info(request, 'Study groups are read-only in student view.')
            return redirect('communication:workspaces')
        form = forms.WorkspaceForm(request.POST, request=request)
        if form.is_valid():
            workspace = form.save(commit=False)
            workspace.created_by = request.user
            workspace.save()
            form.save_m2m()
            from . import study_groups
            study_groups.ensure_chat(workspace)
            _tell_new_members(workspace, set(), request.user)
            messages.success(request, t('messages.created', '{name} created.', name='Study group'))
            return redirect('communication:workspace-detail', pk=workspace.pk)
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        form = forms.WorkspaceForm(request=request)

    visible = _study_group_cards(_visible_workspaces(request))
    user = request.user
    mine = visible.filter(Q(created_by=user) | Q(members=user)).distinct()
    mine_ids = set(mine.values_list('pk', flat=True))
    others = [ws for ws in visible[:100] if ws.pk not in mine_ids]
    context = {
        'page_title': 'Study groups', 'form': form,
        'my_groups': _count_people(mine[:100]), 'other_groups': _count_people(others),
        'open_new': form.is_bound or request.GET.get('new') == '1',
        'read_only': _is_preview(request),
    }
    context.update(_picker_context(request, form))
    return render(request, 'communication/workspaces.html', context)


def _study_group_chat_context(request, workspace):
    """The group's chat, embedded on its page: one place to see it and talk in it."""
    from . import study_groups
    if not workspace.is_active:
        return {'active_group': None, 'chat_group': workspace.chat_group}
    try:
        study_groups.ensure_chat(workspace)
        study_groups.join_chat(workspace, request.user)
        group = services.decorate_group(
            models.ChatGroup.objects.prefetch_related('memberships__user__profile')
            .get(pk=workspace.chat_group_id), request.user)
    except Exception:
        logger.exception('study group %s: could not open its chat', workspace.pk)
        return {'active_group': None, 'chat_group': None}
    return {'active_group': group, 'chat_group': group, 'dm_gate': None}


@login_required
def workspace_detail(request, pk):
    """One study group's page: files, notes, members, settings and its embedded chat.

    Only people who may open the group get in, and a closed group only opens for
    those who may manage it. POST ``action`` is one of ``file`` (a re-upload with
    the same title bumps the version), ``note``, ``settings`` (managers only, not
    in student view), ``leave`` or ``close`` (managers only).

    The new note is held in ``group_note``, never ``note``: assigning ``note``
    anywhere in this function would shadow the :func:`core.errors.note` helper
    used by the file branch.
    """
    workspace = get_object_or_404(models.Workspace.objects.select_related(
        'module__programme__institution', 'module__module', 'created_by__profile'), pk=pk)
    if not workspace.can_access(request.user) or (not workspace.is_active
                                                  and not _may_manage_group(request, workspace)):
        note('USER-2001', request, workspace=pk)
        messages.error(request, 'You do not have access to that study group.')
        return redirect('communication:workspaces')

    may_manage = _may_manage_group(request, workspace)
    settings_form = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'file':
            form = forms.WorkspaceFileForm(request.POST, request.FILES)
            if form.is_valid():
                workspace_file = form.save(commit=False)
                workspace_file.workspace = workspace
                workspace_file.uploaded_by = request.user
                if workspace_file.title:
                    previous = workspace.files.filter(title=workspace_file.title).order_by('-version').first()
                    if previous:
                        workspace_file.version = previous.version + 1
                workspace_file.save()
                workspace.save(update_fields=['updated_at'])
                messages.success(request, 'File uploaded.')
            else:
                note('FILE-1001', request, workspace=pk, errors=list(form.errors))
                messages.error(request, 'Could not upload that file.')
        elif action == 'note':
            form = forms.WorkspaceNoteForm(request.POST)
            if form.is_valid():
                group_note = form.save(commit=False)
                group_note.workspace = workspace
                group_note.author = request.user
                group_note.save()
                workspace.save(update_fields=['updated_at'])
                messages.success(request, 'Note added.')
            else:
                messages.error(request, 'Could not save that note.')
        elif action == 'settings' and may_manage and not _is_preview(request):
            before = set(workspace.members.values_list('pk', flat=True))
            settings_form = forms.WorkspaceForm(request.POST, instance=workspace, request=request)
            if settings_form.is_valid():
                settings_form.save()
                from . import study_groups
                study_groups.ensure_chat(workspace)
                _tell_new_members(workspace, before, request.user)
                messages.success(request, 'Study group updated.')
                return redirect('communication:workspace-detail', pk=workspace.pk)
            messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
        elif action == 'leave' and workspace.members.filter(pk=request.user.pk).exists():
            workspace.members.remove(request.user)
            from . import study_groups
            study_groups.leave_chat(workspace, request.user)
            messages.success(request, 'You have left {}.'.format(workspace.name))
            return redirect('communication:workspaces')
        elif action == 'close' and may_manage:
            workspace.is_active = False
            workspace.save(update_fields=['is_active', 'updated_at'])
            from . import study_groups
            study_groups.sync_chat(workspace)
            messages.success(request, 'Study group closed.')
            return redirect('communication:workspaces')
        if settings_form is None:
            return redirect('communication:workspace-detail', pk=workspace.pk)

    if settings_form is None and may_manage:
        settings_form = forms.WorkspaceForm(instance=workspace, request=request)
    context = _study_group_chat_context(request, workspace)
    context.update({
        'page_title': workspace.name, 'workspace': workspace,
        'files': workspace.files.select_related('uploaded_by__profile'),
        'notes': workspace.notes.select_related('author__profile'),
        'members': workspace.members.select_related('profile').order_by('profile__first_name'),
        'is_member': workspace.members.filter(pk=request.user.pk).exists(),
        'may_manage': may_manage, 'read_only': _is_preview(request),
        'file_form': forms.WorkspaceFileForm(), 'note_form': forms.WorkspaceNoteForm(),
        'form': settings_form, 'open_settings': bool(settings_form is not None and settings_form.is_bound),
    })
    if settings_form is not None:
        context.update(_picker_context(request, settings_form, workspace))
    return render(request, 'communication/workspace-detail.html', context)


@login_required
def mail_inbox(request):
    """Old in-system e-mail inbox URL; e-mail moved to Metanoia Mail (:mod:`apps.mailbox`, ``/mail/``).

    This and the other ``mail_*`` views only keep old links working.
    """
    return redirect('mailbox:inbox')


@login_required
def mail_compose(request):
    """Old compose URL: redirect to Metanoia Mail's composer."""
    return redirect('mailbox:compose')


@login_required
def mail_read(request, pk):
    """Old message URL: redirect to the Metanoia Mail inbox."""
    return redirect('mailbox:inbox')


@login_required
def mail_action(request, pk):
    """Old message-action URL: redirect to the Metanoia Mail inbox."""
    return redirect('mailbox:inbox')


def _infer_media_kind(uploaded):
    """Classify an upload as ``image``, ``video`` or ``file`` by content type or extension."""
    ct = (getattr(uploaded, 'content_type', '') or '').lower()
    name = (getattr(uploaded, 'name', '') or '').lower()
    if ct.startswith('image') or name.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
        return 'image'
    if ct.startswith('video') or name.endswith(('.mp4', '.webm', '.mov', '.ogg')):
        return 'video'
    return 'file'


@login_required
@require_POST
def feed_create(request):
    """Post to a community from the composer (text + photos/videos/files).

    The post goes to the community picked in the composer, which must be one the
    poster belongs to (default: the Metanoia Community). Community members are
    rate-limited and may attach photos only; people tagged with ``@Name`` are
    notified (see apps.communication.community).

    * A post may carry several attachments (photos / videos / documents), all
      posted under the same ``media`` field name by the composer; every file
      becomes its own self-classifying attachment row.
    * ``FeedAttachment(...).save()`` skips the field's validators, so each file is
      checked here: a photo or video may be up to the media limit, a document
      must be an attachment type.
    * The post's headline kind reflects its first attachment (text if none).
    * The post is filed under a module only when the poster belongs to it, so
      the feed's module filter can find it (blank = a general post).
    """
    from . import community as cm

    body = (request.POST.get('body') or '').strip()
    files = request.FILES.getlist('media')
    visibility = request.POST.get('visibility') or models.Feed.VIS_PUBLIC
    if visibility not in dict(models.Feed.VISIBILITY_CHOICES):
        visibility = models.Feed.VIS_PUBLIC
    if not body and not files:
        note('FEED-1001', request)
        messages.error(request, 'Write something or attach a file to post.')
        return redirect_back(request, 'pages:dashboard')
    community = _posting_community(request)
    if community is None:
        messages.error(request, 'You can only post in a community you belong to.')
        return redirect_back(request, 'pages:dashboard')
    limited = cm.rate_limit_error(request.user, 'post')
    if limited:
        messages.error(request, limited)
        return redirect_back(request, 'pages:dashboard')
    if cm.is_member_role(request.user):
        if any(_infer_media_kind(f) != 'image' for f in files):
            messages.error(request, 'Community members can attach photos only.')
            return redirect_back(request, 'pages:dashboard')
        if len(files) > cm.MEMBER_MAX_ATTACHMENTS:
            messages.error(request, f'You can attach up to {cm.MEMBER_MAX_ATTACHMENTS} photos to a post.')
            return redirect_back(request, 'pages:dashboard')
    from core import validators as v
    rejected = [f'{f.name}: {" ".join(errs)}' for f in files
                for errs in [v.upload_errors(f, v.validate_media)
                             and v.upload_errors(f, v.validate_attachment)] if errs]
    if rejected:
        messages.error(request, 'Not posted — ' + '; '.join(rejected))
        return redirect_back(request, 'pages:dashboard')
    first_kind = _infer_media_kind(files[0]) if files else ''
    kind = first_kind if first_kind else models.Feed.KIND_TEXT
    module_id = (request.POST.get('module') or '').strip()
    if module_id.isdigit():
        from .feed import user_modules
        if not user_modules(request.user).filter(pk=int(module_id)).exists():
            module_id = ''
    else:
        module_id = ''
    post = models.Feed.objects.create(
        author=request.user, body=body, media_kind=first_kind, community=community,
        kind=kind, visibility=visibility, module_id=int(module_id) if module_id else None,
    )
    for upload in files:
        models.FeedAttachment(feed=post, file=upload).save()
    tagged = cm.resolve_mentions(request.user, community, body, request.POST.getlist('mentions'))
    cm.record_mentions(post, tagged, actor=request.user)
    messages.success(request, f'Posted to {community.name}.')
    return redirect(cm.feed_url(community))


def _posting_community(request):
    """The community picked in a form (``community`` = slug or id), if the
    user belongs to it; the default community when none was picked."""
    from . import community as cm
    raw = (request.POST.get('community') or '').strip()
    mine = cm.communities_for(request.user)
    if not raw:
        return models.Community.default()
    lookup = {'pk': int(raw)} if raw.isdigit() else {'slug': raw}
    return mine.filter(**lookup).first()


def _visible_post(request, pk):
    """A post the user may see (404 otherwise)."""
    return get_object_or_404(models.Feed.visible_to(request.user), pk=pk)


@login_required
def alert_counts(request):
    """Tiny JSON of the viewer's unread counts, polled by the client so a sound
    can play the moment a new message or notification arrives while they are on
    the site (see the poller in chrome.js). Kept deliberately cheap — two counts,
    two queries — because it runs on a short interval.
    """
    user = request.user
    try:
        notifications = models.Notification.objects.filter(recipient=user, is_read=False).count()
        messages_unread = models.ChatMembership.unread_total_for(user)
    except Exception:
        notifications, messages_unread = 0, 0
    return JsonResponse({'notifications': notifications, 'messages': messages_unread})


@login_required
def alerts_bell(request):
    """The bell dropdown's list, redrawn by chrome.js when an alert lands.

    Also returns the newest unread row so the page can show it as a toast, and
    the unread total for the bell's badge.

    The counters cache is invalidated first: the alert that brought the page here is news.
    """
    from django.template.loader import render_to_string

    from core.counters import invalidate_counters

    from .context_processors import comm_stats
    invalidate_counters(request.user)
    stats = comm_stats(request)['comm_stats']
    rows = list(stats['alerts'])
    html = render_to_string('partials/_bell_items.html', {'comm_stats': stats}, request=request)
    latest = next((r for r in rows if r.get('unread')), None)
    return JsonResponse({
        'html': html, 'total': stats['alerts_total'],
        'latest': ({'title': latest['title'], 'body': latest['body'], 'url': latest['url'],
                    'kind': latest['kind'], 'key': f"{latest['kind']}-{latest['url']}"} if latest else None),
    })


@login_required
def push_key(request):
    """The VAPID public key a browser needs to subscribe ('' when Web Push is off)."""
    from . import webpush
    subscribed = False
    endpoint = request.GET.get('endpoint', '')
    if endpoint:
        subscribed = models.WebPushSubscription.objects.filter(user=request.user, endpoint=endpoint).exists()
    return JsonResponse({'key': webpush.public_key(), 'subscribed': subscribed})


@login_required
@require_POST
def push_subscribe(request):
    """Save this browser's push subscription for the signed-in user."""
    import json
    try:
        data = json.loads(request.body or b'{}')
        endpoint = str(data['endpoint'])
        keys = data.get('keys') or {}
        p256dh, auth = str(keys['p256dh']), str(keys['auth'])
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'ok': False, 'error': 'That subscription is incomplete.'}, status=400)
    if not endpoint.startswith('https://') or len(endpoint) > 1000:
        return JsonResponse({'ok': False, 'error': 'That push address is not valid.'}, status=400)
    models.WebPushSubscription.objects.update_or_create(
        user=request.user, endpoint=endpoint,
        defaults={'p256dh': p256dh[:255], 'auth': auth[:255],
                  'user_agent': request.META.get('HTTP_USER_AGENT', '')[:300]})
    return JsonResponse({'ok': True})


@login_required
@require_POST
def push_unsubscribe(request):
    """Forget this browser's push subscription (or, with no endpoint posted, all of the user's)."""
    import json
    try:
        endpoint = str(json.loads(request.body or b'{}').get('endpoint', ''))
    except ValueError:
        endpoint = ''
    qs = models.WebPushSubscription.objects.filter(user=request.user)
    if endpoint:
        qs = qs.filter(endpoint=endpoint)
    qs.delete()
    return JsonResponse({'ok': True})


def service_worker(request):
    """``/sw.js``: the service worker that shows desktop alerts (scope: the whole site)."""
    from django.http import HttpResponse
    from django.template.loader import render_to_string
    from django.templatetags.static import static
    js = render_to_string('communication/sw.js', {
        'icon': static('images/brand/metanoia-tick-app.png'),
    })
    response = HttpResponse(js, content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


@login_required
@require_POST
def feed_like(request, pk):
    """Toggle a like on a feed post.

    Answers XHR with the new state so the card can update its counter in place —
    liking a post must never navigate away from the feed. A plain form post (no
    JavaScript) still falls back to a redirect.
    """
    post = _visible_post(request, pk)
    liked, count = post.toggle_like(request.user)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'liked': liked, 'likes': count})
    return redirect_back(request, 'pages:dashboard')


@login_required
@require_POST
def feed_comment(request, pk):
    """Add a comment to a feed post (the card's inline comment thread)."""
    from . import community as cm

    post = _visible_post(request, pk)
    xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    body = (request.POST.get('body') or '').strip()
    if not body:
        note('FEED-1002', request, post=pk)
        return JsonResponse({'ok': False, 'error': 'empty'}, status=400)
    limited = cm.rate_limit_error(request.user, 'comment')
    if limited:
        if xhr:
            return JsonResponse({'ok': False, 'error': limited}, status=429)
        messages.error(request, limited)
        return redirect_back(request, 'pages:dashboard')
    comment = models.FeedComment.objects.create(post=post, author=request.user, body=body)
    tagged = cm.resolve_mentions(request.user, post.community or models.Community.default(),
                                 body, request.POST.getlist('mentions'))
    cm.record_mentions(post, tagged, actor=request.user, comment=comment)
    if xhr:
        from core.utils import avatar_url, display_name as _name
        return JsonResponse({
            'ok': True,
            'comment': {
                'id': comment.pk,
                'author': _name(request.user),
                'avatar': avatar_url(request.user),
                'body': comment.body,
                'html': cm.highlight_mentions(comment.body, [f'@{cm.mention_label(u)}' for u in tagged]),
                'when': 'just now',
            },
            'count': post.comments.count(),
        })
    return redirect_back(request, 'pages:dashboard')


@login_required
@require_POST
def feed_share(request, pk):
    """Share a post into another community the sharer belongs to.

    Sharing a share passes on the original, not a chain of wrappers.
    """
    from . import community as cm

    original = _visible_post(request, pk)
    if original.shared_from_id and original.shared_from and not original.shared_from.is_removed:
        original = original.shared_from
    if original.is_removed:
        messages.error(request, 'That post has been removed and cannot be shared.')
        return redirect_back(request, 'pages:dashboard')
    target = _posting_community(request)
    if target is None:
        messages.error(request, 'You can only share to a community you belong to.')
        return redirect_back(request, 'pages:dashboard')
    if (original.community_id or models.Community.default().pk) == target.pk:
        messages.info(request, f'That post is already in {target.name}.')
        return redirect_back(request, 'pages:dashboard')
    limited = cm.rate_limit_error(request.user, 'post')
    if limited:
        messages.error(request, limited)
        return redirect_back(request, 'pages:dashboard')
    body = (request.POST.get('body') or '').strip()
    share = models.Feed.objects.create(author=request.user, body=body, community=target,
                                       shared_from=original, kind=models.Feed.KIND_TEXT)
    tagged = cm.resolve_mentions(request.user, target, body, request.POST.getlist('mentions'))
    cm.record_mentions(share, tagged, actor=request.user)
    messages.success(request, f'Shared to {target.name}.')
    return redirect(cm.feed_url(target))


@login_required
@require_POST
def feed_report(request, pk):
    """Report a post to the Metanoia team."""
    from . import community as cm

    post = _visible_post(request, pk)
    xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    error, done = None, ''
    if post.author_id == request.user.pk:
        error = 'You cannot report your own post — delete it instead.'
    else:
        error = cm.rate_limit_error(request.user, 'report')
    if error is None:
        _, created = cm.report_post(post, request.user, request.POST.get('reason'),
                                    request.POST.get('note', ''))
        done = ('Thank you — the Metanoia team will review this post.' if created
                else 'You have already reported this post.')
    if xhr:
        return JsonResponse({'ok': error is None, 'message': error or done},
                            status=400 if error else 200)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, done)
    return redirect_back(request, 'pages:dashboard')


@login_required
@require_POST
def feed_delete(request, pk):
    """An author deletes their own post."""
    post = get_object_or_404(models.Feed, pk=pk, author=request.user)
    post.delete()
    messages.success(request, 'Your post was deleted.')
    return redirect_back(request, 'pages:dashboard')


def _moderator_required(view):
    """Decorator: signed-in community moderators only; anyone else is sent to the dashboard with a message."""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        """Run ``view`` for moderators, otherwise redirect."""
        from . import community as cm
        if not cm.is_moderator(request.user):
            messages.error(request, 'Only the Metanoia team can moderate the community.')
            return redirect('pages:dashboard')
        return view(request, *args, **kwargs)
    return login_required(wrapped)


@_moderator_required
@require_POST
def feed_remove(request, pk):
    """Staff remove a post from the community (it can be restored)."""
    from . import community as cm
    post = get_object_or_404(models.Feed, pk=pk)
    cm.remove_post(post, request.user, request.POST.get('reason', ''))
    messages.success(request, 'The post was removed from the community.')
    return redirect_back(request, 'pages:dashboard')


@_moderator_required
@require_POST
def feed_restore(request, pk):
    """Staff put a removed post back."""
    from . import community as cm
    post = get_object_or_404(models.Feed, pk=pk)
    cm.restore_post(post, request.user)
    messages.success(request, 'The post is back in the community.')
    return redirect_back(request, 'communication:community-moderation')


@_moderator_required
@require_POST
def feed_dismiss_reports(request, pk):
    """Staff keep a reported post: its open reports are closed as dismissed."""
    post = get_object_or_404(models.Feed, pk=pk)
    post.reports.filter(status=models.FeedReport.STATUS_OPEN).update(
        status=models.FeedReport.STATUS_DISMISSED, handled_by=request.user, handled_at=timezone.now())
    messages.success(request, 'Reports dismissed — the post stays up.')
    return redirect_back(request, 'communication:community-moderation')


@login_required
@require_POST
def feed_comment_remove(request, pk):
    """Staff remove any comment; an author removes their own."""
    from . import community as cm
    comment = get_object_or_404(models.FeedComment, pk=pk)
    if not (cm.is_moderator(request.user) or comment.author_id == request.user.pk):
        messages.error(request, 'You cannot remove that comment.')
    else:
        comment.delete()
        messages.success(request, 'Comment removed.')
    return redirect_back(request, 'pages:dashboard')


def community_moderation_context():
    """What the "Community posts" tab of the Moderation page shows: reported
    posts first (most-reported first), then the latest removed ones."""
    reported = (models.Feed.objects.filter(reports__status=models.FeedReport.STATUS_OPEN)
                .annotate(open_reports=Count('reports', filter=Q(reports__status='open')))
                .select_related('author__profile', 'community')
                .prefetch_related(Prefetch('reports', queryset=models.FeedReport.objects
                                           .select_related('reporter__profile')))
                .distinct().order_by('-open_reports', '-created_at'))
    removed = (models.Feed.objects.filter(is_removed=True)
               .select_related('author__profile', 'community', 'removed_by__profile')
               .order_by('-removed_at')[:50])
    return {'reported': reported, 'removed': removed}


@_moderator_required
def community_moderation(request):
    """Community moderation now lives in the one Moderation page, as its
    "Community posts" tab. The old address keeps working."""
    return redirect(f"{reverse('staffdesk:ops-moderation')}?tab=community")


@login_required
def community_mentions(request):
    """People to tag: members of a community whose name starts with ``q``."""
    from . import community as cm
    from core.utils import avatar_url
    community = cm.communities_for(request.user).filter(
        slug=(request.GET.get('c') or '').strip()).first() or models.Community.default()
    people = cm.search_members(community, request.GET.get('q', ''), exclude=request.user,
                                viewer=request.user)
    return JsonResponse({'results': [
        {'id': u.pk, 'label': cm.mention_label(u), 'avatar': avatar_url(u),
         'role': getattr(getattr(u, 'profile', None), 'get_user_type_display', lambda: '')()}
        for u in people]})


@login_required
@require_POST
def community_explore(request):
    """Switch "Explore other communities" on (``on=1``) or off.

    Off is the default: the general Metanoia Community and your own. On, the
    other communities are listed beside the feed to join. Staff are in every
    community already, so there is nothing for them to switch.
    """
    from . import community as cm
    if cm.is_moderator(request.user):
        return redirect('pages:dashboard')
    on = request.POST.get('on') == '1'
    cm.set_explore(request.user, on)
    if on:
        messages.success(request, 'Exploring is on — join any other community from the list beside the feed.')
    else:
        messages.success(request, 'Exploring is off — you are back to the Metanoia Community and your own.')
    return redirect('pages:dashboard')


@login_required
@require_POST
def community_join(request, slug):
    """Join another community while exploring."""
    from . import community as cm
    community = models.Community.objects.filter(slug=slug, is_active=True).first()
    if not cm.join_community(request.user, community):
        messages.error(request, 'Switch on "Explore other communities" first, then choose one to join.'
                       if not cm.explores(request.user) else 'You cannot join that community.')
        return redirect('pages:dashboard')
    messages.success(request, f'You joined {community.name}.')
    return redirect(cm.feed_url(community))


@login_required
@require_POST
def community_leave(request, slug):
    """Leave a community joined while exploring."""
    from . import community as cm
    community = get_object_or_404(models.Community, slug=slug)
    if cm.leave_community(request.user, community):
        messages.success(request, f'You left {community.name}.')
    else:
        messages.error(request, 'That is one of your own communities — it comes with your programme.')
    return redirect('pages:dashboard')


@login_required
def message_metanoia(request):
    """Open the member's private conversation with the Metanoia team."""
    from . import community as cm
    if cm.is_moderator(request.user):
        messages.info(request, 'Message Metanoia threads from community members appear in your Messages.')
        return redirect('communication:chat-home')
    group = cm.support_chat_for(request.user)
    return redirect('communication:chat-room', group_id=group.pk)


def _back(request):
    """Redirect to the page the wall action came from (default: My Hub)."""
    return redirect_back(request, 'myhub:index')


def _wall_title(body):
    """Discussions need a title; a wall post is just a body. Take the first line."""
    first = (body or '').strip().splitlines()[0] if (body or '').strip() else ''
    return (first[:97] + '…') if len(first) > 100 else (first or 'Post')


@login_required
@require_POST
def wall_post(request):
    """Publish a post to a wall (the composer on a profile / course / module)."""
    body = (request.POST.get('body') or '').strip()
    files = request.FILES.getlist('attachments')
    if not body and not files:
        messages.error(request, 'Write something or attach a file to post.')
        return _back(request)

    discussion = models.Discussion.objects.create(
        author=request.user,
        title=_wall_title(body),
        body=body,
        module_id=request.POST.get('module') or None,
    )
    _save_attachments(request, discussion=discussion)
    messages.success(request, 'Posted.')
    return _back(request)


@login_required
@require_POST
def wall_like(request, pk):
    """Toggle a like on a wall post.

    Answers XHR with the new state so the wall can tick the counter in place.
    Reloading the page to register a like threw the reader back to the top of a
    long wall and lost every open comment thread. The redirect is kept for a
    no-JavaScript post.
    """
    post = get_object_or_404(models.Discussion, pk=pk)
    if post.likes.filter(pk=request.user.pk).exists():
        post.likes.remove(request.user)
        liked = False
    else:
        post.likes.add(request.user)
        liked = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'ok': True, 'liked': liked, 'likes': post.likes.count()})
    return _back(request)


@login_required
@require_POST
def wall_comment(request, pk):
    """Comment on a wall post — or reply to a comment when ``reply_to`` is given."""
    post = get_object_or_404(models.Discussion, pk=pk)
    body = (request.POST.get('body') or '').strip()
    if not body:
        return _back(request)

    parent = None
    reply_to = request.POST.get('reply_to')
    if reply_to:
        parent = models.DiscussionReply.objects.filter(pk=reply_to, discussion=post).first()

    reply = models.DiscussionReply.objects.create(
        discussion=post, author=request.user, body=body, reply_to=parent,
    )
    _save_attachments(request, reply=reply)
    return _back(request)


@login_required
@require_POST
def wall_comment_like(request, pk):
    """Toggle a like on a comment."""
    reply = get_object_or_404(models.DiscussionReply, pk=pk)
    if reply.likes.filter(pk=request.user.pk).exists():
        reply.likes.remove(request.user)
    else:
        reply.likes.add(request.user)
    return _back(request)
