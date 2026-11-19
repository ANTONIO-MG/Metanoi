#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -euo pipefail

DOMAIN=learn.metanoiagroup.co.za
IP="${SERVER_IP:?set SERVER_IP to this server's public IP}"
APP=/home/ubuntu/projects/Metanoia/metanoia
BK=/home/ubuntu/metanoia-backups/domain-switch
PY=$APP/.environment/bin/python
TS=$(date +%Y%m%d-%H%M%S)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

if [ "${1:-}" = "--rollback" ]; then
  say "Rolling back to plain HTTP on the IP"
  latest=$(ls -1d $BK/*/ 2>/dev/null | tail -1) || true
  [ -n "$latest" ] || { echo "No backup found in $BK"; exit 1; }
  systemctl stop caddy 2>/dev/null || true
  cp "$latest/metanoia.service" /etc/systemd/system/metanoia.service
  cp "$latest/env" "$APP/.env"; chown ubuntu:ubuntu "$APP/.env"
  sudo -u ubuntu $PY $APP/manage.py shell -c "from django.contrib.sites.models import Site; Site.objects.filter(pk=1).update(domain='$IP')" >/dev/null
  systemctl daemon-reload && systemctl restart metanoia
  if [ -f "$latest/Caddyfile" ]; then
    install -m 644 "$latest/Caddyfile" /etc/caddy/Caddyfile
    systemctl start caddy
  else
    systemctl disable caddy 2>/dev/null || true
  fi
  echo "Rolled back from $latest. Site is on http://$IP again."
  exit 0
fi

say "1/7 Checking DNS"
resolve() {
  if command -v dig >/dev/null; then dig +short @8.8.8.8 "$1" A | sort -u | tr '\n' ' '
  else { getent ahostsv4 "$1" || true; } | awk '{print $1}' | sort -u | tr '\n' ' '; fi
}
for h in $DOMAIN; do
  got=$(resolve $h)
  echo "$h -> ${got:-nothing}"
  case " $got " in *" $IP "*) ;; *) echo "Not pointing at $IP yet. Update DNS at Axxess, wait, and run again."; exit 1;; esac
done

say "2/7 Backing up current config to $BK/$TS"
mkdir -p "$BK/$TS"
cp /etc/systemd/system/metanoia.service "$BK/$TS/metanoia.service"
cp "$APP/.env" "$BK/$TS/env"; chmod 600 "$BK/$TS/env"
[ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "$BK/$TS/Caddyfile" || true
chown -R ubuntu:ubuntu "$BK"

say "3/7 Installing Caddy"
if ! command -v caddy >/dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
fi
systemctl stop caddy 2>/dev/null || true   # it must not grab :80 before the app moves off it
install -m 644 "$APP/deploy/server/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

say "4/7 Updating .env for HTTPS on $DOMAIN"
sudo -u ubuntu $PY - "$APP/.env" "$DOMAIN" <<'PYEOF'
import sys
path, host = sys.argv[1:3]
lines = open(path).read().split('\n')
drop = {'ALLOW_PLAIN_HTTP', 'CSRF_COOKIE_SECURE', 'SECURE_SSL_REDIRECT', 'SESSION_COOKIE_SECURE',
        'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS', 'SECURE_HSTS_PRELOAD', 'USE_X_FORWARDED_PROTO'}
out, seen = [], set()
for line in lines:
    key = line.split('=', 1)[0].strip() if '=' in line and not line.lstrip().startswith('#') else None
    if key in drop:
        continue
    if key == 'ALLOWED_HOSTS':
        hosts = [h for h in line.split('=', 1)[1].split(',') if h]
        if host not in hosts: hosts.append(host)
        line = 'ALLOWED_HOSTS=' + ','.join(hosts)
    elif key == 'CSRF_TRUSTED_ORIGINS':
        origins = [o for o in line.split('=', 1)[1].split(',') if o]
        if f'https://{host}' not in origins: origins.append(f'https://{host}')
        line = 'CSRF_TRUSTED_ORIGINS=' + ','.join(origins)
    elif key == 'SITE_URL':
        line = f'SITE_URL=https://{host}'
    seen.add(key)
    out.append(line)
out += ['', f'# --- HTTPS via Caddy on {host} (deploy/server/switch-to-domain.sh) ---',
        '# Secure cookies + TLS redirect are the defaults with DEBUG off. HSTS starts at',
        '# one day without subdomains/preload (www is Vercel, mail.* is Axxess); raise',
        '# SECURE_HSTS_SECONDS to 31536000 once everything is confirmed working.',
        'USE_X_FORWARDED_PROTO=true', 'SECURE_HSTS_SECONDS=86400',
        'SECURE_HSTS_INCLUDE_SUBDOMAINS=false', 'SECURE_HSTS_PRELOAD=false']
open(path, 'w').write('\n'.join(out).rstrip('\n') + '\n')
print('  .env updated')
PYEOF

say "5/7 Pointing the Django Site at $DOMAIN, collecting static files"
cd "$APP"
sudo -u ubuntu $PY manage.py check --deploy 2>&1 | tail -3 || true
sudo -u ubuntu $PY manage.py shell -c "from django.contrib.sites.models import Site; Site.objects.filter(pk=1).update(domain='$DOMAIN', name='Metanoia Group')" >/dev/null
sudo -u ubuntu $PY manage.py collectstatic --noinput -v0

say "6/7 Moving the app to 127.0.0.1:8000 (4 workers) and starting Caddy"
install -m 644 "$APP/deploy/server/metanoia.service" /etc/systemd/system/metanoia.service
systemctl daemon-reload
systemctl restart metanoia
sleep 8
systemctl enable --now caddy
systemctl restart caddy

say "7/7 Checking"
sleep 12
systemctl is-active metanoia caddy
curl -s -o /dev/null -w "https://$DOMAIN/ -> %{http_code} %{redirect_url}\n" --resolve "$DOMAIN:443:$IP" "https://$DOMAIN/" || true
curl -s -o /dev/null -w "http://$IP/ -> %{http_code} %{redirect_url}\n" "http://$IP/" || true
echo
echo "Done. If the https check failed, port 443 may be closed in Oracle Cloud's security list,"
echo "or Let's Encrypt is still issuing: see 'journalctl -u caddy -n 50'."
echo "Undo with: sudo bash $APP/deploy/server/switch-to-domain.sh --rollback"
