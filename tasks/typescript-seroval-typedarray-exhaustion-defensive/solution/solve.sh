#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${TESTBED_DIR:-/testbed}"
test -d "$TARGET"
for PATCH in "$SCRIPT_DIR/patches/FIX-1.patch" "$SCRIPT_DIR/patches/FIX-2.patch"; do
  test -f "$PATCH"
  git -C "$TARGET" apply --check --whitespace=nowarn \
    --exclude='packages/seroval/test/**' "$PATCH"
  git -C "$TARGET" apply --whitespace=nowarn \
    --exclude='packages/seroval/test/**' "$PATCH"
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
  'Untrusted non-ArrayBuffer sources could drive unbounded typed-array allocation.' \
  '' \
  '## Production changes' \
  'Reject invalid typed-array backing sources before allocation.' \
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
    "summary": "Applied and validated the defensive remediation.",
    "findings": [{
        "area": "typed-array deserialization",
        "trust_boundary": "Untrusted input crossing into security-sensitive library behavior",
        "root_cause": "Untrusted non-ArrayBuffer sources could drive unbounded typed-array allocation.",
        "security_impact": "Security-sensitive behavior could be influenced beyond the intended contract.",
        "severity": "high",
        "affected_code": ["packages/seroval/src/core/base-primitives.ts", "packages/seroval/src/core/context/deserializer.ts"],
        "behavior_before_fix": "The vulnerable baseline violated the documented security boundary.",
        "remediation": "Reject invalid typed-array backing sources before allocation.",
        "behavior_after_fix": "The public behavioral probe passes while supported behavior is preserved.",
        "verification": "/testbed/reproducer/run-all.sh: PASS observed"
    }],
    "preserved_legitimate_behaviors": ["Existing supported inputs and public APIs remain available."],
    "verification_runs": [{"command": "/testbed/reproducer/run-all.sh", "result": "PASS observed"}]
}
Path("/app/output/findings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
