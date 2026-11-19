#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -euo pipefail
SECRET_ID="${METANOIA_SECRET_ID:-metanoia/production}"
REGION="${METANOIA_SECRET_REGION:-af-south-1}"
OUT_DIR=/run/metanoia
OUT="$OUT_DIR/secrets.env"
AWS=$(command -v aws || echo /snap/bin/aws)

umask 077
mkdir -p "$OUT_DIR"
TMP=$(mktemp "$OUT_DIR/.secrets.XXXXXX")
trap 'rm -f "$TMP"' EXIT

"$AWS" secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" \
    --query SecretString --output text \
  | python3 -c '
import json, re, sys
data = json.load(sys.stdin)
for k, v in sorted(data.items()):
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", k):
        sys.exit(f"bad key name in secret: {k!r}")
    v = "" if v is None else str(v)
    if not v:
        continue
    if "\n" in v:
        sys.exit(f"{k}: multi-line values are not supported")
    for c in "\\\"`$":
        v = v.replace(c, "\\" + c)
    print(f"{k}=\"{v}\"")
' > "$TMP"

chmod 600 "$TMP"
mv "$TMP" "$OUT"
trap - EXIT
echo "fetched $(grep -c = "$OUT") secrets from $SECRET_ID into $OUT"
