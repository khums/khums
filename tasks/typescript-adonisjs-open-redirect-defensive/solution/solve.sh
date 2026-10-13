#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${TESTBED_DIR:-/testbed}"
PATCH="$SCRIPT_DIR/patches/FIX-1.patch"
PROXY_PATCH="$SCRIPT_DIR/patches/FIX-2-forwarded-host.patch"

test -d "$TARGET"
test -f "$PATCH"
test -f "$PROXY_PATCH"
COOKIE_PATCH="$SCRIPT_DIR/patches/FIX-3-native-cookies.patch"
QUERY_PATCH="$SCRIPT_DIR/patches/FIX-4-query-forwarding.patch"
test -f "$COOKIE_PATCH"
test -f "$QUERY_PATCH"
git -C "$TARGET" apply --check --whitespace=nowarn \
  --exclude='tests/**' "$PATCH"
git -C "$TARGET" apply --whitespace=nowarn \
  --exclude='tests/**' "$PATCH"
git -C "$TARGET" apply --check --whitespace=nowarn "$PROXY_PATCH"
git -C "$TARGET" apply --whitespace=nowarn "$PROXY_PATCH"
for extra_patch in "$COOKIE_PATCH" "$QUERY_PATCH"; do
  git -C "$TARGET" apply --check --whitespace=nowarn "$extra_patch"
  git -C "$TARGET" apply --whitespace=nowarn "$extra_patch"
done

mkdir -p /app/output
set +e
probe_output="$(/testbed/reproducer/run-all.sh 2>&1)"
probe_code=$?
set -e
printf '%s\n' "$probe_output"
test "$probe_code" -eq 0
printf '%s\n' \
  '# Remediation Plan' \
  '' \
  '## Root causes identified' \
  'Unencoded route parameters altered redirect destinations; comma-separated trusted-proxy forwarded hosts were treated as a single hostname; native Set-Cookie headers were collapsed; and explicit query-forwarding overrides were ignored.' \
  '' \
  '## Production changes' \
  'Encode route parameters, select the first forwarded host, preserve native Set-Cookie values, and honor explicit query-forwarding overrides.' \
  '' \
  '## Preserved legitimate behavior' \
  'Existing supported inputs and public APIs remain available.' \
  '' \
  '## Validation' \
  '/testbed/reproducer/run-all.sh: PASS observed.' > /app/output/plan.md
python3 - <<'PY'
import json
from pathlib import Path
data = {
    "summary": "Applied and validated the defensive upstream remediation.",
    "findings": [{
        "area": "redirect URL construction",
        "trust_boundary": "Untrusted input crossing into security-sensitive library behavior",
        "root_cause": "Unencoded route parameter interpolation allowed reserved URL characters to alter the redirect destination.",
        "security_impact": "Security-sensitive behavior could be influenced beyond the intended contract.",
        "severity": "high",
        "affected_code": ["src/client/helpers.ts", "src/request.ts", "src/response.ts"],
        "behavior_before_fix": "The vulnerable baseline violated the documented security boundary.",
        "remediation": "Encode route parameters, parse the first trusted forwarded host, preserve separate cookies, and respect query-forwarding overrides.",
        "behavior_after_fix": "The public behavioral probe passes while supported behavior is preserved.",
        "verification": "/testbed/reproducer/run-all.sh: PASS observed"
    }],
    "preserved_legitimate_behaviors": ["Existing supported inputs and public APIs remain available."],
    "verification_runs": [{"command": "/testbed/reproducer/run-all.sh", "result": "PASS observed"}]
}
Path("/app/output/findings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
