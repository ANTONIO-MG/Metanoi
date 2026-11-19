#!/usr/bin/env bash
# Documented in metanoia/docs/reference/deployment.md

set -euo pipefail
cd "$(dirname "$0")/../../.."
set -a; eval "$(sudo cat /run/metanoia/secrets.env)"; set +a
exec .environment/bin/python manage.py "$@"
