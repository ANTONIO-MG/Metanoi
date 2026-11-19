"""Provision the MetanoiaLMS OneDrive folder tree from the academic spine.

Mirrors the LMS structure onto OneDrive so documents and Teams recaps have a stable
home:

    <root>/ <Institution> / <Programme> / <Module> / <Test N> / <Week n> /
        <Topic>/                          ← study materials, one set per TOPIC
            Blueprints/  Study guides/  Study guide blueprints/  Mock exams/  Test samples/
        Recordings/                       ← per WEEK
        Session summaries/                ← per WEEK

Idempotent: every folder is created with Graph's "fail-then-read" behaviour, so a
re-run after more schedule is built simply fills in the new branches. ``--dry-run``
prints the tree without touching Graph, so you can review it before anything is
created. Dormant unless ``MS_GRAPH_*`` is configured (see docs/TEAMS_INTEGRATION.md).

``STUDY_BUCKETS`` are the folders created under each TOPIC; ``WEEK_BUCKETS``
hang off the WEEK instead, because a session can span several of the week's
topics.
"""

from django.core.management.base import BaseCommand

from apps.communication.models import _safe_folder_name
from apps.learning.models import ProgrammeModule


STUDY_BUCKETS = ['Blueprints', 'Study guides', 'Study guide blueprints',
                 'Mock exams', 'Test samples']
WEEK_BUCKETS = ['Recordings', 'Session summaries']


class Command(BaseCommand):
    """``manage.py provision_onedrive``: build (or preview) the OneDrive folder tree."""

    help = 'Create the MetanoiaLMS OneDrive folder tree from institutions/programmes/modules.'

    def add_arguments(self, parser):
        """``--dry-run``, ``--root``, ``--owner`` and the institution/programme/module filters."""
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the tree without creating anything (no Graph calls).')
        parser.add_argument('--root', default=None,
                            help='Top folder name (default: the live-session OneDrive root).')
        parser.add_argument('--owner', default=None,
                            help='UPN whose drive to build in (default: the configured owner).')
        parser.add_argument('--institution', default=None, help='Only this institution code.')
        parser.add_argument('--programme', default=None, help='Only this programme code.')
        parser.add_argument('--module', default=None, help='Only this module code.')

    def _root_name(self, opt_root):
        """The top folder: ``--root``, else the live-session OneDrive root setting."""
        if opt_root:
            return opt_root
        from apps.livesessions.models import LiveSessionSettings
        return (LiveSessionSettings.load().onedrive_root or 'Metanoia Sessions').strip()

    def _offerings(self, opts):
        """Active programme modules, narrowed by the command-line filters."""
        qs = (ProgrammeModule.objects.filter(is_active=True)
              .select_related('programme__institution', 'module')
              .order_by('programme__institution__code', 'programme__code', 'code'))
        if opts['institution']:
            qs = qs.filter(programme__institution__code=opts['institution'])
        if opts['programme']:
            qs = qs.filter(programme__code=opts['programme'])
        if opts['module']:
            qs = qs.filter(code=opts['module'])
        return qs

    def _leaf_paths(self, root, offerings):
        """Yield every folder path (list of already-sanitised segments) to ensure.

        Emits the module skeleton for every offering, then the deeper
        phase/week/topic/bucket folders wherever the schedule exists. The module
        skeleton is emitted even before any schedule exists. Each intake's blocks
        sit under their own folder (``<module>/<B26S>/Test 1/…``) — the same path
        :meth:`MeetingRoom.storage_path_parts` files sessions into.
        """
        def seg(value):
            """``value`` as a safe OneDrive folder name."""
            return _safe_folder_name(str(value))

        for off in offerings:
            inst = off.programme.institution
            base = [root, seg(getattr(inst, 'code', '') or str(inst)),
                    seg(getattr(off.programme, 'code', '') or str(off.programme)),
                    seg(off.code or off.display_name)]
            yield base
            for phase in off.phases.filter(is_active=True).select_related('cohort').order_by('order', 'id'):
                p = base + ([seg(phase.cohort.code)] if phase.cohort_id else []) + [seg(phase.short_label)]
                yield p
                for week in phase.weeks.filter(is_active=True).order_by('number'):
                    w = p + [seg(f'Week {week.number}')]
                    yield w
                    for topic in week.topic_list:
                        t = w + [seg(f'{topic.code} {topic.title}')]
                        for bucket in STUDY_BUCKETS:
                            yield t + [bucket]
                    for bucket in WEEK_BUCKETS:
                        yield w + [bucket]

    def handle(self, *args, **opts):
        """Print the tree with ``--dry-run``; otherwise create it in OneDrive."""
        root = self._root_name(opts['root'])
        offerings = list(self._offerings(opts))
        if not offerings:
            self.stdout.write(self.style.WARNING('No matching offerings — nothing to do.'))
            return

        paths = list(self._leaf_paths(root, offerings))
        unique_folders = self._unique_folders(paths)

        if opts['dry_run']:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'DRY RUN — {len(unique_folders)} folders under "{root}" '
                f'({len(offerings)} module(s))'))
            self._print_tree(paths)
            return

        self._provision(root, paths, unique_folders, opts.get('owner'))

    def _unique_folders(self, paths):
        """Every distinct folder (including intermediates) across all paths."""
        folders = set()
        for parts in paths:
            for i in range(1, len(parts) + 1):
                folders.add(tuple(parts[:i]))
        return folders

    def _print_tree(self, paths):
        """Print ``paths`` as an indented folder tree."""
        tree = {}
        for parts in paths:
            node = tree
            for part in parts:
                node = node.setdefault(part, {})

        def walk(node, depth):
            """Print ``node``'s children at ``depth``, recursively."""
            for name in sorted(node):
                self.stdout.write('  ' * depth + '📁 ' + name)
                walk(node[name], depth + 1)
        walk(tree, 0)

    def _provision(self, root, paths, unique_folders, owner):
        """Create every folder in the owner's drive.

        Folders are created shallow-to-deep so a parent always exists before
        its child, and folder ids are cached by path so shared ancestors are
        created once.
        """
        from apps.msteams import graph
        if not graph.is_configured():
            self.stdout.write(self.style.ERROR(
                'Microsoft Graph is not configured (set MS_TEAMS_ENABLED=true and the '
                'MS_GRAPH_* credentials). Nothing created.'))
            return
        if owner is None:
            from apps.livesessions.models import LiveSessionSettings
            from django.conf import settings as dj
            owner = (LiveSessionSettings.load().onedrive_owner_upn
                     or getattr(dj, 'MS_GRAPH_DEFAULT_ORGANIZER', '') or '').strip()
        if not owner:
            self.stdout.write(self.style.ERROR('No drive owner UPN configured. Nothing created.'))
            return

        drive_id = graph.get_drive_id(owner)
        if not drive_id:
            self.stdout.write(self.style.ERROR(f'Could not resolve the drive for {owner}.'))
            return
        root_item = graph.get_item_by_path(drive_id, '')
        if not root_item or not root_item.get('id'):
            self.stdout.write(self.style.ERROR('Could not read the drive root.'))
            return

        cache = {(): root_item['id']}
        created = 0
        failed = 0
        for parts in sorted(unique_folders, key=len):
            parent_id = cache[tuple(parts[:-1])]
            node = graph.create_folder(drive_id, parent_id, parts[-1])
            if node and node.get('id'):
                cache[tuple(parts)] = node['id']
                created += 1
            else:
                failed += 1
                self.stdout.write(self.style.WARNING('  could not create: ' + '/'.join(parts)))

        self.stdout.write(self.style.SUCCESS(
            f'OneDrive provisioned under "{root}" for {owner}: '
            f'{created} folder(s) ensured' + (f', {failed} failed' if failed else '') + '.'))
