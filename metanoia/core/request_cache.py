"""Memoise a value for the lifetime of one request.

Context processors run for every template rendered in a response, so badge
queries were recomputed per template. The result is stashed on the request
object: no cross-request leakage, no cache backend, no invalidation.

    stats = cached_per_request(request, 'comm_stats', build_comm_stats)

When there is no request (a management command, a shell), it simply calls
through — correctness first, caching second.
"""

_PREFIX = '_metanoia_cache__'


def cached_per_request(request, key, builder):
    """Return ``builder()``, computing it at most once per request.

    ``builder`` is only called on a miss, so an expensive query is skipped
    entirely on the second and later reads.

    A sentinel rather than a ``None`` check decides a miss: ``None``, ``0`` and
    ``{}`` are all legitimate cached values and must not be recomputed on every
    subsequent read.
    """
    if request is None:
        return builder()

    attr = _PREFIX + key
    missing = object()
    value = getattr(request, attr, missing)
    if value is missing:
        value = builder()
        try:
            setattr(request, attr, value)
        except Exception:  # pragma: no cover - exotic request objects
            pass
    return value
