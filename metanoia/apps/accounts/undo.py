"""Undo for staff actions that are destructive or move money.

Each action records an :class:`~apps.accounts.models.UndoAction`. Undoing is
admin/staff only, needs a reason on a confirmation page, and is audited.

Handlers register here by ``kind``:

* ``payment``       — :mod:`apps.finance.undo`
* ``invoice_void``  — :mod:`apps.finance.undo`
* ``delete``        — :func:`delete_with_undo` below (any model rows)
* ``archive``       — :mod:`apps.accounts.archiving` (admin "delete" of an account)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from django.contrib import messages
from django.core import serializers
from django.db import router, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

logger = logging.getLogger('apps')


class UndoError(Exception):
    """The action cannot (or can no longer) be undone; the message says why."""


@dataclass
class Handler:
    """The callbacks that undo one ``kind`` of :class:`~apps.accounts.models.UndoAction`.

    * ``perform(action, *, by, reason) -> str`` — reverses the action and returns
      a one-line result for the success message; raises :class:`UndoError`.
    * ``describe(action) -> list[str]`` — what undoing will do, for the
      confirmation page.
    * ``warning(action) -> str`` — a strong warning (money really moved); when
      set, the confirmation page requires an explicit acknowledgement.
    * ``blocked(action) -> str`` — why this cannot be undone right now.
    * ``extra`` — handler-specific data.
    """
    perform: Callable
    describe: Callable = None
    warning: Callable = None
    blocked: Callable = None
    extra: dict = field(default_factory=dict)


HANDLERS = {}


def register(kind, **kwargs):
    """Register the :class:`Handler` for ``kind`` (keyword arguments are its fields)."""
    HANDLERS[kind] = Handler(**kwargs)


def _load_handlers():
    """Import the modules that register handlers (idempotent)."""
    from apps.accounts import archiving  # noqa: F401  (registers 'archive')
    from apps.finance import undo as _finance_undo  # noqa: F401


def handler_for(action):
    """The registered :class:`Handler` for ``action``; raises :class:`UndoError` if none."""
    _load_handlers()
    handler = HANDLERS.get(action.kind)
    if handler is None:
        raise UndoError(f'No undo is available for “{action.get_kind_display()}”.')
    return handler


def can_undo(user):
    """Administrators and staff (finance) only."""
    if not getattr(user, 'is_authenticated', False) or not user.is_active:
        return False
    if user.is_staff or user.is_superuser:
        return True
    person = getattr(user, 'profile', None)
    return getattr(person, 'user_type', '') in ('admin', 'staff')


def ref(obj):
    """``app_label.model:pk`` for ``obj``."""
    return f'{obj._meta.label_lower}:{obj.pk}'


def record(kind, summary, *, object_ref='', payload=None, actor=None, target_user=None):
    """Create and return an :class:`~apps.accounts.models.UndoAction` (summary capped at 255 characters)."""
    from .models import UndoAction
    return UndoAction.objects.create(
        kind=kind, summary=(summary or '')[:255], object_ref=object_ref or '',
        payload=payload or {},
        actor=actor if getattr(actor, 'pk', None) else None,
        target_user=target_user if getattr(target_user, 'pk', None) else None)


def latest_for(obj, kind=None):
    """The newest UndoAction recorded for ``obj`` that has not been undone."""
    from .models import UndoAction
    qs = UndoAction.objects.filter(object_ref=ref(obj), undone_at__isnull=True)
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by('-created_at').first()


def undo_url(action):
    """The confirmation page for ``action``."""
    return reverse('accounts:undo', args=[action.pk])


def success_with_undo(request, text, action):
    """``messages.success`` with an "Undo" link to the confirmation page."""
    if action is None:
        messages.success(request, text)
        return
    messages.success(request, format_html(
        '{} <a href="{}" class="alert-link fw-semibold ms-1">'
        '<i class="bi bi-arrow-counterclockwise"></i> Undo</a>', text, undo_url(action)))


def _audit(action, by, reason, result):
    """Write the ``undo`` row to the activity log, with the reason and the result."""
    from .models import ActivityLog, Person
    actor = Person.all_objects.filter(user=by).first() if by else None
    target = (Person.all_objects.filter(user_id=action.target_user_id).first()
              if action.target_user_id else None)
    ActivityLog.objects.create(
        actor=actor, target_user=target, action='undo',
        description=(f'Undid “{action.get_kind_display()}”: {action.summary}. '
                     f'Reason: {reason}. {result}').strip()[:5000])


def perform(action, *, by, reason, acknowledged=False):
    """Reverse ``action``. Returns the handler's one-line result."""
    from .models import UndoAction

    reason = (reason or '').strip()
    if not can_undo(by):
        raise UndoError('Only administrators and staff can undo actions.')
    if not reason:
        raise UndoError('Please say why this is being undone — it goes into the audit log.')
    handler = handler_for(action)
    if handler.warning and handler.warning(action) and not acknowledged:
        raise UndoError('Tick the box to confirm you understand the warning.')

    with transaction.atomic():
        locked = UndoAction.objects.select_for_update().get(pk=action.pk)
        if locked.undone_at:
            raise UndoError('This has already been undone.')
        if handler.blocked:
            why = handler.blocked(locked)
            if why:
                raise UndoError(why)
        result = handler.perform(locked, by=by, reason=reason) or ''
        locked.undone_at = timezone.now()
        locked.undone_by = by
        locked.undo_reason = reason
        locked.save(update_fields=['undone_at', 'undone_by', 'undo_reason'])
        _audit(locked, by, reason, result)
    action.refresh_from_db()
    return result


def _serialize(instances):
    """Rows as JSON-safe dicts (dates as ISO strings — the deserializer reads them back)."""
    return json.loads(serializers.serialize('json', list(instances)))


def delete_with_undo(objs, *, summary, actor=None, target_user=None, object_ref='',
                     next_url=''):
    """Delete ``objs`` (a list of instances of one model) and everything the
    delete cascades to, keeping a serialised copy so it can be put back.

    Rows that merely pointed at them (``SET_NULL``) are remembered too, and are
    re-linked by the undo. Returns the :class:`UndoAction`.

    The payload holds ``groups`` (serialised rows in deletion order, restored in
    reverse) and ``relinks`` (``[model label, pk, attname, original value]``).
    """
    from django.db.models.deletion import Collector

    objs = [o for o in objs if o is not None and o.pk is not None]
    if not objs:
        return None
    using = router.db_for_write(objs[0].__class__)
    with transaction.atomic(using=using):
        collector = Collector(using=using, origin=objs[0])
        collector.collect(objs)
        collector.sort()

        groups = []
        for model, instances in collector.data.items():
            groups.append(_serialize(instances))
        for qs in collector.fast_deletes:
            groups.append(_serialize(qs))

        relinks = []
        for (fld, _value), batches in collector.field_updates.items():
            for batch in batches:
                model = getattr(batch, 'model', None) or batch[0].__class__
                if hasattr(batch, 'values_list'):
                    pairs = list(batch.values_list('pk', fld.attname))
                else:
                    pairs = [(o.pk, getattr(o, fld.attname)) for o in batch]
                relinks += [[model._meta.label_lower, pk, fld.attname,
                             value if isinstance(value, (int, str)) else str(value)]
                            for pk, value in pairs if value is not None]

        action = record(
            'delete', summary, object_ref=object_ref or ref(objs[0]), actor=actor,
            target_user=target_user,
            payload={'groups': groups, 'relinks': relinks, 'next': next_url,
                     'labels': sorted({m._meta.verbose_name for m in collector.data})})
        collector.delete()
    return action


def _restore_deleted(action, *, by, reason):
    """``delete`` handler: re-insert the serialised rows and re-link ``SET_NULL`` pointers.

    Groups are restored in reverse deletion order; many-to-many data is added
    once every row exists. Raises :class:`UndoError` if a row with the same id
    exists again.
    """
    from django.apps import apps as django_apps

    payload = action.payload or {}
    groups = payload.get('groups') or []
    restored = 0
    m2m_later = []
    for group in reversed(groups):
        for obj in serializers.deserialize('python', group, ignorenonexistent=True):
            model = obj.object.__class__
            if model._base_manager.filter(pk=obj.object.pk).exists():
                raise UndoError(f'A {model._meta.verbose_name} with the same id exists again '
                                f'— it cannot be put back automatically.')
            obj.save(save_m2m=False)
            if obj.m2m_data:
                m2m_later.append(obj)
            restored += 1
    for obj in m2m_later:
        for name, values in obj.m2m_data.items():
            getattr(obj.object, name).add(*values)
    for label, pk, attname, value in payload.get('relinks') or []:
        try:
            model = django_apps.get_model(label)
        except LookupError:
            continue
        model._base_manager.filter(pk=pk, **{f'{attname}__isnull': True}).update(**{attname: value})
    return f'{restored} record(s) put back.'


def _describe_deleted(action):
    """What undoing a delete will put back, for the confirmation page."""
    payload = action.payload or {}
    count = sum(len(g) for g in payload.get('groups') or [])
    labels = ', '.join(payload.get('labels') or [])
    return [f'Put back {count} deleted record(s) ({labels}).',
            'Links other records had to them are restored.']


register('delete', perform=_restore_deleted, describe=_describe_deleted)
