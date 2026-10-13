#!/usr/bin/env bash
set -euo pipefail
# Output is the sealed probe's own coarse summary; its exit code is preserved
# (0 PASS, 1 FAIL, 2 ERROR).
exec sudo -n /usr/local/bin/run-sealed-probe public
