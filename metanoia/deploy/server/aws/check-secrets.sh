#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/../../.." && pwd)"
SECRET_ID="${METANOIA_SECRET_ID:-metanoia/production}"
REGION="${METANOIA_SECRET_REGION:-af-south-1}"
echo "caller: $(aws sts get-caller-identity --query Arn --output text)"
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
    --query SecretString --output text \
  | "$APP/.environment/bin/python" -c '
import json, sys
from dotenv import dotenv_values
remote = json.load(sys.stdin)
env = dotenv_values(sys.argv[1])
keys = [l.strip() for l in open(sys.argv[2]) if l.strip() and not l.startswith("#")]
bad = 0
for k in keys:
    if k not in remote:
        st = "MISSING from secret"; bad += 1
    elif not remote[k]:
        st = "empty (placeholder)"
    elif k in env and env[k] and env[k] != remote[k]:
        st = "DIFFERS from .env"; bad += 1
    else:
        st = "ok"
    print(f"  {k:30} {st}")
extra = sorted(set(remote) - set(keys))
if extra: print("  extra keys in secret:", ", ".join(extra))
n = sum(1 for k in keys if remote.get(k))
print(f"{n} secrets with values, {len(keys)-n} empty placeholders, {bad} problems")
sys.exit(1 if bad else 0)
' "$APP/.env" "$HERE/secret-keys.txt"
