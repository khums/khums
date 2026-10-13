#!/usr/bin/env bash
# Entrypoint: keep shared temp world-writable (the platform stages agent I/O
# there), then hand off to whatever command the platform runs.
set -euo pipefail

# Harbor requires the container to start as root; the platform itself selects
# the agent-facing identity when it execs the actual command, this entrypoint
# does not drop privilege itself.
chmod 1777 /tmp /var/tmp /dev/shm 2>/dev/null || true

mkdir -p /app/output
chown agent:agent /app/output 2>/dev/null || true

exec "$@"
