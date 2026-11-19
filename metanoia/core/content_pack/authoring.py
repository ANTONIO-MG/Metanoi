"""Ask the AI assistant to *write* teaching content, not just convert it.

The admin describes what they want — "the Test 2 blueprint for CGAU B26S, then
weeks 1–3 with a study guide and a ten-question self-check each" — picks where
it goes, and may attach source documents (past papers, a topic allocation, last
year's guide) for Claude to work from. Claude returns a ``metanoia-bundle/1``: one
``metanoia-pack`` per week, each holding the week's study guide, blueprint and
quizzes.

Exactly the same safety as every other route: the bundle is validated, held in
the review queue as a draft, and nothing is imported until an admin approves it
or published until they publish it. The placement is pinned by the caller and
overwritten on the way back, so the model cannot move content to another module.
"""

import json
import logging

from .drafting import PACK_SCHEMA, DraftingError, is_enabled
from .schema import BUNDLE_VERSION, PACK_VERSION, validate_bundle

logger = logging.getLogger('apps')

SYSTEM = f"""You are the Metanoia Group content author: an experienced South African
CA(SA) coach who writes for candidates on Milpark, Unisa and IAS PGDA/BCTA
programmes and the SAICA ITC/IAC board exam.

You write in the house style of Metanoia Group material:
- Plain, direct, exam-focused English. Short paragraphs. No padding, no praise.
- A STRATEGIC BLUEPRINT answers "what will be on this test and how do I score":
  what is tested (topic frequency across past sittings when the sources give it),
  the 3 rules, the clusters that carry the marks, a prediction table, the question
  forms, standards/paragraphs to lock in, a marker watch list of avoidable errors,
  the week-by-week programme to the test, and a one-page cheat sheet.
- A STUDY GUIDE teaches one topic for one week: what you will be able to do,
  why it matters for the marks, the concepts with the authority behind each
  (IFRS/IAS paragraph, ISA, Income Tax Act section, Companies Act section, SAICA
  Code section), a worked example applied to a short scenario, common errors, and
  a "working this topic" checklist.
- A SELF-CHECK QUIZ ("quiz") is 5–10 multiple-choice questions on the week's
  guide. Each part has "choices" (one or more marked correct), marking "auto",
  1 mark each, and an "explanation" of why the right answer is right.
- A WRITTEN PRACTICE QUESTION ("challenge") is a short integrated scenario with
  REQUIRED parts, each marked "ai_assisted" against a "rubric": one row per point
  that earns a mark, with its authority. Row marks must add up to the part.

HTML in body_html / scenario_html: <p>, <h4>, <ul>/<ol>/<li>, <strong>, <em>,
<table> with <thead>/<tbody>, and <div class="lesson-callout"> for a key point.
No scripts, styles, classes other than lesson-callout, or images.

ACCURACY RULES — you are writing for professional exams:
- Only cite an authority you are confident of. If unsure of a paragraph number,
  cite the standard without the paragraph.
- When source documents are attached, prefer their facts, dates, frequencies and
  marks over your own; never contradict them. Do not invent past-paper names,
  sittings or statistics that the sources do not give — say "past sittings show"
  only when they do.
- Every figure in a worked example must be internally consistent.

OUTPUT: one JSON object: {{"pack": "{BUNDLE_VERSION}", "packs": [ ... ]}} where each
pack is a "{PACK_VERSION}" for ONE week (its "week" number and a "week_title"),
holding that week's items. A blueprint goes in the first pack. Copy the placement
values you are given verbatim. No prose outside the JSON.
"""

BUNDLE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['pack', 'packs'],
    'properties': {
        'pack': {'type': 'string'},
        'packs': {'type': 'array', 'items': PACK_SCHEMA},
    },
}

WANTS = {
    'blueprint': 'a strategic blueprint for the test (in the first week\'s pack)',
    'study_guide': 'a study guide for each week\'s topic',
    'quiz': 'a self-check multiple-choice quiz for each week',
    'challenge': 'a written practice question with a marking rubric for each week',
}


def _instruction(*, offering, cohort_code, phase_kind, phase_sequence, weeks, wants, topics):
    """The per-request instruction: the placement to copy verbatim, the weeks and what each should hold."""
    programme = f'{offering.programme.institution.code}-{offering.programme.code}'
    lines = [
        'PLACEMENT — copy into every pack verbatim:',
        f'  "pack": "{PACK_VERSION}", "module": "{offering.code}", "programme": "{programme}"',
        f'  "phase": {{"kind": "{phase_kind}", "sequence": {phase_sequence}}}',
    ]
    if cohort_code:
        lines.append(f'  "cohort": "{cohort_code}"')
    lines += [
        f'Module: {offering.display_name if hasattr(offering, "display_name") else offering.name}',
        f'Write {len(weeks)} pack(s), for week(s) {", ".join(str(w) for w in weeks)}.',
        'Each pack needs a "topic": {"code": "W<week>", "title": <the week\'s topic>} and a "week_title".',
        'Include: ' + '; '.join(WANTS[w] for w in wants if w in WANTS) + '.',
    ]
    if topics:
        lines.append('Week topics, in order: ' + '; '.join(topics))
    return '\n'.join(lines)


def draft_from_prompt(prompt, *, offering, cohort=None, phase_kind='test', phase_sequence=1,
                      weeks=(1,), wants=('study_guide', 'quiz'), topics=(), sources='',
                      max_tokens=64000):
    """A written brief (+ optional source text) → ``(bundle, problems)``.

    Raises :class:`DraftingError` when the model is unavailable or returns nothing usable.
    """
    if not is_enabled():
        raise DraftingError('The AI assistant needs ANTHROPIC_API_KEY and ADMIN_AI_ENABLED.')
    if not (prompt or '').strip():
        raise DraftingError('Describe what you want written.')

    from apps.ai_assistant import generate

    weeks = sorted({int(w) for w in weeks if int(w) > 0}) or [1]
    instruction = _instruction(offering=offering, cohort_code=cohort.code if cohort else '',
                               phase_kind=phase_kind, phase_sequence=phase_sequence,
                               weeks=weeks, wants=wants, topics=[t for t in topics if t])
    brief = prompt.strip()
    if sources:
        brief += '\n\nSOURCE DOCUMENTS TO WORK FROM (extracted text):\n\n' + sources[:300000]
    try:
        bundle = generate.generate_from_prompt(
            brief, system=SYSTEM, schema=BUNDLE_SCHEMA, instruction=instruction,
            constrain=False, max_tokens=max_tokens)
    except Exception as exc:
        logger.exception('prompt authoring failed')
        raise DraftingError(str(exc))
    if not isinstance(bundle, dict) or not isinstance(bundle.get('packs'), list):
        raise DraftingError('The assistant did not return a bundle of packs.')

    return pin(bundle, offering=offering, cohort=cohort, phase_kind=phase_kind,
               phase_sequence=phase_sequence)


def pin(bundle, *, offering, cohort=None, phase_kind='test', phase_sequence=1):
    """Overwrite the placement in every pack — the model shapes content, the admin
    decides where it goes. Returns ``(bundle, problems)``.

    A topic is shared across the module, so each topic code carries the block
    (:func:`topic_code`): Test 2's week 1 never lands in Test 1's week 1.
    """
    programme = f'{offering.programme.institution.code}-{offering.programme.code}'
    bundle['pack'] = BUNDLE_VERSION
    for i, pack in enumerate(bundle.get('packs') or []):
        if not isinstance(pack, dict):
            continue
        pack['pack'] = PACK_VERSION
        pack['module'] = offering.code
        pack['programme'] = programme
        pack['phase'] = {'kind': phase_kind, 'sequence': int(phase_sequence)}
        if cohort is not None:
            pack['cohort'] = cohort.code
        else:
            pack.pop('cohort', None)
        topic = pack.setdefault('topic', {})
        week = pack.get('week') or i + 1
        pack['week'] = week
        topic['code'] = topic_code(phase_kind, phase_sequence, week)
        topic['title'] = (topic.get('title') or pack.get('week_title') or f'Week {week}')[:250]
    return bundle, validate_bundle(bundle)


def topic_code(phase_kind, phase_sequence, week):
    """``T2-W1`` / ``E1-W3`` / ``O1-W1`` — unique per block and week."""
    return f'{(phase_kind or "t")[:1].upper()}{int(phase_sequence)}-W{int(week)}'


def as_json(bundle):
    """The bundle as indented, non-ASCII-preserving JSON."""
    return json.dumps(bundle, indent=2, ensure_ascii=False)
