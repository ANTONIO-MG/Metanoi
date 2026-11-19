"""Catch what nothing else did.

``process_exception`` sees every exception that escapes a view. Recording it
here means an unhandled 500 still arrives in the log with a code (``SYS-9001``)
and a **reference** the user can quote — which turns "the site broke" into a
row a developer can open.

The middleware never suppresses the exception: Django's own handling still runs,
so DEBUG still shows the technical page and production still returns the 500.

``IGNORED`` names exceptions that are normal control flow rather than faults
(including allauth's redirect-by-exception, ``ImmediateHttpResponse``). Logging
these would bury real errors under 404s and redirects.
"""

import logging

logger = logging.getLogger('apps.diagnostics')

IGNORED = (
    'Http404',
    'PermissionDenied',
    'SuspiciousOperation',
    'DisallowedHost',
    'ImmediateHttpResponse',
)


class ErrorCaptureMiddleware:
    """Record unhandled exceptions, and expose the reference on the request."""

    def __init__(self, get_response):
        """Store the next middleware or view in the chain."""
        self.get_response = get_response

    def __call__(self, request):
        """Pass the request straight through."""
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Report an unhandled exception as ``SYS-9001`` and return ``None``.

        The event reference is set on ``request.error_reference`` so templates and
        error pages can give the user something concrete to quote to support.
        Returning ``None`` lets Django build the response as usual.
        """
        if type(exception).__name__ in IGNORED:
            return None
        try:
            from . import catalog, services
            event = services.report(catalog.UNHANDLED, exc=exception, request=request)
            if event is not None:
                request.error_reference = event.reference
        except Exception:               # pragma: no cover
            logger.exception('diagnostics: failed to record an unhandled exception')
        return None
