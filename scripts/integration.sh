#!/usr/bin/env bash
# The integration checks that do not disturb services. See tests/integration/README.md.
#
# Deliberately excludes check_failures.py (restarts and kills services) and
# check_hardware.py (needs a human). Run those by hand.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO_ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3

cd "$REPO_ROOT/tests/integration" || exit 1

status=0
for check in check_policy.py check_soak.py; do
    echo "== $check =="
    "$PY" "$check" "$@" || status=1
    echo
done

cat <<'NOTE'
NOTE: these read state back from the OpenRGB server's own model. That proves the
packets were accepted, not that any LED lit. For that, run:

    python tests/integration/check_hardware.py
NOTE
exit $status
