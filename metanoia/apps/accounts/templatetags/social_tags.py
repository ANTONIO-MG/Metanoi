"""Which social logins are actually usable right now.

An installed provider without configured credentials makes its login route
fail (Facebook 500s), so templates render only the buttons this returns.
"""

from django import template

register = template.Library()

KNOWN_PROVIDERS = {
    'google': ('Google', 'images/icon/google-icon.svg'),
    'apple': ('Apple', 'images/icon/apple-logo.svg'),
    'facebook': ('Facebook', 'images/icon/facebook-icon.svg'),
}
"""Provider id → (label, brand icon file under ``static/images/icon/``).
Listed providers are shown first, in this order."""

_CACHE_ATTR = '_metanoia_social_providers'


@register.simple_tag(takes_context=True)
def social_providers(context):
    """``[{'id', 'label', 'icon'}, …]`` for every usable provider, cached on the
    request. ``[]`` if socialaccount is not installed.

    A provider counts as usable when either

    1. it has credentials in settings (``SOCIALACCOUNT_PROVIDERS[x]['APP']``
       with a non-blank ``client_id``). This is how Google is wired here — env
       vars, no database row — so checking only the ``SocialApp`` table would
       hide a working button; a blank ``client_id`` means the env var was never
       filled in; or
    2. a ``SocialApp`` is registered in the admin **and attached to this
       Site**. One not attached to the Site cannot authenticate against it, so
       the row existing is not enough.

    Known providers come first, in :data:`KNOWN_PROVIDERS` order (Google, then
    Apple). Apple is shown greyed out (``pending``) until the ``APPLE_*``
    settings are filled in.
    """
    request = context.get('request')
    cached = getattr(request, _CACHE_ATTR, None) if request is not None else None
    if cached is not None:
        return cached

    found = {}
    try:
        from django.conf import settings

        for provider_id, conf in (getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {}) or {}).items():
            app = (conf or {}).get('APP') or {}
            if app.get('client_id'):
                found[provider_id] = True

        from allauth.socialaccount.models import SocialApp
        site_id = getattr(settings, 'SITE_ID', None)
        rows = SocialApp.objects.all()
        if site_id:
            rows = rows.filter(sites__id=site_id)
        for provider_id in rows.values_list('provider', flat=True).distinct():
            found[provider_id] = True
    except Exception:  # pragma: no cover - socialaccount optional / un-migrated
        found = {}

    providers = []
    order = list(KNOWN_PROVIDERS)
    for provider_id in sorted(found, key=lambda p: (order.index(p) if p in order else len(order), p)):
        label, icon = KNOWN_PROVIDERS.get(provider_id, (provider_id.title(), ''))
        providers.append({'id': provider_id, 'label': label, 'icon': icon})

    if providers and 'apple' not in found:
        from django.conf import settings
        if 'allauth.socialaccount.providers.apple' in settings.INSTALLED_APPS:
            label, icon = KNOWN_PROVIDERS['apple']
            providers.append({'id': 'apple', 'label': label, 'icon': icon, 'pending': True})

    if request is not None:
        setattr(request, _CACHE_ATTR, providers)
    return providers
