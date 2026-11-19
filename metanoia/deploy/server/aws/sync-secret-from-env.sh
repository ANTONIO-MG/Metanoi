#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/../../.." && pwd)"
SECRET_ID="${METANOIA_SECRET_ID:-metanoia/production}"
REGION="${METANOIA_SECRET_REGION:-af-south-1}"
umask 077
TMP=$(mktemp)
trap 'shred -u "$TMP" 2>/dev/null || rm -f "$TMP"' EXIT

"$APP/.environment/bin/python" - "$APP/.env" "$HERE/secret-keys.txt" > "$TMP" <<'PY'
import json, sys
from dotenv import dotenv_values
env = dotenv_values(sys.argv[1])
keys = [l.strip() for l in open(sys.argv[2]) if l.strip() and not l.startswith('#')]
json.dump({k: env.get(k) or '' for k in keys}, sys.stdout)
PY

if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
      --secret-string "file://$TMP" --query VersionId --output text >/dev/null
  echo "updated $SECRET_ID"
else
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_ID" \
      --description "Metanoia Group LMS production secrets (keys: deploy/server/aws/secret-keys.txt)" \
      --secret-string "file://$TMP" --query ARN --output text
fi
