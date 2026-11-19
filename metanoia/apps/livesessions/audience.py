"""Who a session is for — the single answer used by notifications *and* the calendar.

There are two questions here and they must never be allowed to drift apart:

1. **Who gets told?** (:func:`recipients_for`) — invite e-mails, the platform
   notification, and the 24-hour / 30-minute reminders.
2. **Who sees it on the calendar?** (:func:`visible_sessions`) — the queryset the
   calendar page renders.

Both are derived from ``MeetingRoom.audience`` by the helpers below and
nowhere else, so they cannot drift apart.

**The one deliberate asymmetry** is supervisory: admin and staff *see* every
session (to find free slots) and are *told* about every **scoped** session, but
not invite-only ones. Educators are the supervisory case for their modules. So:

* anyone who is notified can see it, always; and
* anyone who can see it was notified, **unless** they are seeing it through the
  supervisory widening (admin/staff, or an educator's own modules).

Being told is therefore always a subset of being able to see, never the reverse
— which is the direction that would actually hurt somebody.

``JOIN_UNPAID`` is the reason :func:`can_join` gives for a refused join; an
empty reason means the join was allowed.
"""

import logging

from django.contrib.auth import get_user_model
from django.db.models import Q

from core.roles import role_of_user

logger = logging.getLogger('apps')


def recipients_for(meeting, *, include_host=True):
    """Every :class:`User` who should be told about ``meeting``.

    Returns a ``User`` queryset, de-duplicated and limited to active accounts
    that can actually be reached. Named invitees are always included, whatever
    the audience — inviting a specific person to a module class is a normal
    thing to do and must not be silently dropped.

    A scoped session also tells the admin and staff team; invite-only sessions
    do not — a one-on-one stays between its two people. A module class taught to
    one intake reaches that intake (plus the students not placed in one yet),
    not every intake on the module.

    ``AUDIENCE_PRIVATE``, or a derived audience whose scope object was cleared
    (an institution deleted, say), falls back to "invitees only". That is the
    safe direction to fail in: nobody is told about a session they were never
    named on.
    """
    User = get_user_model()
    from apps.communication.models import MeetingRoom

    invited = Q(meeting_invites__meeting=meeting)
    team = Q(profile__user_type__in=('admin', 'staff'))

    if meeting.audience == MeetingRoom.AUDIENCE_EVERYONE:
        scope = Q(is_active=True)
    elif meeting.audience == MeetingRoom.AUDIENCE_INSTITUTION and meeting.institution_id:
        scope = Q(profile__programme_enrolments__programme__institution=meeting.institution) | \
                Q(profile__taught_modules__programme__institution=meeting.institution) | team
    elif meeting.audience == MeetingRoom.AUDIENCE_PROGRAMME and meeting.programme_id:
        scope = Q(profile__programme_enrolments__programme=meeting.programme) | \
                Q(profile__taught_modules__programme=meeting.programme) | team
    elif meeting.audience == MeetingRoom.AUDIENCE_COHORT and meeting.cohort_id:
        scope = Q(profile__programme_enrolments__cohort=meeting.cohort) | team
    elif meeting.audience == MeetingRoom.AUDIENCE_MODULE and meeting.module_id:
        on_module = Q(profile__module_enrolments__programme_module=meeting.module)
        if meeting.cohort_id:
            on_module &= _intake_q(meeting.cohort)
        scope = on_module | Q(profile__taught_modules=meeting.module) | team
    else:
        scope = Q(pk__in=[])

    people = User.objects.filter(Q(is_active=True) & (scope | invited)).distinct()
    if include_host and meeting.host_id:
        people = User.objects.filter(Q(pk__in=people.values('pk')) | Q(pk=meeting.host_id)).distinct()
    elif meeting.host_id:
        people = people.exclude(pk=meeting.host_id)
    return people


def _intake_q(cohort):
    """Users in ``cohort``, or on its programme with no intake set (see :func:`class_students`)."""
    from apps.learning.models import ProgrammeEnrolment
    on_programme = ProgrammeEnrolment.objects.filter(programme_id=cohort.programme_id)
    return (Q(profile__pk__in=on_programme.filter(cohort_id=cohort.pk).values('person_id'))
            | Q(profile__pk__in=on_programme.filter(cohort__isnull=True).values('person_id')))


def intake_scope(person, field='cohort'):
    """A ``Q`` on rows tagged with an intake (``<field>``) that ``person`` belongs to.

    The calendar's half of :func:`class_students`: a row with no intake is for
    everyone it otherwise reaches; a row for an intake is for that intake's
    students, and for students on its programme who have not been placed in an
    intake yet. Another intake's row is left out.
    """
    enrolments = person.programme_enrolments.all()
    return (Q(**{f'{field}__isnull': True})
            | Q(**{f'{field}__in': enrolments.filter(cohort__isnull=False).values('cohort')})
            | Q(**{f'{field}__programme__in': enrolments.filter(cohort__isnull=True).values('programme')}))


def class_students(meeting):
    """The students a finished session's material belongs to.

    Narrower than :func:`recipients_for`, on purpose: the recording, transcript
    and session pack of a class go to the **intake it was taught to**, not to
    every intake on the module, and not to the admin team. So:

    * a module class → that intake's students on that module (every student on
      the module when the class has no intake);
    * an intake-wide class → the whole intake;
    * for both, students on the programme whose intake has **not been set** are
      included too — they have not been placed, so nothing says the class is not
      theirs. A student placed in a different intake is left out;
    * a programme / institution class → its enrolled students;
    * a private session (incl. one-on-one) → the people invited by name.
    """
    User = get_user_model()
    from apps.communication.models import MeetingRoom

    users = User.objects.filter(is_active=True)
    if meeting.audience == MeetingRoom.AUDIENCE_PRIVATE:
        return users.filter(meeting_invites__meeting=meeting).exclude(
            meeting_invites__role='host').exclude(pk=meeting.host_id).distinct()

    users = users.filter(profile__user_type='student')
    if meeting.cohort_id:
        from apps.learning.models import ProgrammeEnrolment
        on_programme = ProgrammeEnrolment.objects.filter(programme_id=meeting.cohort.programme_id)
        in_intake = on_programme.filter(cohort_id=meeting.cohort_id)
        unplaced = on_programme.filter(cohort__isnull=True)
        intake = Q(profile__pk__in=in_intake.values('person_id')) | Q(profile__pk__in=unplaced.values('person_id'))
    if meeting.module_id:
        users = users.filter(profile__module_enrolments__programme_module_id=meeting.module_id)
        if meeting.cohort_id:
            users = users.filter(intake)
    elif meeting.cohort_id:
        users = users.filter(intake)
    elif meeting.programme_id:
        users = users.filter(profile__programme_enrolments__programme_id=meeting.programme_id)
    elif meeting.institution_id:
        users = users.filter(profile__programme_enrolments__programme__institution_id=meeting.institution_id)
    elif meeting.audience != MeetingRoom.AUDIENCE_EVERYONE:
        return users.none()
    return users.distinct()


def sees_whole_calendar(user):
    """Admin and staff see every session at every institution (visibility only)."""
    return role_of_user(user) in ('admin', 'staff')


def is_supervisory_viewer(user, meeting):
    """True when ``user`` can see ``meeting`` only because of their role.

    Not in the audience and not invited, but their job lets them look.
    """
    if not can_view(user, meeting):
        return False
    return not recipients_for(meeting).filter(pk=user.pk).exists()


def visible_sessions(user, queryset=None):
    """The sessions ``user`` may see on the calendar.

    * admin / staff — everything.
    * educator — everything on a module they teach, everything they host, and
      anything they were personally invited to.
    * student / parent — sessions whose audience covers them, plus personal
      invitations (which is how a one-on-one appears).
    * anonymous — nothing.

    A parent sees exactly what their linked children see, which keeps the
    calendar consistent with every other page ``ParentAccessMiddleware`` gates.
    A one-on-one student is not part of the group classes: they see
    whole-platform sessions plus their own personal invitations.
    """
    from apps.communication.models import MeetingRoom

    queryset = MeetingRoom.objects.all() if queryset is None else queryset
    queryset = queryset.filter(is_active=True)

    if user is None or not getattr(user, 'is_authenticated', False):
        return queryset.none()
    if sees_whole_calendar(user):
        return queryset

    person = getattr(user, 'profile', None)
    mine = Q(host=user) | Q(participants__user=user)

    if person is None:
        return queryset.filter(mine).distinct()

    if role_of_user(user) == 'educator':
        taught = Q(module__in=person.taught_modules.all()) | \
                 Q(programme__in=person.taught_modules.values('programme')) | \
                 Q(audience=MeetingRoom.AUDIENCE_EVERYONE)
        return queryset.filter(mine | taught).distinct()

    if role_of_user(user) == 'parent':
        from core.scoping import children_of
        children = list(children_of(user))
        if not children:
            return queryset.filter(mine).distinct()
        scope = Q(pk__in=[])
        for child in children:
            child_user = getattr(child, 'user', None)
            if child_user is not None:
                scope |= Q(pk__in=visible_sessions(child_user, queryset).values('pk'))
        return queryset.filter(mine | scope).distinct()

    if person.is_one_on_one:
        return queryset.filter(mine | Q(audience=MeetingRoom.AUDIENCE_EVERYONE)).distinct()
    enrolled_programmes = person.programme_enrolments.values('programme')
    enrolled_cohorts = person.programme_enrolments.values('cohort')
    enrolled_modules = person.module_enrolments.values('programme_module')

    scope = (
        Q(audience=MeetingRoom.AUDIENCE_EVERYONE)
        | Q(audience=MeetingRoom.AUDIENCE_INSTITUTION,
            institution__programmes__in=enrolled_programmes)
        | Q(audience=MeetingRoom.AUDIENCE_PROGRAMME, programme__in=enrolled_programmes)
        | Q(audience=MeetingRoom.AUDIENCE_COHORT, cohort__in=enrolled_cohorts)
        | (Q(audience=MeetingRoom.AUDIENCE_MODULE, module__in=enrolled_modules) & intake_scope(person))
    )
    return queryset.filter(mine | scope).distinct()


def can_view(user, meeting):
    """True when ``user`` may open ``meeting``'s page."""
    from apps.communication.models import MeetingRoom
    return visible_sessions(user, MeetingRoom.objects.filter(pk=meeting.pk)).exists()


JOIN_UNPAID = 'unpaid'


def can_join(user, meeting):
    """``(allowed, reason)`` — may ``user`` actually open the call?

    Deliberately stricter than :func:`can_view`: an unpaid student still sees and
    is told about every class, but the join link stays shut until they pay.

    Everyone who is not an unpaid student passes:

    * the host, and anyone invited by name (a host who names someone has already
      made the call that this person belongs in the room, whatever they owe);
    * educators, staff and admins, who have to be able to run the class;
    * anonymous link visitors, who cannot be identified at all — that is the
      anonymous join Teams depends on, and it is not new here.

    A module class is gated on that module: a free module, a live free week and
    a paid-up enrolment all open it, exactly as the material pages already
    honour (``apps.learning.access``). Everything wider than one module
    (institution / programme / cohort / the whole platform) is gated on the
    enrolment invoice instead, as there is no single module to ask about. A
    failing billing check lets the student in rather than erroring the class.
    """
    from apps.communication.models import MeetingRoom

    if user is None or not getattr(user, 'is_authenticated', False):
        return True, ''
    if meeting.host_id == user.pk:
        return True, ''
    if role_of_user(user) != 'student':
        return True, ''
    if meeting.participants.filter(user=user).exclude(role='host').exists():
        return True, ''

    person = getattr(user, 'profile', None)
    if person is None:
        return True, ''

    if meeting.audience == MeetingRoom.AUDIENCE_MODULE and meeting.module_id:
        try:
            if meeting.module.is_unlocked_for(person):
                return True, ''
        except Exception:       # pragma: no cover - never let billing 500 a class
            logger.exception('audience: module unlock check failed for %s', meeting)
            return True, ''
        return False, JOIN_UNPAID

    try:
        if person.enrolment_settled:
            return True, ''
    except Exception:           # pragma: no cover
        logger.exception('audience: enrolment check failed for %s', user)
        return True, ''
    return False, JOIN_UNPAID


def can_download_recording(user):
    """True when ``user`` may download a session recording or be shown its raw
    YouTube / OneDrive address: admin, staff and educators.

    Students and parents watch on the platform only — no download button, no
    link they could pass on (owner, 2026-11-09). The player itself still has to
    fetch the file, so this keeps the recording off the open web rather than
    making it impossible to capture.
    """
    return role_of_user(user) in ('admin', 'staff', 'educator')


def can_manage(user, meeting=None):
    """True when ``user`` may create, edit or cancel sessions.

    Admin and staff always may. An educator may manage a session they host or
    one on a module they teach — but not another educator's class. With no
    ``meeting`` an educator may create; the form then limits the scope.
    """
    role = role_of_user(user)
    if role in ('admin', 'staff'):
        return True
    if role != 'educator':
        return False
    if meeting is None:
        return True
    if meeting.host_id == user.pk:
        return True
    person = getattr(user, 'profile', None)
    if person is None or not meeting.module_id:
        return False
    return person.taught_modules.filter(pk=meeting.module_id).exists()
