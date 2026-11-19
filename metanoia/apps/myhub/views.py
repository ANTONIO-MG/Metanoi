"""Views for the MyHub dashboard.

Domain sections (Professors, Students, Courses, Library, Staff,
Holidays, Fees, CMS) are backed by real models and support full
list / create / update / delete.  The remaining pages are UI showcase pages
from the MyHub theme and are rendered as plain templates.

NOTE: view function names are intentionally kept in sync with the keys in
``dz.py`` so the shared layout can resolve the correct per-page CSS/JS.

User-facing strings come from the central string catalogue
(``.strings.json``) through :func:`core.branding.t`. ``TOS_VERSION`` is the
version of the public Terms of Service (``myhub/pages/_tos-body.html``): bump
it, and re-run ``manage.py export_terms_pdf``, when that wording changes
materially.
"""

import logging

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth import logout as auth_logout
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from allauth.core import ratelimit

from apps.accounts.forms import TERMS_VERSION, MetanoiaSignupForm
from core.branding import t
from core.redirects import safe_target
from core.roles import role_flags

from . import forms, models

logger = logging.getLogger(__name__)


def _email_is_verified(user):
    """True if the user has at least one verified e-mail address (django-allauth)."""
    return EmailAddress.objects.filter(user=user, verified=True).exists()


def _send_verification_email(request, user, *, signup=False):
    """Register the user's (unverified) e-mail address if needed and e-mail a
    confirmation link via django-allauth. No-op if the user has no e-mail."""
    if not user.email:
        return
    email_address, _ = EmailAddress.objects.get_or_create(
        user=user, email=user.email,
        defaults={'primary': True, 'verified': False},
    )
    email_address.send_confirmation(request, signup=signup)


def _save_form(request, form_class, instance, template, *, list_url, page_title,
               extra_context=None):
    """Render and process a create/edit form for ``instance``; redirect to ``list_url`` once saved."""
    if request.method == 'POST':
        form = form_class(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, t('messages.saved', '{name} saved successfully.', name=page_title))
            return redirect(list_url)
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        form = form_class(instance=instance)
    context = {'form': form, 'object': instance, 'page_title': page_title}
    if extra_context:
        context.update(extra_context)
    return render(request, template, context)


def _delete(request, model, pk, list_url, label):
    """Delete ``model`` row ``pk`` (404 if missing) and redirect to ``list_url``."""
    obj = get_object_or_404(model, pk=pk)
    obj.delete()
    messages.success(request, t('messages.deleted', '{name} deleted.', name=label))
    return redirect(list_url)


def index(request):
    """Home dashboard: an academic-journey summary strip, module progress,
    study analytics, the events/live-session lists, a notice board and a right
    rail of messages + notifications.

    The student-facing numbers are assembled by :mod:`apps.myhub.dashboard`;
    heavy institution counts still come from the ``get_dashboard_data`` context
    processor (admin/staff/educators only).

    A parent has no studies of their own, so their home is the child(ren) they
    follow (:func:`~apps.myhub.parent_home.parent_home`). The admin team and
    educators have dashboards of their own on the staff desk, so they are
    redirected there; ``?view=classic`` still opens this page for them.

    Recent messages (the newest message received in each of the user's chats,
    newest chat first) are set here, not only inside
    :func:`~apps.myhub.dashboard.student_dashboard`, so the card still fills if
    that heavier builder trips over another panel; and a failure in the
    builder is logged rather than raised, because the dashboard must never
    500. The notice board then falls back to the generic announcement list.
    """
    from django.utils import timezone as _tz

    from . import dashboard as dash

    user = request.user
    from core.roles import role_of_user
    if role_of_user(user) == 'parent':
        from .parent_home import parent_home
        return parent_home(request)
    if request.GET.get('view') != 'classic' and user.is_authenticated:
        role = role_of_user(user)
        if role in ('admin', 'staff'):
            return redirect('staffdesk:home')
        if role == 'educator':
            return redirect('staffdesk:teaching')
    now = _tz.now()
    announcements, recent_activity = [], []

    def _announcements():
        """The latest sent announcements.

        Only the notice board's fallback (when the student dashboard builder
        failed), so it is wrapped lazily and not queried unless a template
        actually reads it.
        """
        try:
            from apps.communication.models import Announcement
            return list(Announcement.objects.filter(sent_at__isnull=False)
                        .select_related('sender', 'sender__profile').order_by('-sent_at')[:5])
        except Exception:
            return []
    from django.utils.functional import SimpleLazyObject
    announcements = SimpleLazyObject(_announcements)
    flags = role_flags(request)
    if flags.get('is_admin_staff'):
        try:
            from apps.accounts.models import ActivityLog
            recent_activity = list(ActivityLog.objects.select_related('actor').order_by('-timestamp')[:8])
        except Exception:
            pass


    upcoming = [i for i in _calendar_items(request) if i['when'] >= now][:8]

    context = {
        'page_title': 'Dashboard',
        'announcements': announcements,
        'recent_activity': recent_activity, 'upcoming_events': upcoming,
    }
    if user.is_authenticated:
        try:
            context['recent_messages'] = dash.recent_messages(user)
        except Exception:
            logger.exception('index: recent_messages failed')
            context['recent_messages'] = []
    try:
        context.update(dash.student_dashboard(user, recent=context.get('recent_messages')))
    except Exception:  # pragma: no cover — the dashboard must never 500
        logger.exception('index: student dashboard failed')
    context.setdefault('notices', announcements)
    return render(request, 'myhub/index.html', context)


def events_feed(request):
    """Aggregated JSON feed consumed by FullCalendar.

    Delegates to :mod:`apps.livesessions.sources`, which is now the single place
    every calendar entry is gathered. This used to hold its own copy of that
    aggregation, and the two drifted: this one showed only the meetings a user
    hosted or was individually invited to, so a module class never reached the
    calendar of the students it was actually for. One aggregation, one answer.
    """
    from apps.livesessions import sources

    start, end = sources.window_around()
    entries = sources.entries_for(request.user, start, end)
    return JsonResponse(sources.as_fullcalendar(entries), safe=False)


def _calendar_items(request):
    """The user's calendar entries as dicts, for the dashboard's event cards."""
    from apps.livesessions import sources

    start, end = sources.window_around()
    return sources.as_items(sources.entries_for(request.user, start, end))


def event_detail(request, pk):
    """Detail page for a stored calendar Event.

    Scoped like the API (``Event.visible_to``): someone else's personal
    reminder is a 404, not a page that reads it out by id."""
    event = get_object_or_404(models.Event.visible_to(request.user), pk=pk)
    return render(request, 'myhub/event-detail.html', {'page_title': event.title, 'event': event})


@require_POST
def add_reminder(request):
    """Create a personal calendar reminder for the signed-in user.

    An ``<input type="datetime-local">`` submits a tz-naive value (e.g.
    ``2026-07-05T08:13``); a date-only value falls back to midnight, and the
    result is made aware for ``USE_TZ``.
    """
    from django.http import HttpResponseBadRequest
    from django.utils import timezone as _tz
    from django.utils.dateparse import parse_date, parse_datetime

    if not request.user.is_authenticated:
        return HttpResponseBadRequest('auth required')
    title = (request.POST.get('title') or '').strip()
    raw_start = (request.POST.get('start') or '').strip()
    start = parse_datetime(raw_start)
    if start is None:
        date = parse_date(raw_start)
        if date is not None:
            start = _tz.datetime(date.year, date.month, date.day)
    if start is not None and _tz.is_naive(start):
        start = _tz.make_aware(start, _tz.get_current_timezone())
    if not title or not start:
        messages.error(request, 'A reminder needs a title and a date/time.')
        return redirect('livesessions:calendar')
    models.Event.objects.create(
        title=title, start=start, owner=request.user,
        category=models.Event.CATEGORY_REMINDER, color='#6f42c1',
        description=request.POST.get('description', ''),
    )
    messages.success(request, 'Reminder added to your calendar.')
    return redirect('livesessions:calendar')


def page_login(request):
    """Sign-in page (open to anonymous users — see ``LoginRequiredMiddleware``).

    Accounts sign in with their **e-mail address** — there is no username. New
    accounts store the e-mail as their username too, so authentication is a
    direct match; older accounts are looked up by e-mail as a fallback.

    An account whose e-mail has not been verified cannot sign in: a fresh
    confirmation link is e-mailed instead. Staff / superusers are exempt from
    that check so an administrator can never lock themselves out.

    Throttled with allauth's own rate limiter (ACCOUNT_RATE_LIMITS), because
    this page authenticates directly rather than going through allauth's
    LoginView — without it the limits configured for the project would simply
    not apply here, leaving the page open to credential stuffing.

    The order of the checks matters:

    1. ``login`` caps attempts per IP — a coarse flood guard.
    2. The typed address is resolved to a real username BEFORE authenticating,
       so one sign-in attempt is exactly one authentication attempt. This used
       to call ``authenticate()`` twice — once with the e-mail, once with a
       legacy username — which spent two of the account's lockout budget per
       try and halved how many honest fumbles a user got before django-axes
       locked them.
    3. django-axes blocks a locked-out account inside ``authenticate()``, which
       just returns ``None`` — indistinguishable from a wrong password. So axes
       is asked directly, and BEFORE allauth's own throttle, so a locked-out
       person gets the page that explains what happened instead of a bare 429
       (the two budgets are matched deliberately — see ``AXES_FAILURE_LIMIT``).
    4. ``login_failed`` additionally caps *failures* per account, so one
       address can't be brute-forced from a spread of IPs. It is checked with
       ``dry_run`` first (a successful sign-in must not spend the failure
       budget) and only consumed if the credentials turn out to be wrong; a
       successful sign-in clears the count, so a user who fumbled their
       password a few times isn't still throttled.

    Community members joined with no e-mail check by design (``/join/``);
    theirs is verified only if they choose to become a student, so they are
    exempt from the verification rule too.

    A successful sign-in is handed to allauth's login stages instead of calling
    ``auth_login()`` directly: that is what runs allauth.mfa's "authenticate"
    stage, so an account with an authenticator is sent to the code prompt and
    is NOT signed in until it passes. Without a second factor the stages fall
    straight through and the session is created here. E-mail verification was
    already enforced (with the staff exemption), so allauth's own stage is
    off. The "Welcome, …" message comes from ``HubAccountAdapter.add_message``,
    so it also appears after the 2FA step.

    "Remember me" ticked keeps the sign-in for ``SESSION_COOKIE_AGE`` (two
    weeks); unticked ends it when the browser closes. The expiry is set before
    the stages run — it lives in the session data, which the login's
    ``cycle_key()`` carries over, so it survives the 2FA step.
    """
    error = None
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''

        limited = ratelimit.consume_or_429(request, action='login')
        if limited:
            return limited

        User = get_user_model()
        username = email
        match = User.objects.filter(email__iexact=email).first()
        if match and match.get_username() != email:
            username = match.get_username()

        try:
            from axes.handlers.proxy import AxesProxyHandler
            if not AxesProxyHandler.is_allowed(request, {'username': username}):
                from django.shortcuts import render as _render
                return _render(request, 'account/lockout.html', status=429)
        except ImportError:
            pass

        key = email.lower()
        if key and not ratelimit.consume(request, action='login_failed', key=key, dry_run=True):
            return ratelimit.respond_429(request)

        user = authenticate(request, username=username, password=password)
        if user is None:
            if key:
                ratelimit.consume(request, action='login_failed', key=key)
            error = t('messages.invalid_credentials', 'Invalid e-mail or password.')
        elif (not user.is_staff and not _email_is_verified(user)
              and getattr(getattr(user, 'profile', None), 'user_type', '') != 'community'):
            _send_verification_email(request, user)
            error = t('messages.verify_email_required',
                      "Please verify your e-mail before signing in. We've sent a fresh link to {email}.",
                      email=user.email)
        else:
            if key:
                ratelimit.clear(request, action='login_failed', key=key)
            from allauth.account.app_settings import EmailVerificationMethod
            from allauth.account.internal.flows.login import perform_password_login
            from allauth.account.models import Login
            target = safe_target(request, request.POST.get('next') or request.GET.get('next'),
                                 'myhub:index')
            login = Login(user=user, email_verification=EmailVerificationMethod.NONE,
                          redirect_url=target, email=user.email)
            if request.POST.get('remember'):
                request.session.set_expiry(settings.SESSION_COOKIE_AGE)
            else:
                request.session.set_expiry(0)
            return perform_password_login(request, {'email': email}, login)
    return render(request, 'myhub/pages/page-login.html', {'page_title': 'Login', 'error': error})


def _apply_invite_to_signup(request, user):
    """If sign-up followed an invite link, stamp the new account's role + invite
    from the pending invite in the session (parent/educator). No-op otherwise —
    a normal sign-up stays a student."""
    token = request.session.pop('invite_token', None)
    if not token or user is None:
        return
    try:
        from apps.accounts.models import Invitation
        invite = Invitation.objects.filter(token=token, status=Invitation.STATUS_PENDING).first()
        if invite is None:
            return
        person = getattr(user, 'profile', None)
        if person is not None:
            person.user_type = invite.role
            person.invite = invite
            person.save(update_fields=['user_type', 'invite'])
    except Exception:
        logger.warning('Could not apply invite to sign-up', exc_info=True)


def page_register(request):
    """Sign-up page (open to anonymous users).

    Sign-up is **e-mail only** — there is no username field. The account is
    created, a verification link is e-mailed (django-allauth) and the visitor
    is sent to the "check your inbox" page; the account is unusable until that
    link is confirmed, so the user is deliberately *not* logged in here.

    The work is delegated to :class:`~apps.accounts.forms.MetanoiaSignupForm`
    (allauth's ``SignupForm`` + a recorded Terms & Conditions consent) rather
    than being hand-rolled, so password validators, the password-confirm check,
    e-mail-format validation and allauth's rate limits all apply. allauth keeps
    the default ``User`` model's required ``username`` column in sync with the
    e-mail for us — see ``ACCOUNT_USER_MODEL_USERNAME_FIELD`` in settings.

    Sign-up uses the same rate limiter as allauth's own ``SignupView``;
    otherwise this view would be an unthrottled account-creation endpoint.

    Account enumeration is guarded at every step:

    * ``form.try_save()`` is used, never ``save()``. With
      ``ACCOUNT_PREVENT_ENUMERATION`` a sign-up for an address that already
      exists is deliberately *valid*: ``try_save`` e-mails "you already have an
      account" (or the honeypot mail) and hands back a response, while
      ``save()`` would raise ``ValueError``.
    * ONE "check your e-mail" page is shown whether the account is new or
      already existed; different responses would reveal which addresses are
      registered.
    * That page must be *identical*. allauth queues a Django message
      ("Confirmation e-mail sent to jane@example.com.") on one of the two
      paths and not the other, which would render into the page and tell an
      attacker whether the address was registered. Iterating the message
      storage is not enough — it marks the messages used but leaves them in
      ``_loaded_messages``, so the template still renders them — so a fresh
      storage is swapped in and the page needs no alert of its own.

    For a new account, the role from a pending invite link (parent/educator)
    is applied now, so onboarding routes them correctly and they never have to
    pick a user type (no invite means student). ``try_save`` created the user
    and ``EmailAddress`` but does not send the activation e-mail
    (``complete_signup`` used to, but it also logged the user in and
    redirected to a URL *name*, which raised ``DisallowedRedirect``), so it is
    sent here, tolerating a mail hiccup so a transient SMTP error can't 500
    the sign-up. ``?email=`` from an invite link prefills the form.
    """
    if request.user.is_authenticated:
        return redirect('myhub:index')

    if request.method == 'POST':
        limited = ratelimit.consume_or_429(request, action='signup')
        if limited:
            return limited
        form = MetanoiaSignupForm(request.POST)
        if form.is_valid():
            user, resp = form.try_save(request)  # never save(): see the docstring (account enumeration)
            email = form.cleaned_data.get('email')
            if resp is None:
                _apply_invite_to_signup(request, user)
                try:
                    addr = EmailAddress.objects.filter(
                        user=user, email__iexact=email).first()
                    if addr and not addr.verified:
                        addr.send_confirmation(request, signup=True)
                except Exception:
                    logger.warning(
                        'Activation e-mail failed to send for %s', email, exc_info=True)

            from django.contrib.messages import get_messages
            from django.contrib.messages.storage import default_storage
            list(get_messages(request))
            request._messages = default_storage(request)  # discard queued messages: they would reveal the branch

            return render(request, 'myhub/pages/page-register-done.html', {
                'page_title': 'Check your e-mail',
                'email': email,
                'login_url': reverse('myhub:page-login'),
                'redirect_seconds': 10,
            })
    else:
        form = MetanoiaSignupForm()

    return render(request, 'myhub/pages/page-register.html',
                  {'page_title': 'Register', 'form': form,
                   'terms_version': TERMS_VERSION,
                   'prefill_email': request.GET.get('email', '')})


def page_logout(request):
    """Sign the user out and return to the sign-in page."""
    auth_logout(request)
    messages.info(request, t('messages.signed_out', 'You have been signed out.'))
    return redirect('myhub:page-login')


def page_forgot_password(request):
    """The hub's only "forgot password" page.

    It posts through **allauth's** ``ResetPasswordForm``, which is what actually
    sends the mail and mints the key — the emailed link then opens allauth's
    ``/accounts/password/reset/key/<key>/`` page. (This view used to just flash
    "a reset link has been sent" and send nothing at all.)

    The outcome message is deliberately identical whether or not the address
    exists, so the page cannot be used to enumerate accounts.

    Throttled, because this endpoint sends mail to an attacker-chosen address
    and would otherwise be an e-mail-bombing vector. ``reset_password`` is
    configured per IP *and* per key (``20/m/ip,5/m/key``), so the key must be
    passed — one call covers both rates, and omitting it raises
    ``ImproperlyConfigured`` rather than silently skipping the limit.
    """
    from allauth.account.forms import ResetPasswordForm

    sent = False
    if request.method == 'POST':
        email_in = (request.POST.get('email') or '').strip()
        limited = ratelimit.consume_or_429(request, action='reset_password',
                                           key=email_in.lower() or 'anonymous')
        if limited:
            return limited

        form = ResetPasswordForm({'email': email_in})
        if form.is_valid():
            form.save(request)
        sent = True
        messages.info(request, t('messages.reset_link_sent',
                                 'If that e-mail exists, a reset link has been sent.'))
    return render(request, 'myhub/pages/page-forgot-password.html',
                  {'page_title': 'Forgot Password', 'sent': sent})



def social_dashboard(request):
    """The Metanoia Community, inside the shared app chrome.

    One community for everyone — students, educators, parents, staff and
    community members — made of the default **Metanoia Community** plus one
    community per programme. ``?c=<slug>`` shows one community; without it the
    page shows **All**: every community the viewer belongs to. The composer
    posts into the community being viewed (or any other the viewer picks), and
    a post can be shared on into another one. See apps.communication.community.

    A failing feed never blanks the page, but the reason is never swallowed
    either: it is reported (``FEED-9001``), because a silent ``pass`` turns a
    broken feed into "you have no activity", which reads as normal and so goes
    unreported for weeks. The "Explore other communities" card is only for
    non-moderators (staff are in every community): off, it offers the switch;
    on, it lists the other communities with Join / Leave.
    """
    from apps.communication import community as cm
    from apps.communication.models import Community

    communities = list(cm.communities_for(request.user))
    slug = (request.GET.get('c') or '').strip()
    active = next((c for c in communities if c.slug == slug), None)
    feed_items = []
    try:
        feed_items = cm.build_community_feed(request.user, community=active, limit=40)
    except Exception as exc:
        from core.errors import report
        report('FEED-9001', exc, request, context={'source': 'build_community_feed'})

    default = next((c for c in communities if c.is_default), None) or Community.default()
    is_moderator = cm.is_moderator(request.user)
    explore = None
    if not is_moderator and request.user.is_authenticated:
        explore_on = cm.explores(request.user)
        joined = {c.pk for c in communities}
        explore = {
            'on': explore_on,
            'others': [{'community': c, 'joined': c.pk in joined}
                       for c in cm.explorable_communities(request.user)] if explore_on else [],
        }
    if slug and active is None and not is_moderator:
        wanted = Community.objects.filter(slug=slug, is_active=True).first()
        if wanted is not None:
            messages.warning(request, f'{wanted.name} is not one of your communities. Switch on '
                                   '"Explore other communities" to join it.')
    return render(request, 'pages/feed.html', dict(
        {'page_title': active.name if active else 'Community',
         'feed_items': feed_items, 'communities': communities,
         'active_community': active, 'post_community': active or default,
         'is_moderator': is_moderator, 'explore': explore,
         'member_limits': cm.LIMITS if cm.is_member_role(request.user) else None,
         'member_max_photos': cm.MEMBER_MAX_ATTACHMENTS,
         'report_reasons': _report_reasons()},
        **_feed_rail(request)))


def _report_reasons():
    """The reasons a feed post can be reported for."""
    from apps.communication.models import FeedReport
    return FeedReport.REASON_CHOICES


def _feed_rail(request):
    """The programme feed's right rail: key dates, the current week, programme
    notifications and the educators/staff a candidate can contact.

    Each panel is built inside its own ``try`` so one empty or broken source
    costs its own card rather than the whole rail.
    """
    person = getattr(request.user, 'profile', None)
    rail = {'rail_dates': [], 'rail_week': None, 'rail_notifications': [],
            'rail_people': [], 'rail_readiness': None}

    try:
        from apps.learning import progress
        rail['rail_dates'] = progress.upcoming_dates(request.user, person, limit=5)
        rail['rail_week'] = progress.current_week(request.user, person)
        rail['rail_readiness'] = progress.readiness(request.user, person)
    except Exception:                                    # pragma: no cover
        logger.exception('feed rail: study dates failed')

    try:
        from apps.communication.models import Notification
        rail['rail_notifications'] = list(
            Notification.objects.filter(recipient=request.user)
            .order_by('-created_at')[:6])
    except Exception:                                    # pragma: no cover
        logger.exception('feed rail: notifications failed')

    try:
        from apps.accounts.models import Person
        team = (['staff', 'admin'] if getattr(person, 'user_type', '') == 'community'
                else ['educator', 'staff', 'admin'])
        rail['rail_people'] = list(
            Person.objects.filter(user_type__in=team,
                                  user__is_active=True)
            .select_related('user').order_by('user_type', 'first_name')[:8])
    except Exception:                                    # pragma: no cover
        logger.exception('feed rail: people failed')

    return rail


def page_landing(request):
    """The old public landing page. Retired: the sign-in page is the front door
    and the website is the shop window. Kept as a redirect so old links and
    bookmarks still arrive somewhere useful."""
    if request.user.is_authenticated:
        return redirect('myhub:index')
    return redirect('myhub:page-login')


def page_privacy_terms(request):
    """Privacy policy & terms. Open to everyone, signed in or not."""
    return render(request, 'pages/privacy-and-terms.html',
                  {'page_title': 'Terms & privacy', 'terms_version': TERMS_VERSION})


TOS_VERSION = '2026-10b'


def page_terms_and_conditions(request):
    """Shareable link straight to the Terms & Conditions on the terms page."""
    return redirect(reverse('pages:privacy-and-terms') + '#terms')


def page_refund_policy(request):
    """Shareable link straight to the Refund Policy (clause 13 of the terms)."""
    return redirect(reverse('pages:privacy-and-terms') + '#refund-policy')


def page_shipping_policy(request):
    """Shareable link straight to the Shipping Policy (clause 14 of the terms)."""
    return redirect(reverse('pages:privacy-and-terms') + '#shipping-policy')


def page_terms_of_service(request):
    """Public Terms of Service (website use). Open to everyone."""
    return render(request, 'pages/terms-of-service.html',
                  {'page_title': 'Terms of Service', 'tos_version': TOS_VERSION})
