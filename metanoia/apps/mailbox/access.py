"""Who may use Metanoia Mail: admin, staff and educators."""

from core.roles import role_of_user


def can_use_mail(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return user.is_staff or user.is_superuser or role_of_user(user) in ('admin', 'staff', 'educator')


def can_manage_mail(user):
    """Server settings and other people's mailboxes: admin and staff."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    return user.is_staff or user.is_superuser or role_of_user(user) in ('admin', 'staff')
