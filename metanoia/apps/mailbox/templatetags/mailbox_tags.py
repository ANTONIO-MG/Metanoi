"""Template filters for the Metanoia Mail pages.

``_COLOURS`` are Outlook's persona colours; :func:`mail_colour` picks one per
address so a sender keeps theirs.
"""

import hashlib

from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe

register = template.Library()

_COLOURS = ['#0F6CBD', '#C239B3', '#038387', '#CA5010', '#8764B8', '#498205', '#DA3B01', '#005B70',
            '#986F0B', '#4F6BED', '#B146C2', '#00727A']


@register.filter
def mail_colour(address):
    """The avatar colour for ``address``, stable per address."""
    digest = hashlib.md5((address or '').lower().encode()).digest()
    return _COLOURS[digest[0] % len(_COLOURS)]


@register.filter
def mail_when(value):
    """Outlook's list timestamp: time today, weekday this week, date before that."""
    if not value:
        return ''
    local = timezone.localtime(value)
    today = timezone.localtime().date()
    if local.date() == today:
        return local.strftime('%H:%M')
    if (today - local.date()).days < 7:
        return local.strftime('%a %H:%M')
    if local.year == today.year:
        return local.strftime('%a %d/%m')
    return local.strftime('%d/%m/%Y')


@register.filter
def mail_clean(html):
    """``html`` cleaned to the mail allow-list (:func:`~apps.mailbox.parse.clean_compose_html`) and marked safe."""
    from apps.mailbox.parse import clean_compose_html
    return mark_safe(clean_compose_html(html or ''))


@register.filter
def words(value):
    """``value`` split on whitespace."""
    return (value or '').split()


@register.filter
def get_form_field(form, name):
    """The bound field ``name`` of ``form``."""
    return form[name]


@register.filter
def mb_widget(bound_field):
    """Render a form field with Bootstrap's class for its widget type."""
    widget = bound_field.field.widget
    kind = getattr(widget, 'input_type', '')
    css = 'form-select' if widget.__class__.__name__.endswith('Select') else (
        'form-check-input' if kind == 'checkbox' else 'form-control')
    if bound_field.errors:
        css += ' is-invalid'
    return bound_field.as_widget(attrs={'class': css})
