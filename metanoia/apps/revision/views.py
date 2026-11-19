"""Revision home and the one-card-at-a-time review session.

The session is stateless: each request shows the oldest due card the student
may review, so grading a card (which pushes it into the future) is all it
takes to move on. Every POST re-checks that the card is still due and still
allowed — module open, memo released — before it is graded or its answer shown.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from core.student_hidden import hidden_from_students

from . import services
from .models import ReviewCard, ReviewLog


def _module_filter(request):
    """The ``module`` id from the query string or form, or ``None``."""
    raw = request.GET.get('module') or request.POST.get('module') or ''
    return int(raw) if raw.isdigit() else None


def _session_url(module_id):
    """URL of the review session, filtered to ``module_id`` when given."""
    url = reverse('revision:session')
    return f'{url}?module={module_id}' if module_id is not None else url


@login_required
@hidden_from_students
def home(request):
    """Revision home: due counts and progress per module (hidden from students)."""
    data = services.overview(request.user)
    return render(request, 'revision/home.html', {'page_title': 'Revision', **data})


def _card_context(card, *, module_id, remaining, state, chosen=(), correct=None):
    """Template context for showing one card in ``state`` (question / revealed / feedback).

    The solution is only populated once the answer may be shown; memo release
    was already checked by :func:`~apps.revision.services.due_cards`, so nothing
    unreleased can reach the page.
    """
    question = card.question
    choice_card = services.is_choice_card(question)
    show_answer = state in ('revealed', 'feedback')
    return {
        'page_title': 'Review session',
        'card': card, 'question': question,
        'assessment': question.section.assessment,
        'module': services.module_of(question),
        'choice_card': choice_card,
        'multi': question.type == 'multi',
        'choices': list(question.choices.all()) if choice_card else [],
        'chosen': set(chosen), 'correct': correct,
        'state': state,
        'solution': services.solution_for(question) if show_answer else None,
        'remaining': remaining, 'module_id': module_id,
        'grades': [(ReviewLog.AGAIN, 'Again', '1', ReviewCard.interval_for(1).days),
                   (ReviewLog.HARD, 'Hard', '2', ReviewCard.interval_for(card.box).days),
                   (ReviewLog.GOOD, 'Good', '3', ReviewCard.interval_for(card.box + 1).days),
                   (ReviewLog.EASY, 'Easy', '4', ReviewCard.interval_for(card.box + 2).days)],
    }


@login_required
@hidden_from_students
@require_http_methods(['GET', 'POST'])
def session(request):
    """The one-card-at-a-time review session.

    GET shows the next due card (``?reveal=<id>`` reveals a self-graded card's
    answer). POST suspends, answers (choice cards are checked automatically) or
    self-grades the posted card. A card that is no longer due — already graded by
    a double submit or the back button, or no longer allowed — just redirects
    back to the session.
    """
    user = request.user
    module_id = _module_filter(request)
    due = services.due_cards(user, module_id=module_id)

    if request.method == 'POST':
        card_id = request.POST.get('card') or ''
        card = next((c for c in due if str(c.pk) == card_id), None)
        if card is None:
            return redirect(_session_url(module_id))
        action = request.POST.get('action')
        if action == 'suspend':
            card.suspended = True
            card.save(update_fields=['suspended'])
            return redirect(_session_url(module_id))
        if action == 'answer' and services.is_choice_card(card.question):
            chosen = [int(x) for x in request.POST.getlist('choice') if x.isdigit()]
            if not chosen:
                return redirect(_session_url(module_id))
            correct = services.check_choices(card.question, chosen)
            services.grade_card(card, ReviewLog.GOOD if correct else ReviewLog.AGAIN)
            remaining = len(due) - 1
            return render(request, 'revision/session.html', _card_context(
                card, module_id=module_id, remaining=remaining, state='feedback',
                chosen=chosen, correct=correct))
        if action == 'grade':
            grade = request.POST.get('grade')
            if grade in services.GRADES:
                services.grade_card(card, grade)
        return redirect(_session_url(module_id))

    if not due:
        streak, today = services.day_streak(user)
        return render(request, 'revision/session.html', {
            'page_title': 'Review session', 'state': 'done',
            'streak': streak, 'reviewed_today': today, 'module_id': module_id,
            'left_overall': services.due_count(user) if module_id is not None else 0,
        })

    card = due[0]
    reveal = request.GET.get('reveal') == str(card.pk)
    choice_card = services.is_choice_card(card.question)
    state = 'revealed' if (reveal and not choice_card) else 'question'
    return render(request, 'revision/session.html', _card_context(
        card, module_id=module_id, remaining=len(due), state=state))
