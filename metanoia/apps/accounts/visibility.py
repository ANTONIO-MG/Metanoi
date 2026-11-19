"""Who may look somebody up — the rule behind the person card and the profile.

Anyone can open an account at ``/join/`` and is signed in on the spot, so
"signed in" says nothing about whether a viewer has any business knowing who
somebody is. :func:`can_view_person` answers that: a person is only shown to a
viewer who legitimately shares a context with them. Everybody else gets a 404,
so iterating ids learns nothing — not even which ids exist.

The contexts, in the order they are checked (cheapest first):

* yourself;
* admin and staff, who manage everyone (``core.roles``);
* the people directory rule (:func:`core.scoping.directory_users`) — the same
  rule the chat directory and the hub search already use: your institution /
  programme / intake, plus the team who must stay reachable; a parent's
  narrower list; a community member's Message Metanoia team;
* a parent ↔ child link, in either direction;
* a module you both take or teach;
* a chat you are both in (not the platform-wide "General" room, which is
  everybody and so no context at all);
* somebody whose post or comment you can see in the community feed — the feed
  links their name to this card, so it must open.

This decides *whether* the person can be seen at all. What is shown once they
can — and the owner's ``profile_visibility`` / ``show_email`` choices — is up
to the card and the profile themselves.
"""

from django.db.models import Q

from core.roles import role_of_user


def is_team(user):
    """Admin/staff (by role or Django flag) — they manage everyone."""
    return bool(user is not None and getattr(user, 'is_authenticated', False)
                and (user.is_staff or user.is_superuser
                     or role_of_user(user) in ('admin', 'staff')))


def can_view_person(viewer, person):
    """True when ``viewer`` (a User) may see ``person`` (a Person) at all.

    A profile with no login (``person.user`` is empty) is only handled by the team, so
    nobody else may view it.
    """
    if viewer is None or not getattr(viewer, 'is_authenticated', False) or person is None:
        return False
    if person.user_id == viewer.pk:
        return True
    if is_team(viewer):
        return True
    target = person.user_id
    if not target:
        return False

    from core.scoping import directory_users
    if directory_users(viewer).filter(pk=target).exists():
        return True

    from .models import ParentLink
    if ParentLink.objects.filter(Q(parent=viewer, student_id=target)
                                 | Q(parent_id=target, student=viewer)).exists():
        return True

    from .views import _shares_group
    if _shares_group(getattr(viewer, 'profile', None), person):
        return True

    try:
        from apps.communication.models import ChatGroup, Feed
    except Exception:        # pragma: no cover - communication is always installed
        return False
    shared_chat = (ChatGroup.objects.filter(memberships__user=viewer)
                   .filter(memberships__user_id=target)
                   .exclude(kind=ChatGroup.KIND_CUSTOM, name='General'))
    if shared_chat.exists():
        return True
    seen_in_feed = Feed.visible_to(viewer).filter(
        Q(author_id=target) | Q(comments__author_id=target) | Q(shared_from__author_id=target))
    return seen_in_feed.exists()
