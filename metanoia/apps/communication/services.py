"""Business logic for the communication app.

Kept out of the models / views so it can be reused and tested in isolation:

* :func:`ensure_chat_group_for_module` — lazily create the auto-managed group
  for a module offering.
* :func:`sync_module_chat_members`
  — reconcile a group's :class:`~apps.communication.models.ChatMembership` rows
  with current enrolment. A module's room is reconciled against *access* (paid
  or on trial), not mere registration, so the chat list only ever shows the
  modules a student can actually open.
* :func:`notify` — create a single :class:`~apps.communication.models.Notification`.
* :func:`create_mention_notifications` — fan a message's @mentions out into
  notifications.
* :func:`resolve_announcement_recipients` / :func:`send_announcement`
  — turn an :class:`~apps.communication.models.Announcement` into notifications
  (and, optionally, e-mails).

The module is organised in sections: chat groups and auto-membership,
direct-message connection requests (the student ↔ student gate),
notifications, announcements, community safety and moderation, attendance
(manual and automatic, time-based), and deadline / live-session reminders.

Module constants:

``_MENTION_RE``
    Matches ``@username`` tokens in a message body (letters, digits, dot,
    ``_``, ``-``, ``@``, ``+``).
``AUTOMATED_EMAIL_KIND``
    Categories whose notices, raised by the system rather than sent by a
    person, are *system notifications*: bell and desktop alert only. Each maps
    to the opt-in switch (``NotificationPreference.email_reminders`` /
    ``email_updates``) that brings an e-mail copy back. A notice someone on the
    team sent (``actor`` set — a coach's nudge, a cancelled class) keeps its
    e-mail.
``_MODERATION_RULES``
    A small local ruleset (category → severity, terms). Real deployments extend
    this list or plug in the local AI (see :func:`moderate_text`). Kept
    deliberately mild in source; severity 4 = severe, 2 = moderate,
    1 = mild/profanity.
``PRESENCE_MAX_GAP_SECONDS``
    Longest stretch a single heartbeat may credit. The client pings faster than
    this; the cap means a closed laptop or a sleeping tab cannot silently
    accrue hours between two pings.
"""

import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from .models import Announcement, ChatGroup, MessageMention, Notification

logger = logging.getLogger(__name__)
User = get_user_model()

_MENTION_RE = re.compile(r'(?<!\w)@([\w.@+\-]{1,150})')


def _users_for_people(people_qs):
    """Map an ``accounts.Person`` queryset to the related ``User`` queryset."""
    user_ids = list(people_qs.values_list('user_id', flat=True))
    return User.objects.filter(pk__in=user_ids)


def decorate_group(group, user):
    """Attach the display-only bits the chat UI needs to draw a conversation.

    ``peers`` are the members other than ``user`` — for a 1:1 that is the one
    person whose face and name the row (and the thread header) should carry; for
    a group it is everyone else, which the header lists and the avatar stacks.
    Nothing is persisted.

    Lives here rather than in the chat view because a thread is now embedded in
    more than one page (the Messages page and a module feed's Messages tab), and
    a half-decorated group renders a header with no name on it.

    A 1:1 conversation is titled — and pictured — by the person you are talking
    to, never by the auto-generated "a, b" group name. The member's own Message
    Metanoia thread is "the Metanoia team", not a list of every staff member who
    happens to be in it.
    """
    from core.utils import avatar_url, display_name as _name, initials as _initials

    memberships = list(group.memberships.all())
    group.member_count = len(memberships)
    peers = [m.user for m in memberships if m.user_id != user.pk and m.user]
    group.peers = peers
    group.peer_names = [_name(p) for p in peers]
    group.is_direct = group.kind == ChatGroup.KIND_DIRECT
    lead = peers[0] if (group.is_direct and peers) else None
    group.lead_peer = lead
    group.chat_title = _name(lead) if lead else group.display_name
    group.avatar = avatar_url(lead) if lead else None
    group.avatar_initials = _initials(lead) if lead else (group.chat_title or '?')[:1].upper()
    if group.kind == ChatGroup.KIND_SUPPORT and group.created_by_id == user.pk:
        group.chat_title = 'Message Metanoia'
        group.peer_names = ['The Metanoia team']
        group.avatar_initials = 'T'
    return group


def ensure_chat_group_for_module(offering):
    """The group conversation for one :class:`~apps.learning.models.ProgrammeModule`.

    The link lives on the offering (``ProgrammeModule.chat_group``) rather than
    on the group, because ``learning`` already depends on ``communication`` and
    not the other way round — the same direction ``LessonBlock.meeting`` runs in.
    """
    group = offering.chat_group
    if group is not None:
        return group
    group = ChatGroup.objects.create(
        kind=ChatGroup.KIND_SUBJECT,
        name=f'{offering.label} · {offering.display_name}',
        description=f'Everyone studying {offering.display_name} at '
                    f'{offering.programme.institution.display_name}.',
    )
    offering.chat_group = group
    offering.save(update_fields=['chat_group', 'updated_at'])
    return group


def module_chat_users(offering):
    """Who belongs in an offering's chat: its coaches, and students with access.

    "Access" is the same test the module feed uses — paid or on a live trial. A
    student who never paid, or whose free week has run out, is not in the room:
    the conversation is part of what the module costs. Paying puts them back in
    with the whole history intact, because only the membership row is removed.

    One-on-one students are left out: they talk in their own private space
    instead (:func:`apps.shop.one_on_one.ensure_coaching_group`).
    """
    from apps.learning.models import ModuleEnrolment

    user_ids = set(offering.educators.values_list('user_id', flat=True))
    for enrolment in (ModuleEnrolment.objects
                      .filter(programme_module=offering)
                      .select_related('person')):
        if enrolment.is_unlocked and not enrolment.person.is_one_on_one:
            user_ids.add(enrolment.person.user_id)
    return User.objects.filter(pk__in=user_ids, is_active=True)


def sync_module_chat_members(offering):
    """Reconcile an offering's chat group with everyone who currently has access."""
    if not offering.is_active:
        return None
    group = ensure_chat_group_for_module(offering)
    _reconcile_members(group, list(module_chat_users(offering)))
    return group


def _reconcile_members(group, target_users):
    """Make ``group``'s *auto* memberships exactly equal ``target_users``.

    Enrolled users who aren't members yet are added (``is_auto=True``); auto
    members who are no longer enrolled are removed. Manually-added members
    (``is_auto=False``) are left completely untouched.
    """
    target_ids = {u.pk for u in target_users}
    existing_ids = set(group.memberships.values_list('user_id', flat=True))

    for user in target_users:
        if user.pk not in existing_ids:
            group.memberships.create(user=user, role='member', is_auto=True)

    group.memberships.filter(is_auto=True).exclude(user_id__in=target_ids).delete()
    group.save(update_fields=['updated_at'])


def ensure_general_group(user=None):
    """The single platform-wide 'General' chat. Adds ``user`` as a member."""
    group, _ = ChatGroup.objects.get_or_create(
        kind=ChatGroup.KIND_CUSTOM, name='General',
        defaults={'description': 'Everyone on the platform.'},
    )
    if user is not None and user.is_authenticated:
        group.add_member(user)
    return group


def get_or_create_direct_chat(user_a, user_b):
    """Find (or create) the 1:1 direct chat between two users."""
    existing = (ChatGroup.objects.filter(kind=ChatGroup.KIND_DIRECT,
                                         memberships__user=user_a)
                .filter(memberships__user=user_b).distinct().first())
    if existing:
        return existing
    group = ChatGroup.objects.create(kind=ChatGroup.KIND_DIRECT)
    group.add_member(user_a)
    group.add_member(user_b)
    return group


def other_member(group, user):
    """The other participant of a 1:1 direct ``group`` (None if not 1:1)."""
    if group is None or group.kind != ChatGroup.KIND_DIRECT:
        return None
    return (User.objects.filter(chat_memberships__group=group)
            .exclude(pk=user.pk).first())


def students_need_connection(user_a, user_b):
    """True when a 1:1 chat between these two users must be *approved* first.

    Only applies when BOTH users are students. Any chat involving an
    admin/staff/educator/parent — and every course/module/custom group — is
    open, so this returns False for those.
    """
    from core.roles import role_of_user
    if user_a is None or user_b is None or user_a.pk == user_b.pk:
        return False
    return role_of_user(user_a) == 'student' and role_of_user(user_b) == 'student'


def connection_between(user_a, user_b):
    """The :class:`ConnectionRequest` between two users (either direction), or None."""
    from django.db.models import Q
    from .models import ConnectionRequest
    return (ConnectionRequest.objects
            .filter(Q(from_user=user_a, to_user=user_b) | Q(from_user=user_b, to_user=user_a))
            .first())


def are_connected(user_a, user_b):
    """True if an *accepted* connection exists between the two users."""
    from .models import ConnectionRequest
    conn = connection_between(user_a, user_b)
    return bool(conn and conn.status == ConnectionRequest.STATUS_ACCEPTED)


def accepts_direct_messages(sender, recipient):
    """``recipient``'s "Let other members start a direct chat with me" setting.

    Staff, admins and educators can always reach someone (a class notice or a
    fee query must not bounce); the setting governs other members. A chat that
    already exists stays open either way: the setting stops new ones.
    """
    from core.roles import role_of_user
    if sender.is_staff or role_of_user(sender) in ('admin', 'staff', 'educator'):
        return True
    row = getattr(recipient, 'account_settings', None)
    if row is None:
        from apps.accounts.models import UserSettings
        row = UserSettings.objects.filter(user=recipient).only('allow_messages').first()
    return row is None or row.allow_messages


def can_direct_message(sender, recipient):
    """Whether ``sender`` may send a 1:1 direct message to ``recipient`` now."""
    if not accepts_direct_messages(sender, recipient):
        from .models import ChatGroup
        return (ChatGroup.objects.filter(kind=ChatGroup.KIND_DIRECT, memberships__user=sender)
                .filter(memberships__user=recipient).exists())
    if not students_need_connection(sender, recipient):
        return True
    return are_connected(sender, recipient)


def direct_gate_state(user, other):
    """Describe the connection gate between ``user`` and ``other`` for the UI.

    Returns ``None`` when no gate applies (they may chat freely). Otherwise a
    dict: ``state`` is one of ``connected`` / ``none`` / ``sent`` / ``incoming``
    / ``declined``; ``conn`` is the :class:`ConnectionRequest` (or None); the
    caller uses these to show a request / approve / pending banner.

    ``incoming`` means they asked me (I can approve or decline); ``sent`` means I asked them and am waiting.
    """
    from .models import ConnectionRequest
    if not students_need_connection(user, other):
        return None
    conn = connection_between(user, other)
    if conn is None:
        state = 'none'
    elif conn.status == ConnectionRequest.STATUS_ACCEPTED:
        state = 'connected'
    elif conn.status == ConnectionRequest.STATUS_DECLINED:
        state = 'declined'
    elif conn.to_user_id == user.pk:
        state = 'incoming'
    else:
        state = 'sent'
    return {'state': state, 'conn': conn, 'other': other}


def request_connection(from_user, to_user):
    """Create (or re-open) a pending connection request and notify the recipient.

    Returns ``(conn, created_or_reopened)``. A previously *declined* request is
    reopened as pending so people can try again.

    A reopened request, in either direction, becomes a fresh pending request from ``from_user``.
    """
    from .models import ConnectionRequest
    if not students_need_connection(from_user, to_user):
        return None, False
    existing = connection_between(from_user, to_user)
    if existing:
        if existing.status == ConnectionRequest.STATUS_ACCEPTED:
            return existing, False
        existing.from_user = from_user
        existing.to_user = to_user
        existing.status = ConnectionRequest.STATUS_PENDING
        existing.responded_at = None
        existing.save(update_fields=['from_user', 'to_user', 'status', 'responded_at'])
        conn = existing
    else:
        conn = ConnectionRequest.objects.create(from_user=from_user, to_user=to_user)

    from django.urls import reverse
    try:
        url = reverse('communication:chat-direct', args=[from_user.pk])
    except Exception:
        url = ''
    notify(
        to_user, actor=from_user, level='info', category='messages',
        title='New connection request',
        verb='wants to connect with you',
        body=f'{from_user.get_full_name() or from_user.get_username()} would like to message you. '
             f'Approve the request to start chatting.',
        url=url,
    )
    return conn, True


def respond_connection(conn, *, accept, by_user):
    """Accept or decline a pending request. Only the recipient may respond.

    Returns True on success. Notifies the original requester when accepted.

    Accepting also makes sure the 1:1 chat exists, so they can start straight away.
    """
    from .models import ConnectionRequest
    if conn is None or conn.to_user_id != by_user.pk or conn.status != ConnectionRequest.STATUS_PENDING:
        return False
    if accept:
        conn.accept()
        get_or_create_direct_chat(conn.from_user, conn.to_user)
        from django.urls import reverse
        try:
            url = reverse('communication:chat-direct', args=[by_user.pk])
        except Exception:
            url = ''
        notify(
            conn.from_user, actor=by_user, level='success', category='messages',
            title='Connection accepted',
            verb='accepted your connection request',
            body=f'{by_user.get_full_name() or by_user.get_username()} accepted your request — '
                 f'you can now message each other.',
            url=url,
        )
    else:
        conn.decline()
    return True


AUTOMATED_EMAIL_KIND = {
    'deadlines': 'reminders',
    'meetings': 'updates',
    'content': 'updates',
    'assessments': 'updates',
    'weekly': 'updates',
}


def notify(recipient, *, title='', body='', verb='', level='info', url='',
           actor=None, message=None, meeting=None, announcement=None, email=False,
           category=None, alert_kind='notification', desktop=False, email_kind=None):
    """Create (and return) one :class:`~apps.communication.models.Notification`.

    ``category`` (one of :attr:`NotificationPreference.CATEGORIES`) lets the
    recipient's :class:`~apps.communication.models.NotificationPreference`
    silence whole categories and decide whether an e-mail copy is sent. Passing
    no category keeps the old "always deliver" behaviour. If ``email`` is true
    (and the user's preferences allow it, outside quiet hours) a branded e-mail
    copy is sent — best effort; failures are logged, never raised.

    ``email_kind`` (``'reminders'`` / ``'updates'``) marks an automated notice:
    its e-mail copy goes only to people who opted in to that kind. Left out, it
    is worked out from :data:`AUTOMATED_EMAIL_KIND` for notices with no actor.

    Delivery channels, in order:

    * Nothing at all when the recipient opted out of the whole category.
    * A real-time push to the recipient's browser (sound and bell badge) — best
      effort; it falls back to polling when WebSockets are not running (see
      :func:`apps.communication.realtime.push_alert`).
    * With ``desktop``, a desktop (system) notification too, for the browsers
      that asked for one.
    * The e-mail copy, as above.
    * WhatsApp: the same notification, on the channel people actually read. It is
      opt-in per user and honours the same category switches and quiet hours as
      e-mail but, unlike e-mail, is not gated on the caller's ``email`` flag:
      someone who asked for WhatsApp asked for all of it.
    """
    if recipient is None:
        return None

    pref = _preference_for(recipient)
    if pref and category and not pref.allows(category):
        return None

    note = Notification.objects.create(
        recipient=recipient, actor=actor, verb=verb, title=title, body=body,
        level=level, url=url, message=message, meeting=meeting, announcement=announcement,
        category=category or '',
    )

    try:
        from .realtime import push_alert
        push_alert(recipient.pk, kind=alert_kind,
                   title=title or verb or 'Notification', body=(body or '')[:80], url=url)
    except Exception:
        pass
    if desktop:
        try:
            from .webpush import send_to_user
            send_to_user(recipient, title=title or verb or 'Notification', body=body or '',
                         url=reverse('communication:notification-detail', args=[note.pk]) + '?go=1',
                         tag=f'{alert_kind}-{note.pk}', kind=alert_kind)
        except Exception:
            pass

    if email_kind is None and actor is None:
        email_kind = AUTOMATED_EMAIL_KIND.get(category or '')
    if email and email_kind:
        if pref is not None and pref.allows_automated_email(email_kind, category) and not pref.in_quiet_hours():
            _email_notification(note, category)
    elif email and (pref is None or (pref.allows_email(category) and not pref.in_quiet_hours())):
        _email_notification(note, category)

    if pref is not None and pref.allows_whatsapp(category) and not pref.in_quiet_hours():
        _whatsapp_notification(note, category)
    return note


def _preference_for(user):
    """Return the user's NotificationPreference (creating defaults), or None on error."""
    from .models import NotificationPreference
    try:
        return NotificationPreference.for_user(user)
    except Exception:  # pragma: no cover - never let preferences break a notify
        logger.exception('Could not load notification preferences for %s', user)
        return None


def _email_notification(note, category=None):
    """Send the e-mail copy of ``note`` and mark it ``emailed``; failures are logged, never raised."""
    from .emails import send_notification_email
    try:
        if send_notification_email(note, category):
            note.emailed = True
            note.save(update_fields=['emailed'])
    except Exception:  # pragma: no cover - never let e-mail break the request
        logger.exception('Failed to e-mail notification #%s', note.pk)


def _whatsapp_notification(note, category=None):
    """Send a notification copy to the recipient's WhatsApp number.

    Best effort in exactly the way e-mail is: a phone that cannot be reached is
    logged and moved on from. The in-app notification has already been created,
    so the person has lost nothing. Free text or template, and the record on the
    conversation, are :func:`~apps.communication.whatsapp_inbox.send_notification_copy`'s.
    """
    from core import whatsapp

    if not whatsapp.enabled():
        return
    try:
        from .whatsapp_inbox import send_notification_copy
        if send_notification_copy(note, category):
            note.whatsapped = True
            note.save(update_fields=['whatsapped'])
    except Exception:  # pragma: no cover - never let WhatsApp break a notify
        logger.exception('Failed to WhatsApp notification #%s', note.pk)


def _strip_html(html):
    """Readable plain text from a system-authored HTML body."""
    from django.utils.html import strip_tags
    from django.utils.text import normalize_newlines

    return normalize_newlines(strip_tags(html)).strip()


def _absolute(url):
    """Make a notification's URL clickable from a phone.

    Uses the same ``SITE_URL`` the invoice e-mails do, so a link in a WhatsApp
    message and a link in an e-mail point at the same host.
    """
    from core.utils import email_link
    return email_link(url)


def _resolve_mentioned_users(message):
    """Users tagged in ``message`` — explicit MessageMention rows plus any
    ``@username`` tokens parsed from the body that match a member of the group."""
    users = {m.user for m in message.mention_links.select_related('user')}

    handles = {h.lower() for h in _MENTION_RE.findall(message.body or '')}
    if handles:
        member_users = User.objects.filter(chat_memberships__group=message.group)
        for u in member_users:
            uname = (u.get_username() or '').lower()
            email = (getattr(u, 'email', '') or '').lower()
            if uname in handles or email in handles or email.split('@')[0] in handles:
                users.add(u)
    users.discard(message.sender)
    return users


def create_mention_notifications(message):
    """Ensure every tagged user has a MessageMention + a notification for ``message``."""
    from core.utils import display_name
    sender_name = display_name(message.sender) if message.sender else 'Someone'
    group_name = message.group.display_name
    for user in _resolve_mentioned_users(message):
        MessageMention.objects.get_or_create(message=message, user=user)
        already = Notification.objects.filter(recipient=user, message=message,
                                              verb='mentioned you').exists()
        if not already:
            notify(
                user,
                actor=message.sender,
                verb='mentioned you',
                title=f'{sender_name} mentioned you in {group_name}',
                body=(message.body or '')[:300],
                level='info',
                url=f'/communication/chat/{message.group_id}/#m{message.pk}',
                message=message,
            )


def resolve_announcement_recipients(announcement):
    """Return the distinct, active ``User`` queryset an announcement targets."""
    from .broadcast import recipients_for_announcement
    return recipients_for_announcement(announcement)


def send_announcement(announcement):
    """Deliver an announcement: notifications, plus e-mail copies if asked.

    Returns the number of people reached. Safe to call more than once — only the
    first call sends (see :func:`apps.communication.broadcast.send`).
    """
    from .broadcast import send
    return send(announcement)


_MODERATION_RULES = {
    'hate_speech': (4, [r'\bk+i+l+l+\s+all\b', r'\bgenocide\b']),
    'threat': (4, [r'\bi(\'| a)?m going to (kill|hurt|beat)\b', r'\bkill yourself\b', r'\bkys\b']),
    'harassment': (3, [r'\bshut up\b', r'\bnobody likes you\b']),
    'bullying': (2, [r'\byou are (so )?(stupid|dumb|ugly|worthless)\b', r'\bidiot\b', r'\bloser\b']),
    'profanity': (1, [r'\bf+u+c+k', r'\bsh+i+t', r'\bb+i+t+c+h', r'\basshole\b', r'\bdamn\b']),
    'spam': (1, [r'(https?://\S+\s*){4,}', r'\b(buy now|free money|click here)\b']),
}


def moderate_text(text):
    """Scan ``text`` locally and return ``(score 0-4, category, matched_terms)``.

    Rule-based + regex (no cloud). If the local AI assistant is enabled it can be
    consulted for nuance, but the platform never sends content off the server.
    """
    text = (text or '')
    if not text.strip():
        return 0, '', []
    lowered = text.lower()
    worst_score, worst_cat, matches = 0, '', []
    for category, (severity, patterns) in _MODERATION_RULES.items():
        for pat in patterns:
            if re.search(pat, lowered):
                matches.append(category)
                if severity > worst_score:
                    worst_score, worst_cat = severity, category
    return worst_score, worst_cat, sorted(set(matches))


def active_silencing_penalty(user):
    """Return the user's current mute/suspension penalty, or None."""
    from .models import Penalty
    for pen in Penalty.objects.filter(user=user, active=True, kind__in=Penalty.SILENCING):
        if pen.is_current():
            return pen
    return None


def is_muted(user):
    """Return whether ``user`` is under a current silencing penalty (mute or suspension)."""
    return active_silencing_penalty(user) is not None


def _adjust_reputation(user, delta):
    """Move ``user``'s reputation by ``delta``, clamped to 0–100; return the new score."""
    from .models import ReputationScore
    rep, _ = ReputationScore.objects.get_or_create(user=user)
    rep.score = max(0, min(100, rep.score + delta))
    rep.save(update_fields=['score', 'updated_at'])
    return rep.score


def apply_penalty_ladder(user, violation):
    """Escalate penalties by prior offence count: warn → mute → suspend → escalate.

    Severe violations (score >= 4) jump straight to a mute. Returns the Penalty.
    """
    from datetime import timedelta
    from .models import Penalty

    prior = Penalty.objects.filter(user=user).count()
    if violation.score >= 4:
        kind, ends = Penalty.KIND_MUTE, timezone.now() + timedelta(hours=24)
    elif prior == 0:
        kind, ends = Penalty.KIND_WARNING, None
    elif prior == 1:
        kind, ends = Penalty.KIND_MUTE, timezone.now() + timedelta(hours=1)
    elif prior == 2:
        kind, ends = Penalty.KIND_CHAT_SUSPEND, timezone.now() + timedelta(days=3)
    else:
        kind, ends = Penalty.KIND_ESCALATE, None

    penalty = Penalty.objects.create(
        user=user, kind=kind, ends_at=ends, violation=violation,
        reason=f'Auto: {violation.category or "policy violation"} (score {violation.score})',
    )
    return penalty


def moderate_message(message):
    """Scan a chat message; on a hit record a Violation, drop reputation, apply a
    penalty, and soft-delete severe content. Returns the Violation or None.

    Content scoring 3 or more is hidden immediately.
    """
    from .models import Violation

    score, category, matches = moderate_text(message.body)
    if score <= 0 or message.sender_id is None:
        return None
    violation = Violation.objects.create(
        user=message.sender, source='chat', text=message.body[:2000],
        category=category, score=score, message=message, detected_by=Violation.DETECTED_RULE,
        evidence={'matched': matches},
    )
    _adjust_reputation(message.sender, -score * 5)
    apply_penalty_ladder(message.sender, violation)
    if score >= 3 and not message.is_deleted:
        message.is_deleted = True
        message.save(update_fields=['is_deleted'])
    return violation


def record_attendance(session, user, *, source='manual', status=None, marked_by=None, note=''):
    """Create or update one student's :class:`Attendance` for ``session``.

    When ``status`` is not given it is derived from the check-in time: present,
    or *late* if the check-in is more than ``session.late_after_minutes`` after
    the scheduled start. Idempotent per (session, student) — a later manual mark
    overrides an automatic one.

    A manual mark always wins; an automatic check-in never downgrades a manual one.
    """
    from .models import Attendance

    now = timezone.now()
    if status is None:
        status = Attendance.STATUS_PRESENT
        if session.starts_at:
            from datetime import timedelta
            late_at = session.starts_at + timedelta(minutes=session.late_after_minutes or 0)
            if now > late_at:
                status = Attendance.STATUS_LATE

    record, created = Attendance.objects.get_or_create(
        session=session, student=user,
        defaults={'status': status, 'source': source, 'check_in_at': now,
                  'marked_by': marked_by, 'note': note},
    )
    if not created:
        if source == Attendance.SOURCE_MANUAL or record.source != Attendance.SOURCE_MANUAL:
            record.status = status
            record.source = source
            record.marked_by = marked_by or record.marked_by
            if note:
                record.note = note
            if record.check_in_at is None:
                record.check_in_at = now
            record.save()
    return record


def record_meeting_join(meeting, user):
    """Open an attendance row when a student joins the live class.

    Joining only *starts the clock* — it no longer marks anyone present on its
    own. The row is created with zero seconds and the status stays absent until
    :func:`record_presence_ping` has accrued enough time to cross the session's
    threshold. That is the difference between "opened the link" and "attended".
    """
    session = getattr(meeting, 'class_session', None)
    if session and session.is_open and user and getattr(user, 'is_authenticated', False):
        return open_attendance(session, user)
    return None


PRESENCE_MAX_GAP_SECONDS = 150


def open_attendance(session, user):
    """Ensure an attendance row exists for ``user``, without judging them yet.

    Created rows start ``absent`` with no time on the clock — presence has to be
    earned by :func:`record_presence_ping`. An existing row (including a manual
    one) is returned untouched.
    """
    from .models import Attendance

    record, created = Attendance.objects.get_or_create(
        session=session, student=user,
        defaults={'status': Attendance.STATUS_ABSENT, 'source': Attendance.SOURCE_AUTO,
                  'seconds_attended': 0},
    )
    return record


def auto_status_for(session, record):
    """The status the measured time implies — present, late or absent.

    Late is decided by *when they first arrived*, not by how long they stayed, so
    a student who joins 20 minutes in but then attends the rest is "late" rather
    than "absent". Below the threshold is absent regardless of arrival time.
    """
    from datetime import timedelta

    from .models import Attendance

    if not record.meets_threshold:
        return Attendance.STATUS_ABSENT

    start = session.scheduled_start
    first = record.first_seen_at
    if start and first:
        late_at = start + timedelta(minutes=session.late_after_minutes or 0)
        if first > late_at:
            return Attendance.STATUS_LATE
    return Attendance.STATUS_PRESENT


def record_presence_ping(session, user, *, max_gap_seconds=PRESENCE_MAX_GAP_SECONDS):
    """Credit one heartbeat of real presence and re-derive the status.

    Each call adds the time since the previous ping, capped at
    ``max_gap_seconds``. The first ping of a visit credits nothing (there is no
    interval yet) but stamps ``first_seen_at`` — so a student who opens the page
    and immediately closes it accrues nothing.

    A manual mark is left alone: the seconds keep accruing (so the educator can
    still see how long the student was actually there) but the status they set
    stands. Returns the :class:`Attendance` row.

    A gap wider than the cap means they were away: nothing is credited and the clock simply resumes from now.
    """
    from .models import Attendance

    if not (user and getattr(user, 'is_authenticated', False)):
        return None

    now = timezone.now()
    record = open_attendance(session, user)

    if record.last_seen_at is not None:
        gap = int((now - record.last_seen_at).total_seconds())
        if 0 < gap <= max_gap_seconds:
            record.seconds_attended = (record.seconds_attended or 0) + gap
    if record.first_seen_at is None:
        record.first_seen_at = now
        record.check_in_at = record.check_in_at or now
    record.last_seen_at = now

    fields = ['seconds_attended', 'first_seen_at', 'last_seen_at', 'check_in_at']
    if not record.is_manual:
        new_status = auto_status_for(session, record)
        if new_status != record.status:
            record.status = new_status
            fields.append('status')
        if record.source != Attendance.SOURCE_AUTO:
            record.source = Attendance.SOURCE_AUTO
            fields.append('source')

    record.save(update_fields=fields)
    return record


def apply_measured_attendance(session, user, *, seconds, first_seen=None,
                              source=None, authoritative=False):
    """Record attendance time measured by something outside the browser.

    Used by the Microsoft Graph reconciliation (:mod:`apps.msteams.services`),
    where the platform itself reports exactly how long each person was in the
    call. That is a better measurement than our own heartbeat, which can only
    see how long the launch page stayed open — so ``authoritative=True``
    *replaces* the accrued seconds rather than taking the larger of the two.

    A manual mark still wins: the seconds are updated so an educator can see the
    real number, but the status they chose stands.
    """
    from .models import Attendance

    if not (user and getattr(user, 'is_authenticated', True)):
        return None

    record = open_attendance(session, user)
    seconds = max(0, int(seconds or 0))
    record.seconds_attended = seconds if authoritative else max(record.seconds_attended or 0, seconds)
    if first_seen and (record.first_seen_at is None or authoritative):
        record.first_seen_at = first_seen
        record.check_in_at = record.check_in_at or first_seen

    fields = ['seconds_attended', 'first_seen_at', 'check_in_at']
    if not record.is_manual:
        record.status = auto_status_for(session, record)
        fields.append('status')
        if source:
            record.source = source
            fields.append('source')
    record.save(update_fields=fields)
    return record


def engagement_signal(session, user):
    """What the student did on the day of ``session``, if not the session itself.

    Coaching here is not only live classes: a candidate who never opened the
    video call but worked through that module's lesson content on the day was
    engaged, and a register that calls them absent is telling the educator
    something false. So when measured presence comes up short we fall back to
    what else the platform already knows, strongest evidence first:

    1. ``lesson``  — studied a lesson belonging to this session's module that day.
    2. ``active``  — signed in and used the platform that day, but not this module.

    Returns ``(source, note)`` or ``None`` when there is no evidence at all.
    Deliberately scoped to the session's own date: engagement a week later is
    not attendance for this session.

    ``last_login`` is a coarser signal than the audit log, but it catches a
    sign-in on a build where the audit log is not being written.
    """
    from django.contrib.auth import get_user_model      # noqa: F401  (typing clarity)

    from .models import Attendance

    day = session.session_date
    module_id = session.module_id

    if module_id:
        from apps.learning.models import LessonSectionProgress
        studied = (LessonSectionProgress.objects
                   .filter(student=user,
                           section__lesson__module_id=module_id,
                           updated_at__date=day)
                   .exists())
        if studied:
            return Attendance.SOURCE_LESSON, 'Studied this module’s lesson content that day'

    from apps.accounts.models import ActivityLog
    person = getattr(user, 'profile', None)
    was_active = False
    if person is not None:
        was_active = ActivityLog.objects.filter(actor=person, timestamp__date=day).exists()
    if not was_active:
        last_login = getattr(user, 'last_login', None)
        was_active = bool(last_login and timezone.localtime(last_login).date() == day)
    if was_active:
        return Attendance.SOURCE_ACTIVE, 'On the platform that day'

    return None


def apply_engagement_attendance(session, record):
    """Credit ``record`` from engagement when the session itself was not attended.

    Only ever upgrades: a manual mark, an excusal, or presence already earned by
    measured time is left exactly as it is. Returns ``True`` when it changed the
    row, so the caller knows to save it.
    """
    from .models import Attendance

    if record.is_manual or record.status == Attendance.STATUS_EXCUSED:
        return False
    if record.meets_threshold:
        return False
    signal = engagement_signal(session, record.student)
    if signal is None:
        return False
    source, note = signal
    if record.status == Attendance.STATUS_PRESENT and record.source == source:
        return False
    record.status = Attendance.STATUS_PRESENT
    record.source = source
    record.note = note[:255]
    return True


def finalise_session_attendance(session, *, roster=None):
    """Close out a session's register once it has ended.

    Two things happen, both idempotent:

    * Everyone on the roster with no row gets one, marked absent — a register
      with gaps is not a register.
    * Every automatic row is re-derived from its measured time, so anyone who
      never crossed the threshold ends up absent.

    Manual and excused marks are never touched. Returns
    ``(present, late, absent, excused)`` counts.

    Measured time is the primary evidence; only where it falls short is
    :func:`engagement_signal` asked what else the student did that day.
    """
    from .models import Attendance

    if roster is None:
        roster = [p.user for p in session.module.member_people.select_related('user') if p.user_id]

    existing = {a.student_id: a for a in session.attendance.all()}
    missing = [u for u in roster if u.id not in existing]
    if missing:
        Attendance.objects.bulk_create([
            Attendance(session=session, student=u, status=Attendance.STATUS_ABSENT,
                       source=Attendance.SOURCE_AUTO, seconds_attended=0)
            for u in missing
        ], ignore_conflicts=True)

    to_update = []
    for record in session.attendance.select_related('student__profile'):
        if record.is_manual or record.status == Attendance.STATUS_EXCUSED:
            continue
        derived = auto_status_for(session, record)
        if derived != record.status:
            record.status = derived
            to_update.append(record)
        if apply_engagement_attendance(session, record) and record not in to_update:
            to_update.append(record)
    if to_update:
        Attendance.objects.bulk_update(to_update, ['status', 'source', 'note'], batch_size=500)

    session.finalised_at = timezone.now()
    session.save(update_fields=['finalised_at'])

    counts = session.attendance.values_list('status', flat=True)
    tally = {s: 0 for s, _ in Attendance.STATUS_CHOICES}
    for status in counts:
        tally[status] = tally.get(status, 0) + 1
    return (tally[Attendance.STATUS_PRESENT], tally[Attendance.STATUS_LATE],
            tally[Attendance.STATUS_ABSENT], tally[Attendance.STATUS_EXCUSED])


def finalise_due_sessions(limit=500):
    """Finalise every ended-but-unfinalised session. Returns how many were closed."""
    from .models import ClassSession

    done = 0
    for session in (ClassSession.objects.filter(finalised_at__isnull=True)
                    .select_related('module', 'meeting')[:limit]):
        if not session.has_ended:
            continue
        try:
            finalise_session_attendance(session)
            done += 1
        except Exception:  # pragma: no cover — one bad session must not stop the sweep
            logger.exception('finalise_due_sessions failed for %s', session)
    return done


def attendance_rate(user, module=None):
    """Return ``(present_or_late, total, pct)`` for a user (optionally one module)."""
    from .models import Attendance
    qs = Attendance.objects.filter(student=user)
    if module is not None:
        qs = qs.filter(session__module=module)
    total = qs.count()
    if not total:
        return 0, 0, 0.0
    present = qs.filter(status__in=[Attendance.STATUS_PRESENT, Attendance.STATUS_LATE,
                                    Attendance.STATUS_EXCUSED]).count()
    return present, total, round(present / total * 100, 1)


def scan_deadline_reminders(window_minutes=None):
    """Create reminder notifications for assessments/meetings coming due.

    Run periodically (Celery beat / cron). For every recipient who hasn't opted
    out of deadline / meeting notifications, look ahead by their own
    ``reminder_lead_minutes`` (or ``window_minutes`` override) and create one
    notification per item — de-duplicated by ``(recipient, url, verb)``. Returns
    the number of reminders created.

    The scan currently covers assessments closing soon, for the students enrolled in each module.
    """
    from datetime import timedelta

    from .models import NotificationPreference

    created = 0
    now = timezone.now()

    try:
        from apps.assessments.models import Assessment
        soon = Assessment.objects.filter(available_to__isnull=False, available_to__gte=now,
                                         status='open').select_related('module')
        for a in soon:
            for person in a.module.students.select_related('user'):
                user = person.user
                if not user:
                    continue
                pref = NotificationPreference.for_user(user)
                lead = window_minutes or pref.reminder_lead_minutes
                if a.available_to <= now + timedelta(minutes=lead):
                    url = f'/assessments/?a={a.pk}'
                    if not Notification.objects.filter(recipient=user, url=url, verb='deadline reminder').exists():
                        if notify(user, category='deadlines', verb='deadline reminder',
                                  title=f'Closing soon: {a.title}',
                                  body=f'“{a.title}” closes {a.available_to:%b %d, %H:%M}.',
                                  level='warning', url=url, email=True):
                            created += 1
    except Exception:  # pragma: no cover
        logger.exception('scan_deadline_reminders: assessments failed')

    return created
