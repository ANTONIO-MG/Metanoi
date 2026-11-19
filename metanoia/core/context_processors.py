"""Template context processors.

``branding`` makes the single control document (see :mod:`core.branding` /
``.strings.json``) available in every template, so layouts and pages render the
organisation's identity, standard wording and dynamic-element toggles without
hard-coding anything:

    {{ brand.name }}   {{ brand.logo }}   {{ brand.contact.phone }}
    {{ org.social.facebook }}             {{ brand.welcome.title }}
    {% if dynamic.badges.unread_messages %}…{% endif %}
    {{ site.header_tagline }}

``brand`` and ``org`` are the same dict (two readable names for the
organisation section). Registered in
``TEMPLATES[...]['OPTIONS']['context_processors']`` in settings.
"""

from django.conf import settings

from core.branding import strings


def branding(request):
    """The string catalog and its sections for every template.

    ``strings`` is the whole catalog (dotted access in templates); ``brand`` the
    organisation's identity (logo, name, contact, …) and ``org`` a readable alias
    for it; ``dynamic`` the badge / widget / banner toggles; ``site`` arbitrary
    site-wide values; ``public_site_url`` the public website (Vercel), where the
    logo and "Home" on the sign-in pages go.
    """
    catalog = strings()
    organisation = catalog.get('brand', {})
    return {
        'strings': catalog,
        'brand': organisation,
        'org': organisation,
        'dynamic': catalog.get('dynamic', {}),
        'site': catalog.get('site', {}),
        'public_site_url': getattr(settings, 'PUBLIC_SITE_URL', '') or 'https://metanoiagroup.co.za/',
    }


def ui_chrome(request):
    """Theme + language for the layouts, resolved once per request.

    ``apps.accounts.middleware.UserPreferenceMiddleware`` has already worked out
    which of ``light`` / ``dark`` / ``auto`` applies; this hands it to the
    templates so ``<html data-bs-theme="…">`` is correct in the FIRST byte of the
    response. Doing it in JS after load is what produces the white flash before a
    dark page paints.

    ``ui_theme_attr`` is empty for ``auto`` — Bootstrap's own
    ``prefers-color-scheme`` handling should decide, and a hard-coded attribute
    would override the very system setting the user asked us to follow.
    """
    theme = getattr(request, 'ui_theme', 'auto')
    return {
        'ui_theme': theme,
        'ui_theme_attr': '' if theme == 'auto' else theme,
        'ui_language': getattr(request, 'LANGUAGE_CODE', 'en'),
    }
