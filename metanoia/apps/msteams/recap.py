"""Turn a Teams meeting transcript into a class recap.

Graph gives us the transcript (VTT), never Copilot's summary — so we build the
recap here. Two engines:

* **extractive** (default, free): heuristics over the transcript — key points,
  action items, decisions, questions, a rough timeline. Runs for every class.
* **Claude** (admin-triggered): rewrites the transcript into a polished recap via
  the existing admin layer (:mod:`apps.ai_assistant.admin_ai`).

Both return the same dict shape so the caller/template don't care which ran.

``_ACTION_RE`` and ``_DECISION_RE`` hold the cue words that flag a sentence as
an action item or a decision.
"""

import logging
import re

logger = logging.getLogger('apps')

_ACTION_RE = re.compile(r'\b(action item|to[- ]do|will|need to|should|must|assign|deadline|by (?:monday|tuesday|wednesday|thursday|friday|next week|friday|tomorrow)|follow up|homework|submit|hand in)\b', re.I)
_DECISION_RE = re.compile(r'\b(we (?:decided|agreed)|decision|conclude[d]?|final(?:ly|ised|ized)?|agreed that|resolved)\b', re.I)


def parse_vtt(vtt_text):
    """Flatten a WEBVTT transcript into ``[(speaker, text), ...]`` in order.

    A cue is either ``<v Speaker Name>text</v>`` or ``Speaker: text``; any
    other line is kept with no speaker.
    """
    if not vtt_text:
        return []
    lines = vtt_text.replace('\r\n', '\n').split('\n')
    out, buf_speaker, buf = [], None, []
    ts = re.compile(r'^\d{2}:\d{2}:\d{2}\.\d{3}\s+-->')
    for raw in lines:
        line = raw.strip()
        if not line or line == 'WEBVTT' or line.isdigit() or ts.match(line) or line.startswith('NOTE'):
            continue
        m = re.match(r'<v\s+([^>]+)>(.*)</v>', line)
        if m:
            out.append((m.group(1).strip(), _clean(m.group(2))))
            continue
        m = re.match(r'^([A-Z][\w .\'-]{1,40}):\s+(.*)$', line)
        if m:
            out.append((m.group(1).strip(), _clean(m.group(2))))
            continue
        out.append((None, _clean(line)))
    return [(s, t) for s, t in out if t]


def _clean(s):
    """``s`` with any markup tags removed."""
    return re.sub(r'<[^>]+>', '', s or '').strip()


def _sentences(cues):
    """All cue text split into sentences."""
    text = ' '.join(t for _s, t in cues)
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if len(p.strip()) > 3]


def extractive_recap(cues, *, title=''):
    """The free, heuristic recap of ``cues`` (see :func:`parse_vtt`).

    Actions and decisions are cue-word sentences that are not questions, and a
    decision is not also listed as an action. Key points are the longest
    content-bearing sentences that are not already actions, and the timeline
    is a handful of evenly sampled cues. Returns the recap dict
    (``summary``, ``key_points``, ``action_items``, ``decisions``,
    ``questions``, ``timeline``, ``speakers``, ``engine``).
    """
    sents = _sentences(cues)
    speakers = sorted({s for s, _t in cues if s})

    questions = [s for s in sents if s.rstrip().endswith('?')][:8]
    decisions = [s for s in sents if not s.rstrip().endswith('?') and _DECISION_RE.search(s)][:6]
    actions = [s for s in sents
               if not s.rstrip().endswith('?') and s not in decisions and _ACTION_RE.search(s)][:8]

    ranked = sorted(
        (s for s in sents if s not in actions and not s.endswith('?')),
        key=lambda s: len(s), reverse=True,
    )
    key_points = _dedupe(ranked)[:6]

    timeline = []
    if cues:
        step = max(1, len(cues) // 6)
        for i in range(0, len(cues), step):
            spk, txt = cues[i]
            timeline.append(f'{spk + ": " if spk else ""}{txt[:110]}')
        timeline = timeline[:6]

    summary = ' '.join(key_points[:3]) or (sents[0] if sents else 'No transcript content was available.')
    return {
        'summary': summary,
        'key_points': key_points,
        'action_items': _dedupe(actions),
        'decisions': _dedupe(decisions),
        'questions': _dedupe(questions),
        'timeline': timeline,
        'speakers': speakers,
        'engine': 'extractive',
    }


def _dedupe(items):
    """``items`` without near-duplicates (same first 80 characters, ignoring case), in order."""
    seen, out = set(), []
    for it in items:
        k = it.lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def claude_recap(cues, *, title=''):
    """Ask the admin Claude layer to structure the transcript. Falls back to the
    extractive recap if Claude isn't enabled or errors.

    ``admin_ai.ask()`` answers a failure with its error text rather than
    raising. Filed as the recap, that text would go to the class word for word
    ("Your credit balance is too low…"), so the free recap goes instead and the
    reason stays in the log for staff. On success the extractive lists are kept
    as a fallback alongside Claude's text.
    """
    try:
        from apps.ai_assistant import admin_ai
        if not admin_ai.is_enabled():
            return extractive_recap(cues, title=title)
        transcript = '\n'.join(f'{s + ": " if s else ""}{t}' for s, t in cues)[:16000]
        prompt = (
            f'You are writing a class recap for "{title}". From the transcript below, produce:\n'
            '1) a 3-4 sentence summary; 2) key points (bullets); 3) action items; '
            '4) decisions; 5) open questions; 6) a short timeline.\n'
            'Be concise and student-friendly.\n\nTRANSCRIPT:\n' + transcript
        )
        text, usage = admin_ai.ask([], prompt)
        if admin_ai.failed(text, usage):
            logger.error('msteams: Claude recap failed for %r, using the extractive recap: %s',
                         title, usage.get('error') or text[:300])
            return extractive_recap(cues, title=title)
        base = extractive_recap(cues, title=title)
        base['summary'] = text.strip() or base['summary']
        base['engine'] = 'claude'
        base['claude_markdown'] = text
        return base
    except Exception:  # pragma: no cover
        return extractive_recap(cues, title=title)


def to_markdown(recap, *, title=''):
    """The recap as Markdown: Claude's text when it wrote one, else a sectioned list."""
    L = []
    if title:
        L.append(f'# {title} — class recap\n')
    if recap.get('claude_markdown'):
        return (f'# {title} — class recap\n\n' if title else '') + recap['claude_markdown']
    L.append('## Summary')
    L.append(recap.get('summary', '') + '\n')
    for label, key in [('Key points', 'key_points'), ('Action items', 'action_items'),
                       ('Decisions', 'decisions'), ('Questions raised', 'questions'),
                       ('Timeline', 'timeline')]:
        items = recap.get(key) or []
        if items:
            L.append(f'## {label}')
            L.extend(f'- {it}' for it in items)
            L.append('')
    if recap.get('speakers'):
        L.append('_Participants: ' + ', '.join(recap['speakers']) + '_')
    return '\n'.join(L).strip()
