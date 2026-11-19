"""Draft a whole folder of source documents into content packs — one per topic.

    python manage.py import_content_folder "PGDA Test3" --programme MILPARK-PGDA --dry-run
    python manage.py import_content_folder "PGDA Test3" --programme MILPARK-PGDA --only "TAX T5"
    python manage.py import_content_folder "PGDA Test3" --programme MILPARK-PGDA --import

Two layouts are understood, because the source folder uses both:

    PGDA Test3/
      CGAU T5/            <MODULE> <TOPIC> — one week's guide, mock and solution
        AUD_T3_Topic5_Substantive_Guide.docx
        AUD_T3_Topic5_Substantive_Mock.docx
        AUD_T3_Topic5_Substantive_Solution.xlsx
      TEST 3 MOCK/        the end-of-block sitting, one directory per module
        CGAU/
          CGAU_P26F_Test3_Mock_Scenario_and_Required.docx
          CGAU_P26F_Test3_Mock_Suggested_Solution.xlsx

A sitting spans the block, so it is filed under topic code ``EXAM``.

**Nothing is imported unless asked.** The default run drafts each topic to a
``.json`` file for a person to check; ``--import`` imports them, still as drafts.
A failing topic is reported and the rest carry on.

Patterns: ``DIR_RE`` matches a topic directory (``CGAU T5`` / ``FREP T3`` /
``MACF T7`` — module code, then topic code). ``EXAM_DIR_RE`` matches the
end-of-block sitting directory (``TEST 3 MOCK``); trailing spaces in the real
folder name are why it is matched loosely rather than compared. ``EXAM_TOPIC`` is
the topic code a full sitting is filed under — it spans the block, not a week.
``READABLE`` lists the file extensions read.
"""

import json
import re
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import content_pack
from core.content_pack.drafting import DraftingError
from core.content_pack.importer import PackImportError

READABLE = ('.pdf', '.docx', '.xlsx', '.xlsm', '.txt', '.md')

DIR_RE = re.compile(r'^\s*(?P<module>[A-Za-z]{2,8})\s+(?P<topic>T\d+)\s*$')

EXAM_DIR_RE = re.compile(r'^\s*TEST\s*\d*\s*MOCK\s*$', re.I)

EXAM_TOPIC = 'EXAM'


class Command(BaseCommand):
    """``manage.py import_content_folder <folder> --programme <CODE>`` — see the module
    docstring.
    """
    help = 'Draft (and optionally import) a folder of topic documents as content packs.'

    def add_arguments(self, parser):
        """Add the folder argument and the ``--programme``, ``--cohort``, ``--only``,
        ``--out``, ``--import``, ``--dry-run`` and ``--reuse`` options.
        """
        parser.add_argument('folder', help='Folder holding one directory per topic.')
        parser.add_argument('--programme', required=True,
                            help='Programme full code every pack belongs to, e.g. MILPARK-PGDA.')
        parser.add_argument('--cohort', default='', help='Cohort code to stamp on each pack.')
        parser.add_argument('--only', default='',
                            help='Only this topic directory (e.g. "TAX T5").')
        parser.add_argument('--out', default='',
                            help='Where to write the drafted packs '
                                 '(default: <folder>/_packs).')
        parser.add_argument('--import', dest='do_import', action='store_true',
                            help='Import each drafted pack. Without this, nothing '
                                 'touches the database.')
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be read, and read nothing.')
        parser.add_argument('--reuse', action='store_true',
                            help='Use packs already drafted in --out instead of '
                                 'asking the model again.')

    def _topics(self, root, only):
        """``(directory, module, topic, files)`` for every topic directory under
        ``root``.

        Directories starting with ``_`` (``_packs`` and friends) are skipped; sitting
        directories are expanded by :meth:`_sittings`; any other name that is not
        ``<MODULE> T<n>`` is reported and skipped. ``only`` limits it to one
        directory.
        """
        out = []
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            if entry.name.startswith('_'):
                continue
            if EXAM_DIR_RE.match(entry.name):
                out.extend(self._sittings(entry, only))
                continue
            match = DIR_RE.match(entry.name)
            if not match:
                self.stdout.write(self.style.WARNING(
                    f'  skipped "{entry.name}" — expected "<MODULE> T<n>", e.g. "TAX T5"'))
                continue
            if only and entry.name.lower() != only.lower():
                continue
            files = sorted(f for f in entry.iterdir()
                           if f.is_file() and f.suffix.lower() in READABLE)
            if files:
                out.append((entry, match.group('module').upper(),
                            match.group('topic').upper(), files))
        return out

    def _sittings(self, root, only):
        """``TEST 3 MOCK/<MODULE>/`` — one full paper per module."""
        out = []
        for entry in sorted(p for p in root.iterdir() if p.is_dir()):
            module = entry.name.strip().upper()
            label = f'{module} {EXAM_TOPIC}'
            if only and label.lower() != only.lower():
                continue
            files = sorted(f for f in entry.iterdir()
                           if f.is_file() and f.suffix.lower() in READABLE)
            if files:
                out.append((entry, module, EXAM_TOPIC, files))
        return out

    @staticmethod
    def _pick(files):
        """One file per artefact: when both .pdf and .docx exist, the Word
        version wins (its tables survive extraction intact)."""
        best = {}
        for path in files:
            key = path.stem.lower()
            current = best.get(key)
            if current is None or (current.suffix.lower() == '.pdf'
                                   and path.suffix.lower() == '.docx'):
                best[key] = path
        return sorted(best.values())

    def handle(self, *args, **options):
        """Draft each topic (or reuse its drafted pack), validate it and, with
        ``--import``, import it.

        Drafting needs AI enabled unless ``--reuse`` is given; ``--dry-run`` lists
        the
        files that would be read and reads nothing.
        """
        root = Path(options['folder'])
        if not root.is_dir():
            raise CommandError(f'No such folder: {root}')

        out_dir = Path(options['out']) if options['out'] else root / '_packs'
        topics = self._topics(root, options['only'])
        if not topics:
            raise CommandError('No topic directories found.')

        self.stdout.write(self.style.SUCCESS(
            f'{len(topics)} topic(s) in {root}'))

        if options['dry_run']:
            for entry, module, topic, files in topics:
                picked = self._pick(files)
                self.stdout.write(f'  {entry.name:14} {module:6} {topic:4} '
                                  f'{len(picked)} file(s) of {len(files)}')
                for path in picked:
                    self.stdout.write(f'      {path.name}')
            self.stdout.write('\nDry run — nothing was read and nothing imported.')
            return

        if not options['reuse'] and not content_pack.drafting.is_enabled():
            raise CommandError(
                'Drafting needs ANTHROPIC_API_KEY and ADMIN_AI_ENABLED. '
                'Use --reuse to import packs you have already drafted.')

        out_dir.mkdir(parents=True, exist_ok=True)
        drafted = imported = failed = 0
        started = time.monotonic()

        for entry, module, topic, files in topics:
            target = out_dir / f'{module.lower()}-{topic.lower()}.json'
            self.stdout.write(f'\n{entry.name}  ({module} · {topic})')

            pack = None
            if options['reuse'] and target.is_file():
                pack = json.loads(target.read_text(encoding='utf-8'))
                self.stdout.write(f'  reusing {target.name}')
            else:
                picked = self._pick(files)
                text, metas, errors = content_pack.extract_many(
                    [(p.read_bytes(), p.name) for p in picked])
                for error in errors:
                    self.stdout.write(self.style.WARNING(f'  ! {error}'))
                if not text:
                    self.stdout.write(self.style.ERROR('  nothing readable — skipped'))
                    failed += 1
                    continue
                self.stdout.write(
                    f'  read {len(metas)} file(s), {len(text):,} characters')
                try:
                    is_sitting = topic == EXAM_TOPIC
                    pack, problems = content_pack.draft_pack(
                        text, module=module, programme=options['programme'],
                        topic_code=topic,
                        topic_title=(f'{module} Test 3 mock exam' if is_sitting
                                     else f'{module} {topic}'),
                        cohort=options['cohort'],
                        note=('This is a full end-of-block sitting, not a weekly topic. '
                              'Produce a single item of type "exam".') if is_sitting else '')
                except DraftingError as exc:
                    self.stdout.write(self.style.ERROR(f'  drafting failed — {exc}'))
                    failed += 1
                    continue
                target.write_text(content_pack.as_json(pack), encoding='utf-8')
                drafted += 1
                self.stdout.write(f'  drafted → {target}')

            problems = content_pack.validate(pack)
            if problems:
                self.stdout.write(self.style.WARNING(
                    f'  {len(problems)} problem(s) — fix {target.name} then re-run with --reuse:'))
                for problem in problems[:6]:
                    self.stdout.write(f'      · {problem}')
                if len(problems) > 6:
                    self.stdout.write(f'      … and {len(problems) - 6} more')
                failed += 1
                continue

            self.stdout.write(self.style.SUCCESS('  valid'))
            if not options['do_import']:
                continue
            try:
                result = content_pack.import_pack(pack, validate=False)
            except PackImportError as exc:
                self.stdout.write(self.style.ERROR(f'  not imported — {exc}'))
                failed += 1
                continue
            imported += 1
            self.stdout.write(f'  imported: {result}')
            for warning in result.warnings:
                self.stdout.write(self.style.WARNING(f'  ! {warning}'))

        elapsed = time.monotonic() - started
        self.stdout.write(self.style.SUCCESS(
            f'\n{drafted} drafted · {imported} imported · {failed} needing attention '
            f'({elapsed:.0f}s)'))
        self.stdout.write(f'Packs are in {out_dir} — they are the record of what was read, '
                          f'and they are editable.')
        if not options['do_import']:
            self.stdout.write('Nothing was imported. Check the packs, then re-run '
                              'with --reuse --import.')
