"""Super student — see the platform exactly as a student does.

Admin and staff can preview every programme and module; an educator only those
they teach. The navbar toggles **student view** for a chosen scope.

Pretending per page would mean every view needing a second answer to "who is
this?", and the first one missed would leak staff-only data. Instead each
eligible account owns one hidden **preview student**: a real ``User`` +
``Person`` (``user_type='student'``) with ``Person.preview_of`` pointing back at
its owner. Turning student view on

1. syncs the preview student's enrolments to the chosen scope (active, paid,
   price 0 — nothing is ever invoiced), and
2. records the scope in the session, so :class:`StudentViewMiddleware` swaps
   ``request.user`` for the preview student on every request until it is
   turned off.

Every role check, enrolment lookup, paywall and sequence gate then answers for
a student without knowing student view exists. Anything done in student view —
lesson progress, notes, a quiz attempt — belongs to the preview student, so it
never mixes into the owner's own record.

The preview account can never be signed into (unusable password, an
``.invalid`` address nothing is delivered to) and is left out of rosters,
counts, analytics and broadcasts via :func:`exclude_previews` and friends.

Module constants:

``SESSION_KEY``
    Session key holding the active :class:`Scope` (see :meth:`Scope.as_session`).
``EXEMPT_PREFIXES``
    Paths that always run as the real account: the toggle itself (it must
    know who is really asking), Django admin, sign-out, and static/media files.
``PREVIEW_EMAIL_DOMAIN``
    The ``.invalid`` domain of every preview account's address.
``READ_ONLY_PREFIXES`` / ``READ_ONLY_ALLOWED_SUFFIXES``
    Chat is read-only in student view: the preview student may read every
    conversation it is in, but nothing it does can reach a real student — no
    message, edit, reaction, new conversation, connection request or call.
    Only marking a thread read (``/mark_read/``, which touches nothing but its
    own membership) is let through.
``READ_ONLY_BLOCKED_GET``
    GETs that would still start something with a real person.
``_ARCHIVED``
    Lookup for an archived ``auth.User`` (deleted by an admin / closed,
    awaiting purge). Such an account is no real person any more either, so
    the same helpers drop it.
``AS_STUDENT`` / ``AS_PARENT``
    Who a scope is seen as.
"""

import logging

from django.db import transaction
from django.db.models import Q

logger = logging.getLogger('apps')

SESSION_KEY = 'student_view'

EXEMPT_PREFIXES = ('/community/student-view/', '/admin/', '/accounts/logout/',
                   '/static/', '/media/')

PREVIEW_EMAIL_DOMAIN = 'student-view.invalid'

READ_ONLY_PREFIXES = ('/api/communication/chat-groups/', '/api/communication/messages/',
                      '/api/communication/meetings/', '/communication/chat/',
                      '/communication/connect/')
READ_ONLY_ALLOWED_SUFFIXES = ('/mark_read/',)
READ_ONLY_BLOCKED_GET = ('/communication/chat/direct/',)


_ARCHIVED = 'account_settings__is_closing'


def exclude_previews(users):
    """A ``User`` queryset without preview students or archived accounts."""
    return (users.filter(Q(profile__isnull=True) | Q(profile__preview_of__isnull=True))
            .exclude(**{_ARCHIVED: True}))


def is_preview_email(address):
    """True when ``address`` belongs to a preview account (``@student-view.invalid``)."""
    return str(address or '').strip().lower().endswith('@' + PREVIEW_EMAIL_DOMAIN)


def _real_role(user):
    """The role of ``user`` per :func:`core.roles.role_of_user`."""
    from core.roles import role_of_user
    return role_of_user(user)


def can_use(user):
    """Admin, staff and educators may switch into student view.

    A preview student can't (it is the thing being switched into), and neither
    can a student or a parent.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    person = getattr(user, 'profile', None)
    if person is not None and person.is_preview:
        return False
    return _real_role(user) in ('admin', 'staff', 'educator')


def _is_educator_only(user):
    """True when ``user`` previews as an educator (scoped to what they teach)."""
    return _real_role(user) == 'educator'


def allowed_modules(user):
    """Module offerings this user may preview.

    Every active offering for admin/staff — they are registered for all of
    them. Only the offerings they teach for an educator. The General module is
    never included: it needs no enrolment (:mod:`apps.learning.general`), so it
    is never previewed into one.
    """
    from apps.learning.models import ProgrammeModule

    qs = ProgrammeModule.objects.without_general().filter(
        is_active=True, programme__is_active=True, programme__institution__is_active=True)
    if not can_use(user):
        return qs.none()
    if _is_educator_only(user):
        qs = qs.filter(educators=user.profile)
    return qs


def allowed_programmes(user):
    """Programmes with at least one module this user may preview, in display order."""
    from apps.learning.models import Programme
    return (Programme.objects.filter(modules__in=allowed_modules(user)).distinct()
            .select_related('institution').order_by('institution__order', 'institution__name',
                                                    'order', 'name'))


def allowed_institutions(user):
    """Institutions with at least one programme this user may preview."""
    from apps.learning.models import Institution
    return (Institution.objects.filter(programmes__in=allowed_programmes(user)).distinct()
            .order_by('order', 'name'))


def picker_choices(user):
    """``[{institution, module_count, programmes: [...]}]`` for the scope picker.

    Each programme carries ``preview_module_count`` — how many of its modules
    this user would be registered for (all of them, or the ones they teach).
    """
    from django.db.models import Count

    counts = dict(allowed_modules(user).values('programme_id')
                  .annotate(n=Count('id')).values_list('programme_id', 'n'))
    from apps.learning.models import Cohort

    cohorts = {}
    for cohort in (Cohort.objects.filter(programme__in=allowed_programmes(user), is_active=True)
                   .order_by('-start_date', 'code')):
        cohorts.setdefault(cohort.programme_id, []).append(cohort)
    groups = {}
    order = []
    for programme in allowed_programmes(user):
        inst = programme.institution
        if inst.pk not in groups:
            groups[inst.pk] = {'institution': inst, 'programmes': [], 'module_count': 0}
            order.append(inst.pk)
        programme.preview_module_count = counts.get(programme.pk, 0)
        programme.preview_cohorts = cohorts.get(programme.pk, [])
        groups[inst.pk]['programmes'].append(programme)
        groups[inst.pk]['module_count'] += programme.preview_module_count
    return [groups[pk] for pk in order]


AS_STUDENT, AS_PARENT = 'student', 'parent'


class Scope:
    """What a student view is registered for: everything, one institution, one
    programme, or one cohort (intake) of a programme — seen either as the
    student or as that student's parent. Stored in the session as ids (any may
    be ``None``) and who to see it as."""

    def __init__(self, institution_id=None, programme_id=None, cohort_id=None, as_=AS_STUDENT):
        """Store the ids; any value of ``as_`` other than ``parent`` means student."""
        self.institution_id = institution_id
        self.programme_id = programme_id
        self.cohort_id = cohort_id
        self.as_ = AS_PARENT if as_ == AS_PARENT else AS_STUDENT

    @classmethod
    def from_session(cls, data):
        """Rebuild a scope from the dict stored under :data:`SESSION_KEY`."""
        data = data or {}
        return cls(data.get('institution'), data.get('programme'), data.get('cohort'),
                   data.get('as') or AS_STUDENT)

    def as_session(self):
        """The scope as a JSON-safe dict for the session."""
        return {'institution': self.institution_id, 'programme': self.programme_id,
                'cohort': self.cohort_id, 'as': self.as_}

    @property
    def as_parent(self):
        """True when the scope is seen as the student's parent."""
        return self.as_ == AS_PARENT

    @property
    def audience(self):
        """``student`` or ``parent`` — for "You're seeing the platform as a …"."""
        return self.as_

    def cohort(self):
        """The scope's :class:`~apps.learning.models.Cohort`, or ``None``."""
        from apps.learning.models import Cohort
        return Cohort.objects.filter(pk=self.cohort_id).first() if self.cohort_id else None

    @property
    def is_global(self):
        """True when the scope covers everything (no institution or programme)."""
        return not self.institution_id and not self.programme_id

    def modules(self, user):
        """The offerings this scope registers ``user``'s preview student for."""
        qs = allowed_modules(user)
        if self.programme_id:
            qs = qs.filter(programme_id=self.programme_id)
        elif self.institution_id:
            qs = qs.filter(programme__institution_id=self.institution_id)
        return qs

    def label(self):
        """``Everything`` / ``Milpark Education`` / ``MILPARK · PGDA …`` / ``… · P27F``."""
        from apps.learning.models import Institution, Programme
        if self.cohort_id:
            cohort = self.cohort()
            if cohort is not None:
                return f'{cohort.programme} · {cohort.code}'
        if self.programme_id:
            programme = Programme.objects.select_related('institution').filter(
                pk=self.programme_id).first()
            return str(programme) if programme else 'A programme'
        if self.institution_id:
            institution = Institution.objects.filter(pk=self.institution_id).first()
            return institution.display_name if institution else 'An institution'
        return 'Everything'


def resolve_scope(user, institution_id=None, programme_id=None, cohort_id=None, as_=AS_STUDENT):
    """Validate a requested scope against what ``user`` may preview.

    Returns a :class:`Scope`, or ``None`` when the request names something out
    of reach (an educator asking for a programme they do not teach on, or a
    cohort of another programme). A cohort's programme, and a programme's
    institution, are filled in from the row itself.
    """
    from apps.learning.models import Cohort

    def as_id(value):
        """``value`` as an ``int`` when it is all digits, else ``None``."""
        return int(value) if str(value or '').isdigit() else None

    institution_id, programme_id, cohort_id = as_id(institution_id), as_id(programme_id), as_id(cohort_id)
    as_ = AS_PARENT if as_ == AS_PARENT else AS_STUDENT
    if cohort_id:
        cohort = Cohort.objects.filter(pk=cohort_id, programme__in=allowed_programmes(user)).first()
        if cohort is None or (programme_id and cohort.programme_id != programme_id):
            return None
        return Scope(cohort.programme.institution_id, cohort.programme_id, cohort.pk, as_)
    if programme_id:
        programme = allowed_programmes(user).filter(pk=programme_id).first()
        if programme is None:
            return None
        return Scope(programme.institution_id, programme.pk, None, as_)
    if institution_id:
        if not allowed_institutions(user).filter(pk=institution_id).exists():
            return None
        return Scope(institution_id, None, None, as_)
    if not allowed_modules(user).exists():
        return None
    return Scope(as_=as_)


def preview_user_for(owner, *, create=True):
    """The owner's preview student ``User`` (created on first use)."""
    from apps.accounts.models import Person

    person = Person.objects.select_related('user').filter(preview_of=owner).first()
    if person is not None:
        return person.user
    if not create:
        return None
    return _create_preview_user(owner)


@transaction.atomic
def _create_preview_user(owner):
    """Create the owner's preview student account and profile.

    The ``User`` gets an unusable password and an ``.invalid`` address; its
    ``post_save`` signal creates the ``Person``, which is then marked a student
    preview of ``owner``, already registered, profile-complete and with the
    current terms accepted, so no onboarding step ever shows. It has no picture
    of its own: ``Person.avatar_url`` shows the owner's, so it stays in step —
    and file cleanup can never delete the owner's file.

    Returns a fresh read of the user, because the signal cached its own (now
    stale) ``Person`` on ``user.profile``.
    """
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.accounts.models import Person

    User = get_user_model()
    owner_person = getattr(owner, 'profile', None)
    first = (getattr(owner_person, 'first_name', '') or owner.first_name or '').strip()
    last = (getattr(owner_person, 'last_name', '') or owner.last_name or '').strip()
    address = f'preview-{owner.pk}@{PREVIEW_EMAIL_DOMAIN}'

    user = User(username=address[:150], email=address, first_name=first[:150],
                last_name=f'{last} (student view)'.strip()[:150], is_active=True)
    user.set_unusable_password()
    user.save()

    person, _ = Person.objects.get_or_create(user=user)
    person.preview_of = owner
    person.user_type = 'student'
    person.first_name = first[:50]
    person.last_name = f'{last} (student view)'.strip()[:50]
    if owner_person is not None:
        person.title = owner_person.title
        person.gender = owner_person.gender
    person.registered = True
    person.profile_status = True
    person.terms_accepted_at = timezone.now()
    try:
        from apps.accounts.forms import TERMS_VERSION
        person.terms_version = TERMS_VERSION
    except Exception:  # pragma: no cover
        pass
    person.save()
    return User.objects.select_related('profile').get(pk=user.pk)


def preview_parent_for(owner, *, create=True):
    """The owner's preview parent ``User``: a parent of their preview student.

    It hangs off the preview student (``Person.preview_of`` → the preview
    student's ``User``), so it is a preview account like any other — left out of
    rosters, counts and e-mail — and wears the owner's face. Linked to the
    preview student by a :class:`~apps.accounts.models.ParentLink`, so it sees
    exactly what a parent of that student sees.
    """
    from apps.accounts.models import ParentLink, Person

    student = preview_user_for(owner, create=create)
    if student is None:
        return None
    person = Person.objects.select_related('user').filter(preview_of=student).first()
    if person is None:
        if not create:
            return None
        parent = _create_preview_parent(owner, student)
    else:
        parent = person.user
    if create:
        ParentLink.objects.get_or_create(parent=parent, student=student,
                                         defaults={'relationship': 'Parent (preview)'})
    return parent


@transaction.atomic
def _create_preview_parent(owner, student):
    """Create the owner's preview parent account and profile.

    Like :func:`_create_preview_user`, but ``user_type='parent'`` and
    ``preview_of`` points at the preview ``student``. Returns a fresh read of
    the user.
    """
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from apps.accounts.models import Person

    User = get_user_model()
    owner_person = getattr(owner, 'profile', None)
    first = (getattr(owner_person, 'first_name', '') or owner.first_name or '').strip()
    last = (getattr(owner_person, 'last_name', '') or owner.last_name or '').strip()
    address = f'preview-parent-{owner.pk}@{PREVIEW_EMAIL_DOMAIN}'

    user = User(username=address[:150], email=address, first_name=first[:150],
                last_name=f'{last} (parent view)'.strip()[:150], is_active=True)
    user.set_unusable_password()
    user.save()

    person, _ = Person.objects.get_or_create(user=user)
    person.preview_of = student
    person.user_type = 'parent'
    person.first_name = first[:50]
    person.last_name = f'{last} (parent view)'.strip()[:50]
    if owner_person is not None:
        person.title = owner_person.title
        person.gender = owner_person.gender
    person.registered = True
    person.profile_status = True
    person.terms_accepted_at = timezone.now()
    try:
        from apps.accounts.forms import TERMS_VERSION
        person.terms_version = TERMS_VERSION
    except Exception:  # pragma: no cover
        pass
    person.save()
    return User.objects.select_related('profile').get(pk=user.pk)


@transaction.atomic
def sync_enrolments(preview_user, modules, cohort=None):
    """Register the preview student for exactly ``modules`` (and their programmes).

    Modules are active and free, so nothing is ever invoiced and no paywall
    shows; rows outside the scope are removed, so the student view shows just
    what was picked. Module enrolments are deleted one by one rather than in
    bulk so the chat-membership signal fires for each. ``cohort`` places the
    student in that intake of its programme; otherwise each programme gets its
    newest active intake. The preview student's cached counters are
    invalidated afterwards.
    """
    from apps.learning.models import Cohort, ModuleEnrolment, ProgrammeEnrolment

    person = preview_user.profile
    modules = list(modules.select_related('programme'))
    module_ids = {m.pk for m in modules}
    programmes = {m.programme_id: m.programme for m in modules}

    for row in ModuleEnrolment.objects.filter(person=person).exclude(programme_module_id__in=module_ids):
        row.delete()
    ProgrammeEnrolment.objects.filter(person=person).exclude(programme_id__in=programmes).delete()

    chosen = cohort
    for programme in programmes.values():
        if chosen is not None and chosen.programme_id == programme.pk:
            intake = chosen
        else:
            intake = (Cohort.objects.filter(programme=programme, is_active=True)
                      .order_by('-start_date', '-id').first())
        enrolment, created = ProgrammeEnrolment.objects.get_or_create(
            person=person, programme=programme, defaults={'cohort': intake, 'is_active': True})
        if not created and (not enrolment.is_active or enrolment.cohort_id != getattr(intake, 'pk', None)):
            enrolment.is_active = True
            enrolment.cohort = intake
            enrolment.save(update_fields=['is_active', 'cohort', 'updated_at'])

    existing = {e.programme_module_id: e for e in ModuleEnrolment.objects.filter(person=person)}
    for module in modules:
        enrolment = existing.get(module.pk)
        if enrolment is None:
            ModuleEnrolment.objects.create(
                person=person, programme_module=module, status=ModuleEnrolment.STATUS_ACTIVE,
                price_at_enrolment=0)
        elif enrolment.status != ModuleEnrolment.STATUS_ACTIVE or enrolment.paid_until:
            enrolment.status = ModuleEnrolment.STATUS_ACTIVE
            enrolment.paid_until = None
            enrolment.save(update_fields=['status', 'paid_until', 'updated_at'])

    try:
        from core.counters import invalidate_counters
        invalidate_counters(preview_user)
    except Exception:  # pragma: no cover
        pass


def start(request, scope):
    """Turn student view on for ``request.user`` with ``scope``.

    Syncs the preview student's enrolments, creates the preview parent when the
    scope is seen as a parent, and records the scope in the session.
    """
    owner = request.user
    preview = preview_user_for(owner)
    sync_enrolments(preview, scope.modules(owner), cohort=scope.cohort())
    if scope.as_parent:
        preview_parent_for(owner)
        try:
            from core.counters import invalidate_counters
            invalidate_counters(preview_parent_for(owner, create=False))
        except Exception:  # pragma: no cover
            pass
    request.session[SESSION_KEY] = scope.as_session()


def stop(request):
    """Turn student view off for this session."""
    request.session.pop(SESSION_KEY, None)


def active_scope(request):
    """The session's :class:`Scope`, or ``None`` when student view is off."""
    session = getattr(request, 'session', None)
    if session is None or SESSION_KEY not in session:
        return None
    return Scope.from_session(session.get(SESSION_KEY))


def real_user(request):
    """The person actually signed in, whether or not student view is on."""
    return getattr(request, 'real_user', None) or getattr(request, 'user', None)


class StudentViewMiddleware:
    """While student view is on, run every request as the preview student.

    Sits after the auth/preference middleware and before the access gates, so
    login, onboarding, management, module-access and MFA all judge the student.
    The real account stays on ``request.real_user``.
    """

    def __init__(self, get_response):
        """Keep the next handler in the chain."""
        self.get_response = get_response

    def __call__(self, request):
        """Swap the user if student view is on, then enforce read-only chat."""
        self._maybe_swap(request)
        if getattr(request, 'real_user', None) is not None and self._chat_blocked(request):
            return self._read_only_response(request)
        return self.get_response(request)

    @staticmethod
    def _maybe_swap(request):
        """Replace ``request.user`` (and ``request.auser``) with the preview account.

        Sets ``request.real_user`` and ``request.student_view`` whenever a valid
        scope is active, even on :data:`EXEMPT_PREFIXES` paths, which keep the real
        user. An owner who has lost the role since switching in, or whose preview
        account has gone, has the stale view dropped.
        """
        scope = active_scope(request)
        if scope is None:
            return
        owner = getattr(request, 'user', None)
        if owner is None or not owner.is_authenticated:
            return
        if not can_use(owner):
            stop(request)
            return
        request.real_user = owner
        request.student_view = scope
        if request.path_info.startswith(EXEMPT_PREFIXES):
            return
        preview = (preview_parent_for(owner, create=False) if scope.as_parent
                   else preview_user_for(owner, create=False))
        if preview is None:
            stop(request)
            return
        request.user = preview

        async def auser():
            """Async accessor returning the preview account."""
            return preview
        request.auser = auser

    @staticmethod
    def _chat_blocked(request):
        """True when this request would write to chat (see :data:`READ_ONLY_PREFIXES`)."""
        path = request.path_info
        if path.startswith(READ_ONLY_BLOCKED_GET):
            return True
        if request.method in ('GET', 'HEAD', 'OPTIONS'):
            return False
        return (path.startswith(READ_ONLY_PREFIXES)
                and not path.endswith(READ_ONLY_ALLOWED_SUFFIXES))

    @staticmethod
    def _read_only_response(request):
        """Refuse a chat write: JSON 403 for the API, else a message and a redirect to chat."""
        from django.contrib import messages
        from django.http import JsonResponse
        from django.shortcuts import redirect

        text = 'Chats are read-only in student view. Exit student view to post as yourself.'
        if request.path_info.startswith('/api/'):
            return JsonResponse({'detail': text}, status=403)
        messages.info(request, text)
        return redirect('/communication/chat/')


def context(request):
    """Template context: whether to offer the toggle, and the live view."""
    owner = real_user(request)
    if not can_use(owner):
        return {'can_student_view': False}
    from core.request_cache import cached_per_request

    scope = getattr(request, 'student_view', None)
    return {
        'can_student_view': True,
        'student_view_active': scope is not None,
        'student_view_label': (cached_per_request(request, 'student_view_label', scope.label)
                               if scope is not None else ''),
        'student_view_scope': scope,
        'student_view_as_parent': bool(scope is not None and scope.as_parent),
        'student_view_audience': scope.audience if scope is not None else 'student',
        'student_view_owner': owner,
        'student_view_is_educator': _is_educator_only(owner),
    }
