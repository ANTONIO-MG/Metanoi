"""Programme packs: a folder (or zip) of a coach's programme → the academic spine and its lessons.

    scanner.scan(folder)            the folders read as a plan (no AI, no database)
    schedule.spine_from_plan(...)   a spine (dates, weeks, sessions, calendar) for a pack
                                    that does not have one written yet
    ai.enrich(plan, spine)          optional: Claude writes unit summaries and reads the
                                    timetable — skipped cleanly when no key is configured
    builder.build(spine, plan)      the rows: programme, cohort, modules, weeks, lessons,
                                    materials, live sessions, calendar — idempotent
    codegen.render(spine)           the spine as a Python seed file, so the programme lives
                                    in the repository and not only in the database

``core/academic_spine_sample.py`` is the first spine written this way; the zip
importer (``apps/learning/programme_pack_views.py``) produces the same shape for
any other pack.
"""

import os

from django.conf import settings


def pack_path(spine, override=None):
    path = override or spine.get('pack') or ''
    return path if os.path.isabs(path) else os.path.join(settings.BASE_DIR, path)


def programme_totals(programme, cohort, key):
    """Row counts for one seeded programme — what a re-run must leave unchanged."""
    from .builder import programme_totals as totals
    return totals(programme, cohort, key)


def seed_spine(spine, *, pack=None, use_ai=False, out=None, **kwargs):
    """Scan the spine's pack, optionally enrich it with AI, and build it.

    Returns ``(builder, report, totals)``.
    """
    from . import ai, builder, scanner

    root = pack_path(spine, pack)
    if not os.path.isdir(root):
        raise FileNotFoundError(f'Programme pack not found: {root}')
    plan = scanner.scan(root, year=spine.get('year'))
    enrichment = {}
    if use_ai:
        enrichment = ai.enrich(plan, spine, out=out)
    made, report = builder.build(spine, plan, ai=enrichment, out=out, **kwargs)
    return made, report, made.totals()
