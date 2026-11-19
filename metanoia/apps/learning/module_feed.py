"""The module feed — one page per module offering, tabbed like a class profile.

The landing tab is the **Schedule** (phases → weeks → material); the other
tabs are only filters over the same material rows, never a second source of
truth.

Gating, all resolved in one pass so a row carries one verdict and one reason:

* :class:`apps.learning.access.Gate` — a locked row is still listed (you can
  see what you are missing) but carries no link, only "unlock" / "buy this item".
* :mod:`apps.learning.week_access` — a locked week shows its title, dates and
  "pay to unlock", never its material.
* :class:`apps.learning.sequence.SequenceGate` — teaching order within a week
  (guide → mock → solution once the attempt is in).

Only the previous/current/next week bodies render on first paint; the rest are
fetched progressively from :func:`module_weeks`.

Module settings:

``MODULE_TABS``
    The module's sections, in the order a student needs them. "Schedule" is
    first and is the landing tab: opening a module means opening its plan for
    the year.
``TAB_KINDS``
    Which material kinds each filter tab shows.
``LAZY_BATCH`` / ``LAZY_MAX``
    How many weeks the progressive loader asks for per request, and the most
    one request may ask for.
"""

import logging

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from core.profiles import _tabs, shell_base

from . import models
from .access import Gate
from .sequence import SequenceGate

logger = logging.getLogger('apps')

MODULE_TABS = [
    ('schedule', 'Schedule', 'bi-calendar3-week'),
    ('messages', 'Messages', 'bi-chat-left-text'),
    ('notifications', 'Notifications', 'bi-bell'),
    ('documents', 'Documents', 'bi-folder2-open'),
    ('live', 'Live sessions', 'bi-camera-video'),
    ('mocks', 'Mock exams', 'bi-file-earmark-ruled'),
    ('blueprints', 'Blueprints', 'bi-diagram-3'),
    ('assessments', 'Assessments', 'bi-ui-checks'),
]

TAB_KINDS = {
    'documents': [models.ModuleMaterial.KIND_DOCUMENT, models.ModuleMaterial.KIND_STUDY_GUIDE,
                  models.ModuleMaterial.KIND_QUESTIONS, models.ModuleMaterial.KIND_ANSWERS,
                  models.ModuleMaterial.KIND_BLUEPRINT],
    'live': [models.ModuleMaterial.KIND_LIVE, models.ModuleMaterial.KIND_RECORDING],
    'mocks': [models.ModuleMaterial.KIND_MOCK_EXAM],
    'blueprints': [models.ModuleMaterial.KIND_BLUEPRINT],
    'assessments': [models.ModuleMaterial.KIND_ASSESSMENT],
}


def _recordings(offering, cohort):
    """Imported YouTube videos mapped to this module (+ intake) and flagged for
    the schedule. Best-effort: never breaks the module page."""
    try:
        from apps.livesessions.imported import videos_for_module
        return list(videos_for_module(offering, cohort, surface='schedule'))
    except Exception:
        return []


def _load_phases(offering, gate, cohort=None, week_ids=None):
    """Every phase of the offering with its weeks and material, in one go.

    ``week_ids`` narrows the load to those weeks (and their phases, without
    phase-level material). Students see published rows only; staff and the
    offering's educators see drafts too.

    With ``cohort``: that intake's blocks plus shared (cohort-less) ones. With
    none: shared blocks only — except staff authors, who see every cohort's.
    """
    weeks = (models.ModuleWeek.objects.filter(is_active=True)
             .prefetch_related('week_topics__topic'))
    materials = (models.ModuleMaterial.objects
                 .select_related('lesson', 'assessment', 'meeting', 'product', 'week', 'topic'))
    phases = models.ModulePhase.objects.filter(programme_module=offering, is_active=True)

    if cohort is not None:
        phases = phases.filter(Q(cohort=cohort) | Q(cohort__isnull=True))
    elif not gate.can_author:
        phases = phases.filter(cohort__isnull=True)

    if not gate.can_author:
        weeks = weeks.filter(is_published=True)
        materials = materials.filter(is_published=True)
        phases = phases.filter(is_published=True)

    if week_ids is not None:
        weeks = weeks.filter(pk__in=week_ids)
        phases = phases.filter(weeks__pk__in=week_ids).distinct()

    prefetches = [Prefetch('weeks', queryset=weeks.prefetch_related(
        Prefetch('materials', queryset=materials)))]
    if week_ids is None:
        prefetches.append(Prefetch('materials', queryset=materials.filter(week__isnull=True),
                                   to_attr='phase_materials'))
    return list(phases.prefetch_related(*prefetches).select_related('calendar_event'))


def _week_row(week, gate, sequence=None, *, deferred=False):
    """One week of the accordion: its verdict, and its material rows.

    A locked week lists only what is open anyway (preview, bought) or sold on
    its own; the rest is held back and counted. ``deferred`` marks a week whose
    body is fetched after first paint.

    The week's materials are grouped under the topics it works through, in the
    topic order set on the week. Materials with no topic (or a topic not in this
    week's series) render at week level under ``loose``.
    """
    from .access import REASON_LABELS
    from .week_access import LOCK_OUT_OF_WINDOW

    week_open, week_reason = gate.week_verdict(week)
    week_materials = list(week.materials.all())
    all_states = gate.states_for(week_materials, sequence=sequence,
                                 week_materials=week_materials)
    states = [s for s in all_states if s.listed]
    by_material = {s.material.pk: s for s in states}
    next_step = sequence.next_step(week_materials) if (sequence and week_open) else None
    topics = week.topic_list
    week_topic_ids = [t.id for t in topics]
    topic_groups = [
        {'topic': topic,
         'materials': [by_material[m.pk] for m in week_materials
                       if m.topic_id == topic.id and m.pk in by_material]}
        for topic in topics
    ]
    loose = [by_material[m.pk] for m in week_materials
             if m.topic_id not in week_topic_ids and m.pk in by_material]
    return {
        'week': week,
        'open': week_open,
        'reason': week_reason,
        'reason_label': REASON_LABELS.get(week_reason, '') if week_open else '',
        'out_of_window': week_reason == LOCK_OUT_OF_WINDOW,
        'materials': states,
        'topic_groups': topic_groups,
        'loose': loose,
        'total': len(all_states),
        'locked': sum(1 for s in all_states if s.locked),
        'held_back': len(all_states) - len(states),
        'is_current': week.is_current,
        'deferred': deferred,
        'next_step_id': getattr(next_step, 'pk', None),
    }


def _schedule(phases, gate, sequence=None, render_ids=None):
    """Phases → weeks → material states: the accordion the Schedule tab renders.

    ``sequence`` applies only *within a week*. ``render_ids``, when given,
    limits which week bodies render now; the rest are ``deferred``. A block's
    ``materials`` span the whole block (the blueprint). A block is ``expanded`` on
    arrival when it is in progress, or holds the weeks rendered on first paint
    (this week and its neighbours).
    """
    rows = []
    for phase in phases:
        week_rows = []
        for week in phase.weeks.all():
            week_rows.append(_week_row(
                week, gate, sequence,
                deferred=render_ids is not None and week.pk not in render_ids))
        phase_states = gate.states_for(getattr(phase, 'phase_materials', []))
        total = len(phase_states) + sum(w['total'] for w in week_rows)
        rows.append({
            'phase': phase,
            'materials': phase_states,
            'weeks': week_rows,
            'total': total,
            'locked': len([s for s in phase_states if s.locked]) + sum(w['locked'] for w in week_rows),
            'days_away': phase.days_away,
            'is_current': phase.is_current,
            'expanded': phase.is_current or any(
                w['is_current'] or (render_ids is not None and not w['deferred'])
                for w in week_rows),
        })
    return rows


def _all_materials(phases):
    """Every material on the page, flat — what the sequence gate preloads from."""
    out = []
    for phase in phases:
        out.extend(getattr(phase, 'phase_materials', []))
        for week in phase.weeks.all():
            out.extend(week.materials.all())
    return out


def _flat_materials(phases, gate, kinds=None, sequence=None):
    """Every material of the offering as a flat, viewer-resolved list, newest
    released first — what the filter tabs show.

    Drafts (for students) and anything in a week that is locked for the viewer are
    left out — the week itself says so. A filter tab shows the same rows as the
    schedule, so it must reach the same verdict: a solution listed under
    "Documents" cannot be a way around the sequence.
    """
    out = []
    for phase in phases:
        buckets = [(None, list(getattr(phase, 'phase_materials', [])))]
        buckets += [(week, list(week.materials.all())) for week in phase.weeks.all()]
        for week, items in buckets:
            for material in items:
                if kinds and material.kind not in kinds:
                    continue
                state = gate.for_material(material)
                if not state.visible or not state.listed:
                    continue
                if sequence is not None and state.open:
                    is_open, reason = sequence.check(material, items)
                    if not is_open:
                        state.open, state.reason = False, reason
                out.append({'state': state, 'material': material, 'phase': phase, 'week': week})
    return out


def _counts(phases, gate):
    """Badge numbers for the tab strip — how much material sits behind each."""
    counts = {}
    for key, kinds in TAB_KINDS.items():
        n = len(_flat_materials(phases, gate, kinds))
        if n:
            counts[key] = n
    return counts


@login_required
def module_feed(request, pk, tab='schedule'):
    """One module offering, tabbed. Defaults to the schedule.

    Registration gate: you may look at a module you are enrolled on (locked or
    not), teach or run — a module you never registered for redirects to My
    Modules. The teaching order is resolved once for every material on the page;
    staff and the offering's educators bypass it, because they are writing the week.

    Context notes: ``base_template`` comes from :func:`core.profiles.shell_base`, so
    an HTMX tab click renders only ``#profContent`` while a full load or boosted
    navigation renders the whole shell. ``view_cohort`` and ``cohorts`` are the
    cohort whose schedule is shown and (for staff) the intakes they can switch
    between. ``recordings`` are imported YouTube recordings mapped to this module
    and intake and flagged for the schedule (:mod:`apps.livesessions.imported`) —
    none for one-on-one students, whose plan does not include class recordings.
    ``activity_items`` and ``files`` fill the shared shell's rail panels.
    """
    offering = get_object_or_404(
        models.ProgrammeModule.objects.select_related(
            'module', 'programme__institution', 'chat_group'),
        pk=pk, is_active=True)

    person = getattr(request.user, 'profile', None)
    gate = Gate(request.user, offering, person=person)

    if not _may_browse(gate, offering):
        flash.error(request, f'You are not registered for {offering.display_name}.')
        return redirect('learning:my-modules')

    valid = {key for key, _label, _icon in MODULE_TABS}
    if tab not in valid:
        tab = 'schedule'

    view_cohort = _view_cohort(request, offering, gate)
    phases = _load_phases(offering, gate, cohort=view_cohort)
    sequence = SequenceGate(request.user, _all_materials(phases),
                            bypass=gate.can_author)

    def url_for(key):
        """The URL of one tab of this module's feed."""
        return reverse('learning:module-feed-tab', args=[offering.pk, key])

    institution = offering.programme.institution
    ctx = {
        'base_template': shell_base(request),
        'page_title': offering.display_name,
        'offering': offering,
        'gate': gate,
        'view_cohort': view_cohort,
        'cohorts': list(offering.programme.cohorts.filter(is_active=True)),
        'profile': {
            'kind': 'module',
            'title': offering.display_name,
            'subtitle': f'{institution.display_name} · {offering.programme.display_name}',
            'avatar': institution.sharp_logo_url or _module_badge(offering, institution),
            'meta': _header_meta(offering, gate),
            'actions': _header_actions(offering, gate),
        },
        'tabs': _tabs(MODULE_TABS, url_for, tab, _counts(phases, gate)),
        'active_tab': tab,
        'accent': institution.accent_colour or '#1F3864',
        'recordings': [] if gate.one_on_one else _recordings(offering, view_cohort),
        'next_phase': _next_phase(phases),
        'activity_items': _rail_activity(offering, phases, gate, view_cohort, sequence=sequence),
        'files': _rail_files(offering, phases, gate, sequence=sequence),
        'documents_url': url_for('documents'),
    }
    ctx.update(_tab_payload(request, offering, phases, gate, tab, sequence, cohort=view_cohort))
    return render(request, 'learning/module_feed.html', ctx)


def _may_browse(gate, offering):
    """You may look at a module you are enrolled on (locked or not), teach or
    run — but a module you never registered for is not yours to browse."""
    return gate.can_author or gate.enrolment is not None or offering.is_free


def _view_cohort(request, offering, gate):
    """Per-cohort schedule (Option B): a student sees their intake's blocks (plus
    shared templates); a staff author previews a cohort with ?cohort=<id>, and
    with none sees every cohort's blocks."""
    if gate.can_author:
        cid = request.GET.get('cohort')
        if cid and cid.isdigit():
            return offering.programme.cohorts.filter(pk=cid).first()
        return None
    if gate.cohort_id:
        return models.Cohort.objects.filter(pk=gate.cohort_id).first()
    return None


LAZY_BATCH = 3
LAZY_MAX = 12


@login_required
def module_weeks(request, pk):
    """The progressive loader: the bodies of a few weeks, as JSON.

    ``GET ?ids=12,13,14`` → ``{"weeks": [{"id": 12, "open": true, "html": "…"}]}``.
    Same gate as first paint: a locked week returns its locked body, and a week
    the viewer cannot see at all (draft, other intake, other module) is omitted.
    """
    offering = get_object_or_404(
        models.ProgrammeModule.objects.select_related('programme__institution'),
        pk=pk, is_active=True)
    person = getattr(request.user, 'profile', None)
    gate = Gate(request.user, offering, person=person)
    if not _may_browse(gate, offering):
        return JsonResponse({'error': 'not registered'}, status=403)

    ids = []
    for raw in (request.GET.get('ids') or '').split(','):
        raw = raw.strip()
        if raw.isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    ids = ids[:LAZY_MAX]
    if not ids:
        return JsonResponse({'weeks': []})

    view_cohort = _view_cohort(request, offering, gate)
    phases = _load_phases(offering, gate, cohort=view_cohort, week_ids=ids)
    sequence = SequenceGate(request.user, _all_materials(phases), bypass=gate.can_author)
    out = []
    for phase in phases:
        for week in phase.weeks.all():
            wk = _week_row(week, gate, sequence)
            html = render_to_string('learning/_module_week_body.html',
                                    {'wk': wk, 'offering': offering, 'gate': gate}, request=request)
            out.append({'id': week.pk, 'open': wk['open'], 'html': html})
    return JsonResponse({'weeks': out})


def _module_badge(offering, institution):
    """An inline-SVG lettered badge for the header avatar when there is no usable logo."""
    from urllib.parse import quote
    from django.utils.html import escape
    colour = escape(institution.accent_colour or '#1F3864')
    code = escape((offering.code or offering.module.code or '')[:5])
    inst = escape((institution.code or '')[:10])
    size = 92 if len(code) <= 3 else (78 if len(code) == 4 else 64)
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>"
        f"<rect width='256' height='256' fill='{colour}'/>"
        "<circle cx='128' cy='128' r='112' fill='none' stroke='#fff' stroke-opacity='.18' stroke-width='3'/>"
        f"<text x='128' y='146' text-anchor='middle' font-family='Montserrat,Helvetica,Arial,sans-serif' "
        f"font-weight='800' font-size='{size}' fill='#fff' letter-spacing='-2'>{code}</text>"
        f"<text x='128' y='190' text-anchor='middle' font-family='Montserrat,Helvetica,Arial,sans-serif' "
        f"font-weight='700' font-size='20' fill='#fff' fill-opacity='.8' letter-spacing='3'>{inst}</text>"
        "</svg>")
    return 'data:image/svg+xml;utf8,' + quote(svg)


def _header_meta(offering, gate):
    """The header's meta rows: code, price (or free / open to all) and the viewer's
    trial or payment state.
    """
    rows = [{'icon': 'bi-hash', 'text': offering.code}]
    if offering.is_general:
        rows.append({'icon': 'bi-unlock', 'text': 'Open to everyone on the programme'})
        return rows
    if offering.is_free:
        rows.append({'icon': 'bi-unlock', 'text': 'Free module'})
    else:
        rows.append({'icon': 'bi-cash-coin', 'text': f'R{offering.price_per_month:.0f} / month'})
    enrolment = gate.enrolment
    if enrolment and enrolment.status == enrolment.STATUS_TRIAL and enrolment.trial_ends_at:
        rows.append({'icon': 'bi-hourglass-split',
                     'text': f'Free week ends {enrolment.trial_ends_at:%d %b}'})
    elif enrolment and enrolment.status == enrolment.STATUS_ACTIVE and enrolment.paid_until:
        rows.append({'icon': 'bi-check-circle', 'text': f'Paid to {enrolment.paid_until:%d %b %Y}'})
    elif not gate.module_open:
        rows.append({'icon': 'bi-lock-fill', 'text': gate.module_status_label})
    return rows


def _header_actions(offering, gate):
    """The header buttons: "Unlock module" when locked; builder and course report for
    authors.
    """
    actions = []
    if not gate.module_open:
        actions.append({'label': 'Unlock module', 'icon': 'bi-unlock',
                        'url': reverse('learning:module-unlock', args=[offering.pk]),
                        'style': 'primary'})
    if gate.can_author:
        actions.append({'label': 'Build schedule', 'icon': 'bi-tools',
                        'url': reverse('learning:module-build', args=[offering.pk]),
                        'style': 'secondary-soft'})
        actions.append({'label': 'Course report', 'icon': 'bi-clipboard-data',
                        'url': reverse('learning:course-analytics', args=[offering.pk]),
                        'style': 'secondary-soft'})
    return actions


def _next_phase(phases):
    """The block being prepared for right now — the countdown at the top."""
    dated = [p for p in phases if p.days_away is not None and p.days_away >= 0]
    if dated:
        return min(dated, key=lambda p: p.days_away)
    current = [p for p in phases if p.is_current]
    return current[0] if current else (phases[0] if phases else None)


def _jump_url(offering_pk, material):
    """The material's row on the Schedule tab — the page opens its block and
    week and highlights it (see the #mat- handler in learning/module_feed.html)."""
    return reverse('learning:module-feed', args=[offering_pk]) + f'#mat-{material.pk}'


def _rail_files(offering, phases, gate, limit=40, *, sequence=None):
    """The Files rail: every document this module holds. Locked ones are still
    listed but link to the Documents tab instead of the file.

    A file that cannot be opened (locked, or being re-uploaded) jumps to its row on
    the schedule, which says why.
    """
    out = []
    for row in _flat_materials(phases, gate, sequence=sequence):
        material = row['material']
        if not material.file:
            continue
        state = row['state']
        name = material.file.name.rsplit('/', 1)[-1]
        ext = name.rsplit('.', 1)[-1].upper() if '.' in name else ''
        broken = state.file_broken
        opens = state.open and state.url and not broken
        reason = ('Being re-uploaded' if broken and state.open
                  else state.reason_label if not state.open else '')
        out.append({'name': material.title,
                    'url': state.url if opens else '',
                    'locked': not opens,
                    'jump': '' if opens else _jump_url(offering.pk, material),
                    'meta': ' · '.join(filter(None, [ext, material.get_kind_display(), reason])),
                    'icon': 'bi-file-earmark-pdf' if ext == 'PDF' else material.icon})
        if len(out) >= limit:
            break
    return out


def _rail_activity(offering, phases, gate, cohort, limit=12, *, sequence=None):
    """The Activity rail: enrolment, trial, invoice/payments, new material and
    recordings for this reader, newest first.

    An open material links straight to it (a document in a new tab); a locked one,
    or a file still being re-uploaded, links to its row on the schedule, which says
    why.
    """
    from django.utils.timesince import timesince

    items = []

    def add(when, text, icon, url='', new_tab=False):
        """Record one activity item; a bare date counts as local midnight, ``None`` is
        skipped.
        """
        if when is None:
            return
        if not hasattr(when, 'hour'):
            when = timezone.make_aware(timezone.datetime(when.year, when.month, when.day))
        items.append({'at': when, 'text': text, 'icon': icon, 'url': url, 'new_tab': new_tab})

    enrolment = gate.enrolment
    if enrolment is not None:
        add(enrolment.created_at, f'You joined {offering.display_name}', 'bi-person-check')
        if enrolment.status == enrolment.STATUS_TRIAL and enrolment.started_at:
            ends = f' — ends {timezone.localtime(enrolment.trial_ends_at):%d %b}' if enrolment.trial_ends_at else ''
            add(enrolment.started_at, f'Your free week started{ends}', 'bi-hourglass-split')
        if enrolment.invoice_uid:
            try:
                from apps.finance.models import Invoice
                invoice = (Invoice.objects.filter(public_id=enrolment.invoice_uid)
                           .prefetch_related('payments').first())
            except Exception:                                # pragma: no cover
                invoice = None
            if invoice is not None:
                number = getattr(invoice, 'number', '') or ''
                add(invoice.created_at, f'Invoice {number} raised'.replace('  ', ' '), 'bi-receipt')
                for payment in invoice.payments.filter(status='completed'):
                    add(payment.paid_at, f'You paid R{payment.amount:,.2f}', 'bi-cash-coin')
        if enrolment.status == enrolment.STATUS_ACTIVE and enrolment.paid_until:
            add(enrolment.updated_at, f'Module unlocked — paid to {enrolment.paid_until:%d %b %Y}',
                'bi-unlock')

    for row in _flat_materials(phases, gate, sequence=sequence):
        material, state = row['material'], row['state']
        if state.open and state.url and not state.file_broken:
            add(material.created_at, f'New: {material.title}', material.icon,
                state.url, new_tab=bool(material.file))
        else:
            add(material.created_at, f'New: {material.title}', material.icon,
                _jump_url(offering.pk, material))

    for meeting in _session_videos(offering, gate, cohort)[:20]:
        add(meeting.scheduled_end or meeting.scheduled_start,
            f'Recording ready: {meeting.title}', 'bi-play-btn', meeting.get_absolute_url() + '#recording')

    items.sort(key=lambda a: a['at'], reverse=True)
    now = timezone.now()
    for a in items[:limit]:
        a['when'] = (f'{timesince(a["at"], now).split(",")[0]} ago' if a['at'] <= now
                     else f'{timezone.localtime(a["at"]):%d %b %Y}')
    return items[:limit]


def _tab_payload(request, offering, phases, gate, tab, sequence=None, cohort=None):
    """The context for the active tab.

    * **schedule** — first paint renders the bodies of the previous, current and
      next week only; the rest load progressively (:func:`module_weeks`).
      ``?weeks=all`` renders the lot (the no-JavaScript fallback).
    * **messages** / **notifications** — see :func:`_messages_tab` and
      :func:`_notifications_for`.
    * **live** — sessions read chronologically, upcoming first: a past recording is
      a different thing from a class that has not happened yet. The recordings come
      straight from the sessions, so a past class is watchable as soon as Teams has
      published it, whether or not it has been filed into a block yet.
    * any other filter tab — the flat material rows of its kinds.
    """
    if tab == 'schedule':
        render_ids = None
        if request.GET.get('weeks') != 'all':
            ordered = [w.pk for phase in phases for w in phase.weeks.all()]
            render_ids = gate.weeks.initial_ids(ordered)
        schedule = _schedule(phases, gate, sequence, render_ids=render_ids)
        return {'schedule': schedule, 'lazy_batch': LAZY_BATCH,
                'any_expanded': any(block['expanded'] for block in schedule)}

    if tab == 'messages':
        return _messages_tab(request, offering, gate)

    if tab == 'notifications':
        return {'notifications': _notifications_for(request, offering)}

    kinds = TAB_KINDS.get(tab)
    rows = _flat_materials(phases, gate, kinds, sequence)
    if tab == 'live':
        now = timezone.now()
        upcoming, past = [], []
        for row in rows:
            start = getattr(row['material'].meeting, 'scheduled_start', None)
            (upcoming if (start and start >= now) else past).append(row)
        upcoming.sort(key=lambda r: r['material'].meeting.scheduled_start)
        videos = _session_videos(offering, gate, cohort)
        shown = {v.pk for v in videos}
        past = [r for r in past if getattr(r['material'], 'meeting_id', None) not in shown]
        payload = {'rows': rows, 'live_upcoming': upcoming, 'live_past': past, 'live_videos': videos}
        if gate.one_on_one:
            payload['one_on_one'] = _one_on_one_sessions(request.user, offering)
        return payload
    return {'rows': rows}


def _session_videos(offering, gate, cohort):
    """Past Teams sessions on this module that have a recording to play.

    Staff/educators see every intake's; a student sees their intake's and
    module-wide ones (all, while their intake is unset). Never private sessions —
    except for a one-on-one student, who watches their own sessions, never the
    class's. OneDrive / Teams recordings only: the YouTube copy is a backup, never
    the player.
    """
    from apps.communication.models import MeetingRoom
    from django.db.models import Q

    qs = (MeetingRoom.objects
          .filter(module=offering, is_active=True, scheduled_end__lt=timezone.now())
          .filter(Q(artifact__recording_item_id__gt='') | Q(artifact__teams_item_id__gt=''))
          .select_related('artifact', 'cohort', 'week')
          .order_by('-scheduled_start'))
    if gate.one_on_one:
        return list(qs.filter(audience=MeetingRoom.AUDIENCE_PRIVATE,
                              participants__user=gate.user).distinct()[:60])
    qs = qs.exclude(audience=MeetingRoom.AUDIENCE_PRIVATE)
    if cohort is not None:
        qs = qs.filter(Q(cohort=cohort) | Q(cohort__isnull=True))
    return list(qs[:60])


def _one_on_one_sessions(user, offering):
    """A one-on-one student's upcoming sessions on this module, and their hours."""
    try:
        from apps.shop import one_on_one
        from apps.shop.models import Booking
        upcoming = list(Booking.objects.filter(
            student=user, programme_module=offering, status=Booking.STATUS_CONFIRMED,
            end__gte=timezone.now()).select_related('educator__user', 'meeting').order_by('start'))
        return {'upcoming': upcoming, 'hours': one_on_one.balance(user, offering),
                'settled': one_on_one.account_settled(user), 'url': one_on_one.hours_url()}
    except Exception:                                    # pragma: no cover
        logger.exception('module feed: one-on-one sessions lookup failed')
        return None


def _messages_tab(request, offering, gate):
    """The module's group chat, rendered **in this page**.

    Reuses ``communication/_chat_thread.html`` (which reads ``active_group``).
    Membership is topped up on the way in: anyone who can open this tab has
    module access, and without a membership row the API would refuse them.

    A viewer without module access is shown the room as locked — the room is part
    of what the module costs (``module_chat_users``) — rather than offered a
    composer the API will refuse. One-on-one students talk to their educators and
    the Metanoia team in their own private coaching space, not the cohort's chat.
    """
    if gate.one_on_one:
        try:
            from apps.communication import models as cmodels, services as cservices
            from apps.shop.one_on_one import ensure_coaching_group
            group = ensure_coaching_group(request.user)
            group = cservices.decorate_group(
                cmodels.ChatGroup.objects.prefetch_related('memberships__user__profile')
                .get(pk=group.pk), request.user)
            return {'chat_group': group, 'active_group': group, 'chat_locked': False,
                    'coaching_chat': True}
        except Exception:                                # pragma: no cover
            logger.exception('module feed: coaching group failed')
            return {'chat_group': None, 'active_group': None}

    group = offering.chat_group
    if group is None:
        return {'chat_group': None, 'active_group': None}

    locked = False
    try:
        from apps.communication import models as cmodels, services as cservices
        if not cmodels.ChatMembership.objects.filter(group=group, user=request.user).exists():
            if gate.can_author or gate.module_open:
                cmodels.ChatMembership.objects.get_or_create(
                    group=group, user=request.user, defaults={'is_auto': True})
            elif not (request.user.is_staff or request.user.is_superuser):
                locked = True
        group = cservices.decorate_group(
            cmodels.ChatGroup.objects.prefetch_related('memberships__user__profile')
            .get(pk=group.pk),
            request.user)
    except Exception:                                    # pragma: no cover
        logger.exception('module feed: chat membership top-up failed')

    return {'chat_group': group, 'active_group': group, 'chat_locked': locked}


def _notifications_for(request, offering, limit=40):
    """This module's notifications for this reader.

    Matched on the notification's ``url`` (it points at the module feed), which
    keeps ``communication`` free of a hard dependency on the learning spine.
    """
    try:
        from apps.communication.models import Notification

        prefix = reverse('learning:module-feed', args=[offering.pk])
        return list(Notification.objects
                    .filter(recipient=request.user, url__startswith=prefix)
                    .order_by('-created_at')[:limit])
    except Exception:  # pragma: no cover
        logger.exception('module feed: notifications lookup failed')
        return []
