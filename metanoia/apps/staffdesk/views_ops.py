"""Ops pages: background jobs, moderation, audit log, invitations and parent links.

The pages call the same service functions as the rest of the app, so a
decision taken here follows the same rules as anywhere else.

Schedule constants (they mirror the production systemd timers in
``deploy/scheduler/production/``):

:data:`QUICK_JOBS`
    The jobs the quarter-hourly timer runs (``*-quick.service``).
:data:`OVERDUE_SLACK`
    How late past its expected tick a job may start before it is called
    overdue. The main tick runs jobs one after another, so a slow job delays
    the rest.
:data:`SESSION_TICKS`
    When the after-class timer runs (``*-sessions.timer``).
:data:`BACKUP_TICKS`
    When the database-backup timer runs (``*-backup.timer``).

:data:`INVITE_STALE_DAYS` — a pending invite older than this is flagged as
worth a nudge or revoke.
"""

import csv
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import connections
from django.db.models import Count, Q
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts import emails as account_emails
from apps.accounts.models import ActivityLog, Invitation, ParentLink
from apps.communication import services as comms
from apps.communication.models import Appeal, FeedReport, Penalty, ReputationScore, Violation
from apps.scheduler import jobs as job_registry
from apps.scheduler import runner
from apps.scheduler.models import JobRun
from core.utils import display_name

from .access import developer_required, staff_required
from .forms_ops import AppealDecisionForm, AuditFilterForm, ParentLinkForm

PAGE_SIZE = 50


def _page(request, qs, size=PAGE_SIZE):
    """A page of ``qs`` plus the current query string minus ``page``, for links."""
    page = Paginator(qs, size).get_page(request.GET.get('page'))
    keep = request.GET.copy()
    keep.pop('page', None)
    return page, keep.urlencode()


def _user_q(prefix, term):
    """Match a user (via ``prefix``) by e-mail or either copy of their name."""
    q = Q()
    for field in ('email', 'first_name', 'last_name', 'profile__first_name', 'profile__last_name'):
        q |= Q(**{f'{prefix}{field}__icontains': term})
    return q


def _back(request, fallback):
    """Return to the page the form was on (keeping its tab and filters).

    Uses :func:`core.redirects.safe_target`, not just ``startswith('/')``:
    browsers read ``/\\evil.example`` as ``//evil.example``, another host.
    """
    from core.redirects import safe_target
    return redirect(safe_target(request, request.POST.get('next', ''), fallback))


SAST = ZoneInfo('Africa/Johannesburg')

QUICK_JOBS = ('session-reminders', 'send-broadcasts', 'shop-bookings', 'auto-notices')

OVERDUE_SLACK = timedelta(minutes=30)

SESSION_TICKS = (time(6, 0), time(22, 0))

BACKUP_TICKS = tuple(time(h, 30) for h in range(0, 24, 3))

SCHEDULE_TEXT = [
    ('Main tick', 'Hourly at :05 from 06:05 to 22:05 SAST — runs every due job except the after-class ones.'),
    ('After-class run', 'At 22:00 and 06:00 SAST only — imports the day\'s classes, copies their recordings '
                        'and transcripts to OneDrive, and has the AI write and send each session pack.'),
    ('Quick tick', 'Every 15 minutes from 06:00 to 22:45 SAST — runs ' + ', '.join(QUICK_JOBS) + '.'),
    ('Database backup', 'Every 3 hours round the clock, at 00:30, 03:30 … 21:30 SAST — the restore point '
                        'and its OneDrive copy ("metanoia DB BCKP"); both keep 3 days.'),
    ('Overnight', 'Apart from the database backup, nothing runs between 22:45 and 06:00; work that fell due overnight runs on the first morning tick.'),
]


def _ticks_on(day, quick, sessions=False, backup=False):
    """Production tick times (aware, SAST) on ``day`` for a main, quick, after-class or backup job."""
    if sessions:
        return [datetime.combine(day, t, tzinfo=SAST) for t in SESSION_TICKS]
    if backup:
        return [datetime.combine(day, t, tzinfo=SAST) for t in BACKUP_TICKS]
    slots = {time(h, 5) for h in range(6, 23)}
    if quick:
        slots |= {time(h, m) for h in range(6, 23) for m in (0, 15, 30, 45)}
    return sorted(datetime.combine(day, t, tzinfo=SAST) for t in slots)


def next_expected_run(job, state, now):
    """When the production timers should next start ``job``.

    Mirrors :func:`apps.scheduler.runner.is_due`: the job becomes due a little
    before its full interval, then waits for the next tick that includes it.
    """
    if state is None or state.last_started_at is None:
        due = now
    else:
        grace = min(runner.GRACE_SECONDS, job.every * 0.1)
        due = state.last_started_at + timedelta(seconds=job.every - grace)
    start = due.astimezone(SAST).date()
    for offset in range(3):
        for tick in _ticks_on(start + timedelta(days=offset), job.name in QUICK_JOBS,
                              sessions=job.tick == 'sessions', backup=job.tick == 'backup'):
            if tick >= due:
                return tick
    return None


def _is_locked(state, now):
    """True while a job holds a lock younger than ``LOCK_STALE_AFTER`` (i.e. is running)."""
    return bool(state and state.locked_at
                and (now - state.locked_at).total_seconds() < job_registry.LOCK_STALE_AFTER)


def _every_label(seconds):
    """Human interval: "hourly", "every 3 h", "every 15 min"."""
    if seconds % 3600 == 0:
        h = seconds // 3600
        return 'hourly' if h == 1 else f'every {h} h'
    m = seconds // 60
    return 'every minute' if m == 1 else f'every {m} min'


def _duration_label(ms):
    """Human duration from milliseconds (ms, s or min)."""
    if ms < 1000:
        return f'{ms} ms'
    if ms < 60_000:
        return f'{ms / 1000:.1f} s'
    return f'{ms / 60_000:.1f} min'


def _job_rows(now):
    """One row per registered job: state, running/failing/overdue flags, next run and labels."""
    states = {s.name: s for s in JobRun.objects.all()}
    rows = []
    for job in job_registry.JOBS:
        state = states.get(job.name)
        running = _is_locked(state, now)
        nxt = next_expected_run(job, state, now) if job.enabled else None
        never = state is None or state.last_started_at is None
        failing = bool(state and (state.consecutive_failures
                                  or (state.last_status == JobRun.STATUS_FAILED and not running)))
        overdue = bool(job.enabled and not running and not never and nxt and now > nxt + OVERDUE_SLACK)
        rows.append({
            'job': job, 'state': state, 'running': running, 'never': never,
            'failing': failing, 'overdue': overdue, 'next': nxt,
            'every': 'twice daily' if job.tick == 'sessions' else _every_label(job.every),
            'tick': ('after-class (06:00, 22:00)' if job.tick == 'sessions'
                     else 'backup (every 3 h, round the clock)' if job.tick == 'backup'
                     else 'quick + main' if job.name in QUICK_JOBS else 'main'),
            'duration': _duration_label(state.last_duration_ms) if state and state.last_finished_at else '',
        })
    return rows


def _background(fn, *args):
    """Run ``fn`` on a daemon thread, closing its DB connection when done.

    A web request must not wait for a job (some take minutes), and the thread's
    connection would otherwise sit open until the worker recycles. Tests patch
    this to run inline.
    """
    def target():
        """Run the job, report any exception as ``DESK-9002``, then close DB connections."""
        try:
            fn(*args)
        except Exception as exc:
            from core.errors import report
            report('DESK-9002', exc, context={'task': getattr(fn, '__name__', str(fn))})
        finally:
            connections.close_all()
    threading.Thread(target=target, name=f'staffdesk-{getattr(fn, "__name__", "job")}', daemon=True).start()


@developer_required
def jobs(request):
    """The background jobs page (developers only) with headline counts and the schedule."""
    now = timezone.now()
    rows = _job_rows(now)
    return render(request, 'staffdesk/ops/jobs.html', {
        'page_title': 'Background jobs', 'rows': rows, 'schedule': SCHEDULE_TEXT,
        'kpis': {
            'total': len(rows),
            'running': sum(r['running'] for r in rows),
            'failing': sum(r['failing'] for r in rows),
            'overdue': sum(r['overdue'] for r in rows),
        },
        'any_running': any(r['running'] for r in rows),
    })


@developer_required
@require_POST
def job_run(request, name):
    """Run one job now in the background, unless it is already running."""
    job = job_registry.JOBS_BY_NAME.get(name)
    if job is None:
        raise Http404('No such job')
    if _is_locked(JobRun.objects.filter(name=name).first(), timezone.now()):
        messages.info(request, f'{name} is already running.')
    else:
        _background(_run_forced, job)
        messages.success(request, f'{name} started. Refresh in a moment to see how it went.')
    return redirect('staffdesk:ops-jobs')


def _run_forced(job):
    """Run ``job`` even if it is not due."""
    runner.run_job(job, force=True)


@developer_required
@require_POST
def jobs_run_due(request):
    """Run one scheduler tick (every due job) in the background."""
    _background(runner.tick)
    messages.success(request, 'Running every job that is due. Each shows "running" until it finishes.')
    return redirect('staffdesk:ops-jobs')


MOD_TABS = ('violations', 'penalties', 'appeals', 'reputation', 'community')


def _review(violation, request, outcome):
    """Record who reviewed a violation in its evidence, for the paper trail."""
    evidence = dict(violation.evidence or {})
    evidence['review'] = {'outcome': outcome, 'by': request.user.email, 'at': timezone.now().isoformat()}
    violation.evidence = evidence
    violation.handled = True
    violation.save(update_fields=['evidence', 'handled'])


@staff_required
def moderation(request):
    """Moderation queues in tabs: violations, penalties, appeals, reputation and
    community posts (community moderation is the "Community posts" tab of this
    one page). ``show=all`` includes handled items; ``q`` searches.
    """
    tab = request.GET.get('tab') if request.GET.get('tab') in MOD_TABS else 'violations'
    term = (request.GET.get('q') or '').strip()
    show = request.GET.get('show', '')
    now = timezone.now()

    counts = {
        'violations': Violation.objects.filter(handled=False).count(),
        'penalties': Penalty.objects.filter(active=True).count(),
        'appeals': Appeal.objects.filter(status=Appeal.STATUS_PENDING).count(),
        'reputation': ReputationScore.objects.filter(score__lt=100).count(),
        'community': (FeedReport.objects.filter(status=FeedReport.STATUS_OPEN)
                      .values('post').distinct().count()),
    }
    ctx = {'page_title': 'Moderation', 'tab': tab, 'search': term, 'show': show, 'counts': counts}

    if tab == 'community':
        from apps.communication.views import community_moderation_context
        ctx.update(community_moderation_context())
        return render(request, 'staffdesk/ops/moderation.html', ctx)

    if tab == 'violations':
        qs = Violation.objects.select_related('user__profile', 'message__group', 'reporter')
        qs = qs if show == 'all' else qs.filter(handled=False)
        if term:
            qs = qs.filter(_user_q('user__', term) | Q(text__icontains=term) | Q(category__icontains=term))
        qs = qs.annotate(n_penalties=Count('penalties')).order_by('-created_at')
    elif tab == 'penalties':
        qs = Penalty.objects.select_related('user__profile', 'violation', 'issued_by')
        qs = qs if show == 'all' else qs.filter(active=True)
        if term:
            qs = qs.filter(_user_q('user__', term) | Q(reason__icontains=term))
    elif tab == 'appeals':
        qs = Appeal.objects.select_related('user__profile', 'penalty', 'reviewed_by')
        qs = qs if show == 'all' else qs.filter(status=Appeal.STATUS_PENDING)
        if term:
            qs = qs.filter(_user_q('user__', term) | Q(message__icontains=term))
    else:
        qs = (ReputationScore.objects.select_related('user__profile')
              .annotate(n_violations=Count('user__violations', distinct=True),
                        n_penalties=Count('user__penalties', distinct=True))
              .order_by('score', '-updated_at'))
        if term:
            qs = qs.filter(_user_q('user__', term))

    page, querystring = _page(request, qs)
    ctx.update({'page': page, 'querystring': querystring, 'now': now})
    return render(request, 'staffdesk/ops/moderation.html', ctx)


@staff_required
@require_POST
def violation_action(request, pk):
    """Dismiss (a false positive) or confirm a flagged item.

    Dismissing undoes what :func:`~apps.communication.services.moderate_message`
    did automatically: lifts the penalties it issued, gives back the reputation
    and un-hides the message. Confirming a user report — which carries no
    automatic penalty — runs the normal penalty ladder.
    """
    violation = get_object_or_404(Violation.objects.select_related('message', 'user'), pk=pk)
    action = request.POST.get('action')
    auto = violation.detected_by != Violation.DETECTED_REPORT

    if action == 'dismiss':
        lifted = violation.penalties.filter(active=True).update(active=False)
        if auto:
            comms._adjust_reputation(violation.user, violation.score * 5)
            msg = violation.message
            if msg and msg.is_deleted and violation.score >= 3:
                msg.is_deleted = False
                msg.save(update_fields=['is_deleted'])
        _review(violation, request, 'dismissed')
        messages.success(request, f'Dismissed. {lifted} penalty(ies) lifted.' if lifted else 'Dismissed.')
    elif action == 'confirm':
        if not violation.penalties.exists():
            comms._adjust_reputation(violation.user, -violation.score * 5)
            penalty = comms.apply_penalty_ladder(violation.user, violation)
            penalty.issued_by = request.user
            penalty.save(update_fields=['issued_by'])
            note = f' {penalty.get_kind_display()} issued.'
        else:
            note = ''
        if request.POST.get('hide') and violation.message and not violation.message.is_deleted:
            violation.message.is_deleted = True
            violation.message.save(update_fields=['is_deleted'])
            note += ' Message hidden.'
        _review(violation, request, 'confirmed')
        messages.success(request, 'Confirmed.' + note)
    else:
        messages.error(request, 'Unknown action.')
    return _back(request, reverse('staffdesk:ops-moderation'))


@staff_required
@require_POST
def penalty_lift(request, pk):
    """Same as the admin's ``lift_penalties`` action, one at a time."""
    penalty = get_object_or_404(Penalty, pk=pk)
    if penalty.active:
        penalty.active = False
        penalty.save(update_fields=['active'])
        messages.success(request, f'{penalty.get_kind_display()} lifted for {display_name(penalty.user)}.')
    return _back(request, reverse('staffdesk:ops-moderation') + '?tab=penalties')


@staff_required
@require_POST
def appeal_decide(request, pk):
    """Approve (lifting the penalty) or deny an appeal, and tell the user why.

    :class:`Appeal` has no field for the reviewer's note, so it travels in the
    notification — which is where the user would look for it anyway. A decided
    appeal cannot be decided again.
    """
    appeal = get_object_or_404(Appeal.objects.select_related('penalty', 'user'), pk=pk)
    form = AppealDecisionForm(request.POST)
    if appeal.status != Appeal.STATUS_PENDING or not form.is_valid():
        messages.error(request, 'That appeal has already been decided.' if form.is_valid()
                       else 'Choose approve or deny.')
        return _back(request, reverse('staffdesk:ops-moderation') + '?tab=appeals')

    approved = form.cleaned_data['decision'] == 'approve'
    appeal.status = Appeal.STATUS_ACCEPTED if approved else Appeal.STATUS_REJECTED
    appeal.reviewed_by = request.user
    appeal.save(update_fields=['status', 'reviewed_by'])
    if approved and appeal.penalty.active:
        appeal.penalty.active = False
        appeal.penalty.save(update_fields=['active'])

    note = form.cleaned_data['note'].strip()
    kind = appeal.penalty.get_kind_display().lower()
    comms.notify(
        appeal.user, actor=request.user, verb='appeal', email=True,
        level='success' if approved else 'info',
        title='Your appeal was approved' if approved else 'Your appeal was not upheld',
        body=(f'The {kind} has been lifted.' if approved else f'The {kind} stays in place.')
             + (f' {note}' if note else ''),
    )
    messages.success(request, f'Appeal {"approved" if approved else "denied"}; {display_name(appeal.user)} has been told.')
    return _back(request, reverse('staffdesk:ops-moderation') + '?tab=appeals')


def _audit_queryset(request):
    """``(form, queryset)`` of the audit log filtered by the GET parameters."""
    form = AuditFilterForm(request.GET or None)
    qs = ActivityLog.objects.select_related('actor__user', 'target_user__user')
    if form.is_bound and form.is_valid():
        f = form.cleaned_data
        if f['user']:
            term = f['user']
            qs = qs.filter(Q(actor__first_name__icontains=term) | Q(actor__last_name__icontains=term)
                           | Q(actor__user__email__icontains=term)
                           | Q(target_user__first_name__icontains=term)
                           | Q(target_user__last_name__icontains=term)
                           | Q(target_user__user__email__icontains=term))
        if f['action']:
            qs = qs.filter(action=f['action'])
        if f['date_from']:
            qs = qs.filter(timestamp__date__gte=f['date_from'])
        if f['date_to']:
            qs = qs.filter(timestamp__date__lte=f['date_to'])
        if f['q']:
            qs = qs.filter(Q(description__icontains=f['q']) | Q(request_path__icontains=f['q'])
                           | Q(ip_address__icontains=f['q']))
    return form, qs


def _person_label(person):
    """A person's display name for the CSV export (``''`` for none)."""
    if person is None:
        return ''
    return display_name(person.user) if person.user_id else f'{person.first_name} {person.last_name}'.strip()


@developer_required
def audit(request):
    """The audit log page, filtered and paginated (developers only)."""
    form, qs = _audit_queryset(request)
    page, querystring = _page(request, qs)
    return render(request, 'staffdesk/ops/audit.html', {
        'page_title': 'Audit log', 'form': form, 'page': page, 'querystring': querystring,
        'total': page.paginator.count,
    })


class _Echo:
    """A file-like object whose ``write`` returns the line, for streaming CSV."""

    def write(self, value):
        """Return ``value`` unchanged."""
        return value


@developer_required
def audit_export(request):
    """CSV of the filtered log, streamed so a large export does not sit in memory."""
    _, qs = _audit_queryset(request)
    writer = csv.writer(_Echo())

    def rows():
        """Yield the CSV header, then one encoded line per log entry."""
        yield writer.writerow(['Time (UTC)', 'Action', 'Actor', 'Actor e-mail', 'Target', 'Target e-mail',
                               'Description', 'IP address', 'Path'])
        for log in qs.iterator(chunk_size=2000):
            yield writer.writerow([
                log.timestamp.isoformat(), log.action,
                _person_label(log.actor), log.actor.user.email if log.actor and log.actor.user_id else '',
                _person_label(log.target_user),
                log.target_user.user.email if log.target_user and log.target_user.user_id else '',
                log.description, log.ip_address or '', log.request_path or '',
            ])

    response = StreamingHttpResponse(rows(), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="audit-log-{timezone.localdate():%Y%m%d}.csv"'
    return response


INVITE_TABS = ('pending', 'accepted', 'revoked', 'all')

INVITE_STALE_DAYS = 30


@staff_required
def invitations(request):
    """Invitations by status tab, searchable; stale pending ones are highlighted."""
    tab = request.GET.get('tab') if request.GET.get('tab') in INVITE_TABS else 'pending'
    term = (request.GET.get('q') or '').strip()
    qs = Invitation.objects.select_related('student__user', 'programme', 'invited_by', 'accepted_by')
    if tab != 'all':
        qs = qs.filter(status=tab)
    if term:
        qs = qs.filter(Q(email__icontains=term) | Q(student__first_name__icontains=term)
                       | Q(student__last_name__icontains=term) | Q(programme__name__icontains=term))
    page, querystring = _page(request, qs)
    stale_before = timezone.now() - timedelta(days=INVITE_STALE_DAYS)
    counts = {t: Invitation.objects.filter(status=t).count() for t in INVITE_TABS if t != 'all'}
    counts['all'] = sum(counts.values())
    return render(request, 'staffdesk/ops/invitations.html', {
        'page_title': 'Invitations', 'tab': tab, 'search': term, 'page': page,
        'querystring': querystring, 'counts': counts, 'stale_before': stale_before,
        'stale_days': INVITE_STALE_DAYS,
    })


@staff_required
@require_POST
def invitation_action(request, pk):
    """Resend or revoke a pending invitation.

    A resent link gets a fresh lease: invites lapse ``EXPIRY_DAYS`` after they
    were (last) sent (see :attr:`Invitation.is_expired`), so ``created_at`` is reset.
    """
    invite = get_object_or_404(Invitation, pk=pk)
    action = request.POST.get('action')
    if invite.status != Invitation.STATUS_PENDING:
        messages.error(request, f'That invite is already {invite.get_status_display().lower()}.')
    elif action == 'resend':
        Invitation.objects.filter(pk=invite.pk).update(created_at=timezone.now())
        if account_emails.send_invite(invite):
            messages.success(request, f'Invite re-sent to {invite.email}.')
        else:
            messages.error(request, f'The e-mail to {invite.email} could not be sent — check the mail settings.')
    elif action == 'revoke':
        invite.status = Invitation.STATUS_REVOKED
        invite.save(update_fields=['status'])
        messages.success(request, f'Invite for {invite.email} revoked; the link no longer works.')
    else:
        messages.error(request, 'Unknown action.')
    return _back(request, reverse('staffdesk:ops-invitations'))


@staff_required
def parent_links(request):
    """List, search and create parent ↔ student links.

    Each row carries ``siblings`` — how many parents that student has — for the
    "1 of 2" column.
    """
    term = (request.GET.get('q') or '').strip()
    form = ParentLinkForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        link = ParentLink.objects.create(parent=form.cleaned_data['parent'], student=form.cleaned_data['student'],
                                         relationship=form.cleaned_data['relationship'])
        messages.success(request, f'{display_name(link.parent)} is now linked to {display_name(link.student)}.')
        return redirect('staffdesk:ops-parent-links')

    qs = ParentLink.objects.select_related('parent__profile', 'student__profile')
    if term:
        qs = qs.filter(_user_q('parent__', term) | _user_q('student__', term) | Q(relationship__icontains=term))
    page, querystring = _page(request, qs)
    student_ids = {link.student_id for link in page}
    per_student = dict(ParentLink.objects.filter(student_id__in=student_ids)
                       .values_list('student_id').annotate(n=Count('id')))
    for link in page:
        link.siblings = per_student.get(link.student_id, 0)

    User = get_user_model()
    return render(request, 'staffdesk/ops/parent_links.html', {
        'page_title': 'Parent links', 'form': form, 'search': term, 'page': page,
        'querystring': querystring, 'max_parents': ParentLink.MAX_PER_STUDENT,
        'total': ParentLink.objects.count(),
        'parent_emails': User.objects.filter(profile__user_type='parent').exclude(email='')
                             .order_by('email').values_list('email', flat=True)[:1000],
        'student_emails': User.objects.filter(profile__user_type='student', profile__preview_of__isnull=True,
                                              is_active=True)
                              .exclude(email='')
                              .order_by('email').values_list('email', flat=True)[:2000],
    })


@staff_required
@require_POST
def parent_link_remove(request, pk):
    """Remove a parent link, with an undo."""
    link = get_object_or_404(ParentLink.objects.select_related('parent', 'student'), pk=pk)
    label = f'{display_name(link.parent)} → {display_name(link.student)}'
    from apps.accounts import undo
    action = undo.delete_with_undo([link], summary=f'Parent link removed: {label}'[:255],
                                   actor=request.user, target_user=link.student,
                                   next_url=reverse('staffdesk:ops-parent-links'))
    undo.success_with_undo(request, f'Unlinked {label}.', action)
    return _back(request, reverse('staffdesk:ops-parent-links'))


ONE_ON_ONE_TABS = ('open', 'enrolled', 'declined', 'all')


def _coaches():
    """Active educators, staff and admins who can take a one-on-one request."""
    from apps.accounts.models import Person
    return (Person.objects.filter(user_type__in=('educator', 'staff', 'admin'), user__is_active=True,
                                  preview_of__isnull=True)
            .select_related('user').order_by('first_name', 'last_name'))


@staff_required
def one_on_one(request):
    """One-on-one coaching requests by tab (open, enrolled, declined, all), searchable."""
    from apps.accounts.models import OneOnOneRequest

    tab = request.GET.get('tab') if request.GET.get('tab') in ONE_ON_ONE_TABS else 'open'
    term = (request.GET.get('q') or '').strip()
    qs = OneOnOneRequest.objects.select_related('person__user', 'educator__user')
    if tab == 'open':
        qs = qs.filter(status__in=OneOnOneRequest.OPEN_STATUSES)
    elif tab != 'all':
        qs = qs.filter(status=tab)
    if term:
        qs = qs.filter(Q(person__first_name__icontains=term) | Q(person__last_name__icontains=term)
                       | Q(institution__icontains=term) | Q(programme__icontains=term))
    page, querystring = _page(request, qs)
    base = OneOnOneRequest.objects
    counts = {'open': base.filter(status__in=OneOnOneRequest.OPEN_STATUSES).count(),
              'enrolled': base.filter(status=OneOnOneRequest.STATUS_ENROLLED).count(),
              'declined': base.filter(status=OneOnOneRequest.STATUS_DECLINED).count()}
    counts['all'] = sum(counts.values())
    return render(request, 'staffdesk/ops/one_on_one.html', {
        'page_title': 'One-on-one requests', 'tab': tab, 'search': term, 'page': page,
        'querystring': querystring, 'counts': counts, 'coaches': _coaches(),
        'statuses': OneOnOneRequest.STATUS_CHOICES,
    })


@staff_required
@require_POST
def one_on_one_action(request, pk):
    """Save the assignment (educator, status, notes) or mark the request enrolled."""
    from apps.accounts.models import OneOnOneRequest

    req = get_object_or_404(OneOnOneRequest.objects.select_related('person__user'), pk=pk)
    action = request.POST.get('action', 'save')
    educator_id = (request.POST.get('educator') or '').strip()
    if educator_id:
        educator = _coaches().filter(pk=educator_id).first() if educator_id.isdigit() else None
        if educator is None:
            messages.error(request, 'Choose an educator from the list.')
            return _back(request, reverse('staffdesk:ops-one-on-one'))
        req.educator = educator
    elif 'educator' in request.POST:
        req.educator = None
    if 'admin_notes' in request.POST:
        req.admin_notes = request.POST.get('admin_notes', '').strip()

    was = req.status
    if action == 'enrol':
        if req.educator is None:
            messages.error(request, 'Assign an educator before marking the request enrolled.')
            return _back(request, reverse('staffdesk:ops-one-on-one'))
        req.status = OneOnOneRequest.STATUS_ENROLLED
    elif action == 'decline':
        req.status = OneOnOneRequest.STATUS_DECLINED
    else:
        status = request.POST.get('status')
        if status in dict(OneOnOneRequest.STATUS_CHOICES):
            req.status = status
        elif req.status == OneOnOneRequest.STATUS_NEW and req.educator is not None:
            req.status = OneOnOneRequest.STATUS_IN_PROGRESS
    if req.status == OneOnOneRequest.STATUS_ENROLLED and not req.enrolled_at:
        req.enrolled_at = timezone.now()
    req.handled_by = request.user
    req.save()

    who = display_name(req.person.user)
    if req.status == OneOnOneRequest.STATUS_ENROLLED and was != req.status:
        comms.notify(req.person.user, title='Your one-on-one coaching is set up',
                     body=f'{display_name(req.educator.user)} will coach you for {req.programme}.',
                     verb='set up your one-on-one coaching', actor=request.user,
                     url=reverse('myhub:index'), category=None)
        messages.success(request, f'{who} is marked enrolled with {display_name(req.educator.user)}.')
    else:
        messages.success(request, f'Saved the request from {who}.')
    return _back(request, reverse('staffdesk:ops-one-on-one'))
