"""Turn the schedule's PDF papers into assessments a student can sit on the system.

    python manage.py build_paper_assessments              # every offering
    python manage.py build_paper_assessments --offering 3
    python manage.py build_paper_assessments --dry-run    # report only, write nothing

Each sitting's published PDFs become one practice
:class:`~apps.assessments.models.Assessment`; see :mod:`apps.assessments.papers`.

Re-runnable: a sitting is found again by its ``source_ref``. Its questions are
rebuilt while nobody has attempted it; once someone has, the existing questions
are updated in place (text, marks, memo) so no answer loses its question.

Built quietly: the papers were announced when they were placed on the schedule,
so the assessment is opened with a queryset update rather than a save — no
second "is open" notification, no task fanned out per student.

``MIN_AGREEMENT`` is the threshold below which a memo's restated requirements do not
read like this paper's, so it is not attached as the memo.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.assessments import models, papers
from apps.learning.file_check import file_is_complete
from apps.learning.models import ModuleMaterial, ProgrammeModule

PAPER_KINDS = ('mock_exam', 'questions')
SOLUTION_KINDS = ('mock_exam', 'questions', 'answers')
MIN_AGREEMENT = 0.2

DESCRIPTION = ('Built from the paper on your module schedule. Read the scenario, answer each '
               'part in your own words and submit. The suggested solution appears beside '
               'each question once you have submitted and the solution has been released.')


class Command(BaseCommand):
    """Management command: build or refresh one practice assessment per paper sitting.
    """
    help = 'Build sittable practice assessments from the PDF papers on module schedules.'

    def add_arguments(self, parser):
        """Add ``--offering`` (one ProgrammeModule id) and ``--dry-run``."""
        parser.add_argument('--offering', type=int, help='Only this ProgrammeModule id.')
        parser.add_argument('--dry-run', action='store_true', help='Report only; write nothing.')

    def handle(self, *args, **opts):
        """Walk every (or the chosen) offering, build its sittings and print a summary.
        """
        self.dry = opts['dry_run']
        self._text = {}
        self.stats = defaultdict(int)
        self.skipped = []
        offerings = ProgrammeModule.objects.select_related('module').order_by('pk')
        if opts.get('offering'):
            offerings = offerings.filter(pk=opts['offering'])
        for offering in offerings:
            self._offering(offering)

        self.stdout.write('')
        for key in ('created', 'updated', 'withdrawn', 'questions', 'with_memo', 'memo_answers'):
            self.stdout.write(f'{key:>13}: {self.stats[key]}')
        if self.skipped:
            self.stdout.write(self.style.WARNING(f'\nSkipped {len(self.skipped)}:'))
            for offering, title, reason in self.skipped:
                self.stdout.write(f'  [{offering}] {title} — {reason}')

    def text(self, material):
        """Return the extracted text of a material's PDF, cached per file name for this
        run.
        """
        name = material.file.name
        if name not in self._text:
            self._text[name] = papers.extract_text(material.file)
        return self._text[name]

    def _offering(self, offering):
        """Group an offering's PDF papers into sittings and build each one.

        Paper files are grouped by pair key into sittings, solutions by paper key, and
        the titles of cut-short paper files are tracked per sitting. The command also
        counts how many sittings share each paper key: a solution that does not match
        part by part is only attached when it can belong to one sitting.
        """
        code = getattr(offering.module, 'code', '') or ''
        rows = list(ModuleMaterial.objects
                    .filter(phase__programme_module=offering, kind__in=SOLUTION_KINDS)
                    .exclude(file='').select_related('phase', 'week').order_by('pk'))
        rows = [m for m in rows if m.file.name.lower().endswith('.pdf')]
        if not rows:
            return

        sittings = defaultdict(list)
        solutions = defaultdict(list)
        truncated = defaultdict(set)
        for material in rows:
            if not file_is_complete(material.file):
                is_paper = (material.kind in PAPER_KINDS and material.is_published
                            and material.audience == material.AUDIENCE_STUDENTS
                            and papers.role_of(material) != 'solution')
                if is_paper:
                    truncated[papers.pair_key(material.title)].add(material.title)
                continue
            role = papers.role_of(material, self.text(material))
            if role == 'solution':
                solutions[papers.paper_key(material.title, code)].append(material)
            elif (material.kind in PAPER_KINDS and material.is_published
                  and material.audience == material.AUDIENCE_STUDENTS):
                sittings[papers.pair_key(material.title)].append((role, material))

        per_paper_key = defaultdict(int)
        for members in sittings.values():
            per_paper_key[papers.paper_key(members[0][1].title, code)] += 1

        for key, members in sorted(sittings.items()):
            cut = sorted(truncated.pop(key, ()))
            built = self._sitting(offering, code, key, members, solutions, per_paper_key, cut)
            if not built:
                self._withdraw(offering, key)
        for key, titles in sorted(truncated.items()):
            for title in sorted(titles):
                self.skipped.append((offering.pk, title, 'PDF is truncated (needs re-upload)'))

    def _pick(self, members, role):
        """The file of a role to read: plain before "with comments", then the
        one with the most parts."""
        found = [m for r, m in members if r == role]
        found.sort(key=lambda m: ('comment' in m.title.lower(), m.pk))
        return found

    def _withdraw(self, offering, key):
        """A sitting that can no longer be built (its file was cut short, say)
        loses the assessment an earlier run made — unless someone has sat it."""
        assessment = models.Assessment.objects.filter(source_ref=papers.ref_for(offering.pk, key)).first()
        if assessment is None or self.dry:
            return
        if assessment.attempts.exists():
            self.stdout.write(f'[{offering.pk}] kept (has attempts): {assessment.title}')
            return
        assessment.delete()
        self.stats['withdrawn'] += 1

    def _sitting(self, offering, code, key, members, solutions, per_paper_key, cut=()):
        """Parse one sitting's papers into parts and a scenario, pick its memo and save it.

        A "question" file can carry the whole case before its REQUIRED heading; a cover
        page is not a scenario. Half a paper is not a paper: when the scenario's file is
        cut short the parts cannot be answered, so the sitting waits for the re-upload —
        unless a complete file of the same sitting carried the case anyway.

        The memo is the solution file whose parts agree best with this paper's. When
        there is one solution for one sitting it is this paper's even if its parts could
        not be lined up — the PDF is still linked from the result.
        """
        required = self._pick(members, 'required')
        combined = self._pick(members, 'combined')
        scenario = self._pick(members, 'scenario')
        lead = (required or combined or scenario)[0]

        parts, scenario_text, read_from = [], '', None
        for material in required:
            found = papers.parse_parts(self.text(material))
            if len(found) > len(parts):
                parts, read_from = found, material
                with_lead, before = papers.parse_parts_with_lead(self.text(material))
                scenario_text = before if (len(with_lead) == len(found) and len(before) > 2000) else ''
        if not parts:
            for material in combined + scenario:
                text = self.text(material)
                found, before = papers.parse_parts_with_lead(text)
                if len(found) > len(parts):
                    parts, read_from, scenario_text = found, material, before
        if not parts:
            self.skipped.append((offering.pk, lead.title, 'no numbered requirements found in the text'))
            return False
        if scenario and not scenario_text:
            scenario_text = papers.reflow(papers.clean_lines(self.text(scenario[0])))
        if cut and not scenario_text:
            for title in cut:
                self.skipped.append((offering.pk, title, 'PDF is truncated (needs re-upload); '
                                                         'its paper is not built until it is'))
            return False
        total = papers.paper_total(self.text(read_from), parts)

        paper_key = papers.paper_key(lead.title, code)
        memo, agreement, memo_files = {}, 0.0, []
        candidates = solutions.get(paper_key, [])
        by_file = {}
        for material in candidates:
            by_file.setdefault(material.file.name, []).append(material)
        best = None
        for name, copies in by_file.items():
            found, score = papers.memo_for_parts(self.text(copies[0]), parts)
            if found and score >= MIN_AGREEMENT and (
                    best is None or (len(found), score) > (len(best[0]), best[1])):
                best = (found, score, copies)
        if best:
            memo, agreement, memo_files = best
        elif len(by_file) == 1 and per_paper_key[paper_key] == 1:
            memo_files = next(iter(by_file.values()))

        paper_materials = [m for _r, m in members]
        title = papers.tidy_title(lead.title) or lead.title
        kind = papers.kind_for(lead.title, lead.kind)
        self._save(offering, key, title, kind, total, scenario_text, parts, memo, agreement,
                   paper_materials, memo_files)
        return True

    def _save(self, offering, key, title, kind, total, scenario_text, parts, memo, agreement,
              paper_materials, memo_files):
        """Create or update the sitting's assessment (or only report it on a dry run).

        A new assessment is saved as a draft so the open/scheduled hooks stay quiet,
        then opened without a save (see the module docstring).
        """
        ref = papers.ref_for(offering.pk, key)
        verb = 'would build' if self.dry else 'built'
        memo_note = f', memo {len(memo)}/{len(parts)} ({agreement:.2f})' if memo else (
            ', solution PDF only' if memo_files else ', no solution yet')
        self.stdout.write(f'[{offering.pk}] {verb}: {title} · {kind} · {len(parts)} parts · '
                          f'{total} marks{memo_note}')
        self.stats['questions'] += len(parts)
        if memo:
            self.stats['with_memo'] += 1
            self.stats['memo_answers'] += len(memo)
        if self.dry:
            self.stats['created'] += 1
            return

        with transaction.atomic():
            assessment = models.Assessment.objects.filter(source_ref=ref).first()
            values = dict(
                title=title[:200], kind=kind, module=offering, total_marks=total,
                description=DESCRIPTION, pass_mark_pct=50, time_limit_minutes=0,
                attempts_allowed=0, release_solution=models.Assessment.RELEASE_ON_SUBMIT,
                proctoring_enabled=False)
            if assessment is None:
                assessment = models.Assessment.objects.create(source_ref=ref, status='draft', **values)
                self.stats['created'] += 1
            else:
                models.Assessment.objects.filter(pk=assessment.pk).update(**values)
                self.stats['updated'] += 1
            models.Assessment.objects.filter(pk=assessment.pk).update(status='open')

            section = assessment.sections.order_by('order').first()
            if section is None:
                section = models.Section.objects.create(assessment=assessment, title='Required', order=0)
            models.Section.objects.filter(pk=section.pk).update(
                title='Required', instructions=scenario_text)

            attempted = assessment.attempts.exists()
            existing = {(q.config or {}).get('label'): q
                        for q in models.Question.objects.filter(section__assessment=assessment)}
            if not attempted:
                models.Question.objects.filter(section__assessment=assessment).delete()
                existing = {}
            for order, part in enumerate(parts):
                values = dict(
                    type='long', text=part['text'], marks=part['marks'] or 0, order=order,
                    marking_mode=models.Question.MARKING_MANUAL,
                    guidance=memo.get(part['key'], ''),
                    config={'label': part['label'], 'source': 'paper'})
                question = existing.get(part['label'])
                if question is None:
                    models.Question.objects.create(section=section, **values)
                else:
                    models.Question.objects.filter(pk=question.pk).update(**values)

            source, _ = models.PaperSource.objects.get_or_create(assessment=assessment)
            source.memo_agreement = agreement if memo else None
            source.memo_parts = len(memo)
            source.save()
            source.source_materials.set(paper_materials)
            source.solution_materials.set(memo_files)
