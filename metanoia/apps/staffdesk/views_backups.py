"""Backups & restore points — developers only (apps.accounts.developers).

Everything here acts on the whole database, so every action is recorded twice:
in the audit log (ActivityLog) and in ``backups/db/audit.log``, because a
restore rewinds the database's own audit log along with everything else.

A restore needs :data:`CONFIRM_WORD` typed on the form (so it is never one
stray click) and, for accounts with a usable password, that password.
"""

import logging
from datetime import datetime, timezone as dt_timezone
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.errors import note, report
from core.roles import is_developer
from core.utils import display_name

from . import backups

logger = logging.getLogger('apps')

CONFIRM_WORD = 'RESTORE'


def admin_required(view):
    """Developers only (apps.accounts.developers): backups and restore are a
    developer tool. Everybody else, ordinary administrators included, gets a 404."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        """Log ``BKP-2001`` and 404 for non-developers; otherwise call the view."""
        if not is_developer(request.user):
            note('BKP-2001', request, view=view.__name__)
            raise Http404
        return view(request, *args, **kwargs)
    return wrapped


def _audit(request, action, text):
    """Record a backup action in ``audit.log`` and in :class:`ActivityLog`.

    Failures are logged and swallowed: the audit trail must not block the action.
    """
    who = display_name(request.user)
    line = f'{datetime.now(dt_timezone.utc).isoformat()}  {who} (user {request.user.pk})  {text}\n'
    try:
        with open(backups.backup_root() / 'audit.log', 'a') as fh:
            fh.write(line)
    except OSError:     # pragma: no cover
        logger.warning('backups: could not write audit.log')
    try:
        from apps.accounts.models import ActivityLog
        ActivityLog.objects.create(actor=getattr(request.user, 'profile', None), action=action, description=f'Backups: {text}',
                                   request_path=request.path[:255],
                                   ip_address=request.META.get('REMOTE_ADDR') or None)
    except Exception:   # pragma: no cover
        logger.exception('backups: audit log write failed')


def _fail(request, exc, **context):
    """Report a :class:`BackupError` to the error log and show it as a message."""
    report(exc.code, exc, request, context={**context, 'detail': exc.detail[-500:]})
    messages.error(request, f'{exc} [{exc.code}]')


@admin_required
def backup_list(request):
    """The Backups page: restore points, disk use, restore status and tool checks.

    ``stale`` is set when the newest automatic backup is over 4.5 hours old (the
    job runs every 3 hours).
    """
    rows = backups.list_backups()
    now = timezone.now()
    last_scheduled = next((r for r in rows if r['kind'] == backups.KIND_SCHEDULED), None)
    stale = last_scheduled is None or (now - last_scheduled['created']).total_seconds() > 4.5 * 3600
    try:
        backups._tool('pg_dump')
        backups._tool('pg_restore')
        tools_ok, tools_error = True, ''
    except backups.BackupError as exc:
        tools_ok, tools_error = False, str(exc)
    return render(request, 'staffdesk/backups/backups.html', {
        'page_title': 'Backups & restore points', 'rows': rows, 'usage': backups.disk_usage(),
        'status': backups.status(), 'restoring': backups.restore_in_progress(),
        'last_scheduled': last_scheduled, 'stale': stale,
        'keep_days': backups.scheduled_keep_days(),
        'keep_safety': backups.RETENTION[backups.KIND_PRE_RESTORE],
        'tools_ok': tools_ok, 'tools_error': tools_error, 'confirm_word': CONFIRM_WORD,
        'needs_password': request.user.has_usable_password(),
    })


@admin_required
@require_POST
def backup_create(request):
    """Take a manual restore point now ("Back up now")."""
    note_text = (request.POST.get('note') or '').strip()
    try:
        meta = backups.create_backup(backups.KIND_MANUAL, by=display_name(request.user), note=note_text)
    except backups.BackupError as exc:
        _fail(request, exc)
        return redirect('staffdesk:backups')
    _audit(request, 'create', f'backup created {meta["name"]}')
    messages.success(request, f'Restore point created: {meta["created"]:%d %b %Y, %H:%M} UTC '
                              f'({meta["size"] // 1024} KB).')
    return redirect('staffdesk:backups')


@admin_required
def backup_download(request, name):
    """Download a dump as an attachment (audited)."""
    try:
        path = backups.path_for(name)
    except backups.BackupError as exc:
        _fail(request, exc, backup=name)
        return redirect('staffdesk:backups')
    _audit(request, 'update', f'backup downloaded {name}')
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=name,
                        content_type='application/octet-stream')


@admin_required
@require_POST
def backup_delete(request, name):
    """Delete a backup and its description (audited)."""
    try:
        backups.delete_backup(name)
    except backups.BackupError as exc:
        _fail(request, exc, backup=name)
        return redirect('staffdesk:backups')
    _audit(request, 'delete', f'backup deleted {name}')
    messages.success(request, 'Backup deleted.')
    return redirect('staffdesk:backups')


@admin_required
@require_POST
def backup_upload(request):
    """Keep an uploaded ``.dump`` as a restore point after checking it is one."""
    upload = request.FILES.get('dump')
    if not upload:
        messages.error(request, 'Choose a .dump file to upload.')
        return redirect('staffdesk:backups')
    try:
        meta = backups.import_upload(upload, by=display_name(request.user))
    except backups.BackupError as exc:
        _fail(request, exc, filename=getattr(upload, 'name', ''))
        return redirect('staffdesk:backups')
    _audit(request, 'create', f'backup uploaded {meta["name"]} from {upload.name}')
    messages.success(request, 'Backup uploaded and checked. It is now listed as a restore point.')
    return redirect('staffdesk:backups')


@admin_required
@require_POST
def backup_restore(request, name):
    """Start a restore of ``name`` after the confirmation word and password check.

    The restore runs in its own process (:func:`backups.start_restore`); the page
    polls :func:`restore_status` for progress.
    """
    if (request.POST.get('confirm') or '').strip() != CONFIRM_WORD:
        note('BKP-2001', request, backup=name, reason='confirmation word')
        messages.error(request, f'Type {CONFIRM_WORD} in capitals to confirm the restore.')
        return redirect('staffdesk:backups')
    if request.user.has_usable_password() and not request.user.check_password(request.POST.get('password', '')):
        note('BKP-2001', request, backup=name, reason='password')
        messages.error(request, 'Your password was not right, so nothing was restored.')
        return redirect('staffdesk:backups')
    try:
        backups.start_restore(name, by=display_name(request.user))
    except backups.BackupError as exc:
        _fail(request, exc, backup=name)
        return redirect('staffdesk:backups')
    _audit(request, 'update', f'restore started from {name}')
    messages.warning(request, 'Restore started. A safety backup is taken first; the site may pause '
                              'for a few seconds while the database is swapped.')
    return redirect('staffdesk:backups')


@admin_required
def restore_status(request):
    """The restore progress as JSON, polled by the Backups page."""
    return JsonResponse(backups.status())
