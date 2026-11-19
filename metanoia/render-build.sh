#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -o errexit   # any failing step fails the deploy, instead of shipping a half-built image
set -o nounset
set -o pipefail

echo "──> Python: $(python --version)"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python manage.py collectstatic --noinput

if [ "${RUN_DB_WIPE:-false}" = "true" ]; then
    echo "══════════════════════════════════════════════════════════════════"
    echo " RUN_DB_WIPE=true — DROPPING ALL TABLES and reseeding from scratch."
    echo " Set RUN_DB_WIPE=false in the Render dashboard after this deploy."
    echo "══════════════════════════════════════════════════════════════════"
    python .academic_seed.py --fresh --yes
else
    python manage.py migrate --noinput
fi

python manage.py check --deploy --fail-level ERROR

echo "──> Build complete."
