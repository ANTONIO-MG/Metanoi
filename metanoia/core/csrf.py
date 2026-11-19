"""What a visitor sees when a form's CSRF token no longer matches.

The common case on this site is harmless: a sign-in (or sign-up) page left
open in one tab while the person signed in from another. Signing in rotates the
CSRF token, so the older tab's form is refused. Django's stock answer is a bare
403 — a dead end for the person, and a WARNING in the error log every time.

For the auth pages this sends people somewhere useful instead:

* already signed in → on to where they were going (they are in; nothing to do);
* not signed in     → back to a freshly rendered form, with a one-line note.

A redirect is logged by Django at INFO, so these stop filling the error log.
Every other refused POST keeps Django's standard 403 — and its warning.

:data:`AUTH_FORMS` lists the URL names of forms where a stale token is ordinary,
not suspicious.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.views.csrf import csrf_failure as django_csrf_failure

from core.redirects import safe_target

AUTH_FORMS = {'myhub:page-login', 'myhub:page-register'}


def csrf_failure(request, reason=''):
    """``CSRF_FAILURE_VIEW``: redirect on the auth forms, Django's 403 everywhere else.

    Signed in → the safe ``next`` URL (or ``/``); otherwise back to the same page
    with a note (skipped when the request has no message storage).
    """
    try:
        match = resolve(request.path_info)
    except Resolver404:
        match = None
    if match is None or match.view_name not in AUTH_FORMS:
        return django_csrf_failure(request, reason=reason)

    user = getattr(request, 'user', None)
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if user is not None and user.is_authenticated:
        return redirect(safe_target(request, next_url, '/'))

    try:
        messages.info(request, 'That page had been open a while, so it expired for your security. '
                               'Please try again.')
    except Exception:           # pragma: no cover
        pass
    return redirect(request.get_full_path())
