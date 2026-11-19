"""Upload a programme pack as a zip → its content, into an existing programme and
cohort.

Nothing is created at the programme level: the uploader picks a cohort that
exists, the pack's module folders are matched to that programme's modules, and
the weeks, unit lessons, files, live sessions and calendar dates are loaded
into them (``core.programme_pack.intake``). What the folders cannot say comes
from the schedule sheet — in the pack, uploaded beside it, or drafted by the AI
and corrected on the review screen (``core.programme_pack.sheet``).

Screens (admin & staff only — the same guard as the content queue):

    /learning/content/programme-pack/                     upload the zip, say where it goes
    /learning/content/programme-pack/<token>/             review: what was read, the weeks,
                                                          sessions and dates — then build it
    /learning/content/programme-pack/<token>/seed.py      the spine as a Python seed file
    /learning/content/programme-pack/<token>/plan.json    what the folders were read as
    /learning/content/programme-pack/<token>/sheet/       save the corrected schedule sheet (POST)
    /learning/content/programme-pack/<token>/schedule.csv the sheet, to keep in the pack

Scanning (:func:`core.programme_pack.scanner.scan`) reads folder structure
only; the optional AI step (:mod:`core.programme_pack.ai`) is skipped without a
key. Building runs :class:`core.programme_pack.builder.Builder` (as
``seed_sample_programme`` does) in a background thread — a full pack takes minutes.

Work files live under ``MEDIA_ROOT/programme_packs/<token>/`` (staff-only in
``core.media_access``): the upload, the extracted pack and ``state.json``.

Limits: ``MAX_ZIP_BYTES`` (400 MB upload), ``MAX_UNPACKED_BYTES`` (800 MB
unpacked) and ``MAX_MEMBERS`` (5000 zip entries) guard against zip bombs;
``TOKEN_RX`` is the shape of a work token (32 hex characters), checked before
any path is built from one.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
import zipfile

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.programme_pack import ai, codegen, intake, scanner, sheet
from core.roles import role_flags

from . import models

logger = logging.getLogger('apps')

MAX_ZIP_BYTES = 400 * 1024 * 1024
MAX_UNPACKED_BYTES = 800 * 1024 * 1024
MAX_MEMBERS = 5000
TOKEN_RX = re.compile(r'^[0-9a-f]{32}$')


def _guard(request):
    """``True`` when the request comes from an administrator or staff member."""
    return role_flags(request)['is_admin_staff']


def _workdir(token):
    """The work directory of ``token``; 404 unless it is a well-formed token (no path
    tricks).
    """
    if not TOKEN_RX.match(token or ''):
        raise Http404
    return os.path.join(settings.MEDIA_ROOT, 'programme_packs', token)


def _load(token):
    """The pack's ``state.json`` as a dict; 404 when there is none."""
    path = os.path.join(_workdir(token), 'state.json')
    if not os.path.exists(path):
        raise Http404
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _save(token, state):
    """Write ``state.json`` atomically (a temporary file, then a rename)."""
    path = os.path.join(_workdir(token), 'state.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(state, fh, indent=1, default=str)
    os.replace(tmp, path)


def _update(token, **changes):
    """Merge ``changes`` into the saved state and return the new state."""
    state = _load(token)
    state.update(changes)
    _save(token, state)
    return state


def safe_extract(archive, target):
    """Unzip ``archive`` into ``target``, refusing anything that escapes it,
    links, and zip bombs. Returns the number of files written.

    Directories, macOS ``__MACOSX`` metadata, hidden files, Office lock files
    (``~$…``) and symlinks are skipped rather than written.
    """
    written, total = 0, 0
    root = os.path.realpath(target)
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError(f'The zip holds {len(members)} entries; the limit is {MAX_MEMBERS}.')
        for info in members:
            name = info.filename.replace('\\', '/')
            if name.startswith('/') or '..' in name.split('/'):
                raise ValueError(f'Unsafe path in the zip: {name}')
            if (info.is_dir() or name.startswith('__MACOSX/') or '/.' in f'/{name}'
                    or os.path.basename(name).startswith('~$')):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            destination = os.path.realpath(os.path.join(root, name))
            if not destination.startswith(root + os.sep):
                raise ValueError(f'Unsafe path in the zip: {name}')
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise ValueError('The zip unpacks to more than 800 MB.')
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with zf.open(info) as src, open(destination, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            written += 1
    return written


def _context_choices():
    """Every active cohort, grouped by programme — the pack goes into one of them."""
    groups = []
    programmes = (models.Programme.objects.filter(is_active=True, institution__is_active=True)
                  .select_related('institution').order_by('institution__order', 'institution__code', 'order'))
    for programme in programmes:
        cohorts = list(programme.cohorts.filter(is_active=True).order_by('-start_date', 'code'))
        if cohorts:
            groups.append((programme, cohorts))
    return {'cohort_groups': groups, 'ai_enabled': ai.is_enabled()}


def _cohort(state):
    """The cohort the pack goes into (from the saved form); 404 if it no longer exists.
    """
    cohort = (models.Cohort.objects.select_related('programme__institution')
              .filter(pk=state['form'].get('cohort_id')).first())
    if cohort is None:
        raise Http404('That cohort no longer exists.')
    return cohort


def _sheet_rows(state):
    """The schedule sheet rows parsed from the saved CSV, with the lines that failed to
    parse.
    """
    rows, problems = sheet.parse_text((state.get('sheet') or {}).get('csv', ''))
    return rows, problems


def _refresh_check(token, state):
    """Re-derive the key and the module mapping after the sheet changed."""
    rows, problems = _sheet_rows(state)
    found = intake.check(state['plan'], _cohort(state), rows)
    state['sheet']['problems'] = problems
    state['check'] = found
    state['form']['key'] = found['key']
    _save(token, state)
    return state


@login_required
def upload(request):
    """Upload a programme pack zip (and optional schedule sheet) into a chosen cohort.

    The zip is extracted safely and scanned; the schedule sheet comes from the
    upload beside the zip, else from inside the pack, else (optionally) the AI's
    draft, which runs in a background thread. Redirects to the review screen. Admin
    and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden('Importing a programme pack is for administrators.')
    year = timezone.localdate().year
    context = {'page_title': 'Import content into a programme', **_context_choices(),
               'form': request.POST.dict() if request.method == 'POST' else {'year': year},
               'recent': _recent(), 'sheet_help': sheet.HEADER_HELP}
    if request.method != 'POST':
        return render(request, 'learning/content/programme_pack.html', context)

    archive = request.FILES.get('archive')
    sheet_upload = request.FILES.get('sheet')
    form = request.POST
    problems = []
    if archive is None or not archive.name.lower().endswith('.zip'):
        problems.append('Choose the pack as a .zip file.')
    elif archive.size > MAX_ZIP_BYTES:
        problems.append('The zip is larger than 400 MB.')
    cohort = (models.Cohort.objects.select_related('programme__institution')
              .filter(pk=form.get('cohort') if str(form.get('cohort') or '').isdigit() else None,
                      is_active=True).first())
    if cohort is None:
        problems.append('Choose the programme and cohort the content is for.')
    if sheet_upload is not None and not sheet_upload.name.lower().endswith(('.csv', '.xlsx')):
        problems.append('The schedule sheet must be a .csv or .xlsx file.')
    if problems:
        for problem in problems:
            messages.error(request, problem)
        return render(request, 'learning/content/programme_pack.html', context)

    token = uuid.uuid4().hex
    work = _workdir(token)
    os.makedirs(work, exist_ok=True)
    zip_path = os.path.join(work, 'upload.zip')
    with open(zip_path, 'wb') as fh:
        for chunk in archive.chunks():
            fh.write(chunk)
    try:
        count = safe_extract(zip_path, os.path.join(work, 'pack'))
    except (ValueError, zipfile.BadZipFile) as exc:
        shutil.rmtree(work, ignore_errors=True)
        messages.error(request, f'That zip could not be read: {exc}')
        return render(request, 'learning/content/programme_pack.html', context)

    year_value = int(form.get('year') or year) if str(form.get('year') or '').isdigit() else year
    plan = scanner.scan(os.path.join(work, 'pack'), year=year_value)

    if sheet_upload is not None:
        path = os.path.join(work, 'sheet' + os.path.splitext(sheet_upload.name)[1].lower())
        with open(path, 'wb') as fh:
            for chunk in sheet_upload.chunks():
                fh.write(chunk)
        rows, sheet_problems = sheet.read_file(path)
        sheet_state = {'source': 'uploaded', 'csv': sheet.to_csv(rows), 'problems': sheet_problems}
    else:
        rows, sheet_problems, text = intake.sheet_for(plan)
        sheet_state = ({'source': 'pack', 'csv': text, 'problems': sheet_problems} if text
                       else {'source': 'none', 'csv': '', 'problems': []})
    use_ai = form.get('use_ai') == '1'
    state = {
        'token': token, 'created_at': timezone.now().isoformat(), 'created_by': request.user.pk,
        'archive': archive.name, 'files_unpacked': count,
        'form': {'cohort_id': cohort.pk, 'cohort_label': f'{cohort.programme.full_code} · {cohort.code}',
                 'institution_code': cohort.programme.institution.code,
                 'programme_code': cohort.programme.code, 'cohort_code': cohort.code,
                 'year': year_value, 'use_ai': use_ai, 'key': ''},
        'plan': plan, 'summary': scanner.summary(plan), 'sheet': sheet_state,
        'ai': {'status': 'off'}, 'build': {'status': 'not started'},
    }
    if use_ai and ai.is_enabled():
        state['ai'] = {'status': 'running'}
    elif use_ai:
        state['ai'] = {'status': 'unavailable',
                       'message': 'The AI has no key yet — the folders and the sheet alone were used.'}
    _save(token, state)
    _refresh_check(token, state)
    if state['ai']['status'] == 'running':
        threading.Thread(target=_run_ai, args=(token,), daemon=True).start()
    messages.success(request, f'Read {count} files for {state["form"]["cohort_label"]}. Check it, then build.')
    return redirect('learning:programme-pack-review', token=token)


def _recent(limit=8):
    """The most recent pack uploads, newest first, for the upload page."""
    root = os.path.join(settings.MEDIA_ROOT, 'programme_packs')
    out = []
    try:
        names = sorted(os.listdir(root), key=lambda n: os.path.getmtime(os.path.join(root, n)), reverse=True)
    except OSError:
        return out
    for name in names[:limit]:
        if not TOKEN_RX.match(name):
            continue
        try:
            state = _load(name)
        except Exception:
            continue
        out.append({'token': name, 'archive': state.get('archive'), 'form': state.get('form', {}),
                    'build': state.get('build', {}), 'created_at': state.get('created_at', '')[:16]})
    return out


def _run_ai(token, close_connection=True):
    """The AI step: draft the sheet when the pack has none, and write unit summaries."""
    from django.db import connection
    try:
        state = _load(token)
        notes = []
        drafted = False
        if not (state.get('sheet') or {}).get('csv'):
            rows, problems = ai.draft_sheet(state['plan'], out=notes.append)
            if rows:
                state = _load(token)
                state['sheet'] = {'source': 'ai', 'csv': sheet.to_csv(rows), 'problems': problems}
                _save(token, state)
                drafted = True
            notes.extend(problems)
        enrichment = ai.enrich(state['plan'], out=notes.append)
        state = _load(token)
        state['ai'] = {'status': 'done', 'enrichment': enrichment, 'notes': notes, 'drafted_sheet': drafted}
        _save(token, state)
        if drafted:
            _refresh_check(token, state)
    except Exception as exc:          # pragma: no cover — the AI is optional
        logger.exception('programme pack: AI step failed')
        _update(token, ai={'status': 'failed', 'message': str(exc)})
    finally:
        if close_connection:
            connection.close()


def _spine(state):
    """The academic spine the pack would build into its cohort."""
    rows, _problems = _sheet_rows(state)
    return intake.spine_for(state['plan'], _cohort(state), rows, state['form']['key'] or 'pack')


@login_required
def review(request, token):
    """Review an uploaded pack: what was read, the weeks, sessions and dates, and any
    problems. Admin and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    cohort = _cohort(state)
    spine = _spine(state)
    running = state['build'].get('status') == 'running' or state['ai'].get('status') == 'running'
    check = state.get('check') or {}
    offering_links = [(o, reverse('learning:module-feed', args=[o.pk]))
                      for o in cohort.programme.modules.order_by('order', 'pk')
                      if o.code in {m[1] for m in check.get('mapping', [])}]
    return render(request, 'learning/content/programme_pack_review.html', {
        'page_title': 'Import content', 'state': state, 'token': token, 'spine': spine, 'cohort': cohort,
        'plan': state['plan'], 'summary': state['summary'], 'running': running, 'check': check,
        'can_build': not check.get('problems') and not running,
        'ai_enabled': ai.is_enabled(), 'offering_links': offering_links, 'sheet_help': sheet.HEADER_HELP,
    })


@login_required
@require_POST
def save_sheet(request, token):
    """Save the corrected schedule sheet (edited on the review screen)."""
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    if state['build'].get('status') == 'running':
        messages.info(request, 'Wait for the build to finish first.')
        return redirect('learning:programme-pack-review', token=token)
    text = request.POST.get('csv', '')
    rows, problems = sheet.parse_text(text)
    state['sheet'] = {'source': 'edited', 'csv': sheet.to_csv(rows) if rows else text, 'problems': problems}
    _save(token, state)
    _refresh_check(token, state)
    if problems:
        messages.warning(request, f'Saved {len(rows)} row(s); {len(problems)} line(s) need fixing (listed below).')
    else:
        messages.success(request, f'Sheet saved: {len(rows)} row(s).')
    return redirect('learning:programme-pack-review', token=token)


@login_required
def sheet_file(request, token):
    """The sheet as schedule.csv — keep it in the pack's Start here folder for next time."""
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    response = HttpResponse((state.get('sheet') or {}).get('csv') or sheet.HEADER_HELP,
                            content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="schedule.csv"'
    return response


@login_required
@require_POST
def build(request, token):
    """Start building the reviewed pack into its cohort in a background thread.

    Refused while a build or the AI step is running, or while the check still lists
    problems. Admin and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    if state['build'].get('status') == 'running':
        messages.info(request, 'Already building.')
        return redirect('learning:programme-pack-review', token=token)
    if state['ai'].get('status') == 'running':
        messages.info(request, 'Wait for the AI step to finish first.')
        return redirect('learning:programme-pack-review', token=token)
    if (state.get('check') or {}).get('problems'):
        messages.error(request, 'Fix the problems listed first — nothing was built.')
        return redirect('learning:programme-pack-review', token=token)
    _update(token, build={'status': 'running', 'started_at': timezone.now().isoformat()})
    threading.Thread(target=_run_build, args=(token, request.user.pk), daemon=True).start()
    messages.success(request, 'Building — this page refreshes until it is done.')
    return redirect('learning:programme-pack-review', token=token)


def _run_build(token, user_id, close_connection=True):
    """The background build: load the pack into its cohort and record the report.

    Progress is written to the state at most once a second; the review page polls
    :func:`build_status` for it. The thread closes its database connection when done.
    """
    from django.contrib.auth import get_user_model
    from django.db import connection

    try:
        state = _load(token)
        rows, _problems = _sheet_rows(state)
        actor = get_user_model().objects.filter(pk=user_id).first()
        enrichment = (state.get('ai') or {}).get('enrichment') or {}
        started = state['build'].get('started_at')
        last = [0.0]

        def progress(done, total, label):
            """Record build progress — at most one state write a second, except for the
            final step.
            """
            now = time.monotonic()
            if now - last[0] < 1 and done < total:
                return
            last[0] = now
            _update(token, build={'status': 'running', 'started_at': started,
                                  'progress': {'done': done, 'total': total, 'label': label}})

        made, report = intake.build_into(state['plan'], _cohort(state), rows, state['form']['key'],
                                         actor=actor, ai=enrichment, progress=progress)
        _update(token, build={'status': 'built', 'finished_at': timezone.now().isoformat(),
                              'report': report.lines(), 'totals': made.totals()})
    except Exception as exc:
        logger.exception('programme pack: build failed')
        _update(token, build={'status': 'failed', 'message': str(exc)})
    finally:
        if close_connection:
            connection.close()


@login_required
def build_status(request, token):
    """Where the AI step and the build are, as JSON — the review page polls this for its bar."""
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    build, ai_step = state.get('build') or {}, state.get('ai') or {}
    progress = build.get('progress') or {}
    total = progress.get('total') or 0
    return JsonResponse({
        'build': build.get('status'), 'ai': ai_step.get('status'),
        'done': progress.get('done', 0), 'total': total, 'label': progress.get('label', ''),
        'pct': int(100 * progress.get('done', 0) / total) if total else 0,
        'started_at': build.get('started_at'),
    })


@login_required
def seed_file(request, token):
    """Download the pack's spine as a Python seed file (``academic_spine_<key>.py``).
    Admin and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    spine = _spine(state)
    key = spine['key']
    source = codegen.render(spine, plan_summary=state['summary'], pack_hint=f'fixtures/{key}',
                            module_path=f'core/academic_spine_{key}.py')
    response = HttpResponse(source, content_type='text/x-python; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="academic_spine_{key}.py"'
    return response


@login_required
def plan_file(request, token):
    """Download what the folders were read as, with the spine, as JSON. Admin and staff
    only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    state = _load(token)
    response = HttpResponse(json.dumps({'plan': state['plan'], 'spine': _spine(state)}, indent=1, default=str),
                            content_type='application/json')
    response['Content-Disposition'] = f'attachment; filename="programme_pack_{token[:8]}.json"'
    return response
