#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${TESTBED_DIR:-/testbed}"
PATCH="$SCRIPT_DIR/patches/FIX-1.patch"

test -d "$TARGET"
test -f "$PATCH"
git -C "$TARGET" apply --check --whitespace=nowarn \
  --exclude='shared/secrets_masker/tests/**' "$PATCH"
git -C "$TARGET" apply --whitespace=nowarn \
  --exclude='shared/secrets_masker/tests/**' "$PATCH"

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
  'A recursion-depth short circuit could leave deeply nested secret-valued keys unmasked.' \
  '' \
  '## Production changes' \
  'Preserve secret-key masking even when structural recursion reaches its depth boundary.' \
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
        "area": "recursive secret masking",
        "trust_boundary": "Untrusted input crossing into security-sensitive library behavior",
        "root_cause": "A recursion-depth short circuit could leave deeply nested secret-valued keys unmasked.",
        "security_impact": "Security-sensitive behavior could be influenced beyond the intended contract.",
        "severity": "high",
        "affected_code": ["shared/secrets_masker/src/airflow_shared/secrets_masker/secrets_masker.py"],
        "behavior_before_fix": "The vulnerable baseline violated the documented security boundary.",
        "remediation": "Preserve secret-key masking even when structural recursion reaches its depth boundary.",
        "behavior_after_fix": "The public behavioral probe passes while supported behavior is preserved.",
        "verification": "/testbed/reproducer/run-all.sh: PASS observed"
    }],
    "preserved_legitimate_behaviors": ["Existing supported inputs and public APIs remain available."],
    "verification_runs": [{"command": "/testbed/reproducer/run-all.sh", "result": "PASS observed"}]
}
Path("/app/output/findings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
