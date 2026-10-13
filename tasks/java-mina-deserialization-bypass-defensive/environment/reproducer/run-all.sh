#!/usr/bin/env bash
set -euo pipefail
set +e
result="$(sudo -n /usr/local/bin/run-sealed-probe public 2>/dev/null)"
code=$?
set -e
if [ -z "$result" ]; then
  printf 'SUMMARY: ERROR\n'
  exit 2
fi
printf '%s\n' "$result"
exit "$code"
