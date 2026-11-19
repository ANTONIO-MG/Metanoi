"""Joining the Metanoia Community, and leaving it to become a student.

* :func:`join` — ``/join/``. No e-mail check: the account is created and signed
  in on the spot as a ``community`` member (confined by CommunityAccessMiddleware).
* :func:`become_student` — e-mails a verification link (:func:`on_email_confirmed`).
* :func:`become_student_password` — a new password under the normal rules turns
  the account into a student's, who then goes through standard registration.
"""
import logging

from allauth.account.models import EmailAddress
from allauth.core import ratelimit
from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.forms import style_fields

from .forms import TERMS_VERSION, JoinCommunityForm

logger = logging.getLogger('apps')


def _is_member(user):
    """True when ``user`` is a Metanoia Community member."""
    return getattr(getattr(user, 'profile', None), 'user_type', '') == 'community'


def join(request):
    """Join the Metanoia Community and land in it, signed in.

    Rate-limited with the same ``signup`` limiter as the sign-up page, because
    this view creates accounts. An address that already has an account is never
    signed in — that would hand a stranger the account of whoever owns the
    address — and the form re-renders with ``existing`` set instead.
    Renders ``accounts/community/join.html``.
    """
    if request.user.is_authenticated:
        return redirect('pages:dashboard')

    form = JoinCommunityForm(request.POST or None)
    existing = False
    if request.method == 'POST':
        limited = ratelimit.consume_or_429(request, action='signup')
        if limited:
            return limited
        if form.is_valid():
            data = form.cleaned_data
            User = get_user_model()
            if (User.objects.filter(email__iexact=data['email']).exists()
                    or EmailAddress.objects.filter(email__iexact=data['email']).exists()):
                existing = True
            else:
                try:
                    user = _create_member(data)
                except IntegrityError:
                    existing = True
                else:
                    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    _welcome(user)
                    messages.success(request, f'Welcome to the Metanoia Community, {data["first_name"]}!')
                    return redirect('pages:dashboard')

    return render(request, 'accounts/community/join.html', {
        'page_title': 'Join the Metanoia Community', 'form': form, 'existing': existing,
    })


@transaction.atomic
def _create_member(data):
    """Create a community member's user, Person and unverified e-mail address.

    The Person row comes from the ``post_save`` signal on User and is filled in
    here: name, phone, the institution and programme they chose (and, for "Other
    institution", where they study and their cohort as typed). A member has
    nothing to onboard, so ``registered`` and ``profile_status`` are set at once.
    The e-mail is recorded unverified, so becoming a student can verify it later.
    """
    User = get_user_model()
    user = User(username=data['email'][:150], email=data['email'],
                first_name=data['first_name'], last_name=data['last_name'])
    user.set_password(data['password'])
    user.save()
    person = user.profile
    person.first_name = data['first_name']
    person.last_name = data['last_name']
    person.phone = data['phone']
    person.user_type = 'community'
    person.community_institution = data['institution']
    person.community_programme = data['programme']
    person.other_institution = data.get('other_institution', '')
    person.other_cohort = data.get('other_cohort', '')
    person.community_joined_at = timezone.now()
    person.registered = True
    person.profile_status = True
    person.terms_accepted_at = timezone.now()
    person.terms_version = TERMS_VERSION
    person.save()
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    return user


def _welcome(user):
    """Send the new member the welcome notification; a failure is only logged."""
    try:
        from apps.communication.services import notify
        notify(user, title='Welcome to the Metanoia Community',
               body=('Say hello in the community, follow what is happening in your programme, '
                     'and use Message Metanoia to talk to our team privately. When you are ready '
                     'for modules, live classes and assessments, choose "Become a student".'),
               url=reverse('pages:dashboard'), verb='welcome')
    except Exception:                                       # pragma: no cover
        logger.exception('community: welcome notification failed')


@login_required
def become_student(request):
    """Ask to become a student: we e-mail a link to verify the address.

    Members only (others go to the hub). An address that is already verified
    (confirmed earlier, say) skips the e-mail and goes straight to the password
    step. Renders ``accounts/community/become_student.html``.
    """
    if not _is_member(request.user):
        return redirect('myhub:index')
    person = request.user.profile
    sent = bool(person.upgrade_requested_at)
    if request.method == 'POST':
        limited = ratelimit.consume_or_429(request, action='manage_email')
        if limited:
            return limited
        address, _ = EmailAddress.objects.get_or_create(
            user=request.user, email=request.user.email,
            defaults={'primary': True, 'verified': False})
        person.upgrade_requested_at = timezone.now()
        person.save(update_fields=['upgrade_requested_at'])
        if address.verified:
            person.upgrade_verified_at = timezone.now()
            person.save(update_fields=['upgrade_verified_at'])
            return redirect('accounts:become-student-password')
        try:
            address.send_confirmation(request, signup=False)
        except Exception:
            logger.exception('community: upgrade verification e-mail failed')
            messages.error(request, 'We could not send the e-mail just now. Please try again shortly.')
            return redirect('accounts:become-student')
        sent = True
        messages.success(request, f'We have sent a verification link to {request.user.email}.')
    return render(request, 'accounts/community/become_student.html', {
        'page_title': 'Become a student', 'person': person, 'sent': sent,
    })


@login_required
def become_student_password(request):
    """Choose the new password; the account becomes a student's."""
    if not _is_member(request.user):
        return redirect('myhub:index')
    person = request.user.profile
    if not person.upgrade_verified_at:
        messages.info(request, 'First confirm your e-mail address with the link we send you.')
        return redirect('accounts:become-student')
    form = SetPasswordForm(request.user, request.POST or None)
    style_fields(form)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        convert_to_student(person)
        messages.success(request, 'Your e-mail is verified and your password is set. '
                                  'Now choose your programme and modules.')
        return redirect('accounts:register')
    return render(request, 'accounts/community/become_student_password.html', {
        'page_title': 'Choose your password', 'form': form,
    })


def convert_to_student(person):
    """Turn a community member into a student who still has to register.

    Their community programme, orders, invoices and Message Metanoia thread stay
    with them; the onboarding gate then sends them through registration.
    """
    person.user_type = 'student'
    person.registered = False
    person.profile_status = False
    person.upgrade_requested_at = None
    person.upgrade_verified_at = None
    person.save(update_fields=['user_type', 'registered', 'profile_status',
                               'upgrade_requested_at', 'upgrade_verified_at'])


def on_email_confirmed(request, email_address, **kwargs):
    """allauth ``email_confirmed``: a member who asked to become a student has
    now proved the address is theirs."""
    person = getattr(email_address.user, 'profile', None)
    if person is None or person.user_type != 'community' or not person.upgrade_requested_at:
        return
    person.upgrade_verified_at = timezone.now()
    person.save(update_fields=['upgrade_verified_at'])
