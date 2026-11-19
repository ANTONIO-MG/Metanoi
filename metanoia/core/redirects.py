"""Safe "return to" redirects.

Several views send the user back to a URL taken from the request — a ``next``
field, a ``?next=`` query parameter or the ``Referer`` header. Redirecting to
that value as-is is an open redirect: a link to ``/login/?next=https://evil``
would forward a freshly signed-in user to a look-alike site. Every such
redirect goes through :func:`safe_target`, which only accepts URLs on this
host (and, on an https request, only https).
"""

from django.shortcuts import redirect, resolve_url
from django.utils.http import url_has_allowed_host_and_scheme


def is_safe(request, url):
    """True when ``url`` points back at this site."""
    return bool(url) and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure())


def safe_target(request, url, fallback):
    """``url`` if it is on this site, else ``fallback`` (resolved like
    ``redirect()``: a URL name, model or path)."""
    if isinstance(url, str):
        url = url.strip()
    return url if is_safe(request, url) else resolve_url(fallback)


def redirect_back(request, fallback):
    """Redirect to the ``Referer`` when it is on this site, else ``fallback``."""
    return redirect(safe_target(request, request.META.get('HTTP_REFERER') or '', fallback))
