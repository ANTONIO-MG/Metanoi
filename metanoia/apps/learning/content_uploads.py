"""The content queue: upload a document, or ask the AI to write one — then
review the draft, approve it, publish it.

Screens (admin & staff only):

    /learning/content/              the queue — everything waiting for a human
    /learning/content/upload/       upload a document + its .json sidecar + its .md text
    /learning/content/create/       ask the AI assistant to write blueprints / guides / weeks
    /learning/content/<uuid>/       review one draft: placement, preview, problems, JSON
    /learning/content/format/       the standard format, and a sample for every type

States: **Drafted** (pack held on the
:class:`~apps.learning.upload_models.ContentUpload`) → **Approved** (imported
via :func:`core.content_pack.import_bundle` as staff-only drafts) →
**Published** (:func:`core.content_pack.publish_imported`). Nothing skips a step.

Module settings: ``SAMPLES_DIR`` is where the format samples live
(``docs/upload_format``); ``MAX_FILES`` and ``MAX_BYTES`` cap one upload (documents
per request, 40 MB in total); ``MATERIAL_KIND_BY_TYPE`` maps an upload's document
type to the :class:`~apps.learning.models.ModuleMaterial` kind its file is placed
as; ``SAMPLE_TYPES`` lists the types shown on the format page.
"""

import io
import json
import logging
import os
import re
import zipfile

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core import content_pack
from core.content_pack import authoring, upload_format
from core.roles import role_flags

from . import models
from .upload_models import ContentUpload, ContentUploadFile

logger = logging.getLogger('apps')

SAMPLES_DIR = os.path.join(settings.BASE_DIR, 'docs', 'upload_format')
MAX_FILES = 10
MAX_BYTES = 40 * 1024 * 1024


def _guard(request):
    """``True`` when the request comes from an administrator or staff member."""
    return role_flags(request)['is_admin_staff']


def _offerings():
    """Every active module offering, ordered by institution, programme and code."""
    return (models.ProgrammeModule.objects.filter(is_active=True)
            .select_related('programme__institution')
            .order_by('programme__institution__code', 'programme__code', 'code'))


@login_required
def queue(request):
    """The content queue — uploads filtered by ``?status=`` (``open`` by default, or
    ``all``).

    Shows the count per status and whether AI drafting is enabled. Admin and staff
    only.
    """
    if not _guard(request):
        return HttpResponseForbidden('The content queue is for administrators.')
    status = request.GET.get('status') or 'open'
    uploads = ContentUpload.objects.select_related(
        'programme_module__programme__institution', 'cohort', 'created_by')
    if status == 'open':
        uploads = uploads.filter(status__in=ContentUpload.OPEN_STATUSES)
    elif status != 'all':
        uploads = uploads.filter(status=status)
    counts = {s: ContentUpload.objects.filter(status=s).count() for s, _ in ContentUpload.STATUS_CHOICES}
    return render(request, 'learning/content/queue.html', {
        'page_title': 'Content queue',
        'uploads': uploads[:200],
        'status': status,
        'counts': counts,
        'open_count': sum(counts[s] for s in ContentUpload.OPEN_STATUSES),
        'statuses': ContentUpload.STATUS_CHOICES,
        'ai_enabled': content_pack.drafting.is_enabled(),
    })


@login_required
def upload(request):
    """Upload one document with its sidecar and text — or several documents at
    once, each matched to its own ``.json`` / ``.md`` by name."""
    if not _guard(request):
        return HttpResponseForbidden('Uploading content is for administrators.')
    context = {'page_title': 'Upload content', 'offerings': _offerings(),
               'doc_types': ContentUpload.TYPE_CHOICES, 'form': request.POST.dict()}
    if request.method != 'POST':
        return render(request, 'learning/content/upload.html', context)

    files = request.FILES.getlist('files')[:MAX_FILES * 3]
    if not files:
        messages.error(request, 'Choose the document, its .json sidecar and its .md text.')
        return render(request, 'learning/content/upload.html', context)
    if sum(f.size for f in files) > MAX_BYTES:
        messages.error(request, 'Those files are too large together (max 40 MB).')
        return render(request, 'learning/content/upload.html', context)

    groups = _group_by_name(files)
    created = []
    for stem, members in groups.items():
        try:
            created.append(_create_upload(request, stem, members))
        except ValueError as exc:
            messages.error(request, f'{stem}: {exc}')
    if not created:
        return render(request, 'learning/content/upload.html', context)

    auto_draft = request.POST.get('auto_draft') == '1'
    for item in created:
        if auto_draft:
            _draft(item, allow_ai=True)
    if len(created) == 1:
        messages.success(request, f'Uploaded "{created[0].title}". Review the draft below.')
        return redirect('learning:content-review', public_id=created[0].public_id)
    messages.success(request, f'{len(created)} documents uploaded. Review each draft from the queue.')
    return redirect('learning:content-queue')


def _group_by_name(files):
    """``{stem: {'document': [...], 'sidecar': f, 'text': f}}`` — a file and its
    ``.json`` / ``.md`` share a name (``X.pdf`` · ``X.pdf.json`` or ``X.json`` · ``X.md``)."""
    groups = {}
    for f in files:
        name = f.name
        lower = name.lower()
        if lower.endswith('.json'):
            role, stem = 'sidecar', re.sub(r'(\.(pdf|docx|xlsx|md))?\.json$', '', name, flags=re.I)
        elif lower.endswith('.md'):
            role, stem = 'text', re.sub(r'(\.(pdf|docx|xlsx))?\.md$', '', name, flags=re.I)
        else:
            role, stem = 'document', os.path.splitext(name)[0]
        group = groups.setdefault(stem, {'document': [], 'sidecar': None, 'text': None})
        if role == 'document':
            group['document'].append(f)
        else:
            group[role] = f
    return groups


def _create_upload(request, stem, members):
    """Create one :class:`ContentUpload` (with its files) from a group of uploaded
    files.

    The ``.json`` sidecar is read first; the form fills in anything the sidecar
    leaves out (module, document type, intake, week, assessment). The sidecar is
    normalised and, when it has no problems, placed on a module and intake. Raises
    :class:`ValueError` when the sidecar is not valid JSON.
    """
    raw = {}
    if members['sidecar'] is not None:
        try:
            raw = json.loads(members['sidecar'].read().decode('utf-8', errors='replace'))
            members['sidecar'].seek(0)
        except json.JSONDecodeError as exc:
            raise ValueError(f'the .json sidecar is not valid JSON ({exc})')
    form = request.POST
    offering = models.ProgrammeModule.objects.filter(pk=form.get('offering') or 0).select_related(
        'programme__institution').first()
    if offering is not None:
        raw.setdefault('institution', offering.programme.institution.code)
        raw.setdefault('programme', offering.programme.code)
        raw.setdefault('module', offering.code)
    for key in ('doc_type', 'cohort', 'week'):
        if form.get(key) and not raw.get(key):
            raw[key] = form.get(key)
    if form.get('assessment_kind') and not raw.get('assessment'):
        raw['assessment'] = {'kind': form.get('assessment_kind'), 'sequence': form.get('assessment_sequence') or 1}

    first = members['document'][0].name if members['document'] else stem
    meta, problems = upload_format.normalise_sidecar(raw, filename=first)
    text = ''
    if members['text'] is not None:
        text = members['text'].read().decode('utf-8', errors='replace')
        members['text'].seek(0)

    item = ContentUpload(
        title=meta['title'] or stem, doc_type=meta['doc_type'] or 'reference',
        metadata=meta, source_text=text, problems=problems, created_by=request.user,
        audience=meta['audience'], phase_kind=meta['assessment']['kind'],
        phase_sequence=meta['assessment']['sequence'], week_number=meta.get('week'),
        topic_code=(meta.get('topic') or {}).get('code', ''),
        topic_title=(meta.get('topic') or {}).get('title', ''),
        used_on=meta.get('used_on') or None)
    placed, cohort, placement_problems = upload_format.resolve_placement(meta) if not problems else (None, None, [])
    item.programme_module, item.cohort = placed, cohort
    item.warnings = placement_problems
    item.status = ContentUpload.STATUS_NEEDS_FIX if (problems or placed is None) else ContentUpload.STATUS_UPLOADED
    item.save()
    for role, fileobj in ([('document', f) for f in members['document']]
                          + [('sidecar', members['sidecar']), ('text', members['text'])]):
        if fileobj is None:
            continue
        ContentUploadFile.objects.create(upload=item, role=role, file=fileobj,
                                         original_name=fileobj.name[:255], size=fileobj.size)
    return item


def _document_text(item):
    """The ``.md`` text, else text extracted from the uploaded document(s).

    An unreadable file is recorded as a warning on the upload, not a crash.
    """
    if item.source_text.strip():
        return item.source_text
    pieces = []
    for f in item.files.filter(role=ContentUploadFile.ROLE_DOCUMENT):
        try:
            with f.file.open('rb') as fh:
                text, _meta = content_pack.extract(fh.read(), f.original_name)
            pieces.append(f'===== {f.original_name} =====\n{text}')
        except Exception as exc:
            item.warnings = (item.warnings or []) + [f'{f.original_name}: {exc}']
    return '\n\n'.join(pieces)


def _draft(item, *, allow_ai):
    """Draft the content for an upload. Template first; the AI only if needed.

    File-only types (and planners and recordings) are placed as a file, link or
    calendar entries — there is nothing to draft, so they go straight to *drafted*.
    """
    offering = item.programme_module
    if offering is None:
        item.status = ContentUpload.STATUS_NEEDS_FIX
        item.message = 'Place the document (module, intake, test) before drafting.'
        item.save()
        return item
    meta = dict(item.metadata, doc_type=item.doc_type, audience=item.audience,
                assessment={'kind': item.phase_kind, 'sequence': item.phase_sequence},
                week=item.week_number, cohort=item.cohort.code if item.cohort_id else '')
    if item.topic_code or item.topic_title:
        meta['topic'] = {'code': item.topic_code, 'title': item.topic_title or item.title}

    if item.doc_type in upload_format.FILE_ONLY + ('planner', 'recording'):
        item.draft_method = ContentUpload.METHOD_FILE
        item.pack = {}
        item.problems = []
        item.status = ContentUpload.STATUS_DRAFTED
        item.message = 'Will be placed as a document on the schedule — no lesson or quiz.'
        item.save()
        return item

    text = item.source_text if item.source_text.strip() else ''
    if text and upload_format.looks_like_template(item.doc_type, text):
        pack, problems = upload_format.pack_from_template(text, meta, offering)
        item.draft_method = ContentUpload.METHOD_TEMPLATE
        problems += content_pack.validate(pack)
        item.pack, item.problems = pack, problems
    elif allow_ai:
        text = text or _document_text(item)
        if not text.strip():
            item.status = ContentUpload.STATUS_NEEDS_FIX
            item.message = 'No text could be read. Upload the .md text, or a text-based PDF/Word file.'
            item.save()
            return item
        try:
            pack, problems = _ai_draft(item, meta, offering, text)
        except content_pack.DraftingError as exc:
            item.status = ContentUpload.STATUS_FAILED
            item.message = str(exc)
            item.save()
            return item
        item.draft_method = ContentUpload.METHOD_AI
        item.pack, item.problems = pack, problems
    else:
        item.status = ContentUpload.STATUS_NEEDS_FIX
        item.message = ('The .md does not follow the template for this type. Ask the AI to draft it, '
                        'or fix the .md (see the format page).')
        item.save()
        return item
    item.status = ContentUpload.STATUS_NEEDS_FIX if item.problems else ContentUpload.STATUS_DRAFTED
    item.message = ''
    item.save()
    return item


def _ai_draft(item, meta, offering, text):
    """A document that does not follow the template, drafted by Claude."""
    pack_programme = f'{offering.programme.institution.code}-{offering.programme.code}'
    topic = meta.get('topic') or {}
    week = meta.get('week')
    code = authoring.topic_code(item.phase_kind, item.phase_sequence, week) if week else (
        topic.get('code') or f'{item.phase_kind[:1].upper()}{item.phase_sequence}')
    want = {
        'study_guide': 'Produce exactly ONE item of type "study_guide" from this document.',
        'blueprint': 'Produce exactly ONE item of type "blueprint" (same shape as a study guide: '
                     'sections with heading and body_html) from this document.',
        'questions': 'Produce ONE "challenge" item (week set) from the question paper and its answers; '
                     'every REQUIRED part marked against a rubric from the answer sheet. If the source '
                     'is multiple-choice, produce a "quiz" item with choices instead.',
        'mock_exam': 'Produce ONE "exam" item from the mock paper and its answer sheet.',
    }.get(item.doc_type, '')
    pack, problems = content_pack.draft_pack(
        text, module=offering.code, programme=pack_programme,
        topic_code=code[:20], topic_title=(topic.get('title') or item.title)[:250],
        cohort=item.cohort.code if item.cohort_id else '', week=week,
        note=want + ('\n' + item.metadata.get('notes', '') if item.metadata.get('notes') else ''))
    pack['phase'] = {'kind': item.phase_kind, 'sequence': item.phase_sequence}
    pack['audience'] = item.audience
    if item.doc_type == 'blueprint':
        for it in pack.get('items') or []:
            if it.get('type') == 'study_guide':
                it['type'] = 'blueprint'
    return pack, content_pack.validate(pack)


@login_required
def create(request):
    """Ask the AI assistant to write content (blueprints, guides, weeks) for a module.

    Optionally reads uploaded source documents and the material already on the
    block, then holds the drafted bundle as a new :class:`ContentUpload` for review.
    Admin and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden('The AI content assistant is for administrators.')
    offerings = _offerings()
    context = {
        'page_title': 'Create content with AI',
        'offerings': offerings,
        'cohorts': models.Cohort.objects.filter(is_active=True).select_related('programme__institution')
                                         .order_by('programme__institution__code', 'programme__code', 'code'),
        'ai_enabled': content_pack.drafting.is_enabled(),
        'form': request.POST.dict() if request.method == 'POST' else {'wants': ['study_guide', 'quiz']},
        'wants': authoring.WANTS,
    }
    if request.method != 'POST':
        return render(request, 'learning/content/create.html', context)

    offering = offerings.filter(pk=request.POST.get('offering') or 0).first()
    prompt = (request.POST.get('prompt') or '').strip()
    if offering is None or not prompt:
        messages.error(request, 'Choose the module and describe what you want written.')
        return render(request, 'learning/content/create.html', context)
    cohort = models.Cohort.objects.filter(pk=request.POST.get('cohort') or 0,
                                          programme=offering.programme).first()
    kind = request.POST.get('phase_kind') or 'test'
    sequence = _int(request.POST.get('phase_sequence'), 1)
    first, last = _int(request.POST.get('week_from'), 1), _int(request.POST.get('week_to'), 1)
    weeks = list(range(min(first, last), max(first, last) + 1))[:8]
    wants = request.POST.getlist('wants') or ['study_guide']
    topics = [t.strip() for t in (request.POST.get('topics') or '').splitlines() if t.strip()]

    sources = ''
    uploads = request.FILES.getlist('sources')[:MAX_FILES]
    if uploads:
        text, _metas, errors = content_pack.extract_many([(f.read(), f.name) for f in uploads])
        for error in errors:
            messages.warning(request, error)
        sources = text
    if request.POST.get('use_module_material') == '1':
        sources += _module_context(offering, cohort, kind, sequence)

    try:
        bundle, problems = authoring.draft_from_prompt(
            prompt, offering=offering, cohort=cohort, phase_kind=kind, phase_sequence=sequence,
            weeks=weeks, wants=wants, topics=topics, sources=sources)
    except content_pack.DraftingError as exc:
        messages.error(request, str(exc))
        return render(request, 'learning/content/create.html', context)

    doc_type = 'blueprint' if wants == ['blueprint'] else 'study_guide'
    item = ContentUpload.objects.create(
        title=_bundle_title(bundle, offering, kind, sequence), doc_type=doc_type,
        programme_module=offering, cohort=cohort, phase_kind=kind, phase_sequence=sequence,
        week_number=weeks[0], audience='students',
        metadata={'format': upload_format.UPLOAD_VERSION, 'origin': 'prompt', 'prompt': prompt[:8000],
                  'weeks': weeks, 'wants': wants, 'topics': topics,
                  'sources': [f.name for f in uploads]},
        draft_method=ContentUpload.METHOD_AI, pack=bundle, problems=problems,
        status=ContentUpload.STATUS_NEEDS_FIX if problems else ContentUpload.STATUS_DRAFTED,
        created_by=request.user)
    messages.success(request, 'Drafted. Read it through, fix anything, then approve and publish.')
    return redirect('learning:content-review', public_id=item.public_id)


def _module_context(offering, cohort, kind, sequence):
    """The text of the lessons already on this block — so a new week builds on
    the blueprint and guides the class already has."""
    from django.db.models import Q
    phases = models.ModulePhase.objects.filter(programme_module=offering, kind=kind, sequence=sequence)
    phases = phases.filter(Q(cohort=cohort) | Q(cohort__isnull=True)) if cohort else phases
    lessons = (models.Lesson.objects.filter(module_materials__phase__in=phases)
               .distinct().prefetch_related('blocks')[:8])
    out = []
    for lesson in lessons:
        body = ' '.join(re.sub(r'<[^>]+>', ' ', (b.data or {}).get('html', '')) for b in lesson.blocks.all())
        out.append(f'===== EXISTING: {lesson.title} =====\n{" ".join(body.split())[:30000]}')
    return ('\n\nMATERIAL ALREADY ON THIS BLOCK:\n\n' + '\n\n'.join(out)) if out else ''


def _bundle_title(bundle, offering, kind, sequence):
    """A queue title for a drafted bundle: module, block and the first item's title."""
    noun = {'test': 'Test', 'exam': 'Exam'}.get(kind, 'Block')
    titles = [it.get('title', '') for p in content_pack.packs_of(bundle) for it in (p.get('items') or [])]
    head = titles[0] if titles else 'AI draft'
    more = f' + {len(titles) - 1} more' if len(titles) > 1 else ''
    return f'{offering.code} {noun} {sequence} · {head}{more}'[:200]


@login_required
def review(request, public_id):
    """Review one draft: its placement, a student-eye preview, problems and JSON. Admin
    and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    item = get_object_or_404(ContentUpload.objects.select_related(
        'programme_module__programme__institution', 'cohort', 'created_by', 'reviewed_by'),
        public_id=public_id)
    preview = _preview(item.pack) if item.pack else []
    return render(request, 'learning/content/review.html', {
        'page_title': f'Review — {item.title}',
        'item': item,
        'preview': preview,
        'pack_json': json.dumps(item.pack, indent=2, ensure_ascii=False) if item.pack else '',
        'files': item.files.all(),
        'offerings': _offerings(),
        'cohorts': (models.Cohort.objects.filter(programme=item.programme_module.programme)
                    if item.programme_module_id else models.Cohort.objects.none()),
        'doc_types': ContentUpload.TYPE_CHOICES,
        'ai_enabled': content_pack.drafting.is_enabled(),
        'build_url': (reverse('learning:module-build', args=[item.programme_module_id])
                      if item.programme_module_id else ''),
        'feed_url': (reverse('learning:module-feed', args=[item.programme_module_id])
                     + (f'?cohort={item.cohort_id}' if item.cohort_id else '')
                     if item.programme_module_id else ''),
    })


def _preview(bundle):
    """What a student will see, item by item, so the reviewer reads content
    rather than JSON."""
    out = []
    for pack in content_pack.packs_of(bundle):
        if not isinstance(pack, dict):
            continue
        week = {'week': pack.get('week'), 'title': pack.get('week_title') or (pack.get('topic') or {}).get('title', ''),
                'items': []}
        for it in pack.get('items') or []:
            entry = {'type': it.get('type'), 'title': it.get('title'), 'sections': it.get('sections') or [],
                     'scenario_html': it.get('scenario_html', ''), 'parts': it.get('parts') or [],
                     'total_marks': it.get('total_marks'),
                     'minutes': sum(max(1, round(len(re.sub(r'<[^>]+>', ' ', s.get('body_html', '')).split()) / 200))
                                    for s in it.get('sections') or [])}
            week['items'].append(entry)
        out.append(week)
    return out


@login_required
@require_POST
def action(request, public_id):
    """Everything the review page can do, one POST each."""
    if not _guard(request):
        return HttpResponseForbidden()
    item = get_object_or_404(ContentUpload, public_id=public_id)
    act = request.POST.get('action')
    back = redirect('learning:content-review', public_id=item.public_id)

    if act == 'place':
        offering = _offerings().filter(pk=request.POST.get('offering') or 0).first()
        if offering is None:
            messages.error(request, 'Choose a module.')
            return back
        item.programme_module = offering
        item.cohort = models.Cohort.objects.filter(pk=request.POST.get('cohort') or 0,
                                                   programme=offering.programme).first()
        item.doc_type = request.POST.get('doc_type') or item.doc_type
        item.phase_kind = request.POST.get('phase_kind') or item.phase_kind
        item.phase_sequence = _int(request.POST.get('phase_sequence'), item.phase_sequence)
        item.week_number = _int(request.POST.get('week_number'), None)
        item.audience = 'staff' if request.POST.get('audience') == 'staff' else 'students'
        item.status = ContentUpload.STATUS_UPLOADED if not item.pack else item.status
        item.save()
        if item.pack:
            _repin(item)
        messages.success(request, f'Placed on {item.placement_label}.')
        return back

    if act in ('draft', 'redraft'):
        _draft(item, allow_ai=True)
        if item.status == ContentUpload.STATUS_FAILED:
            messages.error(request, item.message)
        else:
            messages.success(request, 'Drafted. Review it below.')
        return back

    if act == 'save_json':
        try:
            pack = json.loads(request.POST.get('pack_json') or '{}')
        except json.JSONDecodeError as exc:
            messages.error(request, f'Not valid JSON — {exc}. Nothing saved.')
            return back
        item.pack = pack
        item.problems = content_pack.validate_bundle(pack)
        item.status = ContentUpload.STATUS_NEEDS_FIX if item.problems else ContentUpload.STATUS_DRAFTED
        item.save()
        messages.success(request, 'Saved.' + (f' {len(item.problems)} problem(s) to fix.' if item.problems else ''))
        return back

    if act == 'reject':
        item.mark_reviewed(request.user, ContentUpload.STATUS_REJECTED)
        item.message = (request.POST.get('reason') or '').strip()[:1000]
        item.save()
        messages.info(request, 'Rejected. Nothing was added to the course.')
        return redirect('learning:content-queue')

    if act in ('approve', 'approve_publish'):
        problems = _approve(item, request.user)
        if problems:
            for p in problems[:8]:
                messages.error(request, p)
            return back
        if act == 'approve':
            messages.success(request, 'Approved: it is on the module schedule as a draft only staff can see. '
                                      'Publish it when you are ready.')
            return back
        act = 'publish'

    if act == 'publish':
        if item.status not in (ContentUpload.STATUS_APPROVED, ContentUpload.STATUS_PUBLISHED):
            messages.error(request, 'Approve it first.')
            return back
        problems = content_pack.publish_imported(item.result, actor=request.user)
        item.mark_reviewed(request.user, ContentUpload.STATUS_PUBLISHED)
        item.save()
        for p in problems:
            messages.warning(request, p)
        who = item.cohort.code if item.cohort_id else 'every intake'
        messages.success(request, f'Published to {who} on {item.programme_module}.')
        return back

    messages.error(request, 'Unknown action.')
    return back


def _repin(item):
    """After the reviewer moves an upload, move its draft with it."""
    if item.metadata.get('origin') == 'prompt' or (item.pack or {}).get('pack') == content_pack.BUNDLE_VERSION:
        item.pack, item.problems = authoring.pin(item.pack, offering=item.programme_module, cohort=item.cohort,
                                                 phase_kind=item.phase_kind, phase_sequence=item.phase_sequence)
    else:
        pack = item.pack
        pack['module'] = item.programme_module.code
        pack['programme'] = f'{item.programme_module.programme.institution.code}-{item.programme_module.programme.code}'
        pack['phase'] = {'kind': item.phase_kind, 'sequence': item.phase_sequence}
        if item.cohort_id:
            pack['cohort'] = item.cohort.code
        else:
            pack.pop('cohort', None)
        if item.week_number:
            pack['week'] = item.week_number
            pack.setdefault('topic', {})['code'] = authoring.topic_code(item.phase_kind, item.phase_sequence,
                                                                        item.week_number)
        item.problems = content_pack.validate(pack)
    item.save()


@transaction.atomic
def _approve(item, user):
    """Import the draft (as drafts) and place the uploaded file(s) beside it.

    The reviewer's audience is final: it is stamped on every pack and item before
    importing, so a draft written for students cannot reach them once marked
    staff-only. Returns a list of problems; empty means approved.
    """
    if item.programme_module_id is None:
        return ['Place it on a module first.']
    result = {'lessons': [], 'assessments': [], 'materials': [], 'weeks': [], 'phases': []}
    if item.pack:
        for pack in content_pack.packs_of(item.pack):
            pack['audience'] = item.audience
            for it in pack.get('items') or []:
                it['audience'] = item.audience
        problems = content_pack.validate_bundle(item.pack)
        if problems:
            return [f'{len(problems)} problem(s) in the draft — fix them first.'] + problems[:5]
        try:
            imported = content_pack.import_bundle(item.pack, actor=user)
        except content_pack.PackImportError as exc:
            return [str(exc)]
        result = imported.as_dict()
    week = _placement_week(item, result)
    for f in item.files.filter(role=ContentUploadFile.ROLE_DOCUMENT):
        material = _place_file(item, f, week, user)
        result['materials'].append(material.pk)
    if item.metadata.get('video_url') or item.metadata.get('live_url'):
        result['materials'].append(_place_link(item, week, user).pk)
    if item.doc_type == 'planner' and item.source_text:
        result['events'] = _place_calendar(item)
    for key in ('weeks', 'phases'):
        result[key] = sorted(set(result.get(key) or []) | ({week.pk} if key == 'weeks' else {week.phase_id}))
    item.result = result
    item.mark_reviewed(user, ContentUpload.STATUS_APPROVED)
    item.save()
    return []


def _placement_week(item, result):
    """The week the upload's files go in: the imported week, else the week the
    sidecar names, else the block's revision week."""
    from apps.learning.models import ModulePhase, ModuleWeek
    if result.get('weeks'):
        return ModuleWeek.objects.get(pk=result['weeks'][0])
    phase, _ = ModulePhase.objects.get_or_create(
        programme_module=item.programme_module, cohort=item.cohort, kind=item.phase_kind,
        sequence=item.phase_sequence, defaults={'order': item.phase_sequence, 'is_published': False})
    if item.week_number:
        week, _ = ModuleWeek.objects.get_or_create(phase=phase, number=item.week_number,
                                                   defaults={'order': item.week_number, 'is_published': False})
        return week
    from core.metanoia_materials.seed import SLOT_NUMBER, SLOT_TITLES
    slot = {'past_paper': 'past', 'reference': 'coach', 'mock_exam': 'mocks'}.get(item.doc_type, 'revision')
    week = ModuleWeek.objects.filter(phase=phase, title=SLOT_TITLES[slot]).first()
    if week is None:
        week = ModuleWeek.objects.create(phase=phase, number=SLOT_NUMBER[slot], order=SLOT_NUMBER[slot],
                                         title=SLOT_TITLES[slot], is_published=False)
    return week


MATERIAL_KIND_BY_TYPE = {
    'study_guide': 'study_guide', 'blueprint': 'blueprint', 'questions': 'questions',
    'mock_exam': 'mock_exam', 'past_paper': 'mock_exam', 'answers': 'answers',
    'planner': 'document', 'recording': 'recording', 'reference': 'document',
}


def _place_file(item, f, week, user):
    """Place one uploaded document as a draft
    :class:`~apps.learning.models.ModuleMaterial` in ``week``.

    A file whose name says solution, answer, memo or mark plan is filed as answers.
    """
    kind = MATERIAL_KIND_BY_TYPE.get(item.doc_type, 'document')
    if re.search(r'solution|answer|memo|mark.?plan', f.original_name, re.I):
        kind = 'answers'
    material, _ = models.ModuleMaterial.objects.update_or_create(
        source_ref=f'{item.source_ref}/file/{f.pk}',
        defaults={'phase': week.phase, 'week': week, 'kind': kind,
                  'title': item.title if kind != 'answers' else f'{item.title} — answers',
                  'file': f.file.name, 'audience': item.audience, 'is_published': False,
                  'used_on': item.used_on, 'order': 60 if kind == 'answers' else 45, 'created_by': user})
    return material


def _place_link(item, week, user):
    """Place the upload's video or live-session link as a draft material in ``week``."""
    url = item.metadata.get('video_url') or item.metadata.get('live_url')
    kind = 'recording' if item.metadata.get('video_url') else 'live'
    material, _ = models.ModuleMaterial.objects.update_or_create(
        source_ref=f'{item.source_ref}/link',
        defaults={'phase': week.phase, 'week': week, 'kind': kind, 'title': item.title, 'url': url,
                  'audience': item.audience, 'is_published': False, 'order': 15, 'created_by': user})
    return material


def _place_calendar(item):
    """Planner rows (``| 17 Aug 2026 | CGAU Test 1 | test |``) → calendar events."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    offering = item.programme_module
    calendar, _ = models.AcademicCalendar.objects.get_or_create(
        institution=offering.programme.institution, year=2026)
    ids = []
    for line in item.source_text.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')] if line.strip().startswith('|') else []
        if len(cells) < 2:
            continue
        try:
            day = dt.datetime.strptime(cells[0], '%d %b %Y').date()
        except ValueError:
            try:
                day = dt.date.fromisoformat(cells[0])
            except ValueError:
                continue
        kind = (cells[2].lower() if len(cells) > 2 else 'other')
        kind = kind if kind in dict(models.CalendarEvent.KIND_CHOICES) else 'other'
        event, _ = models.CalendarEvent.objects.update_or_create(
            calendar=calendar, programme_module=offering, cohort=item.cohort, title=cells[1][:200],
            start=dt.datetime.combine(day, dt.time(8, 0), tzinfo=ZoneInfo('Africa/Johannesburg')),
            defaults={'programme': offering.programme, 'kind': kind, 'all_day': True,
                      'description': f'From planner upload "{item.title}".', 'is_published': True})
        ids.append(event.pk)
    return ids


SAMPLE_TYPES = [
    ('study_guide', 'Study guide', 'A reading lesson: one section per ## heading, reading time worked out for you.'),
    ('blueprint', 'Strategic blueprint', 'A reading lesson filed as the test\'s blueprint.'),
    ('questions', 'Questions & answers', 'A quiz students sit and submit: written parts marked against your '
                                         'marking guide, or multiple-choice parts marked instantly.'),
    ('mock_exam', 'Mock exam & answer sheet', 'A timed online mock with its full marking guide.'),
    ('past_paper', 'Past paper', 'Placed as a document in the Past papers week (with its solution).'),
    ('planner', 'Planner / schedule', 'Each table row becomes a calendar entry for the intake.'),
    ('recording', 'Live session / recording', 'A "Join live session" or "Watch recording" button in the week.'),
]


@login_required
def format_page(request):
    """The upload format page: the README and a sample for every document type. Admin
    and staff only.
    """
    if not _guard(request):
        return HttpResponseForbidden()
    samples = []
    for key, label, what in SAMPLE_TYPES:
        files = sorted(n for n in os.listdir(os.path.join(SAMPLES_DIR, key))) if os.path.isdir(
            os.path.join(SAMPLES_DIR, key)) else []
        samples.append({'key': key, 'label': label, 'what': what, 'files': files,
                        'md': _read_sample(key, '.md'), 'json': _read_sample(key, '.json')})
    readme = _read(os.path.join(SAMPLES_DIR, 'README.md'))
    return render(request, 'learning/content/format.html', {
        'page_title': 'Upload format & samples', 'samples': samples, 'readme': readme})


def _read(path):
    """The text of the file at ``path``, or ``''`` when it cannot be read."""
    try:
        return open(path, encoding='utf-8').read()
    except OSError:
        return ''


def _read_sample(key, ext):
    """The first sample file with extension ``ext`` in the ``key`` sample folder, or
    ``''``.
    """
    folder = os.path.join(SAMPLES_DIR, key)
    if not os.path.isdir(folder):
        return ''
    name = next((n for n in sorted(os.listdir(folder)) if n.endswith(ext)), None)
    return _read(os.path.join(folder, name)) if name else ''


@login_required
def sample_download(request, key=None):
    """One sample folder — or every sample — as a zip, ready to copy and fill in."""
    if not _guard(request):
        return HttpResponseForbidden()
    keys = [k for k, _l, _w in SAMPLE_TYPES]
    if key is not None and key not in keys:
        raise Http404
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(SAMPLES_DIR, 'README.md'), 'README.md')
        for k in ([key] if key else keys):
            folder = os.path.join(SAMPLES_DIR, k)
            for name in sorted(os.listdir(folder)):
                zf.write(os.path.join(folder, name), f'{k}/{name}')
    buffer.seek(0)
    name = f'metanoia-upload-sample-{key}.zip' if key else 'metanoia-upload-samples.zip'
    return FileResponse(buffer, as_attachment=True, filename=name)


@login_required
def file_download(request, public_id, file_id):
    """Download one file of an upload, under its original name. Admin and staff only."""
    if not _guard(request):
        return HttpResponseForbidden()
    f = get_object_or_404(ContentUploadFile, pk=file_id, upload__public_id=public_id)
    return FileResponse(f.file.open('rb'), as_attachment=True, filename=f.original_name)


def _int(value, default):
    """``int(value)``, or ``default`` when the value is missing or not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
