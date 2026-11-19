"""Audience scoping — *which* people and academic objects a user may reach.

:mod:`core.roles` answers "what may this person *do*?"; this module answers "to
*whom*, and about *what*?". The two are deliberately separate: an educator has
teaching permissions (``can_teach``) but only over the modules they actually
teach, so nearly every list, dropdown and detail page needs the same narrowing.

The rule, in one line: **admin/staff are never scoped; an educator is scoped to
the modules they teach; everybody else is scoped to nothing** (their own data is
handled by the view itself, not here).

Every helper returns a queryset — never a list — so callers can keep filtering,
and every one is safe for anonymous users. Use them for *both* halves of a
change: narrowing what a form offers **and** re-validating what a POST contains.
A filtered ``<select>`` is a convenience; the queryset on the field is the
control.

Parents see only the students they are linked to. ``ParentLink`` is the only
source of truth, so a guardian who was never linked sees nothing rather than
everything.

Academic-spine scoping (used by global search and the people directory): you
reach the people and material of your own institution, programme and cohort,
plus every admin, staff member and educator. A student's spine comes from their
enrolments, an educator's from the offerings they teach; admin/staff are
unscoped.

Module constants:

``PARENT_CHAT_KINDS``
    The chat kinds a parent takes part in: their own direct threads and their
    private "Message Metanoia" thread with the team (``ChatGroup.KIND_DIRECT`` /
    ``KIND_SUPPORT``) — never a group room full of other families' children.
``PARENT_CHILD_SESSION_KEY``
    Session key holding the child a parent chose to follow (set by
    :func:`apps.accounts.parent_views.select_child`).
"""

from django.contrib.auth import get_user_model

from core.roles import role_of_user


def is_scoped(user):
    """True when this user only ever sees their own teaching audience.

    Educators are scoped. Admin and staff are not — they see the institution.
    Students and parents return ``True`` as well, but every helper below then
    returns an empty queryset: they reach their own data through their own
    views, never through the teaching surfaces these helpers feed.
    """
    return role_of_user(user) not in ('admin', 'staff')


def _person(user):
    """The ``accounts.Person`` profile of an authenticated user, else ``None``."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    return getattr(user, 'profile', None)


def taught_module_qs(user):
    """Subjects this user may teach, author for, or assign work to.

    All active modules for admin/staff; ``person.taught_modules`` for an
    educator; nothing for anyone else.
    """
    from apps.learning.models import ProgrammeModule

    if not is_scoped(user):
        return ProgrammeModule.objects.all()
    person = _person(user)
    if person is None or role_of_user(user) != 'educator':
        return ProgrammeModule.objects.none()
    return ProgrammeModule.objects.filter(pk__in=person.taught_modules.values('pk'))


def taught_programmes(user):
    """The programmes the modules this user teaches belong to."""
    from apps.learning.models import Programme

    if not is_scoped(user):
        return Programme.objects.all()
    return Programme.objects.filter(modules__in=taught_module_qs(user)).distinct()


def taught_people(user):
    """``accounts.Person`` rows enrolled in the modules this user teaches.

    Super-student preview profiles (``preview_of`` set) are never included.
    """
    from apps.accounts.models import Person

    people = Person.objects.filter(preview_of__isnull=True)
    if not is_scoped(user):
        return people
    return people.filter(module_enrolments__programme_module__in=taught_module_qs(user)).distinct()


def taught_students(user):
    """The ``User`` accounts behind :func:`taught_people` (active only)."""
    from core.student_view import exclude_previews
    User = get_user_model()
    if not is_scoped(user):
        return exclude_previews(User.objects.filter(is_active=True))
    return exclude_previews(User.objects
                            .filter(is_active=True,
                                    profile__module_enrolments__programme_module__in=taught_module_qs(user))
                            .distinct())


def is_parent(user):
    """True when this account is a parent/guardian."""
    return role_of_user(user) == 'parent'


def children_of(user):
    """The students this parent looks after, as a ``User`` queryset.

    Empty for anyone who is not a parent, and empty for a parent with no link —
    which is the safe direction: an unlinked guardian sees nothing at all rather
    than the whole school.
    """
    User = get_user_model()
    if not is_parent(user):
        return User.objects.none()
    from apps.accounts.models import ParentLink
    return User.objects.filter(
        pk__in=ParentLink.objects.filter(parent=user).values('student_id'))


def child_modules(user):
    """Every module this parent's children are enrolled in."""
    from apps.learning.models import ProgrammeModule
    return ProgrammeModule.objects.filter(
        enrolments__person__user__in=children_of(user)).distinct()


def parent_contacts(user):
    """Everyone a parent may message: admin, staff, and their children's educators.

    Deliberately *not* other parents or students — a parent's line of contact is
    the school and the people teaching their child, and opening it wider would
    turn the platform into a directory of families.
    """
    User = get_user_model()
    if not is_parent(user):
        return User.objects.none()
    from django.db.models import Q
    return (User.objects
            .filter(is_active=True)
            .filter(Q(profile__user_type__in=('admin', 'staff'))
                    | Q(is_staff=True)
                    | Q(profile__taught_modules__in=child_modules(user)))
            .exclude(pk=user.pk)
            .distinct())


PARENT_CHAT_KINDS = ('direct', 'support')

PARENT_CHILD_SESSION_KEY = 'parent_child_id'


def viewing_child(request):
    """The child a parent's page is about.

    In order: ``?student=<id>`` (which also becomes the remembered choice), the
    child chosen on their home page (kept in the session), then their first
    child. Every candidate is resolved against :func:`children_of`, so a parent
    asking for somebody else's student gets their own child back rather than
    the stranger's data — and a child unlinked since it was chosen is dropped.
    The answer is cached on the request; the nav asks for it on every page.
    """
    cached = getattr(request, '_viewing_child', False)
    if cached is not False:
        return cached
    children = children_of(getattr(request, 'user', None))
    child = None
    session = getattr(request, 'session', None)
    requested = (request.GET.get('student') or '').strip()
    if requested.isdigit():
        child = children.filter(pk=int(requested)).first()
        if child is not None and session is not None:
            session[PARENT_CHILD_SESSION_KEY] = child.pk
    if child is None and session is not None:
        remembered = session.get(PARENT_CHILD_SESSION_KEY)
        if remembered:
            child = children.filter(pk=remembered).first()
            if child is None:
                session.pop(PARENT_CHILD_SESSION_KEY, None)
    if child is None:
        child = children.order_by('first_name', 'last_name', 'pk').first()
    try:
        request._viewing_child = child
    except AttributeError:          # pragma: no cover - an immutable stand-in request
        pass
    return child


def _spine_ids(user):
    """``(institution_ids, programme_ids, cohort_ids, offering_ids)`` for a user.

    Four empty sets for anonymous users; never called for admin/staff.

    An educator's spine is what they coach, not what they study: the offerings
    they teach, those offerings' programmes and institutions, and every intake
    (cohort) of those programmes.

    A student's spine comes from their active programme enrolments. Their
    offerings are every module they hold an enrolment row for — locked ones
    included, so a module registered for but not yet paid is still searchable
    (the feed decides whether its material opens) — plus every active module of
    a programme they are registered for, even before picking it, which is how
    "modules on my programme" reads.
    """
    person = _person(user)
    if person is None:
        return set(), set(), set(), set()

    from apps.learning.models import ProgrammeEnrolment, ProgrammeModule

    role = role_of_user(user)
    if role == 'educator':
        offerings = ProgrammeModule.objects.filter(educators=person)
        offering_ids = set(offerings.values_list('id', flat=True))
        programme_ids = set(offerings.values_list('programme_id', flat=True))
        institution_ids = set(offerings.values_list('programme__institution_id', flat=True))
        from apps.learning.models import Cohort
        cohort_ids = set(Cohort.objects.filter(programme_id__in=programme_ids)
                         .values_list('id', flat=True))
        return institution_ids, programme_ids, cohort_ids, offering_ids

    enrolments = (ProgrammeEnrolment.objects
                  .filter(person=person, is_active=True)
                  .values_list('programme_id', 'programme__institution_id', 'cohort_id'))
    programme_ids, institution_ids, cohort_ids = set(), set(), set()
    for programme_id, institution_id, cohort_id in enrolments:
        programme_ids.add(programme_id)
        institution_ids.add(institution_id)
        if cohort_id:
            cohort_ids.add(cohort_id)
    offering_ids = set(ProgrammeModule.objects
                       .filter(enrolments__person=person)
                       .values_list('id', flat=True))
    offering_ids |= set(ProgrammeModule.objects
                        .filter(programme_id__in=programme_ids, is_active=True)
                        .values_list('id', flat=True))
    return institution_ids, programme_ids, cohort_ids, offering_ids


def my_programmes(user):
    """Programmes this user is registered for, or coaches on."""
    from apps.learning.models import Programme
    if not is_scoped(user):
        return Programme.objects.all()
    _i, programme_ids, _c, _o = _spine_ids(user)
    return Programme.objects.filter(id__in=programme_ids)


def my_offerings(user):
    """Module offerings this user may see — registered for, or on their programme."""
    from apps.learning.models import ProgrammeModule
    if not is_scoped(user):
        return ProgrammeModule.objects.all()
    _i, _p, _c, offering_ids = _spine_ids(user)
    return ProgrammeModule.objects.filter(id__in=offering_ids)


def directory_users(user, *, include_self=False):
    """Everyone this user is allowed to find in a search or a people picker.

    Two groups, unioned:

    * **Your cohort, in the widest sense** — students and coaches who share your
      institution, your programme or your intake. Sharing any one of the three
      is enough: a UNISA PGDA candidate can find another UNISA PGDA candidate
      even on a different intake, but not somebody at IAS.
    * **Everyone whose job is to be reachable** — admins, staff and educators,
      always, whatever spine they sit on. A student must be able to find the
      office and a coach without knowing which programme they are attached to.

    Admin and staff are not scoped and get the whole active directory. Parents
    are handled separately by :func:`parent_contacts`, whose narrower rule wins.
    A community member only reaches the Metanoia team (Message Metanoia), never
    students or coaches directly, because the account is unverified.
    Super-student previews are never returned: they stand in for their owner and
    are nobody to find.
    """
    from django.db.models import Q

    User = get_user_model()
    if user is None or not getattr(user, 'is_authenticated', False):
        return User.objects.none()
    if is_parent(user):
        return parent_contacts(user)
    if role_of_user(user) == 'community':
        from core.student_view import exclude_previews
        team = (Q(profile__user_type__in=('admin', 'staff')) | Q(is_staff=True) | Q(is_superuser=True))
        return exclude_previews(User.objects.filter(is_active=True)).filter(team).exclude(pk=user.pk).distinct()

    from core.student_view import exclude_previews
    qs = exclude_previews(User.objects.filter(is_active=True))
    if not include_self:
        qs = qs.exclude(pk=user.pk)
    if not is_scoped(user):
        return qs.distinct()

    institution_ids, programme_ids, cohort_ids, _offerings = _spine_ids(user)

    reachable = (Q(profile__user_type__in=('admin', 'staff', 'educator'))
                 | Q(is_staff=True) | Q(is_superuser=True))
    same_spine = (Q(profile__programme_enrolments__programme__institution_id__in=institution_ids)
                  | Q(profile__programme_enrolments__programme_id__in=programme_ids)
                  | Q(profile__programme_enrolments__cohort_id__in=cohort_ids))
    return qs.filter(reachable | same_spine).distinct()
