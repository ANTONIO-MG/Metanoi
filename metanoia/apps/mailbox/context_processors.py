"""``mail_stats`` for every template: whether this person has Metanoia Mail and
how many unread messages wait in their inboxes (the sidebar Email badge).
Lazy and per-request — one aggregate query, only when a template reads it."""

from core.request_cache import cached_per_request

from .access import can_use_mail


class _MailStats:
    def __init__(self, user):
        self.user = user
        self._unread = None

    @property
    def enabled(self):
        return can_use_mail(self.user)

    @property
    def unread(self):
        if self._unread is None:
            self._unread = 0
            if self.enabled:
                try:
                    from django.db.models import Sum

                    from .models import MailFolder
                    self._unread = (MailFolder.objects.filter(
                        account__user=self.user, account__is_active=True, role=MailFolder.ROLE_INBOX)
                        .aggregate(n=Sum('unread'))['n'] or 0)
                except Exception:
                    self._unread = 0
        return self._unread


def mail_stats(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'mail_stats': {}}
    return {'mail_stats': cached_per_request(request, 'mail_stats', lambda: _MailStats(user))}
