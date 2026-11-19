"""Signal handlers for the communication app.

* When a :class:`learning.ProgrammeModule` is created, give it a chat group;
  whenever a course is created, give it one too.
* When a module's coaches change, or somebody's :class:`learning.ModuleEnrolment`
  is created or removed, reconcile that module's chat group with who is on it.
  Enrolment on Metanoia is a ModuleEnrolment row — not an M2M — so the student side
  listens to that model rather than to a through-table.

Mention notifications are *not* handled here: they're created explicitly after a
message and its mentions are saved (see :class:`apps.communication.api.MessageViewSet`),
because the through-table rows don't exist yet at ``Message`` ``post_save`` time.
"""

import logging

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.learning.models import ModuleEnrolment, ProgrammeModule

from . import services
from .models import Message

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Message)
def moderate_new_message(sender, instance, created, **kwargs):
    """Scan newly-created chat messages for banned language / inappropriate
    content and auto-flag + penalise the sender (warn → mute → suspend → escalate)."""
    if not created or instance.is_deleted:
        return
    try:
        services.moderate_message(instance)
    except Exception:  # pragma: no cover - never let moderation break sending
        logger.exception('Moderation scan failed for message #%s', getattr(instance, 'pk', None))


@receiver(post_save, sender=ProgrammeModule)
def ensure_module_chat_group(sender, instance, created, **kwargs):
    """Give a newly created programme module its chat group, with its people in it."""
    if created:
        services.sync_module_chat_members(instance)


@receiver(m2m_changed, sender=ProgrammeModule.educators.through)
def sync_module_chat_membership(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    """A coach was added to or removed from an offering.

    The relation is edited from both ends; on the reverse edit
    (``person.taught_modules.add(offering)``) ``instance`` is the *Person* and the
    offerings are in ``pk_set``, so both directions must be handled.

    ``post_clear`` on the reverse side reports no ``pk_set``: the offerings are
    already detached, so there is nothing left to look up.
    """
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    offerings = ([instance] if not reverse
                 else list(ProgrammeModule.objects.filter(pk__in=pk_set or ())))
    for offering in offerings:
        try:
            services.sync_module_chat_members(offering)
        except Exception:  # pragma: no cover
            logger.exception('Failed to sync chat membership for module #%s',
                             getattr(offering, 'pk', None))


@receiver(post_save, sender=ModuleEnrolment)
@receiver(post_delete, sender=ModuleEnrolment)
def sync_module_chat_on_enrolment(sender, instance, **kwargs):
    """Somebody registered for (or came off) a module — reconcile its chat group.

    The student half of the sync; enrolment lives on ModuleEnrolment.
    """
    try:
        services.sync_module_chat_members(instance.programme_module)
    except Exception:  # pragma: no cover
        logger.exception('Failed to sync chat membership for module #%s',
                         getattr(instance, 'programme_module_id', None))


from apps.learning.models import Programme  # noqa: E402


@receiver(post_save, sender=Programme)
def ensure_programme_community(sender, instance, created, raw=False, **kwargs):
    """A new programme gets its community straight away (see Community)."""
    if raw:
        return
    from .models import Community
    try:
        Community.for_programme(instance)
    except Exception:                                       # pragma: no cover
        import logging
        logging.getLogger('apps').exception('community: could not create a programme community')
