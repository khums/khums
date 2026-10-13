#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'environment: %s\n' "$*" >&2; exit 70; }

test -d /testbed || fail 'workspace missing'
test ! -e /testbed/.git || fail 'unexpected repository metadata'
test -d /testbed/reproducer || fail 'public launcher directory missing'
test "$(stat -c '%U:%a' /testbed/reproducer)" = 'root:555' || fail 'public launcher directory permissions changed'
id agent >/dev/null 2>&1 || fail 'agent account missing'
test -d /app/output || fail 'output directory missing'

if [ "$(id -u)" -eq 0 ]; then
  chown agent:agent /app/output
fi
exec "$@"
