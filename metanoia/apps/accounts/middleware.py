"""Request-scoped middleware for the accounts app.

* :class:`CurrentUserMiddleware` — stashes the active request in thread-local
  storage so model signals (see :mod:`apps.accounts.signals`) can record *who*
  triggered a change; read it back with :func:`get_current_request`.

* :class:`LoginRequiredMiddleware` — makes the whole site private: anonymous
  visitors are redirected to the login page for **every** page except a small
  allow-list (login, sign-up, forgot-password, the e-mail-confirmation pages,
  and the ``/admin/`` · ``/api/`` · ``/static/`` · ``/media/`` endpoints, which
  do their own authentication). This is enforced centrally so individual views
  don't each need an ``@login_required`` decorator.

* :class:`OnboardingMiddleware` — registration → profile → enrolment payment
  before the hub.
* :class:`ManagementAccessMiddleware` — institution-management CRUD is
  admin/staff only.
* :class:`ParentAccessMiddleware` and :class:`CommunityAccessMiddleware` —
  fail-closed allow-lists for parent and community-member accounts.
* :class:`MfaRequiredMiddleware` — optional compulsory second factor for
  privileged accounts.
* :class:`ModuleAccessMiddleware` — closes the course material when a trial
  lapses unpaid.
* :class:`SocialAppGuardMiddleware` — an unconfigured social provider becomes a
  polite redirect, not a 500.
* :class:`UserPreferenceMiddleware` — applies the saved language, theme and
  time zone to every request.

The access gates rely on ordering in ``settings.MIDDLEWARE``: login, then
onboarding, then management, parent, community, module access and MFA. The
allow-lists are module-level constants documented where they are defined.
"""

import threading
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.urls import NoReverseMatch, reverse

_thread_locals = threading.local()


class CurrentUserMiddleware:
    """Store the active request on a thread-local for the duration of the view.

    The slot is cleared in a ``finally`` block once the response is built, so a
    request never leaks onto the next job served by a pooled worker thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """Expose ``request`` to :func:`get_current_request` while the view runs."""
        _thread_locals.request = request
        try:
            return self.get_response(request)
        finally:
            _thread_locals.request = None


def get_current_request():
    """Return the request currently being processed on this thread, or ``None``."""
    return getattr(_thread_locals, 'request', None)


_EXEMPT_PREFIXES = ('/static/', '/media/', '/admin/', '/api/', '/accounts/',
                    '/favicon.ico',
                    '/calendar/feed/',
                    '/captcha/',
                    '/reports/verify/', '/social/landing/', '/social/privacy-and-terms/', '/social/terms-and-conditions/', '/social/terms-of-service/', '/social/refund-policy/', '/social/shipping-policy/',
                    '/finance/pay/', '/finance/payfast/', '/community/parents/',
                    '/communication/whatsapp/',
                    '/community/invite/',
                    '/join/',
                    '/mail/sig/',
                    '/communication/email-preferences/',
                    '/sw.js')
"""URL-path prefixes that never require a login (:class:`LoginRequiredMiddleware`).

* ``/admin/`` — the Django admin has its own login screen.
* ``/api/`` — DRF endpoints authenticate themselves and must answer with JSON,
  not an HTML redirect.
* ``/accounts/`` — django-allauth (e-mail confirmation, password reset, …).
* ``/static/``, ``/media/`` — public assets; ``/favicon.ico`` because browsers
  ask for it before anyone signs in.
* ``/calendar/feed/`` — the subscribable ``.ics``. Google, Outlook and Apple
  fetch it from their own servers with no session and no way to sign in, so the
  secret token in the URL is the credential. Only that one prefix is opened; the
  rest of ``/calendar/`` stays behind the gate.
* ``/captcha/`` — the sign-up maths-CAPTCHA image and refresh, served to
  anonymous visitors.
* ``/reports/verify/`` — public certificate verification (QR links).
* ``/social/landing/`` and the ``/social/`` legal pages — the public landing
  page (the platform's front door) and its terms and policies.
* ``/finance/pay/`` — the tokenised invoice pay-link, e-mailed to people who may
  not be signed in; ``/finance/payfast/`` — PayFast return/cancel and the
  server-to-server ITN webhook.
* ``/communication/whatsapp/`` — Meta's webhook: signature-checked, no session.
* ``/community/invite/``, ``/community/parents/`` — invite links open before the
  invitee has an account.
* ``/join/`` — *Join the Metanoia Community*, the website's front door.
* ``/mail/sig/`` — e-mail signature pictures load for whoever reads the mail.
* ``/communication/email-preferences/`` — unsubscribe links carry a signed
  token, not a session.
* ``/sw.js`` — the desktop-alerts service worker updates in the background.
"""

_EXEMPT_VIEW_NAMES = (
    'home',
    'myhub:page-login',
    'myhub:page-register',
    'myhub:page-forgot-password',
    'myhub:logout',
    'accounts:cookie-consent',
)
"""View names reachable without logging in.

``home`` (``/``) sends anonymous visitors on to the public landing page; then
login, sign-up, forgot-password and logout; and ``accounts:cookie-consent``,
because anonymous visitors answer the POPIA cookie banner too. E-mail
confirmation lives under ``/accounts/`` and is covered by
:data:`_EXEMPT_PREFIXES`.
"""


@lru_cache(maxsize=1)
def _exempt_exact_paths():
    """Resolve the allow-listed view names to URL paths (computed once, lazily)."""
    paths = set()
    for name in _EXEMPT_VIEW_NAMES:
        try:
            paths.add(reverse(name))
        except NoReverseMatch:  # pragma: no cover - URL not wired
            pass
    return frozenset(paths)


class LoginRequiredMiddleware:
    """Redirect anonymous users to ``settings.LOGIN_URL`` for every non-exempt page.

    Place this **after** ``AuthenticationMiddleware`` in ``settings.MIDDLEWARE``
    so ``request.user`` is available.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """Redirect an anonymous request to the login page, keeping ``?next=``.

        ``redirect_to_login`` preserves the requested path so the user lands
        back where they were headed after signing in.
        """
        user = getattr(request, 'user', None)
        if (user is None or not user.is_authenticated) and self._requires_login(request):
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        return self.get_response(request)

    @staticmethod
    def _requires_login(request):
        """Whether ``request`` is for a page outside the anonymous allow-list."""
        path = request.path_info
        if path.startswith(_EXEMPT_PREFIXES):
            return False
        if path in _exempt_exact_paths():
            return False
        return True


_ONBOARDING_EXEMPT_PREFIXES = ('/static/', '/media/', '/favicon.ico', '/admin/', '/api/', '/accounts/',
                               '/reports/verify/', '/finance/pay/', '/finance/payfast/',
                               '/communication/whatsapp/', '/mail/sig/', '/communication/email-preferences/',
                               '/sw.js')
"""Path prefixes :class:`OnboardingMiddleware` never gates (assets, auth, webhooks)."""

_ONBOARDING_VIEW_NAMES = (
    'accounts:register',
    'accounts:register-course',
    'accounts:register-review',
    'accounts:register-one-on-one',
    'accounts:register-complete',
    'accounts:register-parent',
    'accounts:register-educator',
    'accounts:register-staff',
    'accounts:phone-send-code',
    'accounts:phone-check-code',
    'accounts:complete-profile',
    'accounts:enrol-checkout',
    'myhub:logout',
    'myhub:page-login',
)
"""The onboarding pages themselves, always reachable while onboarding is unfinished.

The registration wizard (step 1 ``register``, step 2 ``register-course``, step 3
``register-review``), the one-on-one request (step 2's second option), the
"payment completed" hand-off screen, the invited parent/guardian/sponsor and
educator sign-ups, the admin/staff/educator first-login profile, the WhatsApp
number verification used by all of those, profile completion, the enrolment
checkout, login and logout. :class:`ParentAccessMiddleware` reuses this list so
the two gates cannot drift apart.
"""


@lru_cache(maxsize=1)
def _onboarding_paths():
    """Map each :data:`_ONBOARDING_VIEW_NAMES` entry to its URL path (computed once)."""
    paths = {}
    for name in _ONBOARDING_VIEW_NAMES:
        try:
            paths[name] = reverse(name)
        except NoReverseMatch:  # pragma: no cover
            pass
    return paths


class OnboardingMiddleware:
    """Force new users through registration → profile completion before the hub.

    After signing up (and signing in) a user must finish a two-step onboarding
    before they can reach the dashboard or any other page:

    1. **Registration** — pick a role and what they are registering for.
    2. **Profile** — finish their personal profile.

    A third gate follows: the registration invoice must be settled (paid, or a
    free 0-cost enrolment confirmed) before entering the hub; once it is, the
    pending invoice reference is cleared so the check stops querying.

    Staff / superusers are exempt (they're provisioned directly). Place this
    **after** :class:`LoginRequiredMiddleware`, which handles anonymous users.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self._maybe_redirect(request)
        return response if response is not None else self.get_response(request)

    def _maybe_redirect(self, request):
        """Return the redirect to the next unfinished onboarding step, or ``None``.

        A user without a profile yet passes through: the post-save signal is
        about to create one, so there is nothing to gate.
        """
        from django.shortcuts import redirect

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if user.is_staff or user.is_superuser:
            return None

        path = request.path_info
        if path.startswith(_ONBOARDING_EXEMPT_PREFIXES):
            return None

        if path in _onboarding_paths().values():
            return None

        person = getattr(user, 'profile', None)
        if person is None:
            return None

        if not person.registered:
            return redirect('accounts:register')
        if not person.profile_status:
            return redirect('accounts:complete-profile')
        if person.pending_invoice_uid:
            invoice = person.get_pending_invoice()
            if invoice is not None and invoice.status != 'paid':
                return redirect('accounts:enrol-checkout')
            person.pending_invoice_uid = None
            person.save(update_fields=['pending_invoice_uid'])
        return None


_ADMIN_ONLY_URL_NAMES = frozenset({
    'myhub:all-professors', 'myhub:add-professor', 'myhub:edit-professor',
    'myhub:delete-professor', 'myhub:professor-profile',
    'myhub:all-students', 'myhub:add-student', 'myhub:edit-student',
    'myhub:delete-student', 'myhub:about-student',
    'myhub:all-courses', 'myhub:add-courses', 'myhub:edit-courses',
    'myhub:delete-courses', 'myhub:about-courses',
    'myhub:all-departments', 'myhub:add-departments', 'myhub:edit-departments',
    'myhub:delete-departments',
    'myhub:all-staff', 'myhub:add-staff', 'myhub:edit-staff', 'myhub:delete-staff',
    'myhub:staff-profile',
    'myhub:all-library', 'myhub:add-library', 'myhub:edit-library', 'myhub:delete-library',
    'myhub:all-holiday', 'myhub:add-holiday', 'myhub:edit-holiday',
    'myhub:delete-holiday', 'myhub:holiday-calendar',
    'myhub:fees-collection', 'myhub:add-fees', 'myhub:delete-fees', 'myhub:fees-receipt',
    'myhub:content', 'myhub:add-content', 'myhub:delete-content',
    'myhub:menu', 'myhub:delete-menu',
    'myhub:email-template', 'myhub:add-email', 'myhub:delete-email',
    'myhub:email-inbox', 'myhub:email-compose', 'myhub:email-read',
    'myhub:blog', 'myhub:add-blog', 'myhub:edit-blog', 'myhub:delete-blog',
    'myhub:blog-category', 'myhub:delete-blog-category',
    'accounts:closed-accounts', 'accounts:reactivate-account', 'accounts:restore-archive',
    'accounts:undo',
})
"""URL names reserved for admin/staff by :class:`ManagementAccessMiddleware`.

The myhub CRUD sections (list, create, edit, delete and detail pages); the
recovery desk — reactivating or restoring somebody else's account is the most
consequential thing an administrator does here; and ``accounts:undo``, which
reverses a payment, a delete or an account deletion.
"""


class ManagementAccessMiddleware:
    """Block non-admin/staff from institution-management (CRUD) pages.

    Institution management (professors, students, courses, departments, staff,
    library, holidays, fees, CMS…) is full CRUD for admin/staff **only**.
    Educators are teach-only, students view their own data and parents view and
    pay, so none of them may reach these pages. Enforced centrally by URL name
    (:data:`_ADMIN_ONLY_URL_NAMES`); place it **after**
    :class:`OnboardingMiddleware`. It works in ``process_view`` so that
    ``request.resolver_match`` is already set.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Redirect a non-admin to the hub with an error if the view is admin-only."""
        from django.contrib import messages
        from django.shortcuts import redirect

        from core.roles import role_flags
        match = getattr(request, 'resolver_match', None)
        if match is None or not match.url_name:
            return None
        name = f'{match.namespace}:{match.url_name}' if match.namespace else match.url_name
        if name in _ADMIN_ONLY_URL_NAMES and not role_flags(request).get('is_admin_staff'):
            messages.error(request, "You don't have permission to manage that — ask an administrator.")
            return redirect('myhub:index')
        return None


_PARENT_ALLOWED_PREFIXES = (
    '/static/', '/media/', '/favicon.ico', '/accounts/', '/api/', '/__debug__/',

    '/shop/', '/finance/',

    '/communication/notifications/',
    '/communication/chat/',
    '/communication/email-preferences/',
    '/communication/support/',

    '/reports/',

    '/myhub/events.json',

    '/community/profile/', '/community/settings/',

    '/social/', '/communication/feed/', '/communication/community/mentions/',
    '/community/people/',

    '/communication/message-metanoia/', '/communication/alerts/', '/communication/push/',
    '/community/preferences/', '/captcha/', '/sw.js',
    '/communication/link-preview/',

    '/community/my-children/',
    '/community/invite/', '/community/parents/',
)
"""Path prefixes a parent account may reach (:class:`ParentAccessMiddleware`).

In order:

* Infrastructure that must never be gated — assets, auth, webhooks, the debug
  toolbar.
* Money — the shop and their own invoices.
* Being told things, and asking — notifications, chat, the unsubscribe link in
  their e-mails, and the help centre (which has parent articles). **Not**
  ``/communication/announcements/`` or ``/mail/``: both are *senders'* pages
  (compose and what-I-sent) and already require staff/educator. An announcement
  reaches a parent as a notification, which is where they read it; listing the
  compose page would only dead-end them.
* Their child's results and progress (``/reports/``) and when things happen
  (``/myhub/events.json``).
* Their own account — profile and settings.
* The Metanoia Community — the feed, and posting, liking, sharing, tagging and
  reporting in it. Parents belong to the default community and their children's
  programme communities.
* Everything else a community member has (see
  :class:`CommunityAccessMiddleware`): Message Metanoia (their private thread with
  the team), the bell and desktop alerts, the theme/language buttons, the
  service worker, and link preview cards (permission-checked per viewer).
* ``/community/my-children/`` — choosing which child to follow; the view only
  accepts a child from ``core.scoping.children_of``.
* ``/community/invite/``, ``/community/parents/`` — an invite (or a student's
  shareable link) for a further child, opened while signed in; the views link
  the child to this parent account only.
"""

_PARENT_ALLOWED_EXACT = frozenset({
    '/',
    '/myhub/',
    '/social/privacy-and-terms/', '/social/terms-and-conditions/', '/social/terms-of-service/', '/social/refund-policy/', '/social/shipping-policy/',
    '/social/landing/',
    '/calendar/',
})
"""Individual pages a parent may reach inside otherwise-closed sections.

Exact paths, not prefixes, because the section around them is not open.

* ``/`` — the front door, which redirects onward.
* ``/myhub/`` — the dashboard with its parent panel, and what ``myhub:index``
  resolves to. A duplicate ``index/`` route used to claim that name, so the
  redirect target and the allow-listed path were different URLs and parents
  looped. The route is gone; the self-redirect guard in
  :meth:`ParentAccessMiddleware.process_view` stays, so a future divergence
  degrades to one refused page rather than a dead account.
* The ``/social/`` legal pages and landing page — terms and the cookie/privacy
  notice are everybody's, whatever their role. ``/social/`` itself is the
  programme feed, so these are listed one by one rather than by prefix.
* ``/calendar/`` — a parent used to reach the dates through ``/myhub/events/``;
  that page is gone (it was one of three calendars reading the same source, see
  ``apps/myhub/urls.py``) and this is where it went. Exact, not a prefix: the
  month grid and its ``?view=list`` agenda are a parent's business, the rest of
  the section (scheduling, availability, the recording archive, YouTube admin)
  is not.
"""


class ParentAccessMiddleware:
    """Confine a parent account to the small set of pages that concern them.

    Every other role is scoped by *narrowing* what its pages return. A parent is
    different: almost nothing on the platform is theirs to see, so the shape is
    the other way round — an **allow-list** of what they may reach
    (:data:`_PARENT_ALLOWED_PREFIXES`, :data:`_PARENT_ALLOWED_EXACT`) and a
    refusal for everything else. A parent may pay for things, be told things,
    ask questions (contacts restricted separately in
    ``core.scoping.parent_contacts``), follow their own child and look after
    their own account.

    The list is written as path prefixes rather than URL names on purpose: a
    deny-list, or a list of named views, silently opens up every route added
    afterwards, whereas a prefix allow-list fails closed — a new page stays
    private until someone deliberately lists it.

    Runs after :class:`ManagementAccessMiddleware` so admin-only pages are
    already refused for everyone; this narrows the remainder down to a parent's
    own corner of the platform.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Redirect a parent to the hub with a note unless the page is allowed.

        Django staff/superusers are never parents in the permission sense, even
        if their profile says so — ``role_of`` already promotes them.

        An invited parent has to finish registering before there is anything to
        confine them to, and everyone has to be able to log out. Those pages are
        matched by view name (:data:`_ONBOARDING_VIEW_NAMES`, the same list
        :class:`OnboardingMiddleware` exempts) rather than by path: the accounts
        app moved from ``/accounts/`` to ``/community/`` once already, and a path
        list that silently stops matching locks parents out of their own
        registration.

        A page is never redirected to itself — that is an infinite loop, not a
        refusal, and it takes the account down completely rather than denying
        one page.
        """
        from django.contrib import messages
        from django.shortcuts import redirect

        from core.roles import role_of

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if role_of(request) != 'parent':
            return None

        path = request.path
        if path in _PARENT_ALLOWED_EXACT or path.startswith(_PARENT_ALLOWED_PREFIXES):
            return None

        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name:
            view_name = (f'{match.namespace}:{match.url_name}' if match.namespace
                         else match.url_name)
            if view_name in _ONBOARDING_VIEW_NAMES:
                return None

        landing = reverse('myhub:index')
        if path == landing:
            return None

        messages.info(
            request,
            'That part of the hub is not available to parent accounts. '
            'You can follow your child’s progress and results from here.')
        return redirect(landing)


_COMMUNITY_ALLOWED_PREFIXES = (
    '/static/', '/media/', '/favicon.ico', '/accounts/', '/captcha/', '/sw.js', '/__debug__/',
    '/social/', '/communication/feed/', '/communication/community/mentions/',
    '/communication/notifications/', '/communication/alerts/', '/communication/push/',
    '/communication/email-preferences/', '/communication/support/',
    '/communication/link-preview/',
    '/communication/message-metanoia/', '/communication/chat/', '/api/communication/',
    '/shop/', '/finance/',
    '/community/settings/', '/community/people/', '/community/preferences/',
    '/community/become-student/',
)
"""Path prefixes a community member may reach (:class:`CommunityAccessMiddleware`).

Infrastructure (assets, auth, the captcha); the community and its actions;
notifications, the bell and desktop alerts, e-mail preferences and the help
centre; link preview cards (permission-checked per viewer); Message Metanoia —
a chat room, so the chat pages and the chat API; the shop and the money; and
their own account, other people's name cards and becoming a student.
"""

_COMMUNITY_ALLOWED_EXACT = frozenset({'/', '/community/profile/', '/join/'})
"""Exact paths a community member may reach outside the allowed prefixes."""

_UPGRADE_ALLOWED_PREFIXES = ('/static/', '/media/', '/favicon.ico', '/accounts/',
                             '/community/become-student/')
"""What a member who has confirmed "become a student" may reach before choosing
a new password; everything else sends them to that page."""


class CommunityAccessMiddleware:
    """Confine a community member to the community, Message Metanoia and the shop.

    A Metanoia Community member joined at ``/join/`` with no verification at all,
    so the account is confined to the community (the feed; posting, liking,
    sharing, tagging, reporting), being told things (notifications and their
    e-mail preferences), Message Metanoia (their private thread with the Metanoia
    team), the shop (buying, their orders and the invoices that follow),
    becoming a student (the e-mail check and new password that convert them),
    and their own account (profile, settings, sign-out, the legal pages).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Redirect a community member to their community dashboard unless allowed.

        Logout is always allowed. A member whose e-mail has been confirmed for
        "become a student" is sent to the new-password page until it is set.
        ``/shop/manage/`` is staff's: the decorators refuse it anyway, but a
        member should land back in the community, not on an error page. The
        dashboard is never redirected to itself, and ``myhub:index`` forwards
        silently, because their home is the community.
        """
        from django.contrib import messages
        from django.shortcuts import redirect

        from core.roles import role_of

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or role_of(request) != 'community':
            return None
        path = request.path
        match = getattr(request, 'resolver_match', None)
        view_name = ''
        if match is not None and match.url_name:
            view_name = f'{match.namespace}:{match.url_name}' if match.namespace else match.url_name
        if view_name == 'myhub:logout':
            return None

        person = getattr(user, 'profile', None)
        if person is not None and person.upgrade_verified_at:
            if path.startswith(_UPGRADE_ALLOWED_PREFIXES):
                return None
            return redirect('accounts:become-student-password')

        if path in _COMMUNITY_ALLOWED_EXACT or path.startswith(_COMMUNITY_ALLOWED_PREFIXES):
            if not path.startswith('/shop/manage/'):
                return None

        landing = reverse('pages:dashboard')
        if path == landing:
            return None
        if path == reverse('myhub:index'):
            return redirect(landing)
        messages.info(
            request,
            'That part of the hub is for registered students. Become a student to '
            'unlock modules, live classes and assessments.')
        return redirect(landing)


_MFA_EXEMPT_PREFIXES = ('/static/', '/media/', '/accounts/', '/api/',
                        '/lrs/cmi5/', '/finance/payfast/', '/reports/verify/')
"""Prefixes :class:`MfaRequiredMiddleware` never gates. ``/accounts/`` holds the
allauth MFA pages themselves; gating them would make the redirect loop."""


class MfaRequiredMiddleware:
    """Send privileged users to MFA setup until they have a second factor.

    Off by default. With ``MFA_REQUIRED_FOR_STAFF=true``, any account that can
    change other people's data (staff, superusers, and educators/admins by
    profile type) must set up a second factor before it can reach anything else.
    Everyone else is unaffected — MFA stays opt-in for learners and parents.

    Placed **after** the onboarding gate so a brand-new staff member still
    completes registration first. Does nothing if ``allauth.mfa`` is not
    installed or the TOTP activation URL is not wired.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self._maybe_redirect(request)
        return response if response is not None else self.get_response(request)

    def _maybe_redirect(self, request):
        """Return a redirect to TOTP activation for an unprotected privileged user."""
        from django.conf import settings
        from django.contrib import messages
        from django.shortcuts import redirect
        from django.urls import NoReverseMatch, reverse

        if not getattr(settings, 'MFA_REQUIRED_FOR_STAFF', False):
            return None
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if request.path_info.startswith(_MFA_EXEMPT_PREFIXES):
            return None

        person = getattr(user, 'profile', None)
        privileged = (user.is_staff or user.is_superuser
                      or (person and person.user_type in ('admin', 'staff', 'educator')))
        if not privileged:
            return None

        try:
            from allauth.mfa.models import Authenticator
        except ImportError:
            return None
        if Authenticator.objects.filter(user=user).exists():
            return None

        try:
            target = reverse('mfa_activate_totp')
        except NoReverseMatch:
            return None
        messages.warning(
            request,
            'Your role can change other people’s data, so this account needs '
            'two-factor authentication before you can carry on.')
        return redirect(target)


_CONTENT_PREFIXES = (
    '/learning/',
    '/assessments/',
    '/communication/chat/',
    '/communication/announcements/',
    '/reports/',
    '/library/',
)
"""The material :class:`ModuleAccessMiddleware` closes when a trial lapses unpaid.

Lessons, modules and the player; quizzes, tests and assignments; class and
direct chat; announcements; marks and progress; the library. **Not**
``/social/``: that is the Metanoia Community, open to everyone — including a
student whose free week has run out, for whom it is the way back in.
"""

_ALWAYS_OPEN_PREFIXES = (
    '/static/', '/media/', '/accounts/', '/api/', '/admin/',
    '/finance/',
    '/shop/',
    '/learning/modules/',
    '/community/profile/', '/community/settings/', '/community/register/',
    '/myhub/', '/social/privacy-and-terms/', '/social/terms-and-conditions/', '/social/terms-of-service/', '/social/refund-policy/', '/social/shipping-policy/',
)
"""Reachable with everything locked: their own account, the money (invoices, the
pay page, PayFast returns, the shop) and the help/legal pages everybody needs.

``/learning/modules/`` is listed because the module pages *are* the lock screen:
My Modules, the unlock/pay flow, and the module feed, where a student sees the
whole schedule with unpaid material marked locked. Bouncing them to the invoice
list would hide the very thing they are being asked to buy. It is safe to leave
open because the feed gates every row itself (``apps.learning.access.Gate``).
"""


class ModuleAccessMiddleware:
    """Close the content when a student's trial has run out and nothing is paid.

    A student who registers on the trial gets the whole platform for seven
    days. When those days are up and nothing has been paid, the account does
    **not** stop working: they sign in, see their dashboard, profile, invoices
    and the pay page, and can settle up. What closes is the material — lessons,
    assessments, the library, chats and documents — until either the invoice is
    paid or a staff member grants access against a recorded payment (see
    ``apps.finance.access.grant_access``).

    Scoped by path prefix (:data:`_CONTENT_PREFIXES`). Everything not listed
    stays open, because locking a student out of paying is self-defeating.

    Runs after OnboardingMiddleware (so the student has finished registering)
    and only ever looks at students: staff, educators and parents are not on
    per-module billing and are never gated here.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        """Redirect a lapsed student from locked content to their invoices.

        A student with no enrolments at all has bought nothing yet, so there is
        nothing to enforce (the guide course and General material stay
        reachable). The programme's General module (onboarding, timetable,
        blueprints) is free and stays open whatever is owed — see
        ``apps.learning.general``.
        """
        from django.contrib import messages
        from django.shortcuts import redirect

        from core.roles import role_of

        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if role_of(request) != 'student':
            return None

        path = request.path
        if path.startswith(_ALWAYS_OPEN_PREFIXES) or not path.startswith(_CONTENT_PREFIXES):
            return None

        person = getattr(user, 'profile', None)
        if person is None:
            return None

        from apps.learning.models import ModuleEnrolment

        enrolments = list(ModuleEnrolment.objects.filter(person=person))
        if not enrolments or any(e.is_unlocked for e in enrolments):
            return None
        from apps.learning.general import is_general_content_path
        if is_general_content_path(path):
            return None

        messages.warning(
            request,
            'Your free week has ended, so lessons, assessments and chats are closed for now. '
            'Settle your invoice to reopen them straight away — or speak to us and a staff '
            'member can unlock your modules once your payment is recorded.')
        return redirect('finance:invoices')


class SocialAppGuardMiddleware:
    """Turn "this social provider was never set up" into an honest refusal.

    ``allauth.socialaccount`` registers a full set of login routes for every
    provider in ``INSTALLED_APPS``, whether or not anybody has created the
    matching ``SocialApp``. Hitting one of those routes unconfigured raises
    ``SocialApp.DoesNotExist`` — a **500 on a public login URL**, which is both
    the worst place to have one and something a crawler will find on its own.

    The buttons are already hidden (see ``accounts/templatetags/social_tags.py``),
    so reaching one of these means a deep link, an old bookmark or a bot. Any of
    those deserves the login page and a sentence, not a stack trace.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Turn ``SocialApp.DoesNotExist`` into a login redirect and a USER-2003 note."""
        from django.conf import settings
        from django.contrib import messages
        from django.shortcuts import redirect

        try:
            from allauth.socialaccount.models import SocialApp
        except Exception:  # pragma: no cover - socialaccount optional
            return None
        if not isinstance(exception, SocialApp.DoesNotExist):
            return None

        from core.errors import note
        note('USER-2003', request, kind='social-provider-unconfigured',
             path=request.path)
        messages.info(
            request,
            'That sign-in option is not available on this site. '
            'Please sign in with your e-mail address and password.')
        return redirect(settings.LOGIN_URL)


class UserPreferenceMiddleware:
    """Make the saved *Preferences* actually govern the session.

    ``UserSettings.theme`` and ``UserSettings.language`` were stored but never
    applied: the theme buttons flipped a class that nothing persisted, and the
    language picker wrote a row no request ever read. So signing in always
    landed you on light-mode English no matter what you had saved.

    This closes both loops, once per request, before the view runs:

    * **Language** — activate the saved language so ``{% translate %}``, form
      labels and :func:`django.utils.formats` all speak it. An explicit
      ``?lang=`` (the switcher) wins for this request and is written back to the
      user's row, so switching language on one device follows them to the next.
    * **Theme** — resolve to ``light`` / ``dark`` / ``auto`` and hang it on
      ``request.ui_theme``; the context processor puts it on ``<html>`` so the
      first paint is already the right colour, with no flash of white.
    * **Time zone** — activate the saved zone so every ``{{ when }}`` in a
      template, every reminder rendered inside a request, and the calendar itself
      render in the reader's own clock. Stored data stays UTC; only the display
      moves. This is what makes a live session at 14:00 SAST read as 13:00 to a
      candidate in Lagos without anyone converting anything by hand.

    Anonymous visitors fall back to the ``django_language`` cookie and the
    ``ui_theme`` cookie, which is what the navbar toggle writes before sign-in.
    """

    THEME_COOKIE = 'ui_theme'
    TZ_COOKIE = 'ui_tz'
    VALID_THEMES = {'light', 'dark', 'auto'}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """Activate language, theme and time zone, then sync the cookies.

        ``timezone.activate`` is thread-local and the thread is reused, so the
        "no zone" branch must *deactivate* — otherwise an anonymous request on
        the same worker would silently inherit the last signed-in user's clock.
        The cookies are kept in step on the response so the *next* first paint
        (including the login page, before we know who they are) is already right.
        """
        from django.utils import timezone as tz
        from django.utils import translation

        language, theme, zone = self._resolve(request)

        translation.activate(language)
        request.LANGUAGE_CODE = language
        request.ui_theme = theme
        request.ui_timezone = zone or settings.TIME_ZONE
        if zone:
            tz.activate(zone)
        else:
            tz.deactivate()

        response = self.get_response(request)
        response.setdefault('Content-Language', language)
        if request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) != language:
            response.set_cookie(settings.LANGUAGE_COOKIE_NAME, language,
                                max_age=365 * 24 * 3600, samesite='Lax')
        if request.COOKIES.get(self.THEME_COOKIE) != theme:
            response.set_cookie(self.THEME_COOKIE, theme,
                                max_age=365 * 24 * 3600, samesite='Lax')
        if zone and request.COOKIES.get(self.TZ_COOKIE) != zone:
            response.set_cookie(self.TZ_COOKIE, zone,
                                max_age=365 * 24 * 3600, samesite='Lax')
        return response

    def _resolve(self, request):
        """Return ``(language, theme, zone)`` for this request.

        Cookies first, then the signed-in user's :class:`UserSettings` row; an
        explicit ``?lang=`` on this request beats both and is saved to the row.
        Unsupported values fall back to the platform defaults.
        """
        supported = {code for code, _label in settings.LANGUAGES}
        language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) or settings.LANGUAGE_CODE
        theme = request.COOKIES.get(self.THEME_COOKIE) or 'auto'
        zone = request.COOKIES.get(self.TZ_COOKIE) or ''

        user = getattr(request, 'user', None)
        row = None
        if user is not None and user.is_authenticated:
            row = self._settings_for(user)
            if row is not None:
                language, theme = row.language, row.theme
                zone = row.timezone or ''

        wanted = (request.GET.get('lang') or '').strip()
        if wanted in supported and wanted != language:
            language = wanted
            if row is not None:
                row.language = wanted
                row.save(update_fields=['language', 'updated_at'])

        if language not in supported:
            language = settings.LANGUAGE_CODE.split('-')[0]
        if theme not in self.VALID_THEMES:
            theme = 'auto'
        return language, theme, self._valid_zone(zone)

    @staticmethod
    def _valid_zone(name):
        """``name`` if it is a real IANA zone, else '' (meaning: platform default).

        A stale or hand-edited cookie must not be able to raise out of
        middleware, which runs before any error page could be rendered.
        """
        if not name:
            return ''
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(str(name))
        except Exception:
            return ''
        return str(name)

    @staticmethod
    def _settings_for(user):
        """The user's settings row, or ``None`` if the table is not there yet
        (a fresh checkout before ``migrate`` still has to serve pages)."""
        from apps.accounts.models import UserSettings
        try:
            return UserSettings.for_user(user)
        except Exception:                                # pragma: no cover
            return None
