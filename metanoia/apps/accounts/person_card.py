"""The person card — who someone is, at a glance, with nothing private on it.

A pop-up (``?fragment=1``) or page: picture, name, role, institution and shared
modules. Never contact details, date of birth, funding, guardians or grades.

Access is :func:`apps.accounts.visibility.can_view_person`; anyone else gets a
404, so the card cannot be used to walk the user table. A private member's card
shows only name, picture and role to non-team viewers. The e-mail appears only
on a community member's view, and only with ``show_email`` on.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import NoReverseMatch, reverse

from core.roles import role_of_user

from . import models
from .visibility import can_view_person, is_team as _is_team


def _modules_of(person):
    """``(taught, enrolled)`` module offerings, by name."""
    taught = list(person.taught_modules.filter(is_active=True)
                  .select_related('programme__institution').order_by('code'))
    enrolled = []
    try:
        from apps.learning.models import ProgrammeModule
        enrolled = list(ProgrammeModule.objects.filter(enrolments__person=person, is_active=True)
                        .select_related('programme__institution').distinct().order_by('code'))
    except Exception:   # pragma: no cover
        pass
    return taught, enrolled


def _programmes_of(person):
    """The programmes ``person`` is registered for."""
    try:
        return [e.programme for e in person.programme_enrolments.select_related('programme__institution')
                if e.programme_id]
    except Exception:   # pragma: no cover
        return []


def card_context(request, person):
    """The template context for ``person``'s card as seen by the requester.

    ``limited`` is set when the profile is private, or members-only and the
    viewer shares no group with them (the team and the person themselves always
    see the full card). A community member viewing someone else sees a name, and
    the e-mail address only if the owner chose to show it and does not keep the
    profile private.
    """
    from .views import _person_for, _shares_group

    viewer = _person_for(request.user)
    is_self = person.user_id == request.user.id
    viewer_role = role_of_user(request.user)
    is_team = _is_team(request.user)

    limited = False
    prefs = models.UserSettings.for_user(person.user) if person.user_id else None
    if not is_self and not is_team and prefs is not None:
        vis = prefs.profile_visibility
        if vis == models.UserSettings.VISIBILITY_PRIVATE:
            limited = True
        elif vis == models.UserSettings.VISIBILITY_MEMBERS and not _shares_group(viewer, person):
            limited = True

    ctx = {'person': person, 'is_self': is_self, 'limited': limited, 'is_team': is_team,
           'role_label': person.get_user_type_display()}
    if viewer_role == 'community' and not is_self:
        email = ''
        if not limited and prefs is not None and prefs.show_email:
            email = getattr(person.user, 'email', '')
        ctx.update(names_only=not limited, email=email)
        return ctx
    if limited:
        return ctx

    taught, enrolled = _modules_of(person)
    mine = set()
    if viewer is not None and not is_self:
        t, e = _modules_of(viewer)
        mine = {m.pk for m in t + e}
    ctx.update(
        taught=taught[:8],
        programmes=_programmes_of(person)[:3],
        shared=[m for m in enrolled if m.pk in mine][:8],
        enrolled_count=len(enrolled),
        joined=getattr(person.user, 'date_joined', None),
    )
    links = {}
    try:
        links['profile'] = reverse('accounts:profile', args=[person.pk])
        if person.user_id and not is_self:
            links['message'] = reverse('communication:chat-direct', args=[person.user_id])
        if is_team and person.user_type == 'student':
            links['student360'] = reverse('staffdesk:student', args=[person.pk])
    except NoReverseMatch:  # pragma: no cover
        pass
    ctx['links'] = links
    return ctx


@login_required
def person_card(request, pk):
    """The person card as a page, or as a pop-up fragment with ``?fragment=1``.

    A person the requester may not view is a 404, not a 403: a stranger's id must
    look exactly like a missing one.
    """
    person = get_object_or_404(models.Person.objects.select_related('user'), pk=pk)
    if not can_view_person(request.user, person):
        raise Http404('No such person')
    ctx = card_context(request, person)
    if request.GET.get('fragment'):
        return render(request, 'accounts/_person_card.html', ctx)
    ctx['page_title'] = str(person)
    return render(request, 'accounts/person-card.html', ctx)


@login_required
def person_card_for_user(request, user_id):
    """The same card, addressed by login id (what chat and notifications hold)."""
    person = get_object_or_404(models.Person.objects.select_related('user'), user_id=user_id)
    return person_card(request, person.pk)
