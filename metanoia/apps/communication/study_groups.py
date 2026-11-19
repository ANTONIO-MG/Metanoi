"""Study groups — who may be put in one, and which modules it may be about.

A study group (``models.Workspace``; the table kept its old name) is a small
circle of candidates sharing files and notes. The people offered when one is
made, and the modules it can be tied to, are scoped here so the page and the
save agree on the same answer:

* a **student** picks classmates — people registered on the same programme (a
  programme belongs to one institution, so that is "same programme and same
  institution") — and one of the modules they are enrolled in;
* an **educator** picks from the programmes of the modules they teach, and one
  of those modules;
* **admin / staff** may pick anyone registered on any programme, and any module.

Preview students (student view), deactivated and closing accounts are never
offered. The view re-checks every posted id against :func:`eligible_members`
and :func:`eligible_modules`, so a hand-crafted POST cannot reach past them.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

from core.roles import role_flags


def _person(user):
    """Return the user's :class:`~apps.accounts.models.Person`, or ``None``."""
    return getattr(user, 'profile', None)


def scope_programmes(request):
    """Programmes whose people this user may put in a study group.

    ``None`` means every programme (admin / staff)."""
    from apps.learning.models import Programme

    if role_flags(request)['is_admin_staff']:
        return None
    person = _person(request.user)
    if person is None:
        return Programme.objects.none()
    if role_flags(request)['is_educator']:
        return Programme.objects.filter(modules__educators=person).distinct()
    return Programme.objects.filter(enrolments__person=person, enrolments__is_active=True).distinct()


def eligible_modules(request):
    """Programme modules a study group made by this user may be about.

    A student gets the modules they registered for, on a programme they are still
    on. Someone registered for the programme without module rows yet still sees
    their programme's modules rather than an empty list.
    """
    from apps.learning.models import ProgrammeModule

    qs = (ProgrammeModule.objects.filter(is_active=True)
          .select_related('programme__institution', 'module'))
    if role_flags(request)['is_admin_staff']:
        return qs.order_by('programme__institution__name', 'programme__name', 'order', 'id')
    person = _person(request.user)
    if person is None:
        return qs.none()
    if role_flags(request)['is_educator']:
        return qs.filter(educators=person).distinct().order_by('programme__name', 'order', 'id')
    programmes = scope_programmes(request)
    mine = qs.filter(enrolments__person=person, programme__in=programmes).distinct()
    if not mine.exists():
        mine = qs.filter(programme__in=programmes)
    return mine.order_by('programme__name', 'order', 'id')


def eligible_members(request, programme=None):
    """``User`` queryset of people this user may add to a study group.

    ``programme`` narrows it to one programme (a group tied to a module only
    takes people from that module's programme)."""
    User = get_user_model()
    programmes = scope_programmes(request)
    enrolled = Q(profile__programme_enrolments__is_active=True)
    if programmes is not None:
        enrolled &= Q(profile__programme_enrolments__programme__in=programmes)
    if programme is not None:
        enrolled &= Q(profile__programme_enrolments__programme=programme)
    return (User.objects.filter(enrolled, is_active=True, profile__isnull=False,
                                profile__preview_of__isnull=True)
            .exclude(account_settings__is_closing=True)
            .exclude(pk=request.user.pk)
            .select_related('profile')
            .distinct()
            .order_by('profile__first_name', 'profile__last_name', 'first_name', 'last_name'))


def member_rows(request):
    """The picker's rows: one per eligible person, with the programmes they share."""
    from apps.learning.models import ProgrammeEnrolment

    users = list(eligible_members(request)[:600])
    programmes = scope_programmes(request)
    enrolments = (ProgrammeEnrolment.objects
                  .filter(person__user__in=users, is_active=True)
                  .select_related('programme__institution'))
    if programmes is not None:
        enrolments = enrolments.filter(programme__in=programmes)
    by_user = {}
    for e in enrolments:
        by_user.setdefault(e.person.user_id, []).append(e.programme)
    return [{'user': u, 'programmes': by_user.get(u.pk, [])} for u in users]


def ensure_chat(workspace):
    """The study group's own chat (created on first use), with members in step.

    Members are the group's creator and members; anyone removed or leaving is
    taken out. People who reach the group only through its module are added
    when they open the page (:func:`join_chat`).
    """
    from .models import ChatGroup

    group = workspace.chat_group
    if group is None:
        group = ChatGroup.objects.create(
            kind=ChatGroup.KIND_CUSTOM, name=workspace.name[:200],
            description=f'Chat for the study group {workspace.name}.',
            created_by=workspace.created_by)
        workspace.chat_group = group
        workspace.save(update_fields=['chat_group'])
    sync_chat(workspace)
    return group


def sync_chat(workspace):
    """Make the chat's name, open/closed state and members match the group's.

    Only people the group put there are removed; a module classmate who joined
    through the page (``is_auto``) stays while they can still open the group.
    """
    group = workspace.chat_group
    if group is None:
        return
    fields = []
    if group.name != workspace.name[:200]:
        group.name = workspace.name[:200]
        fields.append('name')
    if group.is_active != workspace.is_active:
        group.is_active = workspace.is_active
        fields.append('is_active')
    if fields:
        group.save(update_fields=fields + ['updated_at'])

    wanted = set(workspace.members.values_list('pk', flat=True))
    if workspace.created_by_id:
        wanted.add(workspace.created_by_id)
    have = dict(group.memberships.values_list('user_id', 'is_auto'))
    for user_id in wanted - set(have):
        group.memberships.get_or_create(
            user_id=user_id,
            defaults={'role': 'owner' if user_id == workspace.created_by_id else 'member'})
    gone = [uid for uid, is_auto in have.items() if uid not in wanted and not is_auto]
    if gone:
        group.memberships.filter(user_id__in=gone).delete()


def join_chat(workspace, user):
    """Put someone who may open the group into its chat (module classmates too).

    Staff can read any conversation already; they are not added just by looking.
    """
    group = workspace.chat_group
    if group is None or not workspace.can_access(user):
        return
    named = workspace.created_by_id == user.pk or workspace.members.filter(pk=user.pk).exists()
    if named:
        group.memberships.get_or_create(user=user)
    elif not (user.is_staff or user.is_superuser):
        group.memberships.get_or_create(user=user, defaults={'is_auto': True})


def leave_chat(workspace, user):
    """Take ``user`` out of the study group's chat, if it has one."""
    group = workspace.chat_group
    if group is not None:
        group.memberships.filter(user=user).delete()
