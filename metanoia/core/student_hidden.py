"""Sections students no longer see: the Assessments list, Tasks and Revision.

The owner took these out of the student experience. The apps stay (staff and
educators use them, and quizzes embedded in lessons still open through
``assessments:take`` / ``submit`` / ``result``), but the hub pages are closed
to students: a student who opens one directly is sent back to their dashboard
with a short note, never an error page.

Usage::

    @login_required
    @hidden_from_students
    def my_view(request): ...
"""

from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from core.roles import role_of

NOTE = 'That page is not part of your learning hub any more — everything you need is on your dashboard.'


def hidden_from_students(view):
    """Redirect students to ``/myhub/`` (with a gentle message); everyone else passes."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if role_of(request) == 'student':
            messages.info(request, NOTE)
            return redirect('myhub:index')
        return view(request, *args, **kwargs)
    return wrapper
