"""The archive park — where a closed account goes, and how it comes back.

    close  →  grace (deactivated, reversible in one click)  →  purge

**Purge** (``purge_closed_accounts``): serialise the user and every row the
cascade would destroy, plus uploaded files, into
``backups/archived_accounts/<username>-<id>-<date>/`` (then the only copy), and
delete the user. Other people's records of an interaction (messages, comments)
are ``SET_NULL`` so their threads stay intact, shown as
:data:`~core.utils.DISCONTINUED_USER`.

**Restore** (:func:`restore_archive`): recreate the user with an unusable
password, then re-insert the parked rows. Dropped ``SET_NULL`` links do not
come back.
"""

import json
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import serializers
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger('apps')

SKIP_MODELS = {
    'admin.logentry',
    'sessions.session',
    'authtoken.token',
    'account.emailconfirmation',
    'socialaccount.socialtoken',
    'diagnostics.errorevent',
}
"""Models never worth parking: session-ish, log-ish or derived rows that would
bloat the archive and mean nothing on the way back."""


def park_root():
    """The archive park directory (created on demand)."""
    return Path(settings.BASE_DIR) / 'backups' / 'archived_accounts'


def _iso(value):
    """``value.isoformat()``, or ``None`` for an empty value."""
    return value.isoformat() if value else None


def owned_querysets(user):
    """Every queryset of rows the cascade would destroy along with ``user``.

    Walked off model metadata so new models are covered automatically.
    Non-cascade relations are skipped: those rows outlive the user by design.
    """
    from django.db.models import CASCADE

    User = get_user_model()
    out = []
    for relation in User._meta.related_objects:
        field = relation.field
        if getattr(field, 'remote_field', None) is None:
            continue
        if field.remote_field.on_delete is not CASCADE:
            continue
        model = relation.related_model
        label = model._meta.label_lower
        if label in SKIP_MODELS:
            continue
        try:
            rows = model._base_manager.filter(**{field.name: user})
        except Exception:                                # pragma: no cover
            logger.exception('archive: could not read %s', label)
            continue
        out.append((label, rows))
    return out


def snapshot(user):
    """A JSON-serialisable summary of the account (the human-readable half)."""
    person = getattr(user, 'profile', None)
    us = getattr(user, 'account_settings', None)

    data = {
        'schema_version': 2,
        'archived_at': _iso(timezone.now()),
        'user': {
            'id': user.id,
            'username': user.get_username(),
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'is_active': user.is_active,
            'is_staff': user.is_staff,
            'is_superuser': user.is_superuser,
            'date_joined': _iso(user.date_joined),
            'last_login': _iso(user.last_login),
        },
        'profile': None,
        'settings': None,
        'programme': None,
        'modules': [],
    }

    if person is not None:
        data['profile'] = {
            'first_name': person.first_name,
            'last_name': person.last_name,
            'gender': person.gender,
            'date_of_birth': _iso(person.date_of_birth),
            'phone': person.phone,
            'user_type': person.user_type,
            'profile_status': person.profile_status,
            'registered': person.registered,
        }
        try:
            data['modules'] = [
                enrolment.programme_module.label
                for enrolment in person.module_enrolments.select_related(
                    'programme_module__module', 'programme_module__programme')
            ]
        except Exception:                                # pragma: no cover
            data['modules'] = []

    if us is not None:
        data['settings'] = {
            'profile_visibility': us.profile_visibility,
            'show_email': us.show_email,
            'show_activity': us.show_activity,
            'allow_messages': us.allow_messages,
            'theme': us.theme,
            'language': us.language,
            'closed_at': _iso(us.closed_at),
            'purge_at': _iso(us.purge_at),
        }
    return data


def _copy_files(user, directory):
    """Copy the account's uploaded files into the park, preserving their
    relative media path so a restore can point at them again. Files on remote
    storage (no local path) are skipped."""
    from django.db.models import FileField

    copied = []
    media_root = Path(settings.MEDIA_ROOT)
    targets = [user] + [obj for obj in (getattr(user, 'profile', None),) if obj is not None]
    for obj in targets:
        for field in obj._meta.get_fields():
            if not isinstance(field, FileField):
                continue
            value = getattr(obj, field.name, None)
            if not value:
                continue
            try:
                source = Path(value.path)
            except (NotImplementedError, ValueError):
                continue
            if not source.exists():
                continue
            try:
                relative = source.relative_to(media_root)
            except ValueError:
                relative = Path(source.name)
            destination = directory / 'media' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(str(relative))
    return copied


def park_account(user):
    """Write everything about ``user`` into the archive park.

    Returns ``(directory, summary_dict, content_row_count)``. Does **not** delete
    anything — the caller decides when the live rows go.

    ``content.json`` is the restorable half: real serialised rows, without
    natural keys, so primary keys (and therefore the links between the rows)
    survive the round trip. ``account.json`` is the human-readable summary.
    """
    stamp = timezone.now()
    name = f'{user.get_username() or user.pk}-{user.pk}-{stamp:%Y%m%d%H%M%S}'
    directory = park_root() / name
    directory.mkdir(parents=True, exist_ok=True)

    summary = snapshot(user)

    payload, count = [], 0
    payload.append(serializers.serialize('python', [user]))
    for label, rows in owned_querysets(user):
        chunk = list(rows)
        if not chunk:
            continue
        count += len(chunk)
        payload.append(serializers.serialize('python', chunk))

    summary['files'] = _copy_files(user, directory)
    summary['content_rows'] = count

    (directory / 'account.json').write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    (directory / 'content.json').write_text(
        json.dumps([row for group in payload for row in group],
                   indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    (directory / 'README.txt').write_text(
        'Archived account.\n\n'
        'account.json  — human-readable summary of who this was.\n'
        'content.json  — every database row that belonged to them, restorable\n'
        '                via the admin "Closed accounts" page.\n'
        'media/        — their uploaded files, at their original media paths.\n\n'
        'Messages and other rows they merely took part in are NOT here: they\n'
        'stayed on the live system, attributed to a discontinued user.\n',
        encoding='utf-8')

    return directory, summary, count


def purge_account(user):
    """Park the account, then delete it. Returns the created
    :class:`~apps.accounts.models.AccountArchive` row.

    The user is deleted via the queryset, not ``user.delete()``, so the audit
    ``post_delete`` signal receives a freshly loaded User with no cached
    profile — otherwise it would try to log a reference to the Person the
    cascade is deleting in the same transaction.
    """
    from . import models

    directory, summary, count = park_account(user)
    user_pk = user.pk
    username = user.get_username()
    email = user.email or ''
    User = get_user_model()

    with transaction.atomic():
        archive = models.AccountArchive.objects.create(
            original_user_id=user_pk,
            username=username,
            email=email,
            data=summary,
            file_path=str(directory / 'account.json'),
            archive_dir=str(directory),
            content_rows=count,
        )
        User.objects.filter(pk=user_pk).delete()
    return archive


def restore_archive(archive):
    """Rebuild a purged account from its park directory.

    Returns the restored user. The password is left unusable: whoever asks for
    the account back proves it is theirs through the normal password-reset
    e-mail, not by an administrator handing them a login.

    The user row lands first — everything else points at it. The deserializer
    saves ``raw``, which the profile-creating and audit signals skip, so no
    stand-in Person is made to collide with the archived one on its way in. A
    Person is still ensured afterwards: an archive written before the profile
    existed would otherwise leave an account every profile page would 500 on.
    The account's closing flag is cleared, and parked files are copied back to
    where the restored rows expect them (never over an existing file).
    """
    from . import models

    directory = Path(archive.archive_dir or '').expanduser()
    content = directory / 'content.json'
    if not content.exists():
        raise FileNotFoundError(f'archive content missing at {content}')

    rows = json.loads(content.read_text(encoding='utf-8'))
    media_root = Path(settings.MEDIA_ROOT)
    user_label = get_user_model()._meta.label_lower

    with transaction.atomic():
        objects = list(serializers.deserialize('python', rows, ignorenonexistent=True))
        user_objects = [o for o in objects if o.object._meta.label_lower == user_label]
        if not user_objects:
            raise ValueError('archive holds no user row')

        restored_user = user_objects[0].object
        user_objects[0].save()

        for obj in objects:
            if obj is user_objects[0]:
                continue
            obj.save()

        restored_user.is_active = True
        restored_user.set_unusable_password()
        restored_user.save()

        models.Person.all_objects.get_or_create(user=restored_user)

        user_settings = models.UserSettings.for_user(restored_user)
        user_settings.cancel_closing()

    parked_media = directory / 'media'
    if parked_media.exists():
        for source in parked_media.rglob('*'):
            if not source.is_file():
                continue
            destination = media_root / source.relative_to(parked_media)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)

    archive.restored_at = timezone.now()
    archive.save(update_fields=['restored_at'])
    return restored_user


def _rerank(user):
    """Class positions leave archived accounts out — re-rank their modules."""
    try:
        from apps.reports.models import Grade
        from apps.reports.services import rank_module
        from apps.learning.models import ProgrammeModule
        ids = set(Grade.objects.filter(student=user).values_list('module_id', flat=True))
        for module in ProgrammeModule.objects.filter(pk__in=ids):
            rank_module(module)
    except Exception:  # pragma: no cover - ranking must not block an archive
        logger.exception('archive: could not re-rank modules for user %s', user.pk)


def archive_account(user, *, by=None, reason='', days=None):
    """Admin "delete": deactivate (no sign-in, no API token) and flag closing,
    hiding the account everywhere except the Django admin and closed-accounts
    desk; purged after ``UserSettings.ADMIN_GRACE_DAYS``.

    Returns the restoring :class:`~apps.accounts.models.UndoAction`, or ``None``
    when the account was already archived.
    """
    from . import models, undo

    if by is not None and getattr(by, 'pk', None) == user.pk:
        raise ValueError('You cannot delete your own account from here.')
    days = models.UserSettings.ADMIN_GRACE_DAYS if days is None else days
    with transaction.atomic():
        user_settings = models.UserSettings.for_user(user)
        if user_settings.is_closing:
            return None
        was_active = user.is_active
        user_settings.mark_closing(days=days, by=by, reason=reason or 'Deleted in the advanced admin')
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
        user.is_active = False
        label = f'{user.get_full_name() or user.get_username()} <{user.email}>'
        actor = models.Person.all_objects.filter(user=by).first() if by else None
        target = models.Person.all_objects.filter(user=user).first()
        models.ActivityLog.objects.create(
            actor=actor, target_user=target, action='archive',
            description=(f'Account deleted (archived): {label}. Restorable until '
                         f'{timezone.localtime(user_settings.purge_at):%Y-%m-%d %H:%M}, then '
                         f'purged. Reason: {user_settings.close_reason}'))
        action = undo.record(
            'archive', f'Deleted account {label}'[:255], object_ref=undo.ref(user),
            payload={'user_id': user.pk, 'was_active': was_active}, actor=by, target_user=user)
    _rerank(user)
    return action


def restore_account(user, *, by=None, reason=''):
    """Bring an archived (not yet purged) account back exactly as it was.

    Returns ``False`` if the account was not archived. Any open undo of the
    deletion is marked spent.
    """
    from . import models

    with transaction.atomic():
        user_settings = models.UserSettings.for_user(user)
        if not user_settings.is_closing:
            return False
        user_settings.cancel_closing()
        get_user_model().objects.filter(pk=user.pk).update(is_active=True)
        user.is_active = True
        actor = models.Person.all_objects.filter(user=by).first() if by else None
        target = models.Person.all_objects.filter(user=user).first()
        models.ActivityLog.objects.create(
            actor=actor, target_user=target, action='restore',
            description=f'Account restored: {user.get_username()} <{user.email}>.'
                        + (f' Reason: {reason}' if reason else ''))
        models.UndoAction.objects.filter(
            kind='archive', object_ref=f'{user._meta.label_lower}:{user.pk}',
            undone_at__isnull=True).update(
                undone_at=timezone.now(), undone_by=by if getattr(by, 'pk', None) else None,
                undo_reason=reason or 'Restored')
    _rerank(user)
    return True


def _undo_archive(action, *, by, reason):
    """Undo handler for ``archive``: restore the account and its original active flag.

    Raises :class:`~apps.accounts.undo.UndoError` when the account has already
    been purged or is no longer archived.
    """
    payload = action.payload or {}
    user = get_user_model().objects.filter(pk=payload.get('user_id')).first()
    if user is None:
        from . import undo
        raise undo.UndoError('The account has already been purged — restore it from the '
                             'archived accounts list on the Closed accounts page instead.')
    if not restore_account(user, by=by, reason=reason):
        from . import undo
        raise undo.UndoError('That account is not archived any more.')
    if not payload.get('was_active', True):
        get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    return f'{user.email or user.get_username()} is back and can sign in again.'


def _describe_archive(action):
    """What undoing an archive will do, for the confirmation page."""
    return ['Restore the account: it can sign in again and reappears everywhere '
            '(rosters, counts, finance, chats) exactly as before.',
            'The scheduled permanent deletion is cancelled.']


def _register_undo():
    """Register the ``archive`` undo handler with :mod:`apps.accounts.undo` (runs on import)."""
    from . import undo
    undo.register('archive', perform=_undo_archive, describe=_describe_archive)


_register_undo()
