#!/bin/sh
# Applies the verbatim upstream fix patches, oldest first, to the baseline source,
# plus the one documented adjustment. Hand-verified (not template-generated) because
# FIX-2's patch needs a fuzzy fallback to apply cleanly against this task's baseline.
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

apply_patch /solution/patches/01-FIX-1-833a29f4.1-upstream.patch
apply_patch /solution/patches/02-FIX-2-c62ab83c.1-upstream.patch
git apply --whitespace=nowarn /solution/patches/02-FIX-2-c62ab83c.2-adjust.patch
git apply --whitespace=nowarn /solution/adjustments/web-utils-compile-fix.patch
apply_patch /solution/patches/03-FIX-5-468b6b2a.1-upstream.patch

OUT="${OUTPUT_DIR:-/app/output}"
mkdir -p "$OUT"
cp /solution/plan.md /solution/findings.json "$OUT"/
