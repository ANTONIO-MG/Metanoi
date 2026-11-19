#!/usr/bin/env python3
"""Run Metanoia Group on this server, bringing the whole stack up with it.

    python3 run.py              # serves on 0.0.0.0:8000
    python3 run.py 8080         # a different port
    PORT=80 python3 run.py      # port via the environment
    python3 run.py --check      # run the preflight only, then exit

Before serving, this starts and verifies everything the app needs:

    1. postgresql   — systemd unit, then a real Django connection
    2. redis-server — systemd unit, then a real cache + channel-layer round-trip
    3. migrations   — applies anything unapplied
    4. staticfiles  — collectstatic when the directory is missing
    5. media dirs   — created so a first upload cannot fail

Serves through gunicorn + the uvicorn ASGI worker — the same stack render.yaml
uses in production — so WebSockets (live chat, notification push) work exactly
as they do when deployed. Static files come from whitenoise inside the app
process; there is no nginx in front.

Binds 0.0.0.0, so the site answers on every address this box has: the VCN
private address, the public address, and localhost.

NOTE — .oraclevcn.com is Oracle's PRIVATE DNS zone. Those names resolve only
from inside the VCN; from the public internet you reach this box by its public
IP address instead.

Module constants
----------------
``VCN_FQDN``
    The host name the app is reachable on inside the VCN, echoed in the banner.
``REQUIRED_UNITS``
    The systemd units the app cannot run without. Both are enabled at boot, so
    starting them here is a repair step for the case where one died or was
    stopped by hand.
``RUNTIME_DIRS``
    Directories the app writes into at runtime. Absent, the first upload 500s.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
VENV_PYTHON = BASE_DIR / '.environment' / 'bin' / 'python'
GUNICORN = BASE_DIR / '.environment' / 'bin' / 'gunicorn'

VCN_FQDN = 'metanoia-testing.metanoia.oraclevcn.com'

REQUIRED_UNITS = ('postgresql', 'redis-server')

RUNTIME_DIRS = ('media', 'backups/archived_accounts')

OK, BAD = '  ok  ', ' FAIL '


def _fail(message):
    """Exit with ``message``, prefixed ``run.py:``."""
    sys.exit(f'run.py: {message}')


def _step(label, ok, detail=''):
    """Print one preflight line and return ``ok``."""
    print(f'  [{OK if ok else BAD}] {label:<24} {detail}')
    return ok


def _run_django(snippet):
    """Execute a snippet inside the venv with Django set up.

    Kept as a subprocess rather than an import because run.py itself runs under
    the system python3, which has none of the app's dependencies.
    """
    code = (
        'import os, django\n'
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
        'django.setup()\n'
    ) + snippet
    return subprocess.run(
        [str(VENV_PYTHON), '-c', code],
        cwd=BASE_DIR, capture_output=True, text=True,
    )


def ensure_units():
    """Start postgresql / redis-server if they are not already running.

    ``sudo`` runs with ``-n`` (never prompt): if it would ask for a password, a
    clear message beats a hung, silent start-up.
    """
    all_ok = True
    for unit in REQUIRED_UNITS:
        active = subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0
        if not active:
            started = subprocess.run(
                ['sudo', '-n', 'systemctl', 'start', unit],
                capture_output=True, text=True,
            )
            active = started.returncode == 0
            if not active:
                _step(unit, False, f'could not start — run: sudo systemctl start {unit}')
                all_ok = False
                continue
            _step(unit, True, 'was stopped — started it')
        else:
            _step(unit, True, 'already running')
    return all_ok


def check_database():
    """Open a real Django database connection and report the database name."""
    result = _run_django(
        'from django.db import connection\n'
        'connection.ensure_connection()\n'
        'with connection.cursor() as c:\n'
        "    c.execute('select current_database()')\n"
        "    print(c.fetchone()[0])\n"
    )
    detail = result.stdout.strip() or result.stderr.strip().splitlines()[-1:] or ['']
    return _step('database', result.returncode == 0,
                 detail if isinstance(detail, str) else detail[0])


def check_redis():
    """Cache and channel layer both, because they use different Redis DBs."""
    result = _run_django(
        'import asyncio\n'
        'from django.core.cache import cache\n'
        'from channels.layers import get_channel_layer\n'
        "cache.set('__runpy', 'ok', 30)\n"
        "assert cache.get('__runpy') == 'ok', 'cache round-trip failed'\n"
        'layer = get_channel_layer()\n'
        'async def probe():\n'
        "    await layer.send('runpy.probe', {'type': 'ping'})\n"
        "    return await layer.receive('runpy.probe')\n"
        'asyncio.run(probe())\n'
        "print('cache + channel layer')\n"
    )
    detail = result.stdout.strip() or (result.stderr.strip().splitlines() or [''])[-1]
    return _step('redis', result.returncode == 0, detail)


def apply_migrations():
    """Apply any unapplied migrations, reporting how many there were."""
    pending = _run_django(
        'from io import StringIO\n'
        'from django.core.management import call_command\n'
        'out = StringIO()\n'
        "call_command('showmigrations', '--plan', stdout=out)\n"
        "print(sum(1 for line in out.getvalue().splitlines() if line.startswith('[ ]')))\n"
    )
    if pending.returncode != 0:
        return _step('migrations', False, (pending.stderr.strip().splitlines() or [''])[-1])

    count = int(pending.stdout.strip() or 0)
    if not count:
        return _step('migrations', True, 'up to date')

    print(f'  [ .... ] migrations              applying {count}…')
    applied = subprocess.run(
        [str(VENV_PYTHON), 'manage.py', 'migrate', '--noinput'],
        cwd=BASE_DIR, capture_output=True, text=True,
    )
    return _step('migrations', applied.returncode == 0,
                 f'applied {count}' if applied.returncode == 0
                 else (applied.stderr.strip().splitlines() or [''])[-1])


def ensure_static():
    """whitenoise serves whatever is in staticfiles/, so a missing directory
    shows up as unstyled pages rather than an error — collect it once."""
    if (BASE_DIR / 'staticfiles').is_dir():
        return _step('static files', True, 'present')
    print('  [ .... ] static files            collecting…')
    collected = subprocess.run(
        [str(VENV_PYTHON), 'manage.py', 'collectstatic', '--noinput'],
        cwd=BASE_DIR, capture_output=True, text=True,
    )
    return _step('static files', collected.returncode == 0,
                 'collected' if collected.returncode == 0 else 'collectstatic failed')


def ensure_runtime_dirs():
    """Create the :data:`RUNTIME_DIRS` if they are missing."""
    for rel in RUNTIME_DIRS:
        (BASE_DIR / rel).mkdir(parents=True, exist_ok=True)
    return _step('media dirs', True, 'present')


def check_port(host, port):
    """Catch a port clash here, with the likely cause, instead of letting
    gunicorn die on bind with a bare EADDRINUSE."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, int(port)))
        return _step(f'port {port}', True, 'free')
    except OSError as exc:
        hint = ''
        if port in ('80', '443'):
            hint = ' — the systemd unit serves :80 (sudo systemctl stop metanoia)'
        return _step(f'port {port}', False, f'{exc.strerror}{hint}')
    finally:
        probe.close()


def preflight(host, port):
    """Run every preflight check; True only when all of them pass."""
    print('\n  Preflight')
    results = [
        ensure_units(),
        check_database(),
        check_redis(),
        apply_migrations(),
        ensure_static(),
        ensure_runtime_dirs(),
        check_port(host, port),
    ]
    return all(results)


def _local_ips():
    """Addresses this box answers on, best-effort, for the banner.

    A UDP ``connect`` to a public address selects the outward interface
    without sending anything.
    """
    ips = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('8.8.8.8', 80))
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith('127.'))


def _public_ip():
    """The address the public internet reaches this box on.

    The VNIC metadata does not carry one (the public address is mapped upstream
    of the instance), so ask an outside echo service and keep the timeout short
    — this is banner text, never a reason to delay start-up.
    """
    import urllib.request
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=3) as response:
            return response.read().decode().strip()
    except Exception:
        return ''


def banner(port):
    """Print the addresses the site answers on."""
    print()
    print('  Metanoia Group')
    print(f'  This machine  : http://127.0.0.1:{port}')
    for ip in _local_ips():
        print(f'  On the VCN    : http://{ip}:{port}')
    print(f'  By VCN name   : http://{VCN_FQDN}:{port}   (inside the VCN only)')
    public = _public_ip()
    if public:
        print(f'  On the internet: http://{public}:{port}')
        print(f'  Landing page  : http://{public}:{port}/social/landing/')
    print('  Admin         : add /admin/ to any of the above')
    print()
    print('  Ctrl-C to stop.')
    print()


def main():
    """Parse the port, run the preflight, then replace this process with gunicorn.

    Port 80 is refused for a non-root run: the systemd unit already serves :80
    with ``CAP_NET_BIND_SERVICE``, and a bare ``run.py`` has no such capability and
    would die with ``EACCES``. Output is flushed before ``execv``, because that
    replaces this process image immediately; when stdout is a file or a pipe
    (nohup, systemd, a log redirect) it is block-buffered, and everything printed
    would otherwise be discarded unflushed.
    """
    if not GUNICORN.exists():
        _fail(
            f'{GUNICORN} not found.\n'
            '       The virtualenv is .environment — create it and install the\n'
            '       requirements first:\n'
            '         python3 -m venv .environment\n'
            '         .environment/bin/python .install_requirements.py'
        )

    args = [a for a in sys.argv[1:] if a != '--check']
    check_only = '--check' in sys.argv[1:]

    port = os.getenv('PORT') or (args[0] if args else '8000')
    host = os.getenv('HOST', '0.0.0.0')

    if port == '80' and os.geteuid() != 0:
        _fail(
            'port 80 needs privileges this process does not have.\n'
            '       Port 80 is already served by the systemd unit:\n'
            '         sudo systemctl status metanoia\n'
            '       Use the default port instead:  python3 run.py'
        )

    if not preflight(host, port):
        _fail('preflight failed — see the FAIL lines above. Nothing was started.')

    if check_only:
        print('\n  Preflight passed. (--check: not serving.)\n')
        return

    banner(port)

    sys.stdout.flush()
    sys.stderr.flush()

    os.execv(str(GUNICORN), [
        str(GUNICORN),
        'config.asgi:application',
        '--worker-class', 'uvicorn_worker.UvicornWorker',
        '--workers', '1',
        '--bind', f'{host}:{port}',
        '--access-logfile', '-',
        '--error-logfile', '-',
    ])


if __name__ == '__main__':
    main()
