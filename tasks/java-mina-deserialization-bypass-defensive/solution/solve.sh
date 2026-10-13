#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TARGET="${TESTBED_DIR:-/testbed}"
PATCH="$SCRIPT_DIR/patches/FIX-1.patch"

test -d "$TARGET"
test -f "$PATCH"
git -C "$TARGET" apply --check --whitespace=nowarn "$PATCH"
git -C "$TARGET" apply --whitespace=nowarn "$PATCH"

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
  'The custom ObjectInputStream validated ordinary class descriptors against the configured accept matchers, but did not override proxy-class resolution. A serialized dynamic proxy could therefore introduce interfaces through the default resolution path without applying the same acceptance boundary.' \
  '' \
  'Descriptor inspection also called Class.forName with initialization enabled. Merely examining a serialized class descriptor could consequently execute static initialization before deserialization acceptance was complete.' \
  '' \
  '## Production changes' \
  'Override resolveProxyClass to require every proxy interface name to match the configured accept list, load accepted interfaces without initialization through the caller-provided class loader (not the default latest-user-defined loader), and construct the proxy class from those validated interfaces.' \
  '' \
  'Load classes during descriptor inspection with initialization disabled.' \
  '' \
  '## Preserved legitimate behavior' \
  'Accepted ordinary serializable classes remain supported. Dynamic proxies continue to deserialize when both their invocation handler and every proxy interface are explicitly accepted. Public method signatures and the existing ClassNotFoundException rejection convention are unchanged.' \
  '' \
  '## Validation' \
  '/testbed/reproducer/run-all.sh: PASS observed for groups G1 and G2.' > /app/output/plan.md
python3 - <<'PY'
import json
from pathlib import Path
data = {
    "summary": "Hardened MINA object deserialization so dynamic-proxy interfaces cannot bypass class acceptance checks and descriptor inspection does not initialize classes.",
    "findings": [{
        "area": "object deserialization filtering",
        "trust_boundary": "Untrusted input crossing into security-sensitive library behavior",
        "root_cause": "The custom ObjectInputStream filtered ordinary descriptors but inherited the default proxy-class resolution path, and descriptor lookup loaded classes with initialization enabled.",
        "security_impact": "An untrusted stream could resolve unaccepted proxy interfaces or trigger class static-initialization side effects before the intended deserialization boundary completed.",
        "severity": "high",
        "affected_code": ["mina-core/src/main/java/org/apache/mina/core/buffer/AbstractIoBuffer.java"],
        "behavior_before_fix": "A proxy interface was resolved without being checked against acceptMatchers, and readClassDescriptor used Class.forName with initialization enabled.",
        "remediation": "Validate every proxy interface against acceptMatchers, load accepted interfaces through the configured loader without initialization, and disable initialization during descriptor lookup.",
        "behavior_after_fix": "A proxy containing an unaccepted interface is rejected with ClassNotFoundException, while explicitly accepted proxies and existing ordinary serialization behavior continue to work.",
        "verification": "/testbed/reproducer/run-all.sh: groups G1 and G2 all PASS"
    }],
    "preserved_legitimate_behaviors": ["Accepted ordinary object deserialization", "Dynamic-proxy deserialization when the handler and every interface are accepted", "Existing public APIs and ClassNotFoundException rejection behavior"],
    "verification_runs": [{"command": "/testbed/reproducer/run-all.sh", "result": "PASS for G1 and G2"}]
}
Path("/app/output/findings.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
