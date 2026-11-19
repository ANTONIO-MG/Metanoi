"""Response headers that stop an uploaded file running as script in our origin.

Uploads are served from ``MEDIA_URL`` on the *same host* as the application, so
the browser treats anything served there as same-origin. An uploaded ``.svg`` —
a legitimate image format that may also contain ``<script>`` — or an ``.html``
attachment therefore executes with access to the session cookie and can act as
the signed-in user. Extension validators (see :mod:`core.validators`) alone do
not close this, because ``.svg`` has to remain allowed for logos and avatars.

Two headers do close it:

``X-Content-Type-Options: nosniff``
    Stops the browser second-guessing a declared content type — a ``.png`` whose
    bytes are really HTML stays a broken image instead of becoming a page.

``Content-Disposition: attachment``
    Applied to the extensions in
    :data:`core.validators.NEVER_INLINE_EXTENSIONS`, so those download rather
    than render. An SVG logo still displays through an ``<img>`` tag, which is
    the only way the product uses one; what stops working is *navigating* to it
    directly, which is exactly the attack.

Framing
-------
The feed lightbox and chat media viewer show PDFs in an ``<iframe>``, so framing
rules follow the same split as the disposition:

* An extension in ``NEVER_INLINE_EXTENSIONS`` is the executes-in-our-origin risk
  (``.svg``, ``.html``, ``.js``…). It keeps ``DENY`` and the sandbox CSP, on top
  of already being forced to download — it is never rendered by anything.
* Everything else (PDFs, images, video, office documents) gets
  ``SAMEORIGIN``: our own preview can frame it, every other site still cannot,
  which is the clickjacking risk the header exists for. These carry no sandbox
  CSP, because ``sandbox`` also disables the browser's built-in PDF viewer and a
  PDF that renders nothing is not a preview. None of these formats can reach our
  DOM or our cookies, and ``nosniff`` still stops one being re-interpreted as
  something that could.

In production media is normally served by nginx / a CDN, which should carry the
same rules — this middleware makes the guarantee hold wherever Django serves the
file itself (the dev server, and any storage backend that streams through a
view).
"""

from django.conf import settings

from core.validators import must_download


class MediaSecurityHeadersMiddleware:
    """Add anti-sniffing / anti-inline headers to responses under ``MEDIA_URL``.

    For a :func:`~core.validators.must_download` path the response gets a sandbox
    CSP and ``X-Frame-Options: DENY`` (nothing on it may fetch and nothing may
    frame it), and ``Content-Disposition`` is *overwritten* with ``attachment``.
    Overwriting rather than ``setdefault`` matters: Django's static serve view
    (and some storage backends) already set ``Content-Disposition: inline``, and
    deferring to it left exactly the SVG-executes-in-our-origin case open,
    because the header was present so it looked handled. Every other media file
    is previewable and gets ``SAMEORIGIN`` (see "Framing" in the module
    docstring).
    """

    def __init__(self, get_response):
        """Store the next handler and the media URL prefixes to guard."""
        self.get_response = get_response
        self.media_url = getattr(settings, 'MEDIA_URL', '/media/') or '/media/'
        self.files_url = getattr(settings, 'FILES_URL', self.media_url) or self.media_url

    def __call__(self, request):
        """Stamp the security headers on responses for paths under the media prefixes."""
        response = self.get_response(request)
        path = request.path
        if not (path.startswith(self.media_url) or path.startswith(self.files_url)):
            return response

        response['X-Content-Type-Options'] = 'nosniff'

        if must_download(path):
            response.setdefault('Content-Security-Policy', "sandbox; default-src 'none'")
            response['X-Frame-Options'] = 'DENY'
            filename = path.rsplit('/', 1)[-1].replace('"', '')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
        else:
            response['X-Frame-Options'] = 'SAMEORIGIN'
        return response
