"""Parents and guardians: the invite, the one-page sign-up, and the child picker.

* :func:`parent_signup` — the invite link; the account is created **verified**
  (the invite proved the address).
* :func:`parent_register` — the student's shareable link; the e-mail is typed
  in, so it must still be verified before first sign-in.
* :func:`select_child` — stored in the session for :func:`core.scoping.viewing_child`.

Parents are confined by :class:`apps.accounts.middleware.ParentAccessMiddleware`.
"""
import logging

from allauth.account.models import EmailAddress
from allauth.core import ratelimit
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.errors import note
from core.redirects import safe_target
from core.scoping import PARENT_CHILD_SESSION_KEY as SESSION_CHILD_KEY

from . import emails, models
from .forms import TERMS_VERSION, ParentSignupForm

logger = logging.getLogger('apps')


def invite_guardian(person, email):
    """Invite ``email`` to become a parent/guardian of ``person`` (a student).

    Returns the open :class:`Invitation`, or ``None`` when nothing was sent (no
    or own address, already linked, or guardian slots full). The same address is
    not re-mailed; a changed address revokes the earlier unused invite, and a
    lapsed one is retired and replaced by a fresh invite.
    """
    email = (email or '').strip().lower()
    student_user = getattr(person, 'user', None)
    if not email or student_user is None:
        return None
    if email == (student_user.email or '').strip().lower():
        return None
    Invitation, ParentLink = models.Invitation, models.ParentLink
    if ParentLink.objects.filter(student=student_user, parent__email__iexact=email).exists():
        return None

    mine = Invitation.objects.filter(role=Invitation.ROLE_PARENT, student=person,
                                     invited_by=student_user, status=Invitation.STATUS_PENDING)
    mine.exclude(email__iexact=email).update(status=Invitation.STATUS_REVOKED)

    if not ParentLink.can_add_parent(student_user):
        return None
    pending = (Invitation.objects
               .filter(role=Invitation.ROLE_PARENT, student=person, email__iexact=email,
                       status=Invitation.STATUS_PENDING)
               .order_by('-created_at').first())
    if pending is not None and pending.is_open:
        return pending
    if pending is not None:
        pending.status = Invitation.STATUS_REVOKED
        pending.save(update_fields=['status'])
    invite = Invitation.objects.create(role=Invitation.ROLE_PARENT, email=email,
                                       student=person, invited_by=student_user)
    if not emails.send_invite(invite):
        logger.warning('accounts: guardian invite %s could not be e-mailed', invite.pk)
    return invite


def _student_name(student_user):
    """The student's display name, falling back to the login's name or username."""
    person = getattr(student_user, 'profile', None)
    name = f'{getattr(person, "first_name", "")} {getattr(person, "last_name", "")}'.strip()
    return name or student_user.get_full_name() or student_user.get_username()


def _create_parent(data, email, student_user, *, invite=None, verified):
    """Create the parent's User + Person and link them to ``student_user``.

    Raises ``IntegrityError`` if the address was taken in the meantime and
    :class:`GuardianLimitReached` if the student filled up their guardian slots.
    The Person row is made by the ``post_save`` signal and filled in here; the
    sign-up page asked for everything, so there is nothing left to onboard.
    """
    User = get_user_model()
    if (User.objects.filter(email__iexact=email).exists()
            or User.objects.filter(username__iexact=email).exists()
            or EmailAddress.objects.filter(email__iexact=email).exists()):
        raise IntegrityError('e-mail taken')
    if not models.ParentLink.can_add_parent(student_user):
        raise GuardianLimitReached
    user = User(username=email[:150], email=email,
                first_name=data['first_name'], last_name=data['last_name'])
    user.set_password(data['password1'])
    user.save()
    person = user.profile
    person.first_name = data['first_name']
    person.last_name = data['last_name']
    person.phone = data['phone']
    person.user_type = 'parent'
    person.invite = invite
    person.child_name = _student_name(student_user)[:160]
    person.registered = True
    person.profile_status = True
    person.terms_accepted_at = timezone.now()
    person.terms_version = TERMS_VERSION
    person.save()
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=verified)
    models.ParentLink.objects.create(parent=user, student=student_user,
                                     relationship=data.get('relationship', '')[:60])
    return user


class GuardianLimitReached(Exception):
    """The student already has the maximum number of linked guardians."""


def _welcome(user, student_user):
    """Send the new parent the registration summary and a welcome notification.

    Failures are logged, never raised.
    """
    try:
        emails.send_registration_summary(user.profile)
    except Exception:                                       # pragma: no cover
        logger.exception('parents: registration e-mail failed')
    try:
        from apps.communication.services import notify
        notify(user, title='Welcome to Metanoia Group',
               body=(f'You are now linked to {_student_name(student_user)}. Follow their progress, '
                     'results and fees from your home page, and take part in the Metanoia Community.'),
               url=reverse('myhub:index'), verb='welcome')
    except Exception:                                       # pragma: no cover
        logger.exception('parents: welcome notification failed')


def _render(request, template_ctx, status=200):
    """Render ``accounts/parent-signup.html`` with the shared context."""
    ctx = {'page_title': 'Parent / guardian sign-up',
           'max_parents': models.ParentLink.MAX_PER_STUDENT}
    ctx.update(template_ctx)
    return render(request, 'accounts/parent-signup.html', ctx, status=status)


def _link_signed_in_parent(request, student_user, invite=None):
    """An existing, signed-in parent account opened a link for another child."""
    if models.ParentLink.objects.filter(parent=request.user, student=student_user).exists():
        messages.info(request, f'You are already linked to {_student_name(student_user)}.')
    elif not models.ParentLink.can_add_parent(student_user):
        messages.error(request, f'{_student_name(student_user)} already has the maximum of '
                                f'{models.ParentLink.MAX_PER_STUDENT} parents or guardians linked.')
        return redirect('myhub:index')
    else:
        models.ParentLink.objects.create(parent=request.user, student=student_user)
        messages.success(request, f'You are now linked to {_student_name(student_user)}.')
    if invite is not None and invite.is_open:
        invite.accept(request.user)
    request.session[SESSION_CHILD_KEY] = student_user.pk
    return redirect('myhub:index')


def _is_parent_account(user):
    """True when ``user`` is a parent account."""
    return getattr(getattr(user, 'profile', None), 'user_type', '') == 'parent'


def parent_signup(request, invite):
    """The invite link's single sign-up page (called by ``views.accept_invite``
    once it has checked the invite is open).

    Someone signed in as another account (often the student, testing their own
    link) is refused with 409. One e-mail address, one account: if the invited
    address already has an account, a parent account may sign in and come back
    to the link, and anything else cannot be reused (noted as ``USER-2002``).
    The invite is locked and re-checked inside the creating transaction.
    """
    student_person = invite.student
    student_user = getattr(student_person, 'user', None)
    if student_user is None:
        return render(request, 'accounts/invite-invalid.html', {'invite': invite}, status=410)

    User = get_user_model()
    existing = User.objects.filter(email__iexact=invite.email).first()
    if request.user.is_authenticated:
        if existing is not None and existing.pk == request.user.pk and _is_parent_account(request.user):
            return _link_signed_in_parent(request, student_user, invite)
        return render(request, 'accounts/invite-invalid.html', {
            'invite': invite, 'wrong_account': True}, status=409)

    if existing is not None:
        note('USER-2002', request, kind='invite-email-taken', role=invite.role)
        existing_person = getattr(existing, 'profile', None)
        return render(request, 'accounts/invite-invalid.html', {
            'invite': invite, 'email_taken': True,
            'existing_is_parent': _is_parent_account(existing),
            'login_next': request.path,
            'existing_role': (existing_person.get_user_type_display()
                              if existing_person else 'an account'),
        }, status=409)

    ctx = {'invite': invite, 'email': invite.email, 'student_name': _student_name(student_user)}
    if not models.ParentLink.can_add_parent(student_user):
        return _render(request, {**ctx, 'full': True}, status=409)

    form = ParentSignupForm(request.POST or None, email=invite.email)
    if request.method == 'POST':
        limited = ratelimit.consume_or_429(request, action='signup')
        if limited:
            return limited
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked = models.Invitation.objects.select_for_update().get(pk=invite.pk)
                    if not locked.is_open:
                        return render(request, 'accounts/invite-invalid.html',
                                      {'invite': locked}, status=410)
                    user = _create_parent(form.cleaned_data, invite.email.strip().lower(),
                                          student_user, invite=locked, verified=True)
                    locked.accept(user)
            except GuardianLimitReached:
                return _render(request, {**ctx, 'full': True}, status=409)
            except IntegrityError:
                return render(request, 'accounts/invite-invalid.html', {
                    'invite': invite, 'email_taken': True, 'existing_role': 'an account',
                    'login_next': request.path}, status=409)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            request.session[SESSION_CHILD_KEY] = student_user.pk
            _welcome(user, student_user)
            messages.success(request, f'Welcome, {form.cleaned_data["first_name"]}! '
                                      f'You are now linked to {ctx["student_name"]}.')
            return redirect('myhub:index')
    return _render(request, {**ctx, 'form': form})


def parent_register(request, token):
    """Register as a parent of the student whose link this is. The account starts
    unverified (no invite proved the e-mail); a signed-in parent is just linked.
    """
    student_person = get_object_or_404(models.Person, parent_invite_token=token)
    student_user = student_person.user
    ctx = {'student_name': _student_name(student_user), 'shared_link': True}

    if request.user.is_authenticated:
        if not _is_parent_account(request.user):
            return render(request, 'accounts/invite-invalid.html',
                          {'invite': None, 'wrong_account': True}, status=409)
        if request.method == 'POST':
            return _link_signed_in_parent(request, student_user)
        return _render(request, {**ctx, 'link_only': True})

    if not models.ParentLink.can_add_parent(student_user):
        return _render(request, {**ctx, 'full': True}, status=409)

    form = ParentSignupForm(request.POST or None, with_email=True)
    existing = False
    if request.method == 'POST':
        limited = ratelimit.consume_or_429(request, action='signup')
        if limited:
            return limited
        if form.is_valid():
            email = form.cleaned_data['email']
            if email == (student_user.email or '').lower():
                form.add_error('email', "That is the student's own e-mail address — use your own.")
            else:
                try:
                    with transaction.atomic():
                        user = _create_parent(form.cleaned_data, email, student_user, verified=False)
                except GuardianLimitReached:
                    return _render(request, {**ctx, 'full': True}, status=409)
                except IntegrityError:
                    existing = True
                else:
                    try:
                        from apps.myhub.views import _send_verification_email
                        _send_verification_email(request, user, signup=True)
                    except Exception:                       # pragma: no cover
                        logger.exception('parents: verification e-mail failed')
                    messages.success(request, 'Account created — confirm your e-mail with the link '
                                              'we have just sent, then sign in.')
                    return redirect('myhub:page-login')
    return _render(request, {**ctx, 'form': form, 'existing': existing})


@require_POST
def select_child(request):
    """Remember which linked child a parent is following, then go back."""
    from core.scoping import children_of
    raw = (request.POST.get('student') or '').strip()
    child = children_of(request.user).filter(pk=int(raw)).first() if raw.isdigit() else None
    if child is None:
        note('USER-4003', request, kind='child-select', requested=raw[:20])
        messages.error(request, 'Choose one of your own children.')
    else:
        request.session[SESSION_CHILD_KEY] = child.pk
        messages.success(request, f'Now following {_student_name(child)}.')
    return redirect(safe_target(request, request.POST.get('next'), 'myhub:index'))
