"""System monitor: the server's CPU, memory, disk and network, and the site's traffic.

Three parts, none of which needs an extra service or a paid AWS feature:

* :class:`TrafficMiddleware` counts every page request into per-minute Redis
  buckets: requests, 4xx / 5xx, total and slow response time, the signed-in
  users and anonymous visitors (a HyperLogLog of a hashed IP), and which
  page (URL name) it was. One pipelined round trip to the local Redis; it can
  never break a request.
* :func:`record_sample` (the ``system-sample`` scheduler job, every five
  minutes) reads ``/proc`` and the minute buckets and stores one
  :class:`~apps.diagnostics.models.SystemSample` row, so history survives a
  Redis flush and goes back 90 days.
* :func:`live` and :func:`history` feed the developer page
  (``/diagnostics/system/``) and the "Server" panel on the staff dashboard.

The numbers are the instance's own (what ``top``, ``free`` and ``df`` show).
CloudWatch reports CPU and network for the same machine but not memory or disk
without its agent, so reading them here is both cheaper and more complete.

Module settings:

- ``BUCKET_TTL`` -- how long minute buckets live in Redis (three days; the
  samples keep the long history).
- ``KEEP_DAYS`` -- samples older than this (90 days) are pruned.
- ``SLOW_MS`` -- a response slower than this (2 seconds) counts as slow.
- ``SKIP_PREFIXES`` -- paths that are not "traffic": static files, health
  checks and the monitor's own polling.
- ``WARN`` / ``BAD`` -- percentage thresholds for the colour of a gauge and
  the advice list.
- ``RANGES`` -- the history spans the system page offers.

The store is Redis when the cache is django-redis, and an in-process stand-in
(:class:`_MemoryStore`) otherwise.
"""

import hashlib
import logging
import os
import shutil
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('apps.diagnostics')

BUCKET_TTL = 3 * 24 * 3600
KEEP_DAYS = 90
SLOW_MS = 2000
SKIP_PREFIXES = ('/static/', '/favicon', '/robots.txt', '/healthz', '/diagnostics/system/live')

WARN, BAD = 70, 85


class _MemoryStore:
    """Enough of Redis for tests and a laptop (locmem cache)."""

    def __init__(self):
        self.data = {}

    def pipeline(self, transaction=False):
        """Return a pipeline that queues calls until ``execute``."""
        return _MemoryPipeline(self)

    def hincrby(self, key, field, amount=1):
        """Increment a hash field."""
        bucket = self.data.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount

    def hgetall(self, key):
        """Return a hash as bytes, as redis-py does."""
        return {k.encode(): str(v).encode() for k, v in self.data.get(key, {}).items()}

    def sadd(self, key, *values):
        """Add members to a set (also used for ``pfadd``)."""
        self.data.setdefault(key, set()).update(str(v) for v in values)

    pfadd = sadd

    def pfcount(self, *keys):
        """Count distinct members across sets (stands in for a HyperLogLog)."""
        return len(set().union(*(self.data.get(k, set()) for k in keys)))

    def sunion(self, *keys):
        """Return the union of sets as bytes."""
        return {v.encode() for v in set().union(*(self.data.get(k, set()) for k in keys))}

    def zincrby(self, key, amount, member):
        """Increment a sorted-set member."""
        bucket = self.data.setdefault(key, {})
        bucket[member] = bucket.get(member, 0) + amount

    def zrevrange(self, key, start, end, withscores=False):
        """Return sorted-set members, highest score first."""
        rows = sorted(self.data.get(key, {}).items(), key=lambda kv: -kv[1])[start:end + 1]
        return [(k.encode(), float(v)) for k, v in rows]

    def expire(self, key, seconds):
        """No-op: the in-memory store never expires keys."""
        pass

    def info(self, section=None):
        """Return no server info."""
        return {}

    def dbsize(self):
        """Return the number of keys."""
        return len(self.data)


class _MemoryPipeline:
    """Records pipelined calls on a :class:`_MemoryStore` and replays them on ``execute``."""
    def __init__(self, store):
        """Wrap ``store`` with an empty call queue."""
        self.store, self.calls = store, []

    def __getattr__(self, name):
        """Return a recorder that queues the named store call."""
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return call

    def execute(self):
        """Run the queued calls against the store and return their results."""
        return [getattr(self.store, name)(*args, **kwargs) for name, args, kwargs in self.calls]


_memory = _MemoryStore()


def store():
    """The Redis connection from django-redis, or the in-process store when that is unavailable."""
    try:
        from django_redis import get_redis_connection
        return get_redis_connection('default')
    except Exception:
        return _memory


def _minute(ts=None):
    """The minute number (Unix time // 60) of ``ts``, or of now."""
    return int((ts if ts is not None else time.time()) // 60)


def _client_ip(request):
    """The first ``X-Forwarded-For`` address, falling back to ``REMOTE_ADDR``."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (forwarded.split(',')[0].strip() if forwarded else '') or request.META.get('REMOTE_ADDR', '')


def count_request(request, status, ms):
    """Add one request to this minute's bucket (never raises).

    Any failure is logged at DEBUG and swallowed: monitoring must never cost a page.
    """
    try:
        path = request.path or ''
        if path.startswith(SKIP_PREFIXES):
            return
        minute = _minute()
        hour = minute // 60
        user = getattr(request, 'user', None)
        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name:
            page = f'{match.namespace}:{match.url_name}' if match.namespace else match.url_name
        elif path.startswith('/media/'):
            page = 'media files'
        else:
            page = 'unmatched'
        pipe = store().pipeline(transaction=False)
        key = f'mon:m:{minute}'
        pipe.hincrby(key, 'n', 1)
        pipe.hincrby(key, 'ms', int(ms))
        if status >= 500:
            pipe.hincrby(key, 'e5', 1)
        elif status >= 400:
            pipe.hincrby(key, 'e4', 1)
        if ms >= SLOW_MS:
            pipe.hincrby(key, 'slow', 1)
        pipe.expire(key, BUCKET_TTL)
        if user is not None and getattr(user, 'is_authenticated', False):
            pipe.sadd(f'mon:u:{minute}', user.pk)
            pipe.expire(f'mon:u:{minute}', BUCKET_TTL)
        else:
            visitor = hashlib.sha1(_client_ip(request).encode()).hexdigest()[:16]
            pipe.pfadd(f'mon:v:{minute}', visitor)
            pipe.expire(f'mon:v:{minute}', BUCKET_TTL)
        pipe.zincrby(f'mon:p:{hour}', 1, page)
        pipe.expire(f'mon:p:{hour}', BUCKET_TTL)
        pipe.execute()
    except Exception:
        logger.debug('monitor: could not count a request', exc_info=True)


class TrafficMiddleware:
    """Times every request and counts it (see :func:`count_request`)."""

    def __init__(self, get_response):
        """Store the next middleware or view in the chain."""
        self.get_response = get_response

    def __call__(self, request):
        """Time the request and count it, including requests that raise (counted as 500)."""
        started = time.monotonic()
        try:
            response = self.get_response(request)
        except Exception:
            count_request(request, 500, (time.monotonic() - started) * 1000)
            raise
        count_request(request, getattr(response, 'status_code', 200), (time.monotonic() - started) * 1000)
        return response


def traffic(start_minute, end_minute):
    """Totals over ``[start_minute, end_minute]`` from the minute buckets."""
    out = {'requests': 0, 'errors': 0, 'client_errors': 0, 'slow': 0, 'ms': 0, 'users': 0, 'visitors': 0}
    minutes = list(range(int(start_minute), int(end_minute) + 1))
    if not minutes:
        return out
    try:
        r = store()
        pipe = r.pipeline(transaction=False)
        for m in minutes:
            pipe.hgetall(f'mon:m:{m}')
        for bucket in pipe.execute():
            got = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in (bucket or {}).items()}
            out['requests'] += got.get('n', 0)
            out['errors'] += got.get('e5', 0)
            out['client_errors'] += got.get('e4', 0)
            out['slow'] += got.get('slow', 0)
            out['ms'] += got.get('ms', 0)
        out['users'] = len(r.sunion(*[f'mon:u:{m}' for m in minutes]) or ())
        out['visitors'] = int(r.pfcount(*[f'mon:v:{m}' for m in minutes]) or 0)
    except Exception:
        logger.debug('monitor: could not read traffic', exc_info=True)
    out['avg_ms'] = round(out['ms'] / out['requests']) if out['requests'] else 0
    return out


def top_pages(hours=24, limit=10):
    """The most requested pages (by URL name) over the last ``hours``."""
    now_hour = _minute() // 60
    totals = {}
    try:
        r = store()
        pipe = r.pipeline(transaction=False)
        for h in range(now_hour - hours + 1, now_hour + 1):
            pipe.zrevrange(f'mon:p:{h}', 0, 49, withscores=True)
        for rows in pipe.execute():
            for name, score in rows or ():
                name = name.decode() if isinstance(name, bytes) else name
                totals[name] = totals.get(name, 0) + int(score)
    except Exception:
        logger.debug('monitor: could not read pages', exc_info=True)
    return sorted(totals.items(), key=lambda kv: -kv[1])[:limit]


def _read(path):
    """Read a ``/proc`` file, returning an empty string when it cannot be read."""
    try:
        with open(path, encoding='ascii', errors='replace') as fh:
            return fh.read()
    except OSError:
        return ''


def cpu_jiffies():
    """``(busy, total)`` CPU jiffies since boot, all cores.

    Idle is idle plus iowait; the total is the first eight fields, since guest
    time is already inside user.
    """
    line = _read('/proc/stat').split('\n', 1)[0].split()
    if not line or line[0] != 'cpu':
        return 0, 0
    values = [int(v) for v in line[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values[:8])
    return total - idle, total


def cpu_percent(before, after):
    """CPU busy percentage between two :func:`cpu_jiffies` readings."""
    busy, total = after[0] - before[0], after[1] - before[1]
    return round(100.0 * busy / total, 1) if total > 0 else 0.0


def memory():
    """Memory and swap in MB from ``/proc/meminfo``, with the percentage in use."""
    info = {}
    for line in _read('/proc/meminfo').splitlines():
        name, _, rest = line.partition(':')
        try:
            info[name] = int(rest.split()[0]) // 1024
        except (IndexError, ValueError):
            continue
    total = info.get('MemTotal', 0)
    available = info.get('MemAvailable', info.get('MemFree', 0))
    swap_total = info.get('SwapTotal', 0)
    return {'total_mb': total, 'used_mb': max(total - available, 0), 'available_mb': available,
            'cache_mb': info.get('Cached', 0) + info.get('Buffers', 0),
            'swap_total_mb': swap_total, 'swap_used_mb': max(swap_total - info.get('SwapFree', 0), 0),
            'pct': round(100.0 * (total - available) / total, 1) if total else 0.0}


def disks():
    """Usage of the root filesystem and, when it is a different one, the media folder."""
    seen, out = set(), []
    for label, path in (('System disk', '/'), ('Uploads (media)', str(settings.MEDIA_ROOT))):
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        gb = 1024 ** 3
        out.append({'label': label, 'path': path, 'total_gb': round(usage.total / gb, 1),
                    'used_gb': round(usage.used / gb, 1), 'free_gb': round(usage.free / gb, 1),
                    'pct': round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0})
    return out


def network_bytes():
    """``(received, sent)`` bytes since boot on every interface except loopback."""
    rx = tx = 0
    for line in _read('/proc/net/dev').splitlines()[2:]:
        name, _, rest = line.partition(':')
        if name.strip() == 'lo':
            continue
        fields = rest.split()
        if len(fields) >= 9:
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def load_average():
    """The 1, 5 and 15 minute load averages (zeros when unavailable)."""
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except OSError:
        return [0.0, 0.0, 0.0]


def uptime_seconds():
    """Seconds since boot, from ``/proc/uptime``."""
    try:
        return int(float(_read('/proc/uptime').split()[0]))
    except (IndexError, ValueError):
        return 0


def app_processes():
    """The site's worker processes (gunicorn) and their memory, from /proc."""
    workers, rss = 0, 0
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            cmd = _read(f'/proc/{pid}/cmdline')
            if 'gunicorn' in cmd and 'config.asgi' in cmd and '8000' in cmd:
                workers += 1
                for line in _read(f'/proc/{pid}/status').splitlines():
                    if line.startswith('VmRSS:'):
                        rss += int(line.split()[1]) // 1024
    except OSError:
        pass
    return {'processes': workers, 'rss_mb': rss}


def database():
    """PostgreSQL database size (MB) and connection count; ``None`` values on other databases."""
    from django.db import connection
    out = {'size_mb': None, 'connections': None, 'vendor': connection.vendor}
    if connection.vendor != 'postgresql':
        return out
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_database_size(current_database()), '
                           '(SELECT count(*) FROM pg_stat_activity WHERE datname = current_database())')
            size, conns = cursor.fetchone()
        out.update(size_mb=round(size / 1024 ** 2), connections=conns)
    except Exception:
        logger.debug('monitor: could not read the database size', exc_info=True)
    return out


def redis_info():
    """Redis memory in use (MB), or ``None`` when unknown."""
    try:
        info = store().info('memory') or {}
        return {'used_mb': round(info.get('used_memory', 0) / 1024 ** 2, 1) if info else None}
    except Exception:
        return {'used_mb': None}


def instance():
    """The EC2 instance type and zone (from the instance metadata service), cached for a day."""
    from django.core.cache import cache
    found = cache.get('mon:instance')
    if found is not None:
        return found
    found = {}
    try:
        import urllib.request
        base = 'http://169.254.169.254/latest'
        token = urllib.request.urlopen(urllib.request.Request(
            f'{base}/api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}),
            timeout=0.5).read().decode()
        for key, path in (('type', 'instance-type'), ('zone', 'placement/availability-zone')):
            found[key] = urllib.request.urlopen(urllib.request.Request(
                f'{base}/meta-data/{path}', headers={'X-aws-ec2-metadata-token': token}), timeout=0.5).read().decode()
    except Exception:
        found = {}
    cache.set('mon:instance', found, 24 * 3600)
    return found


def level(pct):
    """The gauge level for a percentage: ``'ok'``, ``'warn'`` or ``'bad'``."""
    return 'bad' if pct >= BAD else 'warn' if pct >= WARN else 'ok'


def record_sample():
    """Store one :class:`SystemSample`; the ``system-sample`` job runs this every 5 minutes.

    CPU and network are averaged since the last sample, unless that was long
    ago (30 minutes) or before a reboot (the counters went backwards); then the
    CPU gets a short look now and the network deltas are zero.
    """
    from .models import SystemSample

    now = timezone.now()
    last = SystemSample.objects.order_by('-at').first()
    raw = last.raw if last and isinstance(last.raw, dict) else {}
    jiffies = cpu_jiffies()
    rx, tx = network_bytes()
    fresh = last is not None and (now - last.at) < timedelta(minutes=30) and jiffies[1] >= raw.get('cpu_total', 0)
    if fresh and raw.get('cpu_total'):
        cpu = cpu_percent((raw['cpu_busy'], raw['cpu_total']), jiffies)
    else:
        time.sleep(0.5)
        cpu = cpu_percent(jiffies, cpu_jiffies())
    net_rx = max(rx - raw.get('net_rx', rx), 0) if fresh else 0
    net_tx = max(tx - raw.get('net_tx', tx), 0) if fresh else 0
    start = _minute(last.at.timestamp()) + 1 if fresh else _minute() - 4
    end = _minute()
    seen = traffic(start, end - 1) if end > start else traffic(end - 1, end - 1)
    mem = memory()
    disk = disks()[0] if disks() else {'used_gb': 0, 'total_gb': 0}
    sample = SystemSample.objects.create(
        at=now, cpu_pct=cpu, load1=load_average()[0], mem_used_mb=mem['used_mb'], mem_total_mb=mem['total_mb'],
        swap_used_mb=mem['swap_used_mb'], disk_used_gb=disk['used_gb'], disk_total_gb=disk['total_gb'],
        net_rx_bytes=net_rx, net_tx_bytes=net_tx, requests=seen['requests'], errors=seen['errors'],
        client_errors=seen['client_errors'], slow=seen['slow'], avg_ms=seen['avg_ms'], users=seen['users'],
        visitors=seen['visitors'], minutes=max(end - start, 1),
        raw={'cpu_busy': jiffies[0], 'cpu_total': jiffies[1], 'net_rx': rx, 'net_tx': tx})
    pruned, _ = SystemSample.objects.filter(at__lt=now - timedelta(days=KEEP_DAYS)).delete()
    return (f'cpu {cpu}% · ram {mem["pct"]}% · disk {disk.get("pct", "?")}% · '
            f'{seen["requests"]} requests, {seen["users"]} users' + (f' · pruned {pruned}' if pruned else '')
            ) if sample else ''


def live():
    """Right now: one short CPU look, memory, disks, the last 5 / 15 / 60 minutes of traffic."""
    before = cpu_jiffies()
    time.sleep(0.25)
    cpu = cpu_percent(before, cpu_jiffies())
    now = _minute()
    mem = memory()
    found = {
        'cpu': cpu, 'cores': os.cpu_count() or 1, 'load': load_average(), 'memory': mem, 'disks': disks(),
        'uptime': uptime_seconds(), 'app': app_processes(),
        'last5': traffic(now - 4, now), 'last15': traffic(now - 14, now), 'last60': traffic(now - 59, now),
    }
    found['load_pct'] = round(100.0 * found['load'][0] / found['cores'], 1)
    found['levels'] = {'cpu': level(cpu), 'memory': level(mem['pct']),
                       'load': level(found['load_pct']),
                       **{f'disk{i}': level(d['pct']) for i, d in enumerate(found['disks'])}}
    return found


RANGES = {'24h': timedelta(hours=24), '7d': timedelta(days=7), '30d': timedelta(days=30), '90d': timedelta(days=90)}


def history(span='24h', points=144):
    """Samples over ``span``, grouped into at most ``points`` buckets, for the charts.

    Requests are stored per minute, so a 5-minute and a 1-hour bucket read on
    one scale.
    """
    from .models import SystemSample

    delta = RANGES.get(span, RANGES['24h'])
    since = timezone.now() - delta
    rows = list(SystemSample.objects.filter(at__gte=since).order_by('at'))
    step = max(delta.total_seconds() / points, 300)
    buckets = {}
    for row in rows:
        buckets.setdefault(int((row.at - since).total_seconds() // step), []).append(row)
    series = {k: [] for k in ('labels', 'cpu', 'mem', 'disk', 'load', 'requests', 'users', 'visitors',
                              'errors', 'avg_ms', 'net_in', 'net_out')}
    local = timezone.get_current_timezone()
    for index in sorted(buckets):
        group = buckets[index]
        series['labels'].append(timezone.localtime(group[-1].at, local).strftime('%d %b %H:%M'))
        series['cpu'].append(round(max(r.cpu_pct for r in group), 1))
        series['mem'].append(round(max(r.mem_pct for r in group), 1))
        series['disk'].append(round(group[-1].disk_pct, 1))
        series['load'].append(round(max(r.load1 for r in group), 2))
        minutes = sum(r.minutes for r in group) or 1
        series['requests'].append(round(sum(r.requests for r in group) / minutes, 1))
        series['users'].append(max(r.users for r in group))
        series['visitors'].append(max(r.visitors for r in group))
        series['errors'].append(sum(r.errors for r in group))
        timed = [r for r in group if r.requests]
        series['avg_ms'].append(round(sum(r.avg_ms * r.requests for r in timed) /
                                      max(sum(r.requests for r in timed), 1)))
        series['net_in'].append(round(sum(r.net_rx_bytes for r in group) / 1024 ** 2, 1))
        series['net_out'].append(round(sum(r.net_tx_bytes for r in group) / 1024 ** 2, 1))
    return {'span': span, 'series': series, 'summary': summarise(rows)}


def _p95(values):
    """The 95th percentile of ``values`` (0 when empty)."""
    values = sorted(values)
    return values[min(int(len(values) * 0.95), len(values) - 1)] if values else 0


def summarise(rows):
    """Peaks, p95s, totals and the busiest hours over a list of samples."""
    if not rows:
        return {}
    local = timezone.get_current_timezone()
    by_hour = {h: [] for h in range(24)}
    by_day = {}
    for r in rows:
        at = timezone.localtime(r.at, local)
        by_hour[at.hour].append(r.requests / (r.minutes or 1))
        by_day[at.date()] = by_day.get(at.date(), 0) + r.requests
    requests = sum(r.requests for r in rows)
    return {
        'samples': len(rows), 'since': rows[0].at,
        'cpu_peak': max(r.cpu_pct for r in rows), 'cpu_p95': _p95([r.cpu_pct for r in rows]),
        'cpu_avg': round(sum(r.cpu_pct for r in rows) / len(rows), 1),
        'mem_peak': max(r.mem_pct for r in rows), 'mem_p95': _p95([r.mem_pct for r in rows]),
        'swap_peak_mb': max(r.swap_used_mb for r in rows),
        'disk_now': rows[-1].disk_pct, 'disk_first': rows[0].disk_pct,
        'disk_growth_gb': round(rows[-1].disk_used_gb - rows[0].disk_used_gb, 2),
        'requests': requests, 'errors': sum(r.errors for r in rows),
        'error_rate': round(100.0 * sum(r.errors for r in rows) / requests, 2) if requests else 0,
        'slow': sum(r.slow for r in rows),
        'peak_users': max(r.users for r in rows), 'peak_rpm': round(max(r.requests / (r.minutes or 1)
                                                                       for r in rows), 1),
        'net_out_gb': round(sum(r.net_tx_bytes for r in rows) / 1024 ** 3, 2),
        'net_in_gb': round(sum(r.net_rx_bytes for r in rows) / 1024 ** 3, 2),
        'hours': [round(sum(v) / len(v), 1) if v else 0 for _h, v in sorted(by_hour.items())],
        'per_day': sorted((d.isoformat(), n) for d, n in by_day.items()),
    }


def advice(now, summary):
    """Plain-language pointers for sizing decisions, from what the numbers show."""
    tips = []
    mem, cpu = now['memory'], now['cpu']
    if summary.get('mem_p95', mem['pct']) >= BAD or mem['swap_used_mb'] > 256:
        tips.append(('bad', 'Memory is tight', f'RAM in use reached {summary.get("mem_peak", mem["pct"])}% '
                     f'and swap holds {mem["swap_used_mb"]} MB. Move to an instance with more RAM, or run '
                     'fewer web workers.'))
    elif summary.get('mem_p95', 0) >= WARN:
        tips.append(('warn', 'Memory is getting busy', f'95% of the time RAM in use stays under '
                     f'{summary["mem_p95"]}%. Keep an eye on it before adding features or students.'))
    if summary.get('cpu_p95', cpu) >= BAD:
        tips.append(('bad', 'CPU is the bottleneck', f'95% of samples were under {summary["cpu_p95"]}% CPU, '
                     'so the busy times are flat out. More vCPUs (or a compute-optimised type) would help.'))
    elif summary.get('cpu_p95', 0) < 20 and summary.get('mem_p95', 100) < 50 and summary.get('samples', 0) > 1000:
        tips.append(('ok', 'There is room to spare', f'CPU stays under {summary["cpu_p95"]}% and RAM under '
                     f'{summary["mem_p95"]}% 95% of the time. A smaller instance could carry this traffic '
                     'if cost matters more than headroom.'))
    for d in now['disks']:
        if d['pct'] >= WARN:
            tips.append(('bad' if d['pct'] >= BAD else 'warn', f'{d["label"]} is {d["pct"]}% full',
                         f'{d["free_gb"]} GB left. Grow the EBS volume, or move old backups and '
                         'recordings off the server.'))
    growth = summary.get('disk_growth_gb') or 0
    if growth > 0 and now['disks'] and summary.get('since'):
        days = max((timezone.now() - summary['since']).total_seconds() / 86400, 1)
        per_day = growth / days
        if per_day > 0.01:
            left = now['disks'][0]['free_gb'] / per_day
            if left < 120:
                tips.append(('warn', f'Disk full in about {int(left)} days',
                             f'It is growing {per_day:.2f} GB a day at the current rate.'))
    if summary.get('error_rate', 0) >= 1:
        tips.append(('bad', 'Server errors', f'{summary["error_rate"]}% of requests failed with a 5xx — '
                     'check the error log.'))
    if now['load_pct'] >= 100:
        tips.append(('warn', 'Load is above the core count', f'Load {now["load"][0]} on {now["cores"]} cores: '
                     'work is queuing for the CPU.'))
    if not tips:
        tips.append(('ok', 'All healthy', 'CPU, memory and disk all have headroom, and errors are low.'))
    return tips
