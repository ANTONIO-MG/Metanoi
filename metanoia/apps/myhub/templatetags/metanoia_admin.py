"""Template tags for the Metanoia-styled Django admin (templates/layouts/base.html).

``metanoia_admin_menu`` is the admin's navigation: Django's own
``admin.site.get_app_list`` (the list the admin index uses, so permissions are
Django's), with a Bootstrap icon per app and the app/model the current page
belongs to marked active. Returns ``{'apps', 'current', 'opts', 'record'}``: ``current``
(the app this page is in) drives the header's model pills; ``opts`` is the
page's model meta whether the view called it ``opts`` or ``cl.opts``.

``APP_ICONS`` gives one icon per app, in the hub's icon set (bootstrap-icons).
``APP_NAMES`` renames apps whose own name is ambiguous on the admin home:
allauth's "account" is also called "Accounts", beside ours.
"""

from django import template
from django.contrib import admin

register = template.Library()

APP_ICONS = {
    'accounts': 'bi-people', 'account': 'bi-envelope-check', 'socialaccount': 'bi-google',
    'auth': 'bi-shield-lock', 'mfa': 'bi-phone-vibrate', 'axes': 'bi-shield-exclamation',
    'authtoken': 'bi-key', 'token_blacklist': 'bi-slash-circle',
    'ai_assistant': 'bi-stars', 'analytics': 'bi-graph-up', 'assessments': 'bi-ui-checks',
    'scheduler': 'bi-clock-history', 'communication': 'bi-chat-left-text',
    'diagnostics': 'bi-bug', 'finance': 'bi-receipt', 'learning': 'bi-mortarboard',
    'livesessions': 'bi-camera-video', 'myhub': 'bi-grid-1x2', 'reports': 'bi-award',
    'revision': 'bi-journal-check', 'shop': 'bi-bag', 'sites': 'bi-globe2',
    'mailbox': 'bi-envelope-paper',
}

APP_NAMES = {'account': 'Sign-in emails'}


@register.simple_tag(takes_context=True)
def metanoia_admin_menu(context):
    """The admin navigation for the current page (see the module docstring).

    ``record`` is the object the page is about: ``original`` on edit pages,
    ``object`` on delete and history pages.
    """
    request = context.get('request')
    if request is None or not getattr(request, 'user', None) or not request.user.is_active:
        return {'apps': [], 'current': None, 'opts': None, 'record': None}
    path = request.path
    apps = admin.site.get_app_list(request)
    current = None
    for app in apps:
        app['icon'] = APP_ICONS.get(app['app_label'], 'bi-box')
        app['name'] = APP_NAMES.get(app['app_label'], app['name'])
        app['active'] = path.startswith(app.get('app_url') or '\0')
        if app['active']:
            current = app
        for model in app['models']:
            url = model.get('admin_url') or ''
            model['active'] = bool(url) and path.startswith(url)
    opts = context.get('opts') or getattr(context.get('cl'), 'opts', None)
    record = context.get('original') or context.get('object')
    return {'apps': apps, 'current': current, 'opts': opts, 'record': record}


@register.filter
def app_icon(app_label):
    """The Bootstrap icon class for an admin app."""
    return APP_ICONS.get(app_label, 'bi-box')


@register.filter
def app_name(app):
    """An admin app's display name, with the APP_NAMES renames applied."""
    return APP_NAMES.get(app.get('app_label'), app.get('name'))
