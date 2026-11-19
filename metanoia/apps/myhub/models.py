"""Domain models backing the MyHub dashboard (calendar events).

``GENDER_CHOICES`` and ``STATUS_CHOICES`` are shared choice lists.
"""

from django.conf import settings
from django.db import models


GENDER_CHOICES = [
    ('male', 'Male'),
    ('female', 'Female'),
    ('other', 'Other'),
]

STATUS_CHOICES = [
    ('active', 'Active'),
    ('inactive', 'Inactive'),
]

class TimeStampedModel(models.Model):
    """Abstract base adding ``created_at`` and ``updated_at``."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Abstract: no table of its own."""

        abstract = True
class Event(TimeStampedModel):
    """A calendar entry: an event or a reminder.

    When ``owner`` is set the event is personal (a reminder / private entry)
    and only that user sees it on the calendar; otherwise it is
    institution-wide. See :meth:`visible_to`.
    """

    CATEGORY_EVENT = 'event'
    CATEGORY_REMINDER = 'reminder'
    CATEGORY_CHOICES = [(CATEGORY_EVENT, 'Event'), (CATEGORY_REMINDER, 'Reminder')]

    title = models.CharField(max_length=200)
    start = models.DateTimeField()
    end = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=200, blank=True)
    color = models.CharField(max_length=20, blank=True, default='#3a87ad')
    description = models.TextField(blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name='calendar_events',
    )
    category = models.CharField(max_length=12, choices=CATEGORY_CHOICES, default=CATEGORY_EVENT)

    class Meta:
        """Soonest first."""

        ordering = ['start']

    def __str__(self):
        """The title."""
        return self.title

    @classmethod
    def visible_to(cls, user):
        """The events ``user`` may see: institution-wide ones (no owner) plus
        their own; admin/staff see every entry. The one rule shared by the API
        (``myhub.api.EventViewSet``), the detail page, search and the dashboard,
        so a personal reminder never shows up for someone else."""
        from core.roles import role_of_user

        qs = cls.objects.all()
        if not getattr(user, 'is_authenticated', False):
            return qs.none()
        if role_of_user(user) in ('admin', 'staff'):
            return qs
        return qs.filter(models.Q(owner__isnull=True) | models.Q(owner=user))
