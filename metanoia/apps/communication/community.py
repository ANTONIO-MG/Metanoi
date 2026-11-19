"""The Metanoia Community — who belongs where, what they may do, and the feed.

There is one **default** community everybody belongs to, plus one community per
programme (:class:`~apps.communication.models.Community`). Membership of a
programme's community is *worked out* rather than stored, so it follows
enrolment by itself:

==============  ==============================================================
admin / staff   every community (they run them)
student         the programmes they are enrolled in (or, with none yet, the one
                they picked when they first joined as a community member)
educator        the programmes of the modules they teach
parent          their children's programmes
community       the programme they picked when they joined (/join/)
==============  ==============================================================

**Explore other communities** (``Person.community_explore``) lets a person
join others (:class:`~apps.communication.models.CommunityMembership`).

Community members are unverified, so their posting is rate-limited
(:data:`LIMITS`) and photos are their only attachments. Enough reports hide a
post until staff review it (:data:`AUTO_HIDE_REPORTS`).

Module constants:

- ``LIMITS`` — posting limits for community members, as ``kind: [(window, max), …]``.
  Generous enough for a real conversation, tight enough that one account cannot flood
  the community: 5 posts an hour (20 a day), 20 comments an hour (100 a day), 10 reports
  a day. A share counts as a post. Students, educators, parents and staff are verified
  accounts and are not limited.
- ``MAX_MENTIONS`` — the most people one post or comment can tag.
- ``AUTO_HIDE_REPORTS`` — distinct open reports that hide a post until staff review it.
- ``MEMBER_MAX_ATTACHMENTS`` — community members may attach photos only (no documents or
  videos from an unverified account), and at most this many per post.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from core.roles import role_of_user

logger = logging.getLogger('apps')

LIMITS = {
    'post': [(timedelta(hours=1), 5), (timedelta(days=1), 20)],
    'comment': [(timedelta(hours=1), 20), (timedelta(days=1), 100)],
    'report': [(timedelta(days=1), 10)],
}

MAX_MENTIONS = 5

AUTO_HIDE_REPORTS = 3

MEMBER_MAX_ATTACHMENTS = 4


def is_member_role(user):
    """True for a Metanoia Community member (not a student, not staff)."""
    return role_of_user(user) == 'community'


def is_moderator(user):
    """Admin and staff moderate the community."""
    return bool(user and getattr(user, 'is_authenticated', False)
                and (user.is_staff or user.is_superuser
                     or role_of_user(user) in ('admin', 'staff')))


def programme_ids_for(user):
    """Programme ids of ``user``'s *own* communities, or ``None`` for all.

    One home per person, worked out from who they are:

    * a student — the programmes they are enrolled in; only if they have none
      yet, the one they picked when they joined as a community member (that
      choice is kept as a record after they become a student, and must not add
      a second programme community on top of their real one);
    * a community member — the programme they picked at /join/;
    * an educator — the programmes they teach;
    * a parent — their children's programmes.

    One-on-one students get the general Metanoia Community only — their conversations with
    educators are in their private space (owner decision, 2026-11-10).
    """
    if not getattr(user, 'is_authenticated', False):
        return set()
    if is_moderator(user):
        return None
    person = getattr(user, 'profile', None)
    if person is None:
        return set()
    role = role_of_user(user)
    ids = set()
    if role == 'student' and person.is_one_on_one:
        return ids
    if role == 'student':
        ids.update(person.programme_enrolments.filter(is_active=True)
                   .values_list('programme_id', flat=True))
    elif role == 'educator':
        ids.update(person.taught_modules.values_list('programme_id', flat=True))
    elif role == 'parent':
        from apps.accounts.models import ParentLink
        from apps.learning.models import ProgrammeEnrolment
        children = ParentLink.objects.filter(parent=user).values('student_id')
        ids.update(ProgrammeEnrolment.objects
                   .filter(person__user_id__in=children, is_active=True)
                   .values_list('programme_id', flat=True))
    ids.discard(None)
    if not ids and role in ('student', 'community') and person.community_programme_id:
        ids.add(person.community_programme_id)
    return ids


def explores(user):
    """True when ``user`` has switched on "Explore other communities".

    One-on-one students stay in the general community only, so they never explore.
    """
    person = getattr(user, 'profile', None) if getattr(user, 'is_authenticated', False) else None
    return bool(person is not None and person.community_explore and not person.is_one_on_one)


def home_communities(user):
    """The general community plus ``user``'s own — what everyone starts with.

    Ensures the default community exists first.
    """
    from .models import Community
    Community.default()
    qs = Community.objects.filter(is_active=True).select_related('programme__institution')
    ids = programme_ids_for(user)
    if ids is None:
        return qs
    return qs.filter(Q(is_default=True) | Q(programme_id__in=ids))


def communities_for(user):
    """The active communities ``user`` belongs to, default first.

    The general one, their own (:func:`home_communities`), plus joined ones
    while exploring; admin/staff get all. Every read/post/comment/like/share/tag
    permission goes through this function.
    """
    from .models import Community
    Community.default()
    qs = Community.objects.filter(is_active=True).select_related('programme__institution')
    ids = programme_ids_for(user)
    if ids is None:
        return qs
    mine = Q(is_default=True) | Q(programme_id__in=ids)
    if explores(user):
        mine |= Q(memberships__user=user)
    return qs.filter(mine).distinct()


def community_ids_for(user):
    """Primary keys of :func:`communities_for` (used to filter posts)."""
    return list(communities_for(user).values_list('pk', flat=True))


def explorable_communities(user):
    """Every community ``user`` could join while exploring: the active ones
    that are not the general one and not their own. Empty for staff (they are
    already in all of them) and for anyone not exploring."""
    from .models import Community
    if not explores(user) or programme_ids_for(user) is None:
        return Community.objects.none()
    home = home_communities(user).values('pk')
    return (Community.objects.filter(is_active=True, is_default=False).exclude(pk__in=home)
            .select_related('programme__institution').order_by('name'))


def set_explore(user, on):
    """Switch "Explore other communities" on or off for ``user``."""
    person = user.profile
    person.community_explore = bool(on)
    person.save(update_fields=['community_explore'])


def join_community(user, community):
    """Join ``community`` while exploring. Returns ``True`` when joined.

    Refused (``False``) unless the person is exploring and the community is one
    of :func:`explorable_communities` — so a hand-made POST cannot join anyone
    to a community they could not have picked.
    """
    from .models import CommunityMembership
    if community is None or not explorable_communities(user).filter(pk=community.pk).exists():
        return False
    CommunityMembership.objects.get_or_create(user=user, community=community)
    return True


def leave_community(user, community):
    """Leave a community joined while exploring (never the general one or
    your own — those come with who you are)."""
    from .models import CommunityMembership
    return CommunityMembership.objects.filter(user=user, community=community).delete()[0] > 0


def members_of(community):
    """Every active account in ``community`` (used to find people to tag).

    A student who joined as a member keeps ``community_programme`` as a record; it only
    places them in that community while they are still a community member.
    """
    from apps.accounts.models import ParentLink
    from core.student_view import exclude_previews

    User = get_user_model()
    active = exclude_previews(User.objects.filter(is_active=True))
    if community.is_default or community.programme_id is None:
        return active
    pid = community.programme_id
    students = (Q(profile__programme_enrolments__programme_id=pid,
                  profile__programme_enrolments__is_active=True)
                & ~Q(profile__learning_track='one_on_one'))
    parents = Q(pk__in=ParentLink.objects.filter(
        student__profile__programme_enrolments__programme_id=pid).values('parent_id'))
    members = Q(profile__community_programme_id=pid, profile__user_type='community')
    explorers = Q(community_memberships__community=community, profile__community_explore=True)
    return active.filter(
        students | parents | members | explorers
        | Q(profile__taught_modules__programme_id=pid)
        | Q(profile__user_type__in=('admin', 'staff')) | Q(is_staff=True) | Q(is_superuser=True)
    ).distinct()


def _recent_count(user, kind, since):
    """How many posts, comments or reports (``kind``) ``user`` made since ``since``."""
    from .models import Feed, FeedComment, FeedReport
    if kind == 'post':
        return Feed.objects.filter(author=user, created_at__gte=since).count()
    if kind == 'comment':
        return FeedComment.objects.filter(author=user, created_at__gte=since).count()
    return FeedReport.objects.filter(reporter=user, created_at__gte=since).count()


def rate_limit_error(user, kind):
    """A friendly refusal if a community member is over a limit, else ``None``.

    Counted from the database rather than a cache, so the limit holds across
    every worker and survives a restart. Only community members are limited.
    """
    if not is_member_role(user):
        return None
    now = timezone.now()
    for window, cap in LIMITS.get(kind, []):
        if _recent_count(user, kind, now - window) >= cap:
            span = 'hour' if window <= timedelta(hours=1) else 'day'
            noun = {'post': 'posts', 'comment': 'comments', 'report': 'reports'}[kind]
            return (f'You have reached the limit of {cap} {noun} per {span} for community '
                    f'members. Please try again a little later.')
    return None


def mention_label(user):
    """How a person is written when tagged: ``First Last``."""
    from core.utils import display_name
    person = getattr(user, 'profile', None)
    full = ' '.join(filter(None, [getattr(person, 'first_name', ''),
                                  getattr(person, 'last_name', '')])).strip()
    return full or display_name(user)


def search_members(community, query, *, exclude=None, limit=8, viewer=None):
    """People in ``community`` whose name starts with ``query``.

    By name only: matching e-mail prefixes too let anyone signed in (a
    ``/join/`` account is enough) confirm, letter by letter, whose address is
    whose. And unless ``viewer`` moderates the community, people who keep their
    profile private are not offered — "Private — only me" includes not being
    listed to strangers by a picker.
    """
    from apps.accounts.models import UserSettings

    query = (query or '').strip()
    qs = members_of(community).select_related('profile')
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    if viewer is None or not is_moderator(viewer):
        qs = qs.exclude(account_settings__profile_visibility=UserSettings.VISIBILITY_PRIVATE)
    if query:
        words = query.split()
        for w in words:
            qs = qs.filter(Q(profile__first_name__istartswith=w) | Q(profile__last_name__istartswith=w)
                           | Q(first_name__istartswith=w) | Q(last_name__istartswith=w))
    return list(qs.order_by('profile__first_name', 'profile__last_name')[:limit])


def resolve_mentions(author, community, body, raw_ids):
    """The people tagged in ``body``: posted ids that really are in the
    community *and* whose ``@Name`` still appears in the text (a tag the
    author deleted from the text again is not a tag)."""
    ids = []
    for raw in raw_ids or []:
        raw = str(raw).strip()
        if raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    if not ids or not body or community is None:
        return []
    found = []
    for user in members_of(community).filter(pk__in=ids[:MAX_MENTIONS * 2]).select_related('profile'):
        if user.pk == author.pk:
            continue
        if f'@{mention_label(user)}'.lower() in body.lower():
            found.append(user)
    return found[:MAX_MENTIONS]


def record_mentions(post, users, *, actor, comment=None):
    """Store the tags and tell each tagged person."""
    from .models import FeedMention
    from .services import notify
    community = post.community
    where = community.name if community else 'the community'
    url = feed_url(community) + f'#post-{post.pk}'
    for user in users:
        _, created = FeedMention.objects.get_or_create(post=post, comment=comment, user=user)
        if not created:
            continue
        try:
            notify(user, actor=actor, url=url,
                   title=f'{mention_label(actor)} mentioned you in {where}',
                   body=((comment.body if comment else post.body) or '')[:280],
                   verb='mentioned you')
        except Exception:                                   # pragma: no cover
            logger.exception('community: mention notification failed')


def mention_labels(post, comment=None):
    """The ``@Name`` strings tagged in a post (or one of its comments)."""
    return [f'@{mention_label(m.user)}' for m in post.mentions.all()
            if (m.comment_id == (comment.pk if comment else None))]


def highlight_mentions(text, labels):
    """Escape ``text``, make its links clickable (with a preview card for the
    first, see :mod:`apps.communication.links`) and wrap each known ``@Name``
    in a highlight."""
    from .links import linkify
    return linkify(text or '', mentions=labels)


def report_post(post, reporter, reason, note=''):
    """Record a report; hide the post once enough people have reported it.

    Returns ``(report, created)``. Staff hear about the first report on a post
    and about an automatic hide — not about every report, which would turn a
    pile-on into a flood of notifications.
    """
    from .models import FeedReport
    if reason not in dict(FeedReport.REASON_CHOICES):
        reason = FeedReport.REASON_OTHER
    report, created = FeedReport.objects.get_or_create(
        post=post, reporter=reporter,
        defaults={'reason': reason, 'note': (note or '')[:500]})
    if not created:
        return report, False
    open_count = post.reports.filter(status=FeedReport.STATUS_OPEN).count()
    if open_count >= AUTO_HIDE_REPORTS and not post.is_removed:
        post.is_removed = True
        post.removed_at = timezone.now()
        post.removal_reason = f'Hidden automatically after {open_count} reports — awaiting review'
        post.save(update_fields=['is_removed', 'removed_at', 'removal_reason'])
        _tell_moderators(post, f'A community post was hidden after {open_count} reports')
    elif open_count == 1:
        _tell_moderators(post, 'A community post was reported')
    return report, True


def _tell_moderators(post, title):
    """Notify every admin/staff moderator about ``post``, linking to the Moderation page.
    """
    from .services import notify
    User = get_user_model()
    moderators = User.objects.filter(is_active=True).filter(
        Q(is_staff=True) | Q(is_superuser=True) | Q(profile__user_type__in=('admin', 'staff'))
    ).distinct()
    url = f"{reverse('staffdesk:ops-moderation')}?tab=community"
    for user in moderators:
        try:
            notify(user, title=title, body=(post.body or '')[:200], url=url, verb='reported')
        except Exception:                                   # pragma: no cover
            logger.exception('community: moderator notification failed')


def remove_post(post, by, reason=''):
    """Remove ``post`` as staff member ``by`` (with an optional reason) and close its
    reports.
    """
    from .models import FeedReport
    post.is_removed = True
    post.removed_by = by
    post.removed_at = timezone.now()
    post.removal_reason = (reason or 'Removed by staff')[:255]
    post.save(update_fields=['is_removed', 'removed_by', 'removed_at', 'removal_reason'])
    post.reports.filter(status=FeedReport.STATUS_OPEN).update(
        status=FeedReport.STATUS_REMOVED, handled_by=by, handled_at=timezone.now())


def restore_post(post, by):
    """Put a removed post back and close its reports as dismissed."""
    from .models import FeedReport
    post.is_removed = False
    post.removed_by = None
    post.removed_at = None
    post.removal_reason = ''
    post.save(update_fields=['is_removed', 'removed_by', 'removed_at', 'removal_reason'])
    post.reports.exclude(status=FeedReport.STATUS_DISMISSED).update(
        status=FeedReport.STATUS_DISMISSED, handled_by=by, handled_at=timezone.now())


def support_team():
    """Everyone who answers Message Metanoia: active admins and staff."""
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(
        Q(is_staff=True) | Q(is_superuser=True) | Q(profile__user_type__in=('admin', 'staff'))
    ).exclude(profile__preview_of__isnull=False).distinct()


def support_chat_for(user):
    """The member's private "Message Metanoia" thread, with the whole team in it.

    Created on first use; every call also adds anybody who joined the team
    since, so a new staff member sees the existing threads.
    """
    from .models import ChatGroup
    group = (ChatGroup.objects.filter(kind=ChatGroup.KIND_SUPPORT, created_by=user)
             .order_by('pk').first())
    if group is None:
        group = ChatGroup.objects.create(
            kind=ChatGroup.KIND_SUPPORT, created_by=user,
            name=f'Message Metanoia · {mention_label(user)}',
            description='A private conversation between a community member and the Metanoia team.')
    group.add_member(user)
    have = set(group.memberships.values_list('user_id', flat=True))
    for member in support_team():
        if member.pk not in have:
            group.add_member(member, auto=True)
    return group


def feed_url(community=None):
    """The dashboard feed URL, filtered to ``community`` when one is given."""
    base = reverse('pages:dashboard')
    if community is None:
        return base
    return f'{base}?c={community.slug}'


def _post_item(post, user, *, mod, my_community_ids):
    """One post as the dict ``partials/_feed.html`` renders."""
    from core.utils import avatar_url, initials

    from .feed import TYPE_STYLE, _actor_name, _post_media

    icon, color, open_label, can_react = TYPE_STYLE['post']
    media_list = _post_media(post)
    original = post.shared_from
    if original is not None and original.is_removed and not mod:
        original = None
    community = post.community
    is_author = post.author_id == user.pk
    viewer_is_member = is_member_role(user)
    actor_url = (reverse('accounts:person-card-user', args=[post.author_id]) if viewer_is_member
                 else _actor_url_for(post.author))
    comments = list(post.comments.all())
    for c in comments:
        c.html = highlight_mentions(c.body, mention_labels(post, c))
    return {
        'type': 'post', 'when': post.created_at, 'actor': post.author, 'title': '',
        'body': post.body, 'body_html': highlight_mentions(post.body, mention_labels(post)),
        'media': media_list[0] if media_list else None, 'media_list': media_list,
        'icon': icon, 'color': color, 'url': '', 'location': '',
        'label': 'shared a post' if original else 'posted',
        'open_label': open_label, 'can_react': True,
        'module_id': post.module_id, 'module_name': getattr(post.module, 'name', ''),
        'avatar': avatar_url(post.author), 'initials': initials(post.author),
        'actor_name': _actor_name(post.author), 'actor_url': actor_url,
        'community': community,
        'extra': {
            'pk': post.pk, 'likes': post.like_count,
            'liked': post.likes.filter(pk=user.pk).exists(),
            'comments': comments,
            'shared_from': original,
            'shared_from_html': highlight_mentions(original.body, mention_labels(original)) if original else '',
            'shared_from_media': _post_media(original) if original else [],
            'can_share': not post.is_removed,
            'can_report': not is_author and not mod,
            'can_remove': mod,
            'can_delete': is_author and not mod,
            'is_removed': post.is_removed,
            'removal_reason': post.removal_reason,
            'report_count': getattr(post, 'open_reports', 0),
        },
    }


def _actor_url_for(actor):
    """Profile URL for a feed actor (delegates to
    :func:`apps.communication.feed._actor_url`).
    """
    from .feed import _actor_url
    return _actor_url(actor)


def build_community_feed(user, community=None, limit=40):
    """Community posts (and, in the default and "All" views, announcements
    this person received), newest first.

    ``community=None`` is "All": every community the viewer belongs to.
    """
    from django.db.models import Count, Prefetch

    from .models import Announcement, Feed, FeedComment

    mod = is_moderator(user)
    my_ids = community_ids_for(user)
    posts = (Feed.visible_to(user)
             .select_related('author__profile', 'module', 'community',
                             'shared_from__author__profile', 'shared_from__community')
             .prefetch_related('attachments', 'mentions__user__profile',
                               'shared_from__attachments', 'shared_from__mentions__user__profile',
                               Prefetch('comments', queryset=FeedComment.objects
                                        .select_related('author__profile').order_by('created_at'))))
    if mod:
        posts = posts.annotate(open_reports=Count('reports', filter=Q(reports__status='open')))
    if community is not None:
        if community.is_default:
            posts = posts.filter(Q(community__isnull=True) | Q(community=community))
        else:
            posts = posts.filter(community=community)
    items = [_post_item(p, user, mod=mod, my_community_ids=my_ids)
             for p in posts.order_by('-created_at')[:limit]]

    if community is None or community.is_default:
        try:
            from core.utils import avatar_url, initials

            from .feed import TYPE_STYLE, _actor_name
            sent = Announcement.objects.filter(sent_at__isnull=False)
            if not mod:
                sent = sent.filter(notifications__recipient=user)
            icon, color, open_label, _ = TYPE_STYLE['announcement']
            for a in sent.select_related('sender__profile').distinct().order_by('-sent_at')[:15]:
                items.append({
                    'type': 'announcement', 'when': a.sent_at, 'actor': a.sender,
                    'title': a.title, 'body': a.body, 'media': None, 'media_list': [],
                    'icon': icon, 'color': color, 'url': '/communication/notifications/',
                    'location': '', 'label': 'posted an announcement', 'open_label': open_label,
                    'can_react': False, 'module_id': None, 'module_name': '',
                    'avatar': avatar_url(a.sender), 'initials': initials(a.sender) if a.sender else '',
                    'actor_name': _actor_name(a.sender), 'actor_url': '',
                    'community': None, 'extra': {},
                })
        except Exception:                                   # pragma: no cover
            logger.exception('community feed: announcements failed')

    items.sort(key=lambda i: i['when'], reverse=True)
    return items[:limit]
