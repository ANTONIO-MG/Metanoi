"""Who may open the staff desk.

Every page here is an admin / staff tool. The educator dashboard is the one
exception and uses :data:`teaching_required` instead. The developer tools
(background jobs, audit log, backups) use :data:`developer_required`: a 404 for
everyone who is not a developer, administrators included. It is re-exported
here from :mod:`apps.accounts.developers` so desk views import every guard from
one place.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

from apps.accounts.developers import developer_required  # noqa: F401  (re-exported)
from core.errors import note
from core.roles import role_of_user


def staff_required(view):
    """Admin and staff only; everyone else goes back to their dashboard."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        """Log ``DESK-2001`` and redirect non-staff; otherwise call the view."""
        if role_of_user(request.user) not in ('admin', 'staff'):
            note('DESK-2001', request, view=view.__name__)
            messages.error(request, "That page is for the admin team.")
            return redirect('myhub:index')
        return view(request, *args, **kwargs)
    return wrapped


def teaching_required(view):
    """Educators, plus admin and staff (who can look at any educator's view)."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        """Log ``DESK-2001`` and redirect non-teaching users; otherwise call the view."""
        if role_of_user(request.user) not in ('admin', 'staff', 'educator'):
            note('DESK-2001', request, view=view.__name__)
            messages.error(request, "That page is for educators.")
            return redirect('myhub:index')
        return view(request, *args, **kwargs)
    return wrapped
