#!/bin/sh
# Verifier-only granular probe runner. Not part of the public reproducer and
# not referenced by /testbed/reproducer/run-all.sh; lives entirely under
# tests/, which is never mounted into the candidate's own working session
# (only /testbed/reproducer/ is). Builds esmd/esmcli fresh from /testbed
# (the same way the public reproducer does) plus this directory's own
# hidden_harness.go, then runs it and reports the full per-case granular
# results (Cases A-Q) for weighted deterministic scoring.
#
# Privilege model: submitted /testbed source is built, and every resulting
# binary is run, only as the unprivileged candidate identity
# (ESM_CANDIDATE_USER, default "agent") with a minimal environment. Root is
# kept only for building and running the verifier-owned hidden harness, whose
# source, binary and work directory stay root-only.
set -u

CANDIDATE_USER="${ESM_CANDIDATE_USER:-agent}"
SUPPORT_DIR="$(cd "$(dirname "$0")" && pwd)"
SAFE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# /tmp is sticky, so the candidate cannot rename or replace these entries.
ROOT_WORK="$(mktemp -d /tmp/esm-repro-verify-XXXXXX)" || { echo "ERROR: cannot create workdir"; exit 3; }
CAND_DIR="$(mktemp -d /tmp/esm-repro-cand-XXXXXX)" || { rm -rf "$ROOT_WORK"; echo "ERROR: cannot create candidate dir"; exit 3; }
trap 'rm -rf "$ROOT_WORK" "$CAND_DIR"' EXIT
chmod 0700 "$ROOT_WORK"

if [ "$(id -u)" -eq 0 ]; then
  if ! CAND_UID="$(id -u "$CANDIDATE_USER" 2>/dev/null)" || [ "$CAND_UID" -eq 0 ]; then
    echo "ERROR: unprivileged candidate user '$CANDIDATE_USER' unavailable; refusing to build or run candidate code as root"
    exit 3
  fi
  CAND_HOME="$(getent passwd "$CANDIDATE_USER" | cut -d: -f6)"
  chown "$CANDIDATE_USER:" "$CAND_DIR"
  as_candidate() { runuser -u "$CANDIDATE_USER" -- "$@"; }
else
  CAND_HOME="${HOME:-/tmp}"
  as_candidate() { "$@"; }
fi
chmod 0700 "$CAND_DIR"

# Build submitted source as the candidate, with no verifier environment
# (API keys, RESULT_DIR, ...) inherited.
candidate_build() {
  out="$1"; pkg="$2"; log="$3"
  (cd /testbed && as_candidate env -i \
      PATH="$SAFE_PATH" HOME="$CAND_HOME" TMPDIR="$CAND_DIR" GOPATH=/go \
      GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off \
      go build -o "$out" "$pkg") >"$log" 2>&1
}

ESMD="$CAND_DIR/esmd"
if ! candidate_build "$ESMD" ./server/esmd "$ROOT_WORK/build-esmd.log"; then
  echo "ERROR: candidate server failed to build"
  sed -n '1,50p' "$ROOT_WORK/build-esmd.log" >&2
  exit 3
fi

ESMCLI="$CAND_DIR/esmcli"
if ! candidate_build "$ESMCLI" . "$ROOT_WORK/build-esmcli.log"; then
  ESMCLI=""
fi

HIDDEN_HARNESS="$ROOT_WORK/hidden_harness"
if ! (cd "$SUPPORT_DIR" && GOFLAGS=-mod=mod GOPROXY=off go build -o "$HIDDEN_HARNESS" hidden_harness.go) 2>"$ROOT_WORK/build-hh.log"; then
  echo "ERROR: hidden harness failed to build"
  sed -n '1,50p' "$ROOT_WORK/build-hh.log" >&2
  exit 3
fi

out="$(ESM_CANDIDATE_USER="$CANDIDATE_USER" "$HIDDEN_HARNESS" "$ESMD" "$ESMCLI" 2>"$ROOT_WORK/harness.err")"
rc=$?
printf '%s\n' "$out"
if [ $rc -ne 0 ] && [ -z "$out" ]; then
  echo "ERROR: hidden harness produced no output"
  sed -n '1,50p' "$ROOT_WORK/harness.err" >&2
fi
exit $rc
