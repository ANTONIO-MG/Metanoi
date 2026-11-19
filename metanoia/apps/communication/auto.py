"""Automatic notifications — telling the right people when something new arrives.

Signals (see :mod:`apps.communication.auto_signals`) call the ``queue_*``
functions below when something worth knowing happens. Each queues one
:class:`~apps.communication.models.AutoNotice`, keyed by the event, so saving
the same blueprint ten times still tells people once. The scheduler job
``auto-notices`` then calls :func:`run`, which:

1. takes every queued notice whose time has come (``due_at`` is the material's
   "available from", the lesson's publish time, or simply now);
2. checks the thing is still true — a material unpublished since, or a lesson
   pulled back to draft, is dropped rather than announced;
3. folds several new items on the same module into one notification, so a
   content-pack import of twelve files is one message, not twelve; and
4. delivers through :func:`apps.communication.services.notify`, which honours
   each person's category switches, e-mail and WhatsApp choices.

Who hears about it comes from :func:`apps.communication.broadcast.audience_users`
— the same resolver staff use in the composer.

``FOLD_AFTER``: items queued on one module within one run are folded together into one
notification above this count.
"""

import json
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

logger = logging.getLogger('apps')

FOLD_AFTER = 1

CONTENT_ROLES = ['student', 'educator']


def _queue(key, **fields):
    """Queue a notice once per ``key``. Returns the row, or None if it existed.

    Returns None too when a concurrent save queued it first (the unique key's
    :class:`IntegrityError`).
    """
    from .models import AutoNotice
    if AutoNotice.objects.filter(key=key).exists():
        return None
    try:
        with transaction.atomic():
            return AutoNotice.objects.create(key=key, **fields)
    except IntegrityError:
        return None


def _ref(obj):
    """A ``app_label.model:pk`` reference string for ``obj``."""
    return f'{obj._meta.label_lower}:{obj.pk}'


def _module_url(offering):
    """The module feed URL for ``offering`` (``''`` if it cannot be reversed)."""
    try:
        return reverse('learning:module-feed', args=[offering.pk])
    except NoReverseMatch:      # pragma: no cover
        return ''


def queue_material(material):
    """A blueprint, guide, mock exam, solutions… published on a module."""
    if not material.is_published:
        return None
    offering = material.programme_module
    kind = material.get_kind_display()
    return _queue(
        f'material:{material.pk}', kind='material', ref=_ref(material), group=f'module:{offering.pk}',
        title=f'New {kind.lower()}: {material.title}'[:200],
        body=(material.description or f'A new {kind.lower()} is available on {offering}.')[:1000],
        url=_module_url(offering), category='content',
        audience={'modules': [offering.pk], 'roles': CONTENT_ROLES},
        actor_id=material.created_by_id,
        due_at=max(material.available_from or timezone.now(), timezone.now()))


def _lesson_audience(lesson):
    """The audience dict for a lesson: its target roles, users, modules and programmes.
    """
    L = type(lesson)
    aud = {'roles': list(lesson.target_roles or []) or CONTENT_ROLES}
    users = list(lesson.target_users.values_list('pk', flat=True))
    modules = list(lesson.target_modules.values_list('pk', flat=True))
    programmes = list(lesson.target_programmes.values_list('pk', flat=True))
    if lesson.module_id:
        modules.append(lesson.module_id)
    if lesson.visibility == L.VIS_INSTITUTION and lesson.module_id:
        aud['institutions'] = [lesson.module.programme.institution_id]
    elif lesson.visibility == L.VIS_PROGRAMME and lesson.module_id and not programmes:
        programmes = [lesson.module.programme_id]
    elif lesson.visibility == L.VIS_INDIVIDUAL:
        modules, programmes = [], []
    aud.update(modules=sorted(set(modules)), programmes=sorted(set(programmes)), users=users)
    return aud


def queue_lesson(lesson):
    """A lesson published now, or scheduled to publish later."""
    L = type(lesson)
    if lesson.status == L.STATUS_PUBLISHED:
        due = timezone.now()
    elif lesson.status == L.STATUS_SCHEDULED and lesson.publish_at:
        due = lesson.publish_at
    else:
        return None
    aud = _lesson_audience(lesson)
    if not (aud.get('modules') or aud.get('programmes') or aud.get('institutions') or aud.get('users')):
        return None
    return _queue(
        f'lesson:{lesson.pk}', kind='lesson', ref=_ref(lesson),
        group=f'module:{lesson.module_id}' if lesson.module_id else '',
        title=f'New lesson: {lesson.title}'[:200],
        body=(lesson.subtitle or f'About {lesson.estimated_minutes} minutes of study.')[:1000],
        url=reverse('learning:lesson-view', args=[lesson.pk]), category='content',
        audience=aud, actor_id=lesson.created_by_id, due_at=max(due, timezone.now()))


def queue_assessment_open(assessment):
    """A test, quiz or mock exam opening (now, or at its "available from")."""
    A = type(assessment)
    if assessment.status not in (A.STATUS_OPEN, A.STATUS_SCHEDULED) or not assessment.module_id:
        return None
    due = assessment.available_from or timezone.now()
    closes = ''
    if assessment.available_to:
        closes = f' It closes {timezone.localtime(assessment.available_to):%a %d %b at %H:%M}.'
    return _queue(
        f'assessment-open:{assessment.pk}', kind='assessment-open', ref=_ref(assessment),
        title=f'{assessment.title} is open'[:200],
        body=f'A new {assessment.get_kind_display().lower() if hasattr(assessment, "get_kind_display") else "assessment"} '
             f'on {assessment.module} is ready for you.{closes}',
        url=reverse('assessments:take', args=[assessment.pk]), level='warning', category='assessments',
        audience={'modules': [assessment.module_id], 'roles': ['student']},
        actor_id=assessment.created_by_id, due_at=max(due, timezone.now()))


def queue_solutions(assessment):
    """Solutions released when an assessment closes (release-on-close)."""
    A = type(assessment)
    if assessment.status != A.STATUS_CLOSED or assessment.release_solution != getattr(A, 'RELEASE_ON_CLOSE', None):
        return None
    attempted = list(assessment.attempts.values_list('student_id', flat=True).distinct())
    if not attempted:
        return None
    return _queue(
        f'assessment-solutions:{assessment.pk}', kind='solutions', ref=_ref(assessment),
        title=f'Solutions released: {assessment.title}'[:200],
        body='The memo is open. Compare it with your answers while the paper is fresh.',
        url=reverse('assessments:take', args=[assessment.pk]), level='success', category='assessments',
        audience={'users': attempted})


def queue_result(attempt, marker=None):
    """A coach finished marking someone's attempt."""
    assessment = attempt.assessment
    total = float(assessment.total_marks or 0)
    pct = f' — {round(float(attempt.score) / total * 100)}%' if total else ''
    return _queue(
        f'result:{attempt.pk}', kind='result', ref=_ref(attempt),
        title=f'Your {assessment.title} has been marked{pct}'[:200],
        body='Open it to see your marks and the feedback on each answer.',
        url=reverse('assessments:take', args=[assessment.pk]), level='success', category='grades',
        audience={'users': [attempt.student_id]}, actor_id=getattr(marker, 'pk', None))


def queue_video(video):
    """A new video in a YouTube playlist that is mapped to a scope.

    The playlist scan also imports old uploads the first time it sees a playlist, so
    only genuinely new videos are news. Recordings the session pipeline uploaded were
    already announced by it, and a video on an unmapped playlist belongs to nobody in
    particular, so neither is queued.
    """
    pl = video.playlist
    if video.is_hidden or not pl.is_active:
        return None
    if video.published_at and video.published_at < timezone.now() - timedelta(days=3):
        return None
    from apps.livesessions.models import SessionArtifact
    if SessionArtifact.objects.filter(youtube_video_id=video.youtube_id).exists():
        return None
    aud = {'roles': CONTENT_ROLES}
    if pl.programme_module_id:
        aud['modules'] = [pl.programme_module_id]
    elif pl.cohort_id:
        aud['cohorts'] = [pl.cohort_id]
    elif pl.programme_id:
        aud['programmes'] = [pl.programme_id]
    elif pl.institution_id:
        aud['institutions'] = [pl.institution_id]
    else:
        return None
    url = _module_url(pl.programme_module) if pl.programme_module_id else reverse('livesessions:past-sessions')
    return _queue(
        f'video:{video.youtube_id}', kind='video', ref=_ref(video),
        group=f'module:{pl.programme_module_id}' if pl.programme_module_id else f'playlist:{pl.pk}',
        title=f'New video: {video.title or pl.title}'[:200],
        body=(video.description or '')[:600], url=url, category='content', audience=aud)


def _resolve(ref):
    """Load the object named by a :func:`_ref` string, or None if it is gone or malformed.
    """
    from django.apps import apps
    try:
        label, pk = ref.split(':', 1)
        app_label, model = label.split('.')
        return apps.get_model(app_label, model).objects.filter(pk=pk).first()
    except Exception:
        return None


def _still_valid(notice):
    """``''`` if the notice should go out, otherwise why not.

    For a lesson the audience is worked out now, at send time, not when it was queued:
    its target modules and people are saved after the lesson row.
    """
    obj = _resolve(notice.ref) if notice.ref else None
    if notice.ref and obj is None:
        return 'deleted'
    kind = notice.kind
    if kind == 'material':
        if not obj.is_published:
            return 'unpublished'
        if not obj.is_released:
            return 'embargoed'
    elif kind == 'lesson':
        notice.audience = _lesson_audience(obj)
        L = type(obj)
        if obj.status == L.STATUS_SCHEDULED and obj.publish_at and obj.publish_at <= timezone.now():
            return ''
        if obj.status != L.STATUS_PUBLISHED:
            return 'not published'
    elif kind == 'assessment-open':
        A = type(obj)
        if obj.status not in (A.STATUS_OPEN, A.STATUS_SCHEDULED):
            return 'no longer open'
    elif kind == 'video':
        if obj.is_hidden:
            return 'hidden'
    return ''


def _deliver(*, title, body, url, level, category, audience, actor=None):
    """Notify every user in ``audience`` (except ``actor``, who needs no telling about what
    they just did); return how many were notified.
    """
    from .broadcast import audience_users
    from .services import notify

    count = 0
    for user in audience_users(**audience).select_related('profile').iterator():
        if actor is not None and user.pk == actor.pk:
            continue
        if notify(user, title=title, body=body, url=url, level=level, verb='new on Metanoia',
                  actor=actor, category=category or None, email=True, email_kind='updates'):
            count += 1
    return count


def _fold_title(notices):
    """The title of one notification standing for several items on the same module."""
    first = notices[0]
    offering = None
    if first.group.startswith('module:'):
        from apps.learning.models import ProgrammeModule
        offering = ProgrammeModule.objects.filter(pk=first.group.split(':', 1)[1]).first()
    where = f' in {offering}' if offering else ''
    return f'{len(notices)} new items{where}'[:200]


def run(now=None):
    """Scheduler entry point: send every queued notice that is due.

    A notice whose moment has not come after all is left queued. Content items that
    share a module *and* the same audience are folded into one notification — a lesson
    aimed at three people must not be folded into a notice for the whole module. A
    notice that fails to send is logged and left queued: the next run retries it, and
    the rest still go out.
    """
    from .models import AutoNotice

    now = now or timezone.now()
    due = list(AutoNotice.objects.filter(sent_at__isnull=True, dropped='', due_at__lte=now)
               .select_related('actor')[:500])
    if not due:
        return 'nothing queued'

    live, dropped = [], 0
    for notice in due:
        why = _still_valid(notice)
        if why == 'embargoed':
            continue
        if why:
            AutoNotice.objects.filter(pk=notice.pk).update(dropped=why[:200])
            dropped += 1
        else:
            live.append(notice)

    groups, singles = {}, []
    for notice in live:
        if notice.kind in ('material', 'lesson', 'video') and notice.group:
            key = (notice.group, json.dumps(notice.audience, sort_keys=True))
            groups.setdefault(key, []).append(notice)
        else:
            singles.append(notice)

    sent = 0
    for notices in groups.values():
        if len(notices) <= FOLD_AFTER:
            singles.extend(notices)
            continue
        lines = '\n'.join(f'• {n.title}' for n in notices)
        first = notices[0]
        url = first.url
        if first.group.startswith('module:'):
            from apps.learning.models import ProgrammeModule
            offering = ProgrammeModule.objects.filter(pk=first.group.split(':', 1)[1]).first()
            url = _module_url(offering) if offering else url
        try:
            count = _deliver(title=_fold_title(notices), body=lines, url=url, level='info',
                             category='content', audience=first.audience, actor=first.actor)
        except Exception as exc:
            logger.exception('auto-notice group %s failed', first.group)
            from core.errors import report
            report('NOTF-9005', exc, context={'keys': [n.key for n in notices][:20]})
            continue
        AutoNotice.objects.filter(pk__in=[n.pk for n in notices]).update(sent_at=now, recipient_count=count)
        sent += 1

    for notice in singles:
        try:
            count = _deliver(title=notice.title, body=notice.body, url=notice.url, level=notice.level,
                             category=notice.category, audience=notice.audience, actor=notice.actor)
        except Exception as exc:
            logger.exception('auto-notice %s failed', notice.key)
            from core.errors import report
            report('NOTF-9005', exc, context={'key': notice.key})
            continue
        AutoNotice.objects.filter(pk=notice.pk).update(sent_at=now, recipient_count=count)
        sent += 1
    return f'sent {sent} notification(s) from {len(live)} event(s); dropped {dropped}'
