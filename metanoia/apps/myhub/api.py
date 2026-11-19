"""REST API viewsets for the MyHub domain.

The calendar is all that is left here after the clean sweep. ``/api/myhub/events/``
supports list / create / retrieve / update / partial-update / destroy, plus
search (``?search=``), ordering (``?ordering=``) and field filtering. Every
method requires an authenticated user.
"""

from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.exceptions import PermissionDenied

from core.roles import role_of_user

from . import models, serializers


def _is_manager(user):
    """Admin/staff — by profile role or Django staff flag (``role_of_user``)."""
    return role_of_user(user) in ('admin', 'staff')


class BaseModelViewSet(viewsets.ModelViewSet):
    """Signed-in users only — including for reads.

    ``/api/`` is on ``LoginRequiredMiddleware``'s exempt list (DRF authenticates
    itself, and has to answer in JSON rather than redirect to a login page), so
    the site-wide login gate does not cover these URLs. That made
    ``IsAuthenticatedOrReadOnly`` the one anonymous data path into an otherwise
    private site: an Event carries its title, location, description and times.
    Nothing in the product reads this logged out, so requiring authentication
    costs nothing.
    """

    permission_classes = [permissions.IsAuthenticated]


class EventViewSet(BaseModelViewSet):
    """Calendar entries, scoped the way the calendar itself scopes them.

    An Event with no ``owner`` is institution-wide; one with an ``owner`` is that
    person's private entry (a reminder). So:

    * everyone sees institution-wide events plus their own; admin/staff see all;
    * anyone may add an entry, but a non-manager's entry is always their own
      (``owner`` is forced to them — the web "Add reminder" form does the same);
    * a non-manager may edit or delete only their own entries; institution-wide
      events and other people's reminders are admin/staff only.
    """

    queryset = models.Event.objects.all()
    filterset_fields = ['all_day', 'category']
    search_fields = ['title', 'location', 'description']
    ordering_fields = ['start', 'end', 'title']

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if _is_manager(user):
            return qs
        return qs.filter(Q(owner__isnull=True) | Q(owner=user))

    def get_serializer_class(self):
        if _is_manager(self.request.user):
            return serializers.EventSerializer
        return serializers.OwnEventSerializer

    def perform_create(self, serializer):
        if _is_manager(self.request.user):
            serializer.save()
        else:
            serializer.save(owner=self.request.user)

    def _check_owner(self, event):
        user = self.request.user
        if _is_manager(user) or (event.owner_id is not None and event.owner_id == user.pk):
            return
        raise PermissionDenied('You can only change your own calendar entries.')

    def perform_update(self, serializer):
        self._check_owner(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_owner(instance)
        instance.delete()
