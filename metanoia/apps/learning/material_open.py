"""The one door onto a module material.

Every material is reached through :func:`material_open`, which asks both gates
before handing anything over:

* :class:`apps.learning.access.Gate` — have they paid / bought / are they staff
* :class:`apps.learning.sequence.SequenceGate` — have they earned it yet

Files are streamed (:class:`~django.http.FileResponse`), never redirected to
storage: a redirect would hand out the very URL this view keeps private.
"""

import logging
import mimetypes
import re

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.roles import role_flags

from . import models
from .access import Gate
from .file_check import file_is_complete
from .sequence import SequenceGate

logger = logging.getLogger('apps')


def _week_scope(material):
    """The materials the sequence gate reasons over — this material's own week.

    Phase-level material (no week) stands alone and is let through.
    """
    if material.week_id:
        return list(material.week.materials.all())
    return [material]


def material_state(user, material):
    """This viewer's final verdict on ``material`` — the commercial gate, then
    the teaching order on top of it. The one rule for opening a material, used
    by :func:`material_open` and by the raw file URL (``core.media_access``)."""
    gate = Gate(user, material.phase.programme_module, person=getattr(user, 'profile', None))
    state = gate.for_material(material)
    if state.visible and state.open:
        scope = _week_scope(material)
        sequence = SequenceGate(user, scope, bypass=gate.can_author)
        is_open, reason = sequence.check(material, scope)
        if not is_open:
            state.open, state.reason = False, reason
    return state


@login_required
def material_open(request, pk):
    """Open one material, if both gates allow it.

    Redirects to what the material points at — its lesson, assessment, live session
    (or, for a recording, the session page's recording) or link — or streams its
    file. A refusal goes back to the material's own row on the module schedule
    (``#mat-<pk>``, which opens and highlights it) carrying the reason.

    * An unpublished material the viewer may not author is a 404: saying "you may
      not see this" would confirm it exists.
    * A visibly truncated file (:func:`~apps.learning.file_check.file_is_complete`)
      is refused with a "being re-uploaded" message.
    * Students and parents watch YouTube links on the platform's own player and are
      never handed the youtube.com address (owner, 2026-11-09); teaching staff are
      redirected to the link itself.
    """
    material = get_object_or_404(
        models.ModuleMaterial.objects.select_related(
            'phase__programme_module__programme__institution', 'week', 'lesson',
            'assessment', 'meeting', 'product'),
        pk=pk)
    offering = material.phase.programme_module
    state = material_state(request.user, material)

    back = reverse('learning:module-feed', args=[offering.pk]) + f'#mat-{material.pk}'

    if not state.visible:
        raise Http404('No such material.')

    if not state.open:
        flash.warning(request, f'{material.title} — {state.reason_label}.')
        return redirect(back)

    if material.lesson_id:
        return redirect('learning:lesson-view', lesson_id=material.lesson_id)
    if material.assessment_id:
        return redirect('assessments:take', assessment_id=material.assessment_id)
    if material.meeting_id:
        if material.kind == models.ModuleMaterial.KIND_RECORDING:
            return redirect(material.meeting.get_absolute_url() + '#recording')
        return redirect(material.meeting.get_join_url())
    if material.file:
        if not file_is_complete(material.file):
            flash.warning(request, f'{material.title} — this file is being re-uploaded and '
                                   'cannot be opened yet. Please check back soon.')
            return redirect(back)
        return _stream(material)
    if material.url:
        embed = _youtube_embed(material.url)
        if embed and not role_flags(request)['can_teach']:
            return render(request, 'learning/material_player.html', {
                'material': material, 'offering': offering, 'embed_url': embed, 'back': back,
                'page_title': material.title,
            })
        return redirect(material.url)

    flash.info(request, f'{material.title} has nothing attached to it yet.')
    return redirect(back)


_YOUTUBE_ID = re.compile(
    r'(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})')


def _youtube_embed(url):
    """The in-page player address for a YouTube link, else ''. Minimal
    branding, no related videos from other channels, no annotations."""
    match = _YOUTUBE_ID.search(url or '')
    if not match:
        return ''
    return (f'https://www.youtube-nocookie.com/embed/{match.group(1)}'
            '?rel=0&modestbranding=1&iv_load_policy=3&playsinline=1')


def _stream(material):
    """Stream the material's file without disclosing its storage URL.

    Sent ``inline``: a PDF study guide should open in the browser, not land in
    Downloads. A file missing from storage is a 404.
    """
    name = material.file.name.rsplit('/', 1)[-1] or 'download'
    content_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    try:
        handle = material.file.open('rb')
    except (FileNotFoundError, OSError, ValueError):
        logger.exception('material %s: file missing from storage', material.pk)
        raise Http404('That file is no longer available.')
    return FileResponse(handle, content_type=content_type, filename=name, as_attachment=False)
