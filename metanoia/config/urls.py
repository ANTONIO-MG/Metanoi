"""Root URL configuration.

Routes:

* ``/``                      → redirect to the MyHub dashboard
* ``/myhub/``                → MyHub dashboard (HTML pages + CRUD)
* ``/communication/``        → chat, meetings (Teams), notifications, announcements (HTML)
* ``/calendar/``             → the Metanoia calendar, live sessions & the recording archive
* ``/accounts/``             → django-allauth (e-mail confirmation, password reset, …)
* ``/admin/``                → Django admin, in the hub's own layout
* ``/api/token/``            → SimpleJWT token obtain / refresh (hardened: apps.accounts.api_auth)
* ``/api/myhub/``            → REST API for the MyHub domain models
* ``/api/accounts/``         → REST API for users / organisations / courses / subjects
* ``/api/communication/``    → REST API for chat, meetings, notifications, announcements

Also mounted: ``/favicon.ico`` (browsers, bookmark tools and feed readers ask
for it whatever the page's ``<link rel=icon>`` says, so it is answered rather
than logged as a 404), ``/sw.js`` (the desktop-alerts service worker must sit at
the root to cover the site), ``/community/`` (accounts community pages — profile,
settings, groups — under the ``accounts`` namespace, mounted there so they do
not clash with allauth's ``/accounts/``), ``/join/`` (the public "Join the
community" page the website links to), ``/social/`` (the social theme pages
under the ``pages`` namespace, so themed navbar/sidebar links resolve), the shop,
finance, learning, assessments, reports, analytics, revision and mail apps,
``/staff/`` (the staff desk), ``/diagnostics/`` (the error dictionary and log;
every view 404s for anyone who is not admin/staff) and ``/captcha/`` (the math
CAPTCHA images of django-simple-captcha). The Django admin is branded here.

Ordering and security rules (URL resolution is first-match):

* **Admin login/logout overrides must precede** ``admin.site.urls``, so the admin
  never shows its own login form (the hub session is the admin session) and
  "leaving" it returns to the dashboard rather than signing out onto a dead
  account page. See :mod:`config.auth_urls`.
* **JWT is for non-browser clients only**, through the hardened views in
  :mod:`apps.accounts.api_auth` (axes lockout, strict throttle, refusal for 2FA
  or unverified accounts). The browser chat uses the session cookie.
* **dj-rest-auth's** ``/api/auth/`` **and** ``/api/auth/registration/`` **are
  deliberately not mounted.** Nothing in the product calls them, and each was a
  second, weaker door beside the real one: its login created a full session
  without the second factor or the e-mail-verification check, and its
  registration skipped the CAPTCHA, the Terms consent and registration billing
  that the sign-up page enforces.
* **One login, one sign-up, one password-reset page** — the hub's own, under
  ``/myhub/``. allauth's own HTML entry pages for all three are intercepted
  before the ``allauth.urls`` include (so they win) and redirected to the hub's
  pages. allauth stays mounted because it is the engine behind e-mail
  confirmation, the e-mailed reset link and Google sign-in. A completed reset
  ends at the login page rather than allauth's dead-end "password changed" page;
  that route keeps allauth's URL name so its view's ``success_url`` still
  reverses. allauth's real HTML confirm-email view owns the
  ``account_confirm_email`` name, so verification links open the HTML page.

Media: with ``SERVE_MEDIA_FROM_DJANGO`` (the default) uploaded files are served
by the app itself — in development and production, ``DEBUG`` on or off —
through one access-controlled view, :func:`core.media_access.protected_serve`.
Some uploads are public (institution logos on the sign-up page), most need a
login, and some (payment proofs, receipts, paid downloads, exam answers, chat
files) are private to staff and the people the owning record names.
:mod:`core.media_headers` still stamps every response so an uploaded
``.svg``/``.html`` cannot execute in this origin. WhiteNoise cannot stand in for
this (it indexes its files once at start-up) and ``static()`` would serve
everything to everyone.

In ``DEBUG`` mode the static directory is also served directly.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path
from apps.accounts import api_auth
from apps.communication import views as communication_views
from config import auth_urls


admin.site.site_header = 'Metanoia Group Admin'
admin.site.site_title = 'Metanoia Group Admin'
admin.site.index_title = 'Administration'


def root_redirect(request):
    """Site root: signed-in users go to their dashboard; everyone else to the
    sign-in page, which is the platform's front door (the public landing page
    was retired on 2026-10-17 — the website is the shop window now)."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return redirect('myhub:index')
    return redirect('myhub:page-login')


def favicon(request):
    """``/favicon.ico`` → the static icon, resolved per request (not at import,
    where a missing manifest entry would take every URL down with it). Before
    ``collectstatic`` has run, the plain ``STATIC_URL`` path is used."""
    from django.http import HttpResponsePermanentRedirect
    from django.templatetags.static import static
    try:
        return HttpResponsePermanentRedirect(static('images/favicon.ico'))
    except ValueError:
        return HttpResponsePermanentRedirect(f'{settings.STATIC_URL}images/favicon.ico')


from apps.accounts.community_views import join as community_join  # noqa: E402

urlpatterns = [
    path('', root_redirect, name='home'),

    path('favicon.ico', favicon),

    path('myhub/', include('apps.myhub.urls')),

    path('communication/', include('apps.communication.urls')),
    path('mail/', include('apps.mailbox.urls')),
    path('sw.js', communication_views.service_worker, name='service-worker'),
    path('calendar/', include('apps.livesessions.urls')),

    path('community/', include('apps.accounts.urls')),
    path('join/', community_join, name='community-join'),

    path('social/', include('apps.myhub.social_urls')),

    path('shop/', include('apps.shop.urls')),
    path('finance/', include('apps.finance.urls')),

    path('learning/', include('apps.learning.urls')),
    path('assessments/', include('apps.assessments.urls')),
    path('reports/', include('apps.reports.urls')),
    path('analytics/', include('apps.analytics.urls')),

    path('diagnostics/', include('apps.diagnostics.urls')),
    path('staff/', include('apps.staffdesk.urls')),
    path('revision/', include('apps.revision.urls')),

    path('captcha/', include('captcha.urls')),

    path('admin/login/', auth_urls.admin_login),
    path('admin/logout/', auth_urls.admin_logout),
    path('admin/', admin.site.urls),

    path('api/token/', api_auth.SecureTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', api_auth.SecureTokenRefreshView.as_view(), name='token_refresh'),

    path('accounts/login/', auth_urls.to_login),
    path('accounts/signup/', auth_urls.to_register),
    path('accounts/password/reset/', auth_urls.to_forgot_password),
    path('accounts/password/reset/key/done/', auth_urls.to_login_after_reset,
         name='account_reset_password_from_key_done'),

    path('accounts/', include('allauth.urls')),

    path('api/myhub/', include('apps.myhub.api_urls')),
    path('api/accounts/', include('apps.accounts.api_urls')),
    path('api/communication/', include('apps.communication.api_urls')),

]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

if getattr(settings, 'SERVE_MEDIA_FROM_DJANGO', True):
    import re as _re

    from django.urls import re_path as _re_path

    from core.media_access import protected_serve as _protected_serve

    _media_prefix = (settings.MEDIA_URL or '/media/').lstrip('/')
    if _media_prefix and '://' not in settings.MEDIA_URL:
        urlpatterns += [
            _re_path(r'^' + _re.escape(_media_prefix) + r'(?P<path>.*)$', _protected_serve,
                     name='protected-media'),
        ]
