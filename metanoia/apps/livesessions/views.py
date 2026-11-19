"""The Metanoia calendar, the session pages, and the scheduler.

Every view is scoped by :func:`apps.livesessions.audience.visible_sessions`.
Dates render in the reader's own time zone: the middleware has already
activated it, so no per-view conversion is needed. Every calendar surface
renders from the one aggregation in :mod:`apps.livesessions.sources`.

Module settings:

* ``AGENDA_LIMIT`` -- how many upcoming entries the agenda lists before it
  stops and points at the month grid (see :func:`_calendar_agenda`).
* ``CELL_LIMIT`` -- how many entries a month-grid day cell shows before
  "+N more". Fixed so every week row is the same height: a busy day opens its
  list in the popup instead of stretching its row (and the whole month) down
  the page.
* ``_ACADEMIC_ICONS`` -- the second icon on an academic date, naming what kind
  of date it is.
"""

import calendar as calendar_module
import logging
import re
from datetime import datetime, time, timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.communication.models import MeetingRoom

from . import audience as aud
from . import forms, services, sources, summaries
from .models import LiveSessionSettings, SessionArtifact

logger = logging.getLogger('apps')


@login_required
def calendar(request):
    """The Metanoia calendar — a month of sessions, plus the institution's dates.

    ``?view=list`` swaps the month grid for an agenda of the same entries.
    ``day_items`` carries every day's entries for the popup (an item, or a
    day's "+N more").
    """
    if (request.GET.get('view') or '').lower() == 'list':
        return _calendar_agenda(request)

    focus = _focus_month(request)
    first = focus.replace(day=1)
    last_day = calendar_module.monthrange(focus.year, focus.month)[1]
    window_start = _aware(first, time.min)
    window_end = _aware(first.replace(day=last_day), time.max)

    selected = _selected_kinds(request)
    entries = sources.entries_for(request.user, window_start, window_end, kinds=selected)
    by_day = sources.group_by_day(entries)

    weeks = [[_cell(day, focus, by_day) for day in week]
             for week in calendar_module.Calendar(firstweekday=0).monthdatescalendar(focus.year, focus.month)]

    now = timezone.now()
    return render(request, 'livesessions/calendar.html', {
        'page_title': 'Calendar',
        'weeks': weeks,
        'day_items': {cell['key']: {'label': f"{cell['date']:%A %-d %B}", 'entries': cell['all_entries']}
                      for week in weeks for cell in week if cell['all_entries']},
        'focus': focus,
        'prev_month': (first - timedelta(days=1)).strftime('%Y-%m'),
        'next_month': (first.replace(day=last_day) + timedelta(days=1)).strftime('%Y-%m'),
        'today': timezone.localdate(),
        'upcoming': [e for e in entries if e.start and e.start >= now][:8],
        'legend': [{'kind': kind, 'label': label, 'colour': colour, 'icon': icon,
                    'on': kind in selected}
                   for kind, (label, colour, icon) in sources.KINDS.items()],
        'selected_kinds': ','.join(sorted(selected)),
        'can_schedule': aud.can_manage(request.user),
        'sees_everything': aud.sees_whole_calendar(request.user),
        'user_timezone': getattr(request, 'ui_timezone', ''),
        'subscribe_url': request.build_absolute_uri(_personal_feed_path(request.user)),
        'view_mode': 'month',
    })


AGENDA_LIMIT = 40


def _calendar_agenda(request):
    """``/calendar/?view=list`` — the same entries as an upcoming/past agenda.

    Uses the wider ``window_around`` span (as the feed and dashboard do), not
    the month boundary. Upcoming entries are capped at ``AGENDA_LIMIT``: an
    admin sees every institution's sessions, which over this window is
    hundreds of rows, and the month grid is for browsing a whole span.
    """
    now = timezone.now()
    window_start, window_end = sources.window_around()
    selected = _selected_kinds(request)
    entries = sources.entries_for(request.user, window_start, window_end, kinds=selected)

    ahead = [e for e in entries if e.start and e.start >= now]
    past = [e for e in reversed(entries) if e.start and e.start < now][:12]

    upcoming = ahead[:AGENDA_LIMIT]

    return render(request, 'livesessions/calendar-agenda.html', {
        'page_title': 'Calendar',
        'upcoming': upcoming,
        'more_ahead': max(0, len(ahead) - len(upcoming)),
        'past': past,
        'today': timezone.localdate(),
        'legend': [{'kind': kind, 'label': label, 'colour': colour, 'icon': icon,
                    'on': kind in selected}
                   for kind, (label, colour, icon) in sources.KINDS.items()],
        'selected_kinds': ','.join(sorted(selected)),
        'can_schedule': aud.can_manage(request.user),
        'sees_everything': aud.sees_whole_calendar(request.user),
        'user_timezone': getattr(request, 'ui_timezone', ''),
        'subscribe_url': request.build_absolute_uri(_personal_feed_path(request.user)),
        'view_mode': 'list',
    })


@login_required
def day(request, on):
    """One day, with the free-slot strip for anyone who can schedule.

    The reader's own connected calendar is subtracted from the free slots too,
    so a slot that is free on Metanoia but taken in their Google diary is not
    offered.
    """
    try:
        the_day = datetime.strptime(on, '%Y-%m-%d').date()
    except ValueError:
        raise Http404('Not a date')

    entries = sources.entries_for(request.user, _aware(the_day, time.min),
                                  _aware(the_day, time.max))
    can_schedule = aud.can_manage(request.user)
    return render(request, 'livesessions/day.html', {
        'page_title': f'{the_day:%A %d %B %Y}',
        'day': the_day,
        'entries': entries,
        'sessions': [e for e in entries if e.kind == 'session'],
        'free_slots': services.free_slots(the_day, for_user=request.user) if can_schedule else [],
        'can_schedule': can_schedule,
    })


@login_required
def availability(request):
    """Bookable gaps across the next fortnight, each linking to a pre-filled create form.

    Restricted to people who can schedule: gaps are computed across every
    session on the platform, including ones the reader may not see, so a slot
    is never offered to two people at once.
    """
    if not aud.can_manage(request.user):
        messages.error(request, 'Only admins, staff and educators can schedule sessions.')
        return redirect('livesessions:calendar')

    start = _parse_iso(request.GET.get('from'))
    start_day = timezone.localtime(start).date() if start else timezone.localdate()
    try:
        days = max(1, min(28, int(request.GET.get('days') or 14)))
    except (TypeError, ValueError):
        days = 14
    try:
        minimum = max(15, min(240, int(request.GET.get('min') or 30)))
    except (TypeError, ValueError):
        minimum = 30
    open_from, open_to = _opening_hours(request)
    weekdays_only = request.GET.get('weekdays') == '1'

    rows = services.availability(
        start_day, days=days, open_from=open_from, open_to=open_to,
        minimum_minutes=minimum, for_user=request.user, skip_weekends=weekdays_only)

    return render(request, 'livesessions/availability.html', {
        'page_title': 'Free slots',
        'rows': rows,
        'start_day': start_day,
        'end_day': start_day + timedelta(days=days - 1),
        'days': days,
        'minimum': minimum,
        'open_from': open_from,
        'open_to': open_to,
        'weekdays_only': weekdays_only,
        'prev_from': (start_day - timedelta(days=days)).isoformat(),
        'next_from': (start_day + timedelta(days=days)).isoformat(),
        'today': timezone.localdate(),
        'total_slots': sum(len(row['slots']) for row in rows),
        'user_timezone': getattr(request, 'ui_timezone', ''),
    })


@login_required
def feed(request):
    """The calendar as JSON, for the month grid's client-side navigation."""
    start = _parse_iso(request.GET.get('from')) or timezone.now() - timedelta(days=30)
    end = _parse_iso(request.GET.get('to')) or timezone.now() + timedelta(days=90)
    entries = sources.entries_for(request.user, start, end, kinds=_selected_kinds(request))
    return JsonResponse({'entries': [{
        'title': entry.title,
        'start': timezone.localtime(entry.start).isoformat() if entry.start else None,
        'end': timezone.localtime(entry.end).isoformat() if entry.end else None,
        'allDay': entry.all_day,
        'kind': entry.kind,
        'label': entry.label,
        'colour': entry.colour,
        'detail': entry.detail,
        'location': entry.location,
        'url': entry.url,
        'meta': entry.meta,
    } for entry in entries]})


@login_required
def ics(request):
    """Download the visible calendar as a one-off ``.ics`` file."""
    start, end = sources.window_around()
    entries = sources.entries_for(request.user, start, end, kinds=_exportable_kinds(request))
    return _ics_response(entries, filename='metanoia-calendar.ics')


def personal_feed(request, token):
    """The subscribable calendar feed — the "export to Google / Outlook" route.

    Deliberately unauthenticated and addressed by a secret token: Google,
    Outlook and Apple fetch the URL from their own servers with no session. The
    token is the credential, which is why it can be rotated from Settings.

    Imported events are left out, or they would loop back into the calendar
    they came from as duplicates.
    """
    from apps.accounts.models import UserSettings

    row = (UserSettings.objects
           .select_related('user')
           .filter(calendar_token=token, user__is_active=True)
           .first())
    if row is None:
        raise Http404('No such calendar')

    start, end = sources.window_around()
    entries = sources.entries_for(row.user, start, end,
                                  kinds=[k for k in sources.KINDS if k != 'external'])
    name = f'{row.user.get_full_name() or row.user.get_username()} · Metanoia Group'
    return _ics_response(entries, filename='metanoia-calendar.ics', name=name)


def _ics_response(entries, *, filename, name='Metanoia Group'):
    """``entries`` as a downloadable ``text/calendar`` response.

    Marked ``no-store``: subscribers re-poll on their own schedule, and nothing
    here is worth caching.
    """
    from . import ics as ics_mod

    body = ics_mod.serialise([{
        'uid': entry.uid or f'entry-{index}@metanoia',
        'title': entry.title,
        'start': entry.start,
        'end': entry.end,
        'all_day': entry.all_day,
        'description': entry.detail,
        'location': entry.location,
    } for index, entry in enumerate(entries)], name=name)

    response = HttpResponse(body, content_type='text/calendar; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Cache-Control'] = 'no-store, max-age=0'
    return response


@login_required
def session_detail(request, pk):
    """One session: details, join button, and afterwards the recording and pack.

    A recap filed while the AI was failing holds the raw API error; it is not a
    summary and not something to show a class, so it is dropped (and repaired
    by ``manage.py repair_session_links``). Only staff and educators may
    download the recording; students watch here. ``join_name`` is the exact
    name the reader is asked to type into Teams (see :func:`session_join`).
    """
    meeting = get_object_or_404(
        MeetingRoom.objects.select_related('module', 'host', 'programme', 'institution',
                                           'cohort', 'week', 'calendar_event'),
        pk=pk)
    if not aud.can_view(request.user, meeting):
        raise Http404('No such session')

    artifact = getattr(meeting, 'artifact', None)
    recap, pack = _reports_for(meeting)
    from apps.ai_assistant import admin_ai
    if recap is not None and admin_ai.contains_failure(recap.content):
        recap = None

    from . import identity as ident
    return render(request, 'livesessions/session-detail.html', {
        'page_title': meeting.title,
        'meeting': meeting,
        'artifact': artifact,
        'recap': recap,
        'recap_sections': summaries.display_sections(recap.content) if recap is not None else [],
        'pack': pack,
        'can_manage': aud.can_manage(request.user, meeting),
        'can_download': aud.can_download_recording(request.user),
        'is_invited': meeting.participants.filter(user=request.user).exists(),
        'attendance': _my_attendance(meeting, request.user),
        'join_name': ident.display_name_for(request.user),
    })


@login_required
def session_ics(request, pk):
    """One session as a ``.ics`` file — the "Add to calendar" button.

    Students are never on the Microsoft invite (only licensed staff are, and
    none goes out while Teams is off), so this is their way to get a session
    into their own calendar.

    Gated on :func:`~apps.livesessions.audience.can_view`, like the session
    page, so the file cannot leak a session you are not allowed to open.

    The event UID is stable across re-downloads, so a second download updates
    the event in place instead of adding a duplicate next to the first.
    """
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_view(request.user, meeting):
        raise Http404('No such session')

    from . import ics as ics_mod

    site = (getattr(django_settings, 'SITE_URL', '') or '').rstrip('/')
    join_url = f'{site}{meeting.get_join_url()}' if site else meeting.get_join_url()
    description = (meeting.description or '').strip()
    description = f'{description}\n\nJoin: {join_url}'.strip()

    body = ics_mod.serialise([{
        'uid': f'session-{meeting.pk}@metanoia',
        'title': meeting.title,
        'start': meeting.scheduled_start,
        'end': meeting.scheduled_end,
        'all_day': False,
        'description': description,
        'location': meeting.teams_join_url or join_url,
    }], name=meeting.title)

    response = HttpResponse(body, content_type='text/calendar; charset=utf-8')
    slug = slugify(meeting.title) or f'session-{meeting.pk}'
    response['Content-Disposition'] = f'attachment; filename="{slug}.ics"'
    response['Cache-Control'] = 'no-store, max-age=0'
    return response


@login_required
@require_POST
def session_share(request, pk):
    """Re-send the join link to the session's audience, as a notification.

    Goes to exactly the same people as the original invitation, via
    :func:`~apps.livesessions.audience.recipients_for`. Restricted to whoever
    may manage the session: it is a broadcast, and a student must not be able
    to fire one at their whole cohort.
    """
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_manage(request.user, meeting):
        raise Http404('No such session')

    from .reminders import announce_new_session

    told = announce_new_session(meeting, email=request.POST.get('email') != '0')
    if told:
        messages.success(
            request,
            f'The joining link has been sent to {told} '
            f'{"person" if told == 1 else "people"} in the audience.')
    else:
        messages.info(request, 'Nobody in the audience has notifications for sessions '
                               'switched on, so there was nobody to tell.')
    return redirect('livesessions:session-detail', pk=meeting.pk)


@login_required
def session_create(request):
    """Schedule a live session (admins, staff and educators only).

    GET renders the form, pre-filled with a one-hour slot when ``?start=`` is
    given (as the free-slot finder links do). POST creates the session through
    :func:`services.create_session`, which notifies the audience. When Teams is
    off or Graph refused the meeting the session is still kept, with a
    warning: its meeting is created on the first join.
    """
    if not aud.can_manage(request.user):
        messages.error(request, 'Only admins, staff and educators can schedule sessions.')
        return redirect('livesessions:calendar')

    if request.method == 'POST':
        form = forms.SessionForm(request.POST, user=request.user)
        if form.is_valid():
            cleaned = form.cleaned_data
            meeting = services.create_session(
                host=request.user,
                title=cleaned['title'],
                start=cleaned['scheduled_start'],
                end=cleaned['scheduled_end'],
                audience=cleaned['audience'],
                description=cleaned.get('description', ''),
                session_kind=cleaned['session_kind'],
                institution=cleaned.get('institution'),
                programme=cleaned.get('programme'),
                cohort=cleaned.get('cohort'),
                module=cleaned.get('module'),
                week=cleaned.get('week'),
                calendar_event=cleaned.get('calendar_event'),
                invitees=cleaned.get('invitees') or (),
                record=cleaned.get('record_automatically'),
                publish_recording=cleaned.get('publish_recording'),
            )
            if cleaned.get('is_recurring'):
                meeting.is_recurring = True
                meeting.recurrence = cleaned.get('recurrence', '')
                meeting.save(update_fields=['is_recurring', 'recurrence', 'updated_at'])

            engine = 'Teams session' if meeting.is_teams else 'Session'
            messages.success(request, f'{engine} scheduled — the audience has been notified.')
            if not meeting.teams_join_url:
                messages.warning(request, 'Microsoft Teams could not create the meeting yet. The '
                                          'session is saved and the meeting will be created when '
                                          'someone joins — or save the session again to retry. '
                                          'The error log has the details from Microsoft.')
            return redirect('livesessions:session-detail', pk=meeting.pk)
        messages.error(request, 'Please correct the errors highlighted below.')
    else:
        initial = {}
        start = _parse_iso(request.GET.get('start'))
        if start:
            initial['scheduled_start'] = timezone.localtime(start)
            initial['scheduled_end'] = timezone.localtime(start + timedelta(hours=1))
        form = forms.SessionForm(user=request.user, initial=initial)

    return render(request, 'livesessions/session-form.html', {
        'page_title': 'Schedule a live session',
        'form': form,
        'teams_ready': _teams_ready(),
    })


@login_required
def session_people_search(request):
    """``?q=`` → up to 15 people the scheduler may invite, by name or e-mail.

    Only people the session form itself would accept come back: every active
    account for admin and staff, an educator's own students for an educator.
    Two characters at least, so it cannot be used to list everyone.
    """
    if not aud.can_manage(request.user):
        raise Http404('No such page')
    from django.db.models import Q
    query = (request.GET.get('q') or '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})
    people = forms.SessionForm(user=request.user).fields['invitees'].queryset
    match = Q()
    for word in query.split()[:4]:
        match &= (Q(first_name__icontains=word) | Q(last_name__icontains=word)
                  | Q(email__icontains=word) | Q(profile__first_name__icontains=word)
                  | Q(profile__last_name__icontains=word))
    rows = people.filter(match).select_related('profile').distinct()[:15]
    from core.utils import display_name
    return JsonResponse({'results': [
        {'id': user.pk, 'name': display_name(user), 'email': user.email or ''} for user in rows]})


@login_required
def session_edit(request, pk):
    """Edit a session the user may manage, syncing its invitees.

    When the time moves, the reminder ledger is cleared: people planned around
    the old time, so the reminder job tells them again against the new one.
    The audience is told about changes to the fields in
    ``services.NOTIFY_ON_CHANGE``.
    """
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_manage(request.user, meeting):
        raise Http404('No such session')

    if request.method == 'POST':
        form = forms.SessionForm(request.POST, instance=meeting, user=request.user)
        if form.is_valid():
            moved = ('scheduled_start' in form.changed_data
                     or 'scheduled_end' in form.changed_data)
            meeting = form.save()
            meeting.participants.exclude(role='host').exclude(
                user__in=form.cleaned_data.get('invitees') or []).delete()
            for user in form.cleaned_data.get('invitees') or []:
                meeting.participants.get_or_create(user=user)
            if moved:
                meeting.reminders_sent.all().delete()
            changed = [name for name in form.changed_data if name in services.NOTIFY_ON_CHANGE]
            services.update_session(meeting, reschedule=moved, changed=changed,
                                    by=request.user, notify=True)
            if changed:
                messages.success(request, 'Session updated — the audience has been told.')
            else:
                messages.success(request, 'Session updated.')
            return redirect('livesessions:session-detail', pk=meeting.pk)
        messages.error(request, 'Please correct the errors highlighted below.')
    else:
        form = forms.SessionForm(instance=meeting, user=request.user, initial={
            'invitees': [p.user_id for p in meeting.participants.exclude(role='host')]})

    return render(request, 'livesessions/session-form.html', {
        'page_title': f'Edit — {meeting.title}',
        'form': form,
        'meeting': meeting,
        'teams_ready': _teams_ready(),
    })


@login_required
@require_POST
def session_cancel(request, pk):
    """Cancel a session (POST, managers only); everyone invited is told."""
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_manage(request.user, meeting):
        raise Http404('No such session')
    services.cancel_session(meeting, by=request.user,
                            reason=(request.POST.get('reason') or '').strip())
    messages.success(request, 'Session cancelled — everyone invited has been told.')
    return redirect('livesessions:calendar')


@login_required
def session_join(request, pk):
    """Record who is going in, then hand them over to Teams.

    The only route into a live session, behind the login gate, so the person's
    identity is written down as a :class:`~apps.livesessions.models.SessionJoin`
    *before* the redirect. Microsoft gives no supported way to pre-fill an
    anonymous attendee's display name, so the Graph report is matched back
    against this roster instead (see :mod:`apps.livesessions.identity`).

    Clicking is not attending: presence is still measured by the Graph report
    and the page heartbeat.

    Re-joining after a dropped connection is normal: the snapshot is kept fresh
    and the visits counted, so an educator can see it happened. The attendance
    row is opened so the student appears on the register straight away, at
    zero seconds and absent until presence is measured.

    A Teams session with no meeting yet (a seeded timetable, or Microsoft
    refused it at scheduling) gets one now, in this request. The redirect is in
    the same tab, straight into Teams: whoever runs the class opens the link
    signed in as the organiser, which starts the meeting; everyone else joins
    as a guest and, with the lobby bypassed, goes straight in once it has
    started. A historical session from the retired in-app video room has
    nothing to open.
    """
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_view(request.user, meeting):
        raise Http404('No such session')

    from . import identity as ident
    from .models import SessionJoin

    join, created = SessionJoin.objects.get_or_create(
        meeting=meeting, user=request.user,
        defaults={'display_name': ident.display_name_for(request.user),
                  'email': request.user.email or ''},
    )
    if not created:
        join.display_name = ident.display_name_for(request.user)
        join.email = request.user.email or ''
        join.click_count += 1
        join.save(update_fields=['display_name', 'email', 'click_count', 'last_clicked_at'])

    session = getattr(meeting, 'class_session', None)
    if session is not None and session.is_open:
        from apps.communication import services as comm
        comm.open_attendance(session, request.user)

    if meeting.is_teams:
        if not meeting.teams_join_url:
            try:
                services.ensure_teams_meeting(meeting)
            except Exception:
                logger.exception('livesessions: could not create the Teams meeting for %s', meeting)
        if not meeting.teams_join_url:
            messages.error(request, 'The Teams meeting for this session is not ready yet. '
                                    'Please try again in a minute — the organiser has been told.')
            _tell_hosts_join_failed(meeting, request.user)
            return redirect('livesessions:session-detail', pk=meeting.pk)
        return redirect(meeting.teams_join_url)
    messages.info(request, 'This session has no live meeting to join.')
    return redirect('livesessions:session-detail', pk=meeting.pk)


def _tell_hosts_join_failed(meeting, user):
    """Let the host know, once an hour at most, that people cannot get in."""
    from django.core.cache import cache
    key = f'livesessions:join-failed:{meeting.pk}'
    if not cache.add(key, 1, 60 * 60):
        return
    try:
        from apps.communication import services as comm
        if meeting.host is not None:
            comm.notify(meeting.host, actor=user, category='meetings', level='error',
                        verb='join failed', title=f'Students cannot join: {meeting.title}',
                        body='The Teams meeting for this session could not be created. '
                             'Open the session and save it again, or check the Teams settings.',
                        url=meeting.get_absolute_url(), meeting=meeting, email=True)
    except Exception:
        logger.exception('livesessions: could not tell the host that %s has no meeting', meeting)


@login_required
def session_register(request, pk):
    """The register for one session, plus the attendees Teams could not name.

    Unnamed Graph attendees ("Tebza 📱") are listed with the unmatched roster;
    one click attaches them and the answer is remembered for future sessions.
    The candidates offered are the session's own roster, minus anyone already
    on the register.
    """
    meeting = get_object_or_404(MeetingRoom.objects.select_related('module'), pk=pk)
    if not aud.can_manage(request.user, meeting):
        raise Http404('No such session')

    from . import identity as ident
    from .models import SessionJoin

    artifact = getattr(meeting, 'artifact', None)
    session = getattr(meeting, 'class_session', None)
    records = list(session.attendance.select_related('student__profile')
                   .order_by('student__first_name')) if session else []

    matched_ids = {r.student_id for r in records}
    joins = (SessionJoin.objects.filter(meeting=meeting)
             .select_related('user__profile').order_by('display_name'))

    return render(request, 'livesessions/session-register.html', {
        'page_title': f'Register — {meeting.title}',
        'meeting': meeting,
        'session': session,
        'records': records,
        'joins': joins,
        'unresolved': (artifact.unresolved_attendees if artifact else []) or [],
        'candidates': [u for u in ident.roster_for(meeting) if u.pk not in matched_ids],
    })


@login_required
@require_POST
def resolve_attendee(request, pk):
    """Attach an unmatched Teams attendee to a candidate, and remember it.

    The measured time is applied to the register, then the label is remembered
    so the same person matches automatically next week.
    """
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not aud.can_manage(request.user, meeting):
        raise Http404('No such session')

    from django.contrib.auth import get_user_model

    from apps.communication import services as comm
    from apps.communication.models import Attendance
    from . import identity as ident
    from .models import SessionArtifact

    label = (request.POST.get('label') or '').strip()
    user_id = (request.POST.get('user') or '').strip()
    seconds = int(request.POST.get('seconds') or 0)

    user = get_user_model().objects.filter(pk=user_id).first() if user_id.isdigit() else None
    if user is None or not label:
        messages.error(request, 'Choose which candidate that attendee is.')
        return redirect('livesessions:session-register', pk=meeting.pk)

    session = getattr(meeting, 'class_session', None)
    if session is not None and seconds > 0:
        comm.apply_measured_attendance(session, user, seconds=seconds,
                                       source=Attendance.SOURCE_TEAMS, authoritative=True)

    person = getattr(user, 'profile', None)
    for key in (request.POST.getlist('keys') or [label]):
        ident.remember(person, key, by=request.user)

    artifact = SessionArtifact.objects.filter(meeting=meeting).first()
    if artifact is not None:
        artifact.unresolved_attendees = [row for row in (artifact.unresolved_attendees or [])
                                         if row.get('label') != label]
        artifact.save(update_fields=['unresolved_attendees', 'updated_at'])

    messages.success(request, f'“{label}” is now recorded as {ident.display_name_for(user)} — '
                              f'and will be matched automatically from now on.')
    return redirect('livesessions:session-register', pk=meeting.pk)


@login_required
def past_sessions(request):
    """The re-watch library: every finished session the viewer may see.

    Also lists the imported YouTube recordings for the modules the person is
    enrolled on.
    """
    sessions = (aud.visible_sessions(request.user)
                .filter(scheduled_end__lt=timezone.now())
                .select_related('module', 'host', 'artifact')
                .order_by('-scheduled_start'))

    module_id = request.GET.get('module')
    if module_id and module_id.isdigit():
        sessions = sessions.filter(module_id=int(module_id))
    if request.GET.get('watchable') == '1':
        sessions = sessions.filter(artifact__state=SessionArtifact.STATE_PUBLISHED)

    recordings = []
    try:
        from . import imported
        person = getattr(request.user, 'profile', None)
        recordings = list(imported.videos_for_person(person, surface='past_sessions')[:60])
    except Exception:
        pass

    return render(request, 'livesessions/past-sessions.html', {
        'page_title': 'Past sessions',
        'sessions': sessions[:120],
        'selected_module': module_id or '',
        'recordings': recordings,
    })


@login_required
@require_POST
def connect_calendar(request):
    """Subscribe to the reader's own Google / Outlook / Apple calendar feed.

    The feed is synced immediately: "did it work?" is the only question anyone
    has at this point, and waiting half an hour for the job is no answer.
    """
    from .forms import CalendarSubscriptionForm
    from . import subscriptions as subs

    form = CalendarSubscriptionForm(request.POST, user=request.user)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(_settings_return(request))

    subscription = form.save(commit=False)
    subscription.user = request.user
    subscription.save()

    ok, detail = subs.refresh(subscription)
    if ok:
        messages.success(request, f'“{subscription.name}” connected — {detail} imported.')
    else:
        messages.error(request, f'Connected, but nothing could be read yet: {detail}')
    return redirect(_settings_return(request))


@login_required
@require_POST
def disconnect_calendar(request, pk):
    """Remove a connected calendar and everything imported from it.

    Deleting the subscription cascades to its ``ExternalEvent`` rows.
    """
    from .models import CalendarSubscription

    subscription = get_object_or_404(CalendarSubscription, pk=pk, user=request.user)
    name = subscription.name
    subscription.delete()
    messages.success(request, f'“{name}” disconnected and its events removed.')
    return redirect(_settings_return(request))


@login_required
@require_POST
def refresh_calendar(request, pk):
    """Re-import one connected calendar now, rather than waiting for the job."""
    from .models import CalendarSubscription
    from . import subscriptions as subs

    subscription = get_object_or_404(CalendarSubscription, pk=pk, user=request.user)
    subscription.is_active = True
    subscription.consecutive_failures = 0
    subscription.save(update_fields=['is_active', 'consecutive_failures', 'updated_at'])

    ok, detail = subs.refresh(subscription)
    if ok:
        messages.success(request, f'“{subscription.name}” refreshed — {detail}.')
    else:
        messages.error(request, f'Could not read “{subscription.name}”: {detail}')
    return redirect(_settings_return(request))


@login_required
@require_POST
def rotate_calendar_token(request):
    """Mint a new secret for the personal feed, revoking the old link.

    Every calendar app subscribed to the old URL stops updating immediately.
    """
    import uuid

    from apps.accounts.models import UserSettings

    row = UserSettings.for_user(request.user)
    row.calendar_token = uuid.uuid4()
    row.save(update_fields=['calendar_token', 'updated_at'])
    messages.success(request, 'Your calendar link has been reset. Any app still using the old '
                              'address will stop updating — add the new one instead.')
    return redirect(_settings_return(request))


def _settings_return(request):
    """Back to whichever page the calendar form was submitted from."""
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get('next') or ''
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return reverse('accounts:settings') + '?tab=connected'


@login_required
def settings_page(request):
    """The live-session settings form (admin and staff only), with pipeline status."""
    from core.roles import role_of_user
    if role_of_user(request.user) not in ('admin', 'staff'):
        raise Http404('Not available')

    conf = LiveSessionSettings.load()
    if request.method == 'POST':
        form = forms.LiveSessionSettingsForm(request.POST, request.FILES, instance=conf)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.updated_by = request.user
            saved.save()
            messages.success(request, 'Live session settings saved.')
            return redirect('livesessions:settings')
        messages.error(request, 'Please correct the errors highlighted below.')
    else:
        form = forms.LiveSessionSettingsForm(instance=conf)

    from . import youtube
    return render(request, 'livesessions/settings.html', {
        'page_title': 'Live session settings',
        'form': form,
        'conf': conf,
        'teams_ready': _teams_ready(),
        'youtube_configured': youtube.is_configured(),
        'youtube_remaining': youtube.uploads_remaining_today() if youtube.is_configured() else 0,
        'pipeline_counts': _pipeline_counts(),
    })


def _focus_month(request):
    """The first of the month named by ``?m=YYYY-MM``, else of the current month."""
    raw = (request.GET.get('m') or '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m').date().replace(day=1)
    except ValueError:
        today = timezone.localdate()
        return today.replace(day=1)


def _aware(day, at):
    """``day`` at time ``at`` as an aware datetime in the current time zone."""
    return timezone.make_aware(datetime.combine(day, at), timezone.get_current_timezone())


CELL_LIMIT = 3

_ACADEMIC_ICONS = {
    'test': 'pencil-square', 'exam': 'journal-check', 'supp': 'arrow-repeat',
    'assignment': 'file-earmark-text', 'deadline': 'hourglass-split', 'session': 'people',
    'intensive': 'lightning-charge', 'mock': 'clipboard-check', 'result': 'award',
    'orientation': 'signpost-split', 'holiday': 'sun', 'other': 'bookmark',
}


def _short_title(title):
    """``MP PGDA P26S | MACF`` → ``MACF``: the last segment of the LMS title convention.

    A summary lesson's prefix and trailing date ("(19 Oct 2026)") are dropped too.
    """
    text = (title or '').replace('Session summary — ', '')
    text = re.sub(r'\s*\(\d{1,2} \w{3} \d{4}\)$', '', text)
    text = text.split(' | ')[-1].strip() if ' | ' in text else text
    return text or title


def _calendar_item(entry):
    """One entry as the month grid and its popup draw it.

    ``icons`` is the combination drawn on the item: the kind first (camera for a
    class, mortarboard for an academic date…), then what state it is in — live
    now, recording ready, a test vs an exam, a session summary.

    A class's detail is its audience, which is often the module line again, so
    it is blanked when it would repeat it.
    """
    meta = entry.meta
    icons = [entry.icon]
    flags = []
    if entry.kind == 'session':
        if meta.get('live'):
            icons.append('broadcast'); flags.append('Live now')
        elif meta.get('watchable'):
            icons.append('play-circle-fill'); flags.append('Recording ready')
        elif not entry.is_past:
            icons.append('clock')
    elif entry.kind == 'academic':
        icons.append(_ACADEMIC_ICONS.get(meta.get('event_kind'), 'bookmark'))
    elif entry.kind == 'lesson' and meta.get('summary'):
        icons.append('card-text'); flags.append('Session summary')
    elif entry.kind == 'assessment':
        icons.append('hourglass-split')
    if entry.is_past and entry.kind != 'session':
        icons.append('check2')

    start = entry.local_start
    end = timezone.localtime(entry.end) if entry.end else None
    if entry.all_day:
        when = start.strftime('%A %-d %B %Y') + ' · all day' if start else ''
    elif start:
        when = start.strftime('%A %-d %B %Y · %H:%M') + (f'–{end:%H:%M}' if end and end.date() == start.date() else '')
    else:
        when = ''
    return {
        'title': entry.title,
        'short': _short_title(entry.title),
        'kind': entry.kind,
        'label': (meta.get('event_kind_label') or entry.label) if entry.kind == 'academic' else entry.label,
        'colour': entry.colour,
        'icons': icons,
        'flags': flags,
        'time': '' if entry.all_day or not start else start.strftime('%H:%M'),
        'when': when,
        'detail': '' if (entry.detail or '') == (meta.get('module') or '') else entry.detail,
        'location': entry.location or '',
        'module': meta.get('module') or '',
        'url': entry.url or '',
        'live': bool(meta.get('live')),
        'past': entry.is_past,
    }


def _cell(day, focus, by_day):
    """One month-grid day cell: its first ``CELL_LIMIT`` items, all items and the overflow count."""
    entries = [_calendar_item(e) for e in by_day.get(day, [])]
    return {
        'date': day,
        'key': day.isoformat(),
        'in_month': day.month == focus.month,
        'is_today': day == timezone.localdate(),
        'entries': entries[:CELL_LIMIT],
        'all_entries': entries,
        'overflow': max(0, len(entries) - CELL_LIMIT),
    }


def _selected_kinds(request):
    """Which sources the reader has switched on, via ``?show=session,task``."""
    raw = (request.GET.get('show') or '').strip()
    if not raw:
        return set(sources.KINDS)
    wanted = {chunk.strip() for chunk in raw.split(',') if chunk.strip()}
    return (wanted & set(sources.KINDS)) or set(sources.KINDS)


def _exportable_kinds(request):
    """Everything except what we imported — see :func:`personal_feed`."""
    return [kind for kind in _selected_kinds(request) if kind != 'external']


def _personal_feed_path(user):
    """The subscribable feed URL for ``user``, minting their token on first use.

    Returns '' if the settings row cannot be had: a missing row must not 500
    the page.
    """
    from apps.accounts.models import UserSettings
    try:
        row = UserSettings.for_user(user)
    except Exception:       # pragma: no cover - a missing row must not 500 the page
        return ''
    return reverse('livesessions:personal-feed', args=[row.calendar_token])


def session_video(request, pk):
    """Play (or download) a session's recording.

    Checks access, then redirects to a short-lived Microsoft download URL (the
    filed copy, else the original Teams saved), cached for half its lifetime.
    The ``<video>`` player follows the redirect and seeks with Range requests;
    nothing is made public or streamed through this server.

    That URL serves the file as an attachment, so it *downloads* when opened in
    a tab: a direct page visit goes to the session page's player instead.
    ``?download=1`` is the explicit download, staff and educators only
    (:func:`audience.can_download_recording`); anyone else gets 403, because
    students and parents watch on the platform and only the team downloads.
    """
    meeting, denied = _session_for_viewer(request, pk)
    if denied:
        return denied
    if request.GET.get('download') and not aud.can_download_recording(request.user):
        raise PermissionDenied('Recordings can be watched here but not downloaded.')
    if not request.GET.get('download') and _is_page_navigation(request):
        return redirect(meeting.get_absolute_url() + '#recording')
    artifact = getattr(meeting, 'artifact', None)
    if artifact is None:
        raise Http404('No recording')

    from django.core.cache import cache
    from apps.msteams import graph
    if not graph.is_configured():
        raise Http404('No recording')
    if not artifact.has_video:
        from .pipeline import locate_teams_recording
        if not locate_teams_recording(artifact):
            raise Http404('No recording yet')

    key = f'session-video:{meeting.pk}:{artifact.recording_item_id or artifact.teams_item_id}'
    url = cache.get(key)
    if not url:
        sources_ = []
        if artifact.recording_item_id:
            drive = artifact.recording_drive_id or graph.get_drive_id(artifact.drive_owner_upn or meeting.organizer_upn)
            sources_.append((drive, artifact.recording_item_id))
        if artifact.teams_item_id:
            sources_.append((artifact.teams_drive_id, artifact.teams_item_id))
        for drive_id, item_id in sources_:
            if drive_id and item_id:
                url = graph.download_url(drive_id, item_id)
                if url:
                    break
        if not url:
            return HttpResponse('The recording is temporarily unavailable. Please try again shortly.',
                                status=503, content_type='text/plain')
        cache.set(key, url, 30 * 60)
    response = redirect(url)
    response['Cache-Control'] = 'private, no-store'
    return response


def _session_for_viewer(request, pk):
    """``(meeting, None)``, or ``(None, response)`` when the viewer may not see it.

    The access check behind the recording, transcript and summary routes: the
    session page's rule plus anyone who manages the session. Linked from
    e-mail, so a signed-out reader is sent to log in rather than shown a 404.
    """
    if not request.user.is_authenticated:
        return None, redirect(f"{reverse('myhub:page-login')}?next={request.get_full_path()}")
    meeting = get_object_or_404(MeetingRoom, pk=pk)
    if not (aud.can_view(request.user, meeting) or aud.can_manage(request.user, meeting)):
        raise Http404('No such session')
    return meeting, None


def _is_page_navigation(request):
    """True when a person opened the URL in a tab, not when a player fetched it.

    ``Sec-Fetch-Dest`` says so outright in every current browser (``document``
    for a tab, ``video`` for the player). Without it, a media fetch carries a
    ``Range`` header and a page load asks for HTML — and anything else is
    treated as a media fetch, so the player never gets bounced to a page.
    """
    dest = request.headers.get('Sec-Fetch-Dest', '')
    if dest:
        return dest in ('document', 'iframe')
    if request.headers.get('Range'):
        return False
    return 'text/html' in request.headers.get('Accept', '')


def _unavailable(request, meeting, what, reason):
    """The friendly "not here (yet)" page, with a 404 — never a JSON blob."""
    return render(request, 'livesessions/session-material.html', {
        'page_title': f'{what} — {meeting.title}',
        'meeting': meeting,
        'what': what,
        'reason': reason,
        'cues': None,
    }, status=404)


@login_required
def session_transcript(request, pk):
    """A session's transcript as a readable page on our own domain.

    Fetched through Graph with the app's credentials (see :mod:`.transcripts`)
    because a OneDrive share link lands students on a Microsoft sign-in page.
    """
    meeting, denied = _session_for_viewer(request, pk)
    if denied:
        return denied
    artifact = getattr(meeting, 'artifact', None)
    from apps.msteams import graph
    if artifact is None or not artifact.has_transcript:
        return _unavailable(request, meeting, 'Transcript',
                            'There is no transcript for this session yet.')
    if not graph.is_configured():
        return _unavailable(request, meeting, 'Transcript',
                            'The transcript cannot be opened right now. Please try again later.')

    from . import transcripts
    cues = transcripts.cues(transcripts.fetch_vtt(artifact))
    if not cues:
        return _unavailable(request, meeting, 'Transcript',
                            'The transcript cannot be opened right now. Please try again later.')
    response = render(request, 'livesessions/session-material.html', {
        'page_title': f'Transcript — {meeting.title}',
        'meeting': meeting,
        'artifact': artifact,
        'what': 'Transcript',
        'cues': cues,
        'can_manage': aud.can_manage(request.user, meeting),
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def session_summary(request, pk):
    """A session's summary, on our own domain.

    The summary reading lesson when there is one (it sits on the module beside
    the study guide); otherwise the recap filed for the session, rendered here.
    A recap that is really an AI failure message is never shown as a summary.
    """
    meeting, denied = _session_for_viewer(request, pk)
    if denied:
        return denied
    artifact = getattr(meeting, 'artifact', None)
    if artifact is not None and artifact.summary_lesson_id:
        return redirect('learning:lesson-view', artifact.summary_lesson_id)

    from apps.ai_assistant import admin_ai
    recap, _pack = _reports_for(meeting)
    text = getattr(recap, 'content', '') or ''
    if not text.strip() or admin_ai.contains_failure(text):
        return _unavailable(request, meeting, 'Summary',
                            'The written summary of this session is not ready yet.')
    from core.sanitize import clean_html
    from core.metanoia_materials.textconv import sections_from_markdown
    sections = [dict(section, html=clean_html(section['html']))
                for section in sections_from_markdown(text, default_title='Summary')]
    return render(request, 'livesessions/session-material.html', {
        'page_title': f'Summary — {meeting.title}',
        'meeting': meeting,
        'artifact': artifact,
        'what': 'Summary',
        'cues': None,
        'sections': sections,
    })


def _reports_for(meeting):
    """``(recap, pack)`` — the automatic recap and the Claude follow-up pack."""
    try:
        from apps.ai_assistant.models import AiReport
        recap = AiReport.objects.filter(linked_ref=f'meeting:{meeting.pk}').first()
        pack = AiReport.objects.filter(linked_ref=f'session-followup:{meeting.pk}').first()
        return recap, pack
    except Exception:
        return None, None


def _my_attendance(meeting, user):
    """``user``'s attendance row for the session's class, or None."""
    session = getattr(meeting, 'class_session', None)
    if session is None:
        return None
    return session.attendance.filter(student=user).first()


def _teams_ready():
    """True when Microsoft Graph (and so Teams) is configured."""
    from apps.msteams import graph
    return graph.is_configured()


def _pipeline_counts():
    """``{state: count}`` of session artifacts, for the settings page."""
    from django.db.models import Count
    rows = (SessionArtifact.objects.values('state')
            .annotate(n=Count('pk')).order_by('state'))
    return {row['state']: row['n'] for row in rows}


def _opening_hours(request):
    """The ``from``/``to`` hours the free-slot scan runs between, clamped (not validated)."""
    def hour(name, fallback):
        """The query parameter ``name`` as an hour 0-23, else ``fallback``."""
        try:
            return max(0, min(23, int(request.GET.get(name) or fallback)))
        except (TypeError, ValueError):
            return fallback

    open_from = hour('from_hour', 8)
    open_to = hour('to_hour', 20)
    if open_to <= open_from:
        return 8, 20
    return open_from, open_to


def _parse_iso(raw):
    """An ISO 8601 string (``Z`` allowed) as an aware datetime, or None.

    A naive value is taken to be in the current time zone.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


@login_required
def youtube_admin(request):
    """Admin/staff screen: scan the channel and map each playlist to a module +
    cohort and the places its videos should appear. Admin/staff only — mapping
    spans every institution's modules, which is not an educator's remit."""
    from core.roles import role_flags
    if not role_flags(request)['is_admin_staff']:
        raise Http404
    from apps.learning.models import Cohort, ProgrammeModule

    from . import models as ls_models
    from . import youtube as yt

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'scan':
            if not yt.can_scan():
                messages.error(request, 'YouTube reading is not configured — set the '
                                        'credentials in .env (see the note on this page).')
            else:
                import io

                from django.core.management import call_command
                buf = io.StringIO()
                try:
                    call_command('scan_youtube', stdout=buf)
                    messages.success(request, buf.getvalue().strip() or 'Scan complete.')
                except Exception as exc:  # pragma: no cover
                    messages.error(request, f'Scan failed: {exc}')
            return redirect('livesessions:youtube-admin')

        if action == 'map':
            pl = get_object_or_404(ls_models.YouTubePlaylist, pk=request.POST.get('playlist'))
            mod_id = (request.POST.get('programme_module') or '').strip()
            coh_id = (request.POST.get('cohort') or '').strip()
            pl.programme_module_id = int(mod_id) if mod_id.isdigit() else None
            pl.cohort_id = int(coh_id) if coh_id.isdigit() else None
            if pl.programme_module_id:
                m = ProgrammeModule.objects.select_related('programme__institution').get(pk=pl.programme_module_id)
                pl.programme_id = m.programme_id
                pl.institution_id = m.programme.institution_id
            else:
                pl.programme_id = pl.institution_id = None
            pl.show_past_sessions = bool(request.POST.get('show_past_sessions'))
            pl.show_my_programme = bool(request.POST.get('show_my_programme'))
            pl.show_schedule = bool(request.POST.get('show_schedule'))
            pl.is_active = bool(request.POST.get('is_active'))
            pl.save()
            messages.success(request, f'Saved “{pl.title or pl.youtube_id}”.')
            return redirect('livesessions:youtube-admin')

    playlists = ls_models.YouTubePlaylist.objects.prefetch_related('videos').all()
    modules = (ProgrammeModule.objects
               .select_related('programme', 'programme__institution', 'module')
               .order_by('programme__institution__name', 'programme__name', 'order'))
    cohorts = (Cohort.objects.select_related('programme', 'programme__institution')
               .order_by('programme__institution__name', 'code'))
    return render(request, 'livesessions/youtube_admin.html', {
        'page_title': 'YouTube recordings',
        'playlists': playlists, 'modules': modules, 'cohorts': cohorts,
        'can_scan': yt.can_scan(), 'unmapped': sum(1 for p in playlists if not p.is_mapped),
    })
