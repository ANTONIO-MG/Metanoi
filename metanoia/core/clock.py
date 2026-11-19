"""Times as people here read them.

Pages use ``TIME_ZONE`` (South Africa). Text written *for people* about a session
(thumbnails, summaries, e-mails) goes through :func:`local`, which uses
``DISPLAY_TIME_ZONE`` (South Africa by default). Per-recipient rendering
(reminders) keeps using each user's own zone.
"""

from zoneinfo import ZoneInfo

from django.conf import settings


def zone():
    return ZoneInfo(getattr(settings, 'DISPLAY_TIME_ZONE', '') or 'Africa/Johannesburg')


def local(when):
    """``when`` in the display zone (None stays None)."""
    return when.astimezone(zone()) if when is not None else None
