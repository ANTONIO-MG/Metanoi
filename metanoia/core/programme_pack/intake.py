"""A pack into an existing cohort: the steps the upload screen and the command share.

    plan  = scanner.scan(folder)                          what the folders hold
    rows  = sheet_for(plan)                               the schedule sheet in the pack (or none)
    check = check(plan, cohort, rows)                     module mapping, key, problems — nothing written
    spine = spine_for(plan, cohort, rows, key)            the programme written down
    build_into(plan, cohort, rows, key, …)                the rows, into the existing modules

Nothing here creates an institution, programme, cohort or module: the pack is
pointed at a cohort that exists, and its folders' module codes must match the
programme's modules (:func:`core.programme_pack.builder.map_modules`). The one
thing that may be added is the programme's own General area (programme-wide
material), which every programme has on demand.
"""
import os

from . import builder, scanner, schedule, sheet


def sheet_for(plan):
    """``(rows, problems, csv_text)`` from the sheet inside the pack, or ``([], [], '')``."""
    rel = plan.get('schedule_sheet')
    if not rel:
        return [], [], ''
    rows, problems = sheet.read_file(os.path.join(plan['root'], rel))
    return rows, problems, sheet.to_csv(rows)


def phase_spec(rows):
    return sheet.phase(rows) or {'kind': 'exam', 'sequence': 1}


def check(plan, cohort, rows):
    """What a build would do, and what stops it. Writes nothing."""
    programme = cohort.programme
    mapped, missing = builder.map_modules(programme, plan['modules'])
    problems = []
    if missing:
        problems.append(f'{programme.full_code} has no module for {", ".join(missing)} — add it to the '
                        'programme first, or rename the pack\'s folders to its module codes '
                        f'({", ".join(o.code for o in programme.modules.without_general())}).')
    if not plan['modules']:
        problems.append('No module folders were found in the pack.')
    return {
        'mapping': [(code, mapped[code].code, mapped[code].display_name) for code in plan['modules']
                    if code in mapped],
        'missing': missing,
        'key': builder.pack_key(cohort, phase_spec(rows)),
        'problems': problems,
    }


def spine_for(plan, cohort, rows, key):
    """The spine for ``plan`` going into ``cohort`` — programme fields read from the database."""
    programme = cohort.programme
    institution = programme.institution
    spine = schedule.spine_from_plan(
        plan, key=key,
        institution={'code': institution.code, 'name': institution.name},
        programme={'code': programme.code, 'abbreviation': programme.abbreviation or programme.code,
                   'name': programme.name, 'full_name': programme.full_name or programme.name,
                   'description': ''},
        cohort={'code': cohort.code, 'name': cohort.name,
                'start_date': cohort.start_date.isoformat() if cohort.start_date else None,
                'end_date': cohort.end_date.isoformat() if cohort.end_date else None},
        sheet_rows=rows)
    if not rows or not sheet.phase(rows):
        spine['phase']['title'] = f'{programme.name} · {cohort.code}'
    return spine


def build_into(plan, cohort, rows, key, *, actor=None, ai=None, out=None, publish=True, progress=None):
    """Build the pack into ``cohort`` (idempotent; a new version updates in place).

    ``update=True`` always: a re-upload re-reads the text into the existing
    lessons (students keep their progress), replaces changed files and retires
    what the new version dropped.
    """
    spine = spine_for(plan, cohort, rows, key)
    made, report = builder.build(spine, plan, actor=actor, ai=ai or {}, out=out, into=cohort,
                                 update=True, publish=publish, progress=progress)
    return made, report


def scan(folder, *, year=None):
    return scanner.scan(folder, year=year)
