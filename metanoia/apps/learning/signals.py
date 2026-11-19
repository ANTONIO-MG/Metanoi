"""Signal handlers for the learning app.

Keeps each module offering's **chat group** in step with who may open the
module. Lives here because the dependency runs learning → communication.
Reconciling is idempotent and cheap, so it fires on every enrolment write.

Trial *expiry* saves nothing, so a signal cannot see it —
``manage.py sync_module_chats`` closes that gap on a schedule.
"""

import logging

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.learning.models import Programme, ProgrammeModule
from .models import ModuleEnrolment
logger = logging.getLogger('apps')


def _sync(offering, why):
    """Reconcile ``offering``'s chat members; ``why`` is logged if it fails.

    Chat must never break enrolment, so any error is logged and swallowed.
    """
    if offering is None:
        return
    try:
        from apps.communication.services import sync_module_chat_members
        sync_module_chat_members(offering)
    except Exception:  # pragma: no cover — chat must never break enrolment
        logger.exception('chat sync failed for %s (%s)', offering, why)


@receiver(post_save, sender=ProgrammeModule)
def ensure_module_chat_group(sender, instance, created, **kwargs):
    """A new offering gets its room straight away, with its coaches in it.

    Not the General module (nobody is enrolled on it)."""
    if created and not instance.is_general:
        _sync(instance, 'offering created')


@receiver(post_save, sender=Programme)
def ensure_general_module(sender, instance, created, raw=False, **kwargs):
    """Every programme has exactly one General module, from the day it exists."""
    if created and not raw:
        from .general import general_module_for
        general_module_for(instance)


@receiver(post_save, sender=ModuleEnrolment)
def sync_chat_on_enrolment(sender, instance, **kwargs):
    """Paying, starting a trial or being locked out all land here.

    ``activate()``, ``start_trial()`` and ``lock()`` each save the row, so one
    receiver covers the whole lifecycle.
    """
    _sync(instance.programme_module, 'enrolment saved')


@receiver(post_delete, sender=ModuleEnrolment)
def sync_chat_on_unenrol(sender, instance, **kwargs):
    """An enrolment removed: reconcile the offering's room."""
    _sync(instance.programme_module, 'enrolment deleted')


@receiver(m2m_changed, sender=ProgrammeModule.educators.through)
def sync_chat_on_educator_change(sender, instance, action, reverse=False, pk_set=None, **kwargs):
    """Coaches are members of the room whether or not anyone has paid.

    Edited from either end: on the reverse side (``person.taught_modules``)
    ``instance`` is the *Person* and the offerings are in ``pk_set`` — do not
    sync ``instance`` blindly. ``post_clear`` on the reverse side reports no
    ``pk_set``: the offerings are already detached, so there is nothing left to
    look up and nothing to sync.
    """
    if action not in ('post_add', 'post_remove', 'post_clear'):
        return
    if not reverse:
        _sync(instance, 'educators changed')
        return
    for offering in ProgrammeModule.objects.filter(pk__in=pk_set or ()):
        _sync(offering, 'educators changed')
