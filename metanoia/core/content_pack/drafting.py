"""Document → ``metanoia-pack`` JSON, via Claude. **Path B.**

The only place a language model appears in the pipeline. It turns extracted text
into *the same JSON a person could have typed by hand* and writes nothing to the
database; the result goes through the same validator and importer as the
hand-written path and lands in draft. With no ``ANTHROPIC_API_KEY`` this module
refuses cleanly and the JSON path still works.

The system prompt is written against the CTA solution workbooks' mark grid (one
row per scoring point), which is what makes a paper markable.

:data:`PACK_SCHEMA` is kept flat and permissive on purpose: the *real* contract
is :mod:`core.content_pack.schema`, which validates totals, mode/data agreement
and duplicate refs — things a JSON Schema cannot express. The schema only
steers the model; the validator is what decides.
"""

import json
import logging

from .schema import ITEM_TYPES, MARKING_MODES, PACK_VERSION, RELEASE_MODES, validate

logger = logging.getLogger('apps')


class DraftingError(Exception):
    """The model could not be reached, or did not return usable JSON."""


SYSTEM = f"""You convert South African CTA / PGDA teaching documents into one strict JSON object.

You are given the extracted text of a set of related documents for ONE topic of
ONE module: usually a study guide, a question paper ("Mock" / "Challenge" /
"Test"), and a solution workbook. Your output is a "{PACK_VERSION}" content pack.

WHAT MATTERS MOST — the solution workbook's mark grid.
A CTA solution workbook contains a table with one row per scoring point, roughly:
    REF | SCENARIO TRIGGER | STANDARD / PARAGRAPH | FULL ANSWER | MARKS
That table is a RUBRIC, not an answer key, and it is the single most valuable
thing in the source. Every row becomes either a `lines` entry (a computation) or
a `rubric` entry (a discussion). Never merge rows; never invent them.

DECIDING HOW A PART IS MARKED
- "auto"        — the rows carry a FIGURE (an amount). A calculation: estate duty,
                  CGT, materiality, a deferred-tax computation. Put every row in
                  `lines` with its numeric `amount` and its `marks`.
- "ai_assisted" — the rows carry PROSE. "Critically assess", "Explain", "Discuss",
                  ethics, governance, audit procedures. Put every row in `rubric`.
- "manual"      — professional-skills marks (the "communication", "critical
                  thinking", "Y2" row that appears in most REQUIRED tables).

A part has EITHER `lines` OR `rubric`, never both. If a part is genuinely both a
computation and a discussion, split it into two parts and split its marks.

ARITHMETIC — checked automatically, so get it right:
- the `marks` on the rows of a part MUST add up to that part's `marks`
- the `marks` on the parts MUST add up to `total_marks`
If the source is ambiguous, follow the mark totals printed in the source and
adjust the row you are least sure of.

AMOUNTS: plain numbers only. 4500000, not "R4 500 000". Negative for a deduction
(an abatement, a s4(q) accrual to a spouse): -3500000.

STUDY GUIDES: `body_html` is simple HTML — <p>, <ul>/<li>, <strong>, <em>,
<table>. No scripts, no styles, no classes. Keep the source's own section
headings.

RULES YOU MUST NOT BREAK:
- Never invent a figure, an authority reference, or a mark allocation. If the
  source does not say, leave the field out.
- Never write a `pack`, `module`, `programme` or `topic` value that was not given
  to you in the instruction.
- Return ONE JSON object. No prose, no markdown fences, no commentary.
"""

PACK_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['pack', 'module', 'programme', 'topic', 'items'],
    'properties': {
        'pack': {'type': 'string'},
        'module': {'type': 'string'},
        'programme': {'type': 'string'},
        'cohort': {'type': 'string'},
        'week': {'type': 'integer'},
        'week_title': {'type': 'string'},
        'audience': {'type': 'string', 'enum': ['students', 'staff']},
        'phase': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'kind': {'type': 'string', 'enum': ['test', 'exam', 'supp', 'other']},
                           'sequence': {'type': 'integer'}, 'title': {'type': 'string'}},
        },
        'topic': {
            'type': 'object', 'additionalProperties': False,
            'required': ['code', 'title'],
            'properties': {'code': {'type': 'string'}, 'title': {'type': 'string'},
                           'description': {'type': 'string'}},
        },
        'items': {
            'type': 'array',
            'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['type', 'title'],
                'properties': {
                    'type': {'type': 'string', 'enum': list(ITEM_TYPES)},
                    'title': {'type': 'string'},
                    'subtitle': {'type': 'string'},
                    'sections': {
                        'type': 'array',
                        'items': {
                            'type': 'object', 'additionalProperties': False,
                            'required': ['heading', 'body_html'],
                            'properties': {'heading': {'type': 'string'},
                                           'body_html': {'type': 'string'},
                                           'summary': {'type': 'string'},
                                           'video_url': {'type': 'string'}},
                        },
                    },
                    'scenario_html': {'type': 'string'},
                    'instructions': {'type': 'string'},
                    'total_marks': {'type': 'number'},
                    'minutes': {'type': 'integer'},
                    'release_solution': {'type': 'string', 'enum': list(RELEASE_MODES)},
                    'week': {'type': 'integer'},
                    'scenario_key': {'type': 'string'},
                    'estimated_minutes': {'type': 'integer'},
                    'audience': {'type': 'string', 'enum': ['students', 'staff']},
                    'parts': {
                        'type': 'array',
                        'items': {
                            'type': 'object', 'additionalProperties': False,
                            'required': ['ref', 'required', 'marks', 'marking'],
                            'properties': {
                                'ref': {'type': 'string'},
                                'required': {'type': 'string'},
                                'marks': {'type': 'number'},
                                'marking': {'type': 'string', 'enum': list(MARKING_MODES)},
                                'explanation': {'type': 'string'},
                                'choices': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object', 'additionalProperties': False,
                                        'required': ['text', 'correct'],
                                        'properties': {'text': {'type': 'string'},
                                                       'correct': {'type': 'boolean'}},
                                    },
                                },
                                'lines': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object', 'additionalProperties': False,
                                        'required': ['label', 'amount', 'marks'],
                                        'properties': {'label': {'type': 'string'},
                                                       'authority': {'type': 'string'},
                                                       'amount': {'type': 'number'},
                                                       'marks': {'type': 'number'},
                                                       'tolerance': {'type': 'number'}},
                                    },
                                },
                                'rubric': {
                                    'type': 'array',
                                    'items': {
                                        'type': 'object', 'additionalProperties': False,
                                        'required': ['point', 'marks'],
                                        'properties': {'point': {'type': 'string'},
                                                       'authority': {'type': 'string'},
                                                       'marks': {'type': 'number'},
                                                       'professional_skill': {'type': 'boolean'}},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def is_enabled():
    """Whether the AI route can run at all. The JSON route never needs this."""
    try:
        from apps.ai_assistant import generate
        return generate.is_enabled()
    except Exception:  # pragma: no cover — the assistant app is optional
        return False


def _instruction(*, module, programme, topic_code, topic_title, cohort='', week=None, note=''):
    """The per-import direction: what this pack is *for*, pinned by the caller.

    The model is told these values rather than asked to read them out of the
    filenames, because a wrong module code silently imports a TAX topic into the
    auditing module — a mistake that looks like content until someone opens it.
    """
    lines = [
        'Produce the content pack for exactly this placement — copy these values verbatim:',
        f'  "pack":      "{PACK_VERSION}"',
        f'  "module":    "{module}"',
        f'  "programme": "{programme}"',
        f'  "topic":     {{"code": "{topic_code}", "title": "{topic_title}"}}',
    ]
    if cohort:
        lines.append(f'  "cohort":    "{cohort}"')
    if week:
        lines.append(f'  "week":      {week}')
    lines += [
        '',
        'Include one item per artefact you can actually find in the documents:',
        '  study_guide — from the guide, keeping its own section headings',
        '  mock        — from the question paper + its solution workbook',
        '  challenge   — an integrated/formidable challenge (set scenario_key and week)',
        '  exam        — a full test/exam sitting',
        'Leave out any artefact the documents do not contain. Do not invent one.',
    ]
    if note:
        lines += ['', 'DIRECTION FROM THE PERSON IMPORTING:', note.strip()[:4000]]
    return '\n'.join(lines)


def draft_pack(text, *, module, programme, topic_code, topic_title,
               cohort='', week=None, note='', max_tokens=32000):
    """Extracted document text → a candidate pack ``dict``.

    Returns ``(pack, problems)``. ``problems`` is the validator's list, so a
    caller can show a draft that *almost* works and let a human fix the three
    figures the model misread — which is far more useful than refusing outright.

    Raises :class:`DraftingError` only when nothing usable came back at all.

    The placement (pack version, module, programme, topic code) is pinned
    whatever came back: the model is shaping content, not deciding which module
    it belongs to.
    """
    if not is_enabled():
        raise DraftingError(
            'The document route needs ANTHROPIC_API_KEY and ADMIN_AI_ENABLED. '
            'The JSON route works without it — write the pack by hand and import that.')
    if not (text or '').strip():
        raise DraftingError('There was no text to work from.')

    from apps.ai_assistant import generate

    instruction = _instruction(module=module, programme=programme,
                               topic_code=topic_code, topic_title=topic_title,
                               cohort=cohort, week=week, note=note)
    try:
        pack = generate.generate_from_prompt(
            'EXTRACTED SOURCE DOCUMENTS:\n\n' + text,
            system=SYSTEM, schema=PACK_SCHEMA, instruction=instruction,
            constrain=False, max_tokens=max_tokens)
    except Exception as exc:
        logger.exception('content pack drafting failed')
        raise DraftingError(str(exc))

    if not isinstance(pack, dict):
        raise DraftingError('The model did not return a JSON object.')

    pack['pack'] = PACK_VERSION
    pack['module'] = module
    pack['programme'] = programme
    pack.setdefault('topic', {})
    pack['topic']['code'] = topic_code
    pack['topic'].setdefault('title', topic_title)
    if cohort:
        pack['cohort'] = cohort
    if week:
        pack['week'] = week

    return pack, validate(pack)


def as_json(pack):
    """The pack as the text a human edits — stable key order, readable indent."""
    return json.dumps(pack, indent=2, ensure_ascii=False)
