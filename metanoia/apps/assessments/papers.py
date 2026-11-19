"""Past papers and mock exams as on-system assessments.

Turns one sitting's schedule PDFs (scenario, required, suggested solution —
:class:`~apps.learning.models.ModuleMaterial`) into one :class:`Assessment`:
:func:`paper_key` groups a sitting's files, :func:`parse_parts` reads the
required into marked long-answer questions, and the solution's answers become
each question's ``guidance``.

What a student may see is decided by the *materials*, not by this copy of them:
:func:`material_states` re-asks the module's own gates (published, released,
paid for, their cohort's schedule) for the paper and for its memo on every page
load, so unpublishing the solution material hides the answers here too.

The conversion itself is :mod:`apps.assessments.management.commands.build_paper_assessments`.

Module constants:

- ``SOURCE_PREFIX`` — the ``source_ref`` prefix of every assessment built from a paper.
- ``_ROLE_WORDS`` — words that say what a file *is* (its role in the sitting), not which
  sitting it belongs to; stripped before files are matched to each other.
- ``_PAIR_WORDS`` — what a file is *for* in its sitting, as opposed to which sitting it
  is. Only these are stripped for the fine key, so "P25S FREP Afternoon Exam Required"
  pairs with "… Exam Scenario" but not with "P25S_Afternoon_Required_FREP".
- ``_NOISE`` — running headers, footers and the marks-column headings: never content.
- ``_PART_WORD`` — "Part a", lower-case only: "Part A:" is a paper's own grouping of
  parts.
"""

import hashlib
import logging
import re

logger = logging.getLogger('apps')

SOURCE_PREFIX = 'paper/'

_ROLE_WORDS = {
    'scenario', 'scenarios', 'required', 'requirements', 'requirement', 'question', 'questions',
    'suggested', 'solution', 'solutions', 'memo', 'memorandum', 'answer', 'answers',
    'marker', 'feedback', 'with', 'comments', 'only', 'and', 'pdf', 'exam', 'final',
    'the', 'of', 'paper', 'combined', 'excl', 'last', 'text', 'txt', 'updated', 'prelim',
    'mock', 'past',
}
_SOLUTION_TITLE = re.compile(r'solution|memo|suggested|marker feedback|answers?\b', re.I)


_PAIR_WORDS = {'scenario', 'required', 'requirements', 'question', 'questions', 'only',
               'with', 'comments', 'and', 'combined', 'pdf', 'txt', 'docx', 'text'}


def _words(title):
    """A title as lower-case words, with ``Test 3`` → ``test3`` kept as one.

    Run-together names ("IACJanuary2025Paper2QuestionFreschKart") are split into words
    first, and a bare number (a ``_1`` copy suffix, a leading ``1.``) is dropped as
    noise.
    """
    text = re.sub(r'\.(pdf|docx|txt)$', '', title or '', flags=re.I)
    if ' ' not in text and '_' not in text:
        text = re.sub(r'(?<=[a-z])(?=[A-Z0-9])|(?<=\d)(?=[A-Z][a-z])', ' ', text)
    text = re.sub(r'[_\-—–·.,()\[\]:/]+', ' ', text.lower())
    text = re.sub(r'\b(test|paper|day|part|aq|qosat)\s*(\d+(?:\s\d+)?)\b',
                  lambda m: m.group(1) + m.group(2).replace(' ', '_'), text)
    return [word for word in text.split() if not word.isdigit()]


def paper_key(title, module_code=''):
    """The words that name a sitting, sorted — the key a paper and its memo share.

    "P22F Final Exam — required (Afternoon)" and "P22F Final Exam — suggested
    solution (Afternoon)" both become ``afternoon p22f``.
    """
    code = (module_code or '').lower()
    return ' '.join(sorted({w for w in _words(title) if w not in _ROLE_WORDS and w != code}))


def pair_key(title):
    """The finer key that pairs one sitting's scenario with its own required."""
    return ' '.join(sorted({w for w in _words(title) if w not in _PAIR_WORDS}))


def ref_for(offering_id, key):
    """A stable ``source_ref`` for one sitting on one offering (fits 120 chars)."""
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    return f'{SOURCE_PREFIX}{offering_id}/{digest}/{key[:80]}'


def extract_text(fieldfile):
    """All the text of a PDF material, page after page. ``''`` when unreadable."""
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    try:
        from pypdf import PdfReader
        with fieldfile.storage.open(fieldfile.name, 'rb') as handle:
            reader = PdfReader(handle)
            return '\n'.join((page.extract_text() or '') for page in reader.pages)
    except Exception:
        logger.warning('papers: could not read %s', getattr(fieldfile, 'name', fieldfile))
        return ''


def role_of(material, text=''):
    """``solution`` / ``scenario`` / ``required`` / ``combined`` for one file.

    The kind a file was filed under is not trusted on its own: some solutions
    were filed as mock exams, and say so only on their first line.
    """
    title = material.title or ''
    if material.kind == 'answers' or _SOLUTION_TITLE.search(title):
        return 'solution'
    head = (text or '')[:400]
    if re.search(r'\bSOLUTION\b', head) or re.search(r'suggested solution', head, re.I):
        return 'solution'
    low = title.lower()
    if 'scenario' in low and 'required' not in low:
        return 'scenario'
    if 'required' in low or 'question' in low:
        return 'required'
    return 'combined'


_NOISE = re.compile(
    r'©|\bPage \d+ of \d+\b|^\s*(Marks|Sub-?|total|Total|Sub-total|Sub- ?total)\s*$'
    r'|^\s*\*?CAN ONLY BE VIEWED|continued on (the )?next page', re.I)
_PART = re.compile(r'^\s*(\(?)([a-z])([\.\)])\s*(.*)$')
_PART_WORD = re.compile(r'^\s*(?:Part|PART)\s+([a-z])\b(?:\s*\(i+\))?[\.:]?\s*(.*)$')
_SUBPART_II = re.compile(r'^\s*\(?ii[\.\)]\s')
_QUESTION = re.compile(r'^\s*QUESTION\s+(\d{1,2})\b(.*)$', re.I)
_NUMBER = re.compile(r'^\s*\(?(\d{1,3})\)?\s*$')
_NUMBERS = re.compile(r'^\s*\d{1,3}(?:\s+\d{1,3})*\s*$')
_TRAILING_MARKS = re.compile(r'^(.*?\S)\s{2,}(\d{1,3}(?:\s+\d{1,3}){0,2})\s*$')
_END = re.compile(r'^\s*\**\s*(Total marks|TOTAL MARKS|END OF (THE )?(REQUIRED|PAPER|EXAM|TEST|QUESTION))', re.I)
_TOTAL = re.compile(r'Total marks\s*:?\s*(\d{1,3})\b', re.I)
_REQUIRED_HEAD = re.compile(r'^\s*(REQUIRED\b|Required\s*:?\s*$)')
_BULLET = re.compile(r'^(•|-|–|\(?[ivx]+[\.\)]|\(?\d+[\.\)]|\(?[a-h][\.\)])\s')


def clean_lines(text):
    """PDF text as lines, without running headers/footers, with split "Part" headings
    joined.

    "Part" on a line of its own with its letter on the next line is one heading.
    """
    lines = [line.rstrip() for line in (text or '').splitlines() if not _NOISE.search(line)]
    out = []
    for line in lines:
        if out and out[-1].strip().lower() == 'part':
            out[-1] = f'Part {line.strip()}'
        else:
            out.append(line)
    return out


def _marker(line):
    """``(letter, style, rest)`` when ``line`` opens a lettered part, else None.

    The style ("a.", "a)", "(a)", "Part a") is what keeps a part's own lettered
    list — "(a) whether the auditor …" inside part a. — from being read as the
    next part: every part of a paper is lettered the same way.
    """
    word = _PART_WORD.match(line)
    if word:
        return word.group(1), 'part', word.group(2)
    part = _PART.match(line)
    if part:
        return part.group(2), part.group(1) + part.group(3), part.group(4)
    return None


def reflow(lines, *, soft=False):
    """PDF lines back into paragraphs.

    A line that does not end a sentence runs on into the next; bullets and
    sub-parts keep their own line. ``soft`` (for solutions, which are full of
    computations laid out line by line) only joins a line that plainly
    continues a sentence — the next one starting in lower case.
    """
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if out and out[-1] != '':
                out.append('')
            continue
        prev = out[-1] if out else ''
        if soft:
            joins = bool(prev) and prev[-1:].isalpha() and line[:1].islower()
        else:
            joins = bool(prev) and not _BULLET.match(line) and not prev.endswith((':', '.', ';', '?', '!'))
        if joins:
            out[-1] = (prev[:-1] + line) if (prev.endswith('-') and line[:1].islower()) else f'{prev} {line}'
        else:
            out.append(line)
    return '\n'.join(out).strip()


def _parse_from(lines):
    """Sequential parts from ``lines``: ``a`` must come before ``b`` and so on,
    which is what keeps a scenario's own "a)" lists from posing as parts.

    ``i`` is the one letter that is also a numeral: an "i)" followed shortly by
    an "ii)" is a sub-part of the part before, not part (i).
    """
    parts, current = [], None
    expect, qno, seen_q, style = 'a', None, set(), None

    def close():
        """Append the part being built, if any, to the parsed parts."""
        if current is not None:
            parts.append(current)

    for index, line in enumerate(lines):
        question = _QUESTION.match(line)
        if question and int(question.group(1)) not in seen_q and (
                qno is None or int(question.group(1)) == qno + 1):
            close()
            current = None
            qno = int(question.group(1))
            seen_q.add(qno)
            expect = 'a'
            continue
        part = _marker(line)
        if (part and part[0] == expect and expect <= 'l'
                and (expect == 'a' or style is None or part[1] == style)):
            if expect == 'i' and any(_SUBPART_II.match(nxt) for nxt in lines[index + 1:index + 12]):
                if current is not None:
                    current['lines'].append(line)
                continue
            close()
            if expect == 'a':
                style = part[1]
            current = {'question': qno, 'letter': expect, 'lines': [part[2]]}
            expect = chr(ord(expect) + 1)
            continue
        if _END.match(line):
            close()
            current = None
            continue
        if current is not None:
            current['lines'].append(line)
    close()
    return parts


def _finish(part):
    """Split a raw part into its text and its marks.

    The marks column comes through either as lines of bare numbers or as
    figures trailing a line after a wide gap ("… for FY2023.  4"). The part's
    own total is the last figure printed for it.
    """
    numbers, text_lines = [], []
    for line in part['lines']:
        if _NUMBERS.match(line):
            numbers.extend(int(n) for n in line.split())
            continue
        trailing = _TRAILING_MARKS.match(line)
        if trailing:
            numbers.extend(int(n) for n in trailing.group(2).split())
            line = trailing.group(1)
        text_lines.append(line)
    letter = part['letter']
    label = f'{part["question"]}({letter})' if part['question'] else letter
    marks = numbers[-1] if numbers and 0 < numbers[-1] <= 100 else None
    return {'key': (part['question'], letter), 'label': label,
            'text': reflow(text_lines), 'marks': marks, 'numbers': numbers}


def _drop_question_totals(parts):
    """The last part of a question often picks up the question's total as well
    ("… 7" then "40" for a 40-mark question). When its last figure is exactly
    the question's parts added up, its own marks are the figure before."""
    for index, part in enumerate(parts):
        last_of_question = (index + 1 == len(parts)
                            or parts[index + 1]['key'][0] != part['key'][0])
        numbers = part['numbers']
        if not last_of_question or len(numbers) < 2:
            continue
        earlier = sum(p['marks'] or 0 for p in parts[:index] if p['key'][0] == part['key'][0])
        if earlier and numbers[-1] == earlier + numbers[-2]:
            part['marks'] = numbers[-2]
    return parts


def parse_parts(text, *, from_heading=False):
    """The numbered parts of a paper, in order.

    ``from_heading`` is for a file that carries the scenario as well: the parts
    are only looked for after a ``REQUIRED`` heading, so the case study's own
    lettered lists are never read as questions. Each candidate start is tried
    and the one giving the most parts wins (ties → the later heading).
    """
    return _parse_best(clean_lines(text), from_heading)[0]


def parse_parts_with_lead(text):
    """``(parts, scenario)`` for a file that is scenario and required in one:
    the parts after the ``REQUIRED`` heading, and the case study before it."""
    lines = clean_lines(text)
    parts, start = _parse_best(lines, True)
    if not parts:
        return _parse_best(lines, False)[0], ''
    return parts, reflow(lines[:start])


def _parse_best(lines, from_heading):
    """Parse ``lines`` from each REQUIRED heading (and the top, unless ``from_heading``)
    and keep the start that yields the most parts.
    """
    starts = [i for i, line in enumerate(lines) if _REQUIRED_HEAD.match(line)]
    if not from_heading:
        starts = [0] + starts
    best, best_start = [], 0
    for start in starts:
        parts = _parse_from(lines[start:])
        if parts and len(parts) >= len(best):
            best, best_start = parts, start
    finished = _drop_question_totals([_finish(p) for p in best])
    return [p for p in finished if p['text']], best_start


def paper_total(text, parts):
    """The paper's total: the parts' own marks when every part has them,
    otherwise the largest "Total marks NN" the paper prints."""
    marks = [p['marks'] for p in parts]
    if marks and all(marks):
        return sum(marks)
    stated = [int(m.group(1)) for m in _TOTAL.finditer(text or '')]
    if stated:
        return max(stated)
    return sum(m for m in marks if m) or 100


def _overlap(a, b):
    """Share of the requirement's words the memo's restatement repeats."""
    words_a = set(re.findall(r'[a-z]{4,}', (a or '').lower()))
    words_b = set(re.findall(r'[a-z]{4,}', (b or '').lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _strip_restatement(lines, requirement):
    """Drop what is left of the restated requirement at the top of an answer:
    its remaining sub-parts and marks figures, i.e. leading lines that are bare
    numbers or whose words the requirement already holds."""
    wanted = set(re.findall(r'[a-z]{3,}', (requirement or '').lower()))
    start = 0
    for index, line in enumerate(lines[:30]):
        words = set(re.findall(r'[a-z]{3,}', line.lower()))
        if not line.strip() or _NUMBERS.match(line):
            continue
        if words and len(words & wanted) / len(words) >= .75:
            start = index + 1
            continue
        break
    rest = lines[start:]
    while rest and (not rest[0].strip() or _NUMBERS.match(rest[0])):
        rest = rest[1:]
    return rest


def memo_for_parts(solution_text, parts):
    """``({part key: answer text}, agreement)`` — a solution matched to a paper.

    A suggested solution restates each part before answering it. The
    restatement (up to the marks figure under it) is compared with the paper's
    own wording — ``agreement`` is how well they agree on average, 0–1, which
    is what tells the real memo of a sitting from the memo of its namesake —
    and then dropped, leaving the answer.

    A paper without ``QUESTION n`` headings against a memo with them (or the
    other way round) falls back to matching on the letter alone.
    """
    found = {}
    for part in _parse_from(clean_lines(solution_text)):
        lines = part['lines']
        restated = lines[:1]
        for index, line in enumerate(lines[:14]):
            if _NUMBER.match(line) or _TRAILING_MARKS.match(line):
                restated, lines = lines[:index + 1], lines[index + 1:]
                break
        found.setdefault((part['question'], part['letter']), (' '.join(restated), lines))
    if not found or not parts:
        return {}, 0.0

    by_key = {p['key']: p for p in parts}
    if not (set(by_key) & set(found)):
        by_letter = {}
        for (_q, letter), value in found.items():
            by_letter.setdefault(letter, value)
        found = {p['key']: by_letter[p['key'][1]] for p in parts if p['key'][1] in by_letter}

    memo, scores = {}, []
    for key, (restated, lines) in found.items():
        if key not in by_key:
            continue
        text = reflow(_strip_restatement(lines, by_key[key]['text']), soft=True)
        if text:
            memo[key] = text
            scores.append(_overlap(by_key[key]['text'][:600], restated))
    agreement = (sum(scores) / len(scores)) if scores else 0.0
    return memo, round(agreement, 2)


def kind_for(title, material_kind):
    """Quiz / Test / Mock exam, from the title the paper was filed under."""
    from .models import Assessment
    low = (title or '').lower()
    if re.search(r'\btest\b|test[\s_]*\d', low):
        return Assessment.KIND_TEST
    if material_kind == 'questions' and not re.search(r'exam|mock|paper', low):
        return Assessment.KIND_QUIZ
    return Assessment.KIND_MOCK_EXAM


def tidy_title(title):
    """A paper's display title: the file's title without its role words.

    A trailing copy number ("…_Scenario_3") is removed, but not the number of "Test 3",
    "Paper 2", "Day 1" or "Part 1".
    """
    title = re.sub(r'\.(pdf|docx|txt)$', '', title or '', flags=re.I).replace('_', ' ')
    title = re.sub(r'^\d+\.\s*', '', title)
    title = re.sub(r'\b(scenario and required|scenario|required|question)( only)?\b', '',
                   title, flags=re.I)
    title = re.sub(r'\(\s*\)', '', title)
    title = re.sub(r'\s*[—–-]\s*(\(|$)', r' \1', title)
    title = re.sub(r'(?<!Test)(?<!Paper)(?<!Day)(?<!Part)\s+\d$', '', title.strip(), flags=re.I)
    return re.sub(r'\s{2,}', ' ', title).strip(' —-')


def _material_visible_to(material, cohort_id):
    """Would this material appear on the student's own module schedule?

    The schedule shows their intake's blocks plus shared ones, published phases
    and weeks only (see ``apps.learning.module_feed._load_phases``).

    What a viewer may see is asked of the materials, every time.
    """
    phase = material.phase
    if not (phase.is_active and phase.is_published):
        return False
    if phase.cohort_id and phase.cohort_id != cohort_id:
        return False
    week = material.week
    if week is not None and not (week.is_active and week.is_published):
        return False
    return True


class PaperAccess:
    """One request's verdict on every paper-backed assessment on the page.

    Builds one :class:`apps.learning.access.Gate` per offering and reuses it, so
    a list of fifty papers costs a handful of queries, not fifty.

        access = PaperAccess(request.user)
        access.paper(assessment)   # → MaterialState | None (None = not theirs)
        access.memo(assessment)    # → MaterialState | None (None = no memo yet)
    """

    def __init__(self, user):
        """Start an empty per-request cache of gates and cohorts for ``user``."""
        self.user = user
        self.person = getattr(user, 'profile', None)
        self._gates = {}
        self._cohorts = {}

    def _gate(self, offering):
        """The (cached) :class:`apps.learning.access.Gate` for ``offering``."""
        if offering.pk not in self._gates:
            from apps.learning.access import Gate
            self._gates[offering.pk] = Gate(self.user, offering, person=self.person)
        return self._gates[offering.pk]

    def _cohort(self, offering):
        """The viewer's cohort id on ``offering``'s programme (cached), or None."""
        if offering.programme_id not in self._cohorts:
            enrolment = None
            if self.person is not None:
                enrolment = (self.person.programme_enrolments
                             .filter(programme_id=offering.programme_id).first())
            self._cohorts[offering.programme_id] = enrolment.cohort_id if enrolment else None
        return self._cohorts[offering.programme_id]

    def _best(self, offering, materials):
        """The most open state among ``materials`` the viewer can see at all."""
        gate = self._gate(offering)
        best = None
        for material in materials:
            if not gate.can_author:
                if material.audience != material.AUDIENCE_STUDENTS:
                    continue
                if not _material_visible_to(material, self._cohort(offering)):
                    continue
            state = gate.for_material(material)
            if not state.visible:
                continue
            if best is None or (state.open and not best.open):
                best = state
        return best

    def can_author(self, assessment):
        """True when the viewer may author on the assessment's module."""
        return bool(assessment.module_id) and self._gate(assessment.module).can_author

    @staticmethod
    def _source(assessment):
        """The assessment's :class:`PaperSource`, or None when it was not built from a
        paper.
        """
        from .models import PaperSource
        try:
            return assessment.paper_source
        except PaperSource.DoesNotExist:
            return None

    def paper(self, assessment):
        """The most open state of the paper's own materials for this viewer, or None."""
        source = self._source(assessment)
        if source is None:
            return None
        return self._best(assessment.module, source.source_materials.select_related('phase__calendar_event', 'week'))

    def memo(self, assessment):
        """The most open state of the paper's solution materials for this viewer, or None.
        """
        source = self._source(assessment)
        if source is None:
            return None
        return self._best(assessment.module, source.solution_materials.select_related('phase__calendar_event', 'week'))

    def memo_open(self, assessment):
        """True when this viewer may open the paper's memo."""
        state = self.memo(assessment)
        return bool(state and state.open)
