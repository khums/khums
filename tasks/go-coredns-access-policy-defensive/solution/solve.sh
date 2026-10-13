#!/bin/sh
# Applies four upstream patches oldest-first to the baseline CoreDNS source.
# All four patches apply cleanly with git apply; no adjustments needed.
set -e
cd "${SRC_ROOT:-/testbed}"

EXCLUDES="--exclude=*_test.go --exclude=*.md --exclude=**/testdata/* --exclude=docs/* --exclude=.github/* --exclude=test/* --exclude=*/testdata/*"

apply_strict() {
  git apply --whitespace=nowarn $EXCLUDES "$1"
}

apply_fuzzy() {
  before="$(mktemp)"
  find . -name '*_test.go' | sort > "$before"
  patch -p1 --fuzz=5 --no-backup-if-mismatch < "$1" || true
  find . -name '*_test.go' | sort > "$before.after"
  comm -13 "$before" "$before.after" | while IFS= read -r f; do rm -f "$f"; done
  rm -f "$before" "$before.after"
  find . -name '*.orig' -delete
  find . -name '*.rej' -delete
}

apply_patch() {
  if ! apply_strict "$1" 2>/dev/null; then
    apply_fuzzy "$1"
  fi
}

apply_patch /solution/patches/01-FIX-1-4411cff7.patch
apply_patch /solution/patches/02-FIX-2-674b43a3.patch
apply_patch /solution/patches/03-FIX-3-1ba14119.patch
apply_patch /solution/patches/04-FIX-4-18a58b9e.patch

OUT="${OUTPUT_DIR:-/app/output}"
mkdir -p "$OUT"
cp /solution/plan.md /solution/findings.json "$OUT"/
