"""Context processors: the current user's profile card (avatar, modules,
tests-and-exams completion, readiness) and the terms-update prompt.

Guarded so a missing table or DB error never takes down every page.
"""

from core.counters import cached_counter
from core.request_cache import cached_per_request


def _build_account_stats(request):
    """Build ``account_stats``: the profile card (name, role, avatar) and study tiles.

    The avatar is the person's picture or the matching default. The study tiles
    are cached per user for ``COUNTER_TTL`` seconds — six queries otherwise, on
    every page (see :mod:`core.counters` for what clears them early). Any error
    yields an empty dict rather than a broken page.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'account_stats': {}}

    try:
        profile = getattr(user, 'profile', None)

        name = ''
        title = 'Member'
        avatar_url = ''

        if profile is not None:
            name = f'{profile.first_name} {profile.last_name}'.strip()
            try:
                title = profile.get_user_type_display()
            except Exception:
                title = 'Member'
            avatar_url = profile.avatar_url

        if not name:
            name = user.get_full_name() or user.get_username()

        return {
            'account_stats': dict(
                {'profile': {'name': name, 'title': title, 'avatar_url': avatar_url}},
                **cached_counter(user, 'account', lambda: _study_tiles(user, profile)))
        }
    except Exception:
        return {'account_stats': {}}


def _study_tiles(user, person):
    """The three sidebar numbers: modules, tests-and-exams completion, readiness.

    Its own ``try`` so a schedule that has not been built yet costs the tiles
    their values, not the whole profile card.
    """
    blank = {'module_count': 0, 'completion_pct': 0, 'readiness': None}
    if person is None:
        return blank
    try:
        from apps.learning import progress
        rows = progress.phase_rows(user, person)
        return {
            'module_count': progress.module_count(person),
            'completion_pct': progress.overall_completion(user, person, rows=rows),
            'readiness': progress.readiness(user, person, rows=rows),
        }
    except Exception:
        return blank


def account_stats(request):
    """Cached for the life of the request — these are nav badges, and this
    processor runs once per template rendered in a response, not once per
    response."""
    return cached_per_request(request, 'account_stats', lambda: _build_account_stats(request))


def _terms_update(request):
    """Build ``terms_update_due`` / ``current_terms_version`` for this request.

    Never due on the terms page itself — the reader needs to be able to read it.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {'terms_update_due': False}
    try:
        from .forms import TERMS_VERSION
        profile = getattr(user, 'profile', None)
        due = profile is not None and profile.terms_version != TERMS_VERSION
        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name == 'privacy-and-terms':
            due = False
        return {'terms_update_due': due, 'current_terms_version': TERMS_VERSION}
    except Exception:
        return {'terms_update_due': False}


def terms_update(request):
    """One-time "we've updated our Terms" prompt, due when the person has not
    accepted the current TERMS_VERSION (shown once per version)."""
    return cached_per_request(request, 'terms_update', lambda: _terms_update(request))
