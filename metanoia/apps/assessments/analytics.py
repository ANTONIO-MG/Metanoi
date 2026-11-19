"""How a class answered a paper, and how one candidate could have scored more.

Two readings of the same answers:

* :func:`item_analysis` — for the coach. Per paper: the spread of scores, the
  pass rate, the time taken. Per question: the average, how many got full marks
  and how many got nothing; per multiple-choice option, who chose it; per marking
  point, how many candidates made it; per computation line, how many got it right.
  The questions that cost the class the most marks come first — that is the
  next live session.
* :func:`score_best` — for the candidate. Every question ordered by the marks
  left on it, with what was missed: the marking points not made, the option
  that was right and why, the lines of the computation that went wrong — and
  what the score would have been with them.

Both read only what marking already stores (``Answer.awarded_marks``, the
selected choices, ``response['rubric_made']`` ticked by the coach and
``response['breakdown']`` from the schedule marker). Nothing here writes.
"""

from collections import Counter, defaultdict
from statistics import mean, median

from .rubric_marking import rubric_of

BUCKETS = [(0, 40), (40, 50), (50, 60), (60, 75), (75, 101)]
BUCKET_LABELS = ['Below 40%', '40–49%', '50–59%', '60–74%', '75% +']


def _pct(part, whole):
    return round(float(part) / float(whole) * 100, 1) if whole else 0.0


def _questions(assessment):
    from .marking import _assessment_questions
    return _assessment_questions(assessment)


def _finished(assessment):
    from .models import AssessmentAttempt
    return list(AssessmentAttempt.objects
                .filter(assessment=assessment,
                        status__in=[AssessmentAttempt.STATUS_SUBMITTED, AssessmentAttempt.STATUS_MARKED])
                .select_related('student').order_by('student_id', '-attempt_no'))


def item_analysis(assessment, *, best_only=True):
    """Everything the coach's analytics page shows, as plain data."""
    from .models import Answer

    attempts = _finished(assessment)
    if best_only:
        best = {}
        for a in attempts:
            if a.student_id not in best or float(a.score or 0) > float(best[a.student_id].score or 0):
                best[a.student_id] = a
        attempts = list(best.values())
    total = float(assessment.total_marks or 0)
    pcts = [_pct(a.score or 0, total) for a in attempts]
    marked = [a for a in attempts if a.status == a.STATUS_MARKED]

    summary = {
        'candidates': len(attempts),
        'awaiting_marking': len(attempts) - len(marked),
        'mean': round(mean(pcts), 1) if pcts else None,
        'median': round(median(pcts), 1) if pcts else None,
        'highest': max(pcts) if pcts else None,
        'lowest': min(pcts) if pcts else None,
        'pass_rate': _pct(sum(1 for p in pcts if p >= (assessment.pass_mark_pct or 0)), len(pcts)) if pcts else None,
        'minutes': round(mean([a.time_spent_seconds / 60 for a in attempts if a.time_spent_seconds]), 0)
        if any(a.time_spent_seconds for a in attempts) else None,
        'distribution': [
            {'label': label, 'count': sum(1 for p in pcts if lo <= p < hi),
             'share': _pct(sum(1 for p in pcts if lo <= p < hi), len(pcts))}
            for (lo, hi), label in zip(BUCKETS, BUCKET_LABELS)],
    }

    answers = defaultdict(list)
    for answer in (Answer.objects.filter(attempt__in=attempts)
                   .prefetch_related('selected_choices').select_related('question')):
        answers[answer.question_id].append(answer)

    rows = []
    for question in _questions(assessment):
        given = answers.get(question.id, [])
        marks = float(question.marks or 0)
        awarded = [float(a.awarded_marks or 0) for a in given if a.marked]
        row = {
            'question': question,
            'answered': sum(1 for a in given if _has_response(a)),
            'mean_pct': _pct(mean(awarded), marks) if awarded else None,
            'full': _pct(sum(1 for x in awarded if x >= marks), len(awarded)) if awarded else None,
            'zero': _pct(sum(1 for x in awarded if x == 0), len(awarded)) if awarded else None,
            'marks_lost': round(sum(marks - x for x in awarded), 1),
            'choices': [], 'points': [], 'lines': [],
        }
        if question.type in ('mcq', 'multi', 'tf'):
            chosen = Counter(c.id for a in given for c in a.selected_choices.all())
            n = len(given) or 1
            row['choices'] = [{'text': c.text, 'correct': c.is_correct, 'count': chosen.get(c.id, 0),
                               'share': _pct(chosen.get(c.id, 0), n)} for c in question.choices.all()]
        rubric = rubric_of(question)
        if rubric:
            ticked = [a for a in given if 'rubric_made' in (a.response or {})]
            made = Counter(i for a in ticked for i in a.response['rubric_made'])
            row['points'] = [{'point': p.get('point'), 'marks': p.get('marks'), 'authority': p.get('authority', ''),
                              'made': made.get(i, 0), 'share': _pct(made.get(i, 0), len(ticked)) if ticked else None}
                             for i, p in enumerate(rubric)]
            row['points_marked'] = len(ticked)
        if question.type == 'schedule':
            right, seen = Counter(), Counter()
            for a in given:
                for i, line in enumerate((a.response or {}).get('breakdown') or []):
                    seen[i] += 1
                    right[i] += bool(line.get('correct'))
            lines = (question.config or {}).get('lines') or []
            row['lines'] = [{'label': l.get('label'), 'marks': l.get('marks'),
                             'share': _pct(right[i], seen[i]) if seen[i] else None} for i, l in enumerate(lines)]
        rows.append(row)

    hardest = sorted([r for r in rows if r['mean_pct'] is not None], key=lambda r: r['mean_pct'])[:3]
    students = sorted(({'attempt': a, 'pct': _pct(a.score or 0, total)} for a in attempts),
                      key=lambda s: -s['pct'])
    return {'summary': summary, 'questions': rows, 'hardest': hardest, 'students': students}


def _has_response(answer):
    response = answer.response or {}
    return bool(response.get('text') or response.get('value') or response.get('lines')
                or answer.file or answer.selected_choices.all())


def score_best(attempt):
    """``{'rows': [...], 'lost': n, 'potential_pct': x, 'class_mean': y}`` for the
    candidate's result page, the biggest losses first."""
    assessment = attempt.assessment
    total = float(assessment.total_marks or 0)
    answers = {a.question_id: a for a in attempt.answers.prefetch_related('selected_choices')}
    analysis = item_analysis(assessment)
    class_means = {row['question'].id: row['mean_pct'] for row in analysis['questions']}

    rows = []
    for question in _questions(assessment):
        answer = answers.get(question.id)
        marks = float(question.marks or 0)
        if answer is not None and not answer.marked and not question.is_auto_marked:
            rows.append({'question': question, 'pending': True, 'lost': 0})
            continue
        got = float(answer.awarded_marks or 0) if answer else 0.0
        lost = round(marks - got, 2)
        entry = {'question': question, 'got': got, 'marks': marks, 'lost': lost, 'pending': False,
                 'unanswered': answer is None or not _has_response(answer),
                 'class_mean': class_means.get(question.id), 'missed_points': [], 'correct_choice': [],
                 'explanation': (question.config or {}).get('explanation', ''), 'wrong_lines': []}
        rubric = rubric_of(question)
        made = set(((answer.response or {}) if answer else {}).get('rubric_made') or [])
        if rubric and answer is not None and 'rubric_made' in (answer.response or {}):
            entry['missed_points'] = [p for i, p in enumerate(rubric) if i not in made]
        if question.type in ('mcq', 'multi', 'tf') and lost > 0:
            entry['correct_choice'] = [c.text for c in question.choices.all() if c.is_correct]
        if question.type == 'schedule' and answer is not None:
            entry['wrong_lines'] = [l for l in (answer.response or {}).get('breakdown') or [] if not l.get('correct')]
        rows.append(entry)

    lost = round(sum(r['lost'] for r in rows), 2)
    rows.sort(key=lambda r: -r['lost'])
    scored = float(attempt.score or 0)
    marks_left = [r for r in rows if r['lost'] > 0 and not r['pending']]
    top_three = round(sum(r['lost'] for r in marks_left[:3]), 2)
    return {
        'rows': rows,
        'lost': lost,
        'top_three': top_three,
        'pct_with_top_three': _pct(scored + top_three, total),
        'class_mean': analysis['summary']['mean'],
    }
