#!/usr/bin/env bash
# Run the unchanged project tests in writable scratch storage, not the protected tree.
set -euo pipefail
exec sudo -n /usr/local/bin/run-sealed-probe project-tests "$@"
