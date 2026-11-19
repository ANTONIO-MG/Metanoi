"""DRF serializers for the MyHub domain.

All that survives the clean sweep is the calendar: the professor / student /
course / library / fees / CMS / blog models this module used to serialise went
with the old MyHub theme. See :mod:`apps.myhub.api`.
"""

from rest_framework import serializers

from . import models


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Event
        fields = '__all__'


class OwnEventSerializer(EventSerializer):
    """What a non-manager may write: their own entry, never someone else's.

    ``owner`` is read-only (the view stamps the requesting user), so a learner
    cannot create an entry on another person's calendar or publish an
    institution-wide one by leaving it blank.
    """

    class Meta(EventSerializer.Meta):
        read_only_fields = ['owner']
