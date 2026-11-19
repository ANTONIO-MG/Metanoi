"""One name for one person, wherever it is read.

* :func:`sync_login_name` copies the Person's name onto ``auth.User`` on save;
* :func:`install` connects that handler and makes ``str(user)`` the person's name rather than the
  username (which is the e-mail address).
"""

from django.db.models.signals import post_save


def sync_login_name(sender, instance, raw=False, **kwargs):
    """``post_save`` on Person: copy the person's name onto their ``auth.User``.

    Skips fixture loads and a Person with no name, so a name the login already
    had is never blanked out. Uses a queryset update, not ``user.save()``: no
    signals, no audit-log noise, no recursion. A failure is reported as
    ``USER-9002`` and never breaks the profile save.
    """
    if raw or not instance.user_id:
        return
    first, last = (instance.first_name or '').strip()[:150], (instance.last_name or '').strip()[:150]
    if not (first or last):
        return
    from django.contrib.auth import get_user_model
    try:
        get_user_model().objects.filter(pk=instance.user_id).exclude(first_name=first, last_name=last) \
            .update(first_name=first, last_name=last)
    except Exception as exc:
        from core.errors import report
        report('USER-9002', exc, context={'person': instance.pk})


def _user_str(self):
    """``auth.User.__str__``: the person's display name."""
    from core.utils import display_name
    return display_name(self)


def install(person_model):
    """Connect :func:`sync_login_name` for ``person_model`` and replace ``User.__str__``."""
    from django.contrib.auth import get_user_model
    post_save.connect(sync_login_name, sender=person_model, dispatch_uid='accounts.sync_login_name',
                      weak=False)
    get_user_model().__str__ = _user_str
