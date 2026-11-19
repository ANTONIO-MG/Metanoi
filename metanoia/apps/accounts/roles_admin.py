"""Who may change what kind of account somebody has — one rule, two doors.

The member-management page (:func:`apps.accounts.views.member_role`) and the
REST API (:class:`apps.accounts.api.PersonViewSet`) both change
``Person.user_type``. They must agree, or the API is a way round the page:

* only an **administrator** (``role_flags(...)['is_admin']``: role ``admin``,
  or a Django superuser/staff account, which ``core.roles`` counts as admin)
  changes anybody's role. Staff manage students but cannot mint staff or
  admins — let alone make *themselves* one;
* only the roles the page offers can be granted (:data:`GRANTABLE_ROLES`);
* the last administrator cannot be demoted;
* Django-admin access (``User.is_staff``) follows the role.

And because an administrator's record is the keys to the platform, nobody who
is not an administrator may edit an administrator's profile at all
(:func:`is_privileged_account` / :func:`may_edit_person`).

Developers (:mod:`apps.accounts.developers`) sit one step above: an
administrator who is not a developer cannot change a developer's role, edit
their record through the API, or grant/revoke developer access.
"""

from core.roles import is_developer, role_of_user

GRANTABLE_ROLES = frozenset({'student', 'educator', 'staff', 'admin', 'parent'})
"""What the member-management page lets an administrator pick."""


def is_admin_user(user):
    """The same test ``member_role`` applies (``role_flags``'s ``is_admin``)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return role_of_user(user) == 'admin' or bool(user.is_superuser)


def is_privileged_account(person):
    """True for an administrator's record (by role or by Django flags)."""
    if person is None:
        return False
    if person.user_type == 'admin':
        return True
    user = getattr(person, 'user', None)
    return bool(user is not None and (user.is_staff or user.is_superuser))


def may_edit_person(actor, person):
    """Whether ``actor`` may write to ``person``'s record at all (beyond the
    self/manager scoping the caller already applies): an administrator's record
    is for administrators — and its owner — only."""
    if person is not None and person.user_id == getattr(actor, 'pk', None):
        return True
    if person is not None and person.is_developer and not is_developer(actor):
        return False
    if is_admin_user(actor):
        return True
    return not is_privileged_account(person)


def role_change_error(actor, person, new_role):
    """``None`` if ``actor`` may make ``person`` a ``new_role``, else the reason."""
    if not is_admin_user(actor):
        return 'Only an administrator can change what kind of account someone has.'
    if new_role not in GRANTABLE_ROLES:
        return 'That is not a role this platform recognises.'
    if person.is_developer and new_role != person.user_type:
        if not is_developer(actor):
            return "Only a developer can change a developer's role."
        from .developers import is_protected
        if new_role != 'admin' and is_protected(person):
            return ('That account is in DEVELOPER_EMAILS, so it would be made an '
                    'administrator again at its next sign-in. Remove it from that setting first.')
    if person.user_type == 'admin' and new_role != 'admin':
        from .models import Person
        if not Person.objects.filter(user_type='admin').exclude(pk=person.pk).exists():
            return ('That is the only administrator left — promote somebody else first, '
                    'or you will lock everyone out of managing this platform.')
    return None


def sync_django_admin_flag(person):
    """Django-admin access follows the role: administrators get it, nobody else
    (a superuser keeps theirs).

    Developer only counts on an administrator's account, so a demoted developer
    loses the flag rather than keeping a dormant one that a later promotion
    would revive.
    """
    user = getattr(person, 'user', None)
    if user is None:
        return
    wants = person.user_type == 'admin'
    if not wants and person.is_developer:
        person.is_developer = False
        person.save(update_fields=['is_developer'])
    if user.is_staff != wants and not user.is_superuser:
        user.is_staff = wants
        user.save(update_fields=['is_staff'])
