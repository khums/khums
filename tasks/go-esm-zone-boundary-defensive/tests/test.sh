#!/usr/bin/env bash
# Two-pass Harbor/RewardKit verifier: deterministic pass plus optional semantic
# (non-deterministic) judge pass, merged for DataOS consumption.
set -euo pipefail

VERIFIER_DIR="${RESULT_DIR:-/logs/verifier}"
TESTS_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TASK_DIR="$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)"
WORKSPACE_DIR="${TESTBED:-/testbed}"
DETERMINISTIC_SUITE="$VERIFIER_DIR/rewardkit_deterministic_suite"
JUDGE_SUITE="$VERIFIER_DIR/rewardkit_judge_suite"
DETERMINISTIC_OUT="$VERIFIER_DIR/rewardkit_deterministic"
JUDGE_OUT="$VERIFIER_DIR/rewardkit_judge"
REWARDKIT_BIN="${REWARDKIT_BIN:-/opt/rewardkit-venv/bin/rewardkit}"
PYTHON_BIN="${PYTHON_BIN:-/opt/rewardkit-venv/bin/python}"

RUN_ROLE="${RUN_ROLE:-${HARBOR_RUN_ROLE:-${EVALUATION_ROLE:-agent}}}"
case "${RUN_ROLE,,}" in
  oracle|reference) ORACLE_RUN=1 ;;
  *) ORACLE_RUN=0 ;;
esac

# Harbor records Oracle execution as an agent artifact but does not export the
# selected agent name into the verifier environment. Treat that canonical
# artifact as the Oracle marker while retaining environment-role compatibility
# for direct runs and other harnesses.
if [ -f /logs/agent/oracle.txt ]; then
  ORACLE_RUN=1
fi

mkdir -p "$VERIFIER_DIR" "$VERIFIER_DIR/internal"
rm -rf "$DETERMINISTIC_SUITE" "$JUDGE_SUITE" "$DETERMINISTIC_OUT" "$JUDGE_OUT" \
       "$VERIFIER_DIR/judge_inputs"
rm -f "$VERIFIER_DIR/reward.json" "$VERIFIER_DIR/reward-details.json" \
      "$VERIFIER_DIR/final-reward-details.json" "$VERIFIER_DIR/reward.txt" \
      "$VERIFIER_DIR/deterministic_reward.json" "$VERIFIER_DIR/semantic_reward.json" \
      "$VERIFIER_DIR/semantic-reward-details.json" "$VERIFIER_DIR/semantic-execution.json" \
      "$VERIFIER_DIR/process_artifacts.json" "$VERIFIER_DIR/test_output.log" \
      "$VERIFIER_DIR/pytest_output.log" "$VERIFIER_DIR/rewardkit.log"

mkdir -p /app/output
export VERIFIER_DIR TESTS_DIR
export RESULT_DIR="$VERIFIER_DIR"
export TESTBED="$WORKSPACE_DIR"
export OUTPUT_DIR="${OUTPUT_DIR:-/app/output}"
export PYTHONNOUSERSITE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [ -f "$src" ]; then
    cp "$src" "$dst"
  fi
}

select_judge_backend() {
  JUDGE_BACKENDS=()
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    JUDGE_BACKENDS+=("openai/gpt-5.6-terra")
  fi
  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    JUDGE_BACKENDS+=("anthropic/claude-sonnet-4-6")
  fi
  if [ "${#JUDGE_BACKENDS[@]}" -eq 0 ]; then
    JUDGE_BACKEND=""
    JUDGE_PROVIDER="none"
    return 1
  fi
  JUDGE_BACKEND="${JUDGE_BACKENDS[0]}"
  JUDGE_PROVIDER="${JUDGE_BACKEND%%/*}"
  return 0
}

find_trajectory() {
  TRAJECTORY_SOURCE=""
  for candidate in \
    "${TRAJECTORY_PATH:-}" \
    /logs/agent/trajectory.json \
    /logs/artifacts/trajectory.json \
    /logs/trajectory.json \
    /app/trajectory.json \
    /logs/agent/terminus_2.pane \
    /logs/agent/claude-code.txt; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
      TRAJECTORY_SOURCE="$candidate"
      return 0
    fi
  done
  return 1
}

build_deterministic_suite() {
  mkdir -p "$DETERMINISTIC_SUITE/deterministic" "$DETERMINISTIC_SUITE/process" \
           "$DETERMINISTIC_OUT"
  cp "$TESTS_DIR/reward.toml" "$DETERMINISTIC_SUITE/reward.toml"
  cp "$TESTS_DIR/deterministic/check.py" "$DETERMINISTIC_SUITE/deterministic/check.py"
  cp "$TESTS_DIR/deterministic/pytest_checks.py" \
     "$DETERMINISTIC_SUITE/deterministic/pytest_checks.py"
  mkdir -p "$DETERMINISTIC_SUITE/deterministic/support"
  cp "$TESTS_DIR/deterministic/support/protected_tests.sha256" \
     "$DETERMINISTIC_SUITE/deterministic/support/protected_tests.sha256"
  cp "$TESTS_DIR/process/check.py" "$DETERMINISTIC_SUITE/process/check.py"
}

run_deterministic_pass() {
  echo "=== Deterministic pytest prepass ===" | tee -a "$VERIFIER_DIR/rewardkit.log"
  if ! "$PYTHON_BIN" "$TESTS_DIR/deterministic/check.py" --prepass 2>&1 \
      | tee -a "$VERIFIER_DIR/rewardkit.log"; then
    echo "Verifier error: deterministic pytest prepass did not complete." \
      | tee -a "$VERIFIER_DIR/rewardkit.log" >&2
    return 2
  fi

  echo "=== RewardKit deterministic pass ===" | tee -a "$VERIFIER_DIR/rewardkit.log"
  if ! "$REWARDKIT_BIN" "$DETERMINISTIC_SUITE" --workspace "$WORKSPACE_DIR" \
      --output "$DETERMINISTIC_OUT/reward.json" 2>&1 \
      | tee -a "$VERIFIER_DIR/rewardkit.log"; then
    echo "Verifier error: deterministic RewardKit execution failed." \
      | tee -a "$VERIFIER_DIR/rewardkit.log" >&2
    return 2
  fi
}

build_judge_suite() {
  mkdir -p "$JUDGE_SUITE" "$JUDGE_OUT"
  cp "$TESTS_DIR/judge_prompt.md" "$JUDGE_SUITE/judge_prompt.md"
  for dimension in process output_quality validity task_quality; do
    mkdir -p "$JUDGE_SUITE/$dimension"
    if [ "$JUDGE_PROVIDER" = "openai" ]; then
      sed "s|^judge = \".*\"|judge = \"$JUDGE_BACKEND\"|" \
        "$TESTS_DIR/$dimension/judge.toml" > "$JUDGE_SUITE/$dimension/judge.toml"
    else
      sed -e "s|^judge = \".*\"|judge = \"$JUDGE_BACKEND\"|" \
          -e '/^reasoning_effort = /d' \
        "$TESTS_DIR/$dimension/judge.toml" > "$JUDGE_SUITE/$dimension/judge.toml"
    fi
  done
}

judge_output_complete() {
  "$PYTHON_BIN" - "$JUDGE_OUT/reward.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    reward = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    raise SystemExit(1)

required = {"process", "output_quality", "validity", "task_quality"}
raise SystemExit(0 if required <= set(reward) else 1)
PY
}

stage_judge_inputs() {
  local destination="$VERIFIER_DIR/judge_inputs"
  mkdir -p "$destination"
  copy_if_exists "$TASK_DIR/instruction.md" "$destination/instruction.md"
  copy_if_exists "$VERIFIER_DIR/deterministic_reward.json" \
                 "$destination/deterministic_reward.json"
  copy_if_exists "$VERIFIER_DIR/process_artifacts.json" \
                 "$destination/process_artifacts.json"
  copy_if_exists "$VERIFIER_DIR/test_output.log" "$destination/test_output.log"
  copy_if_exists "$VERIFIER_DIR/pytest_output.log" "$destination/pytest_output.log"
  # Private, inert interpretation guide for semantic review (never sent to candidates).
  cat >"$destination/case_semantics.md" <<'SEMANTICS'
Verifier-only interpretation guide for the esm.sh zone-boundary task.

Public reproducer (/testbed/reproducer/run-all.sh) prints five aggregate cases.
Each aggregates the internal checks named in parentheses; nothing else is covered:
- Case A (A, H): zone-id write escape on POST /transform; well-formed package install.
- Case B (G, D): package tarball entry escape on install; request without X-Zone-Id.
- Case C (I, C): static/app file-server path escape; valid-domain X-Zone-Id stored under its zone.
- Case D (B, J): zone-id read escape on GET /+<hash>.<ext>; in-app file served normally.
- Case E (A, G, I): escape summary across the three surfaces.
The public reproducer has NO case for outbound fetch, SSRF, DNS, redirect,
authentication, or registry selection. A finding about those surfaces is not
verified by any public run; it needs a targeted request or test in the trajectory.
Compilation, `go vet`, or linting is never behavioral verification.

Deterministic rows (deterministic_reward.json):
- Security rows (escape or unsafe outcome must not happen): test_sec_case_a/b/g/i,
  H01 (garbage zone id handled without 5xx), H02, H03 (zone namespaces isolated),
  H04 (malicious tarball entry must not corrupt legitimate siblings), H06, H10, H11,
  H13 (no stray temp artifacts), H12 (string prefix is not containment), H14 ('..' zone).
- Legitimate-behavior rows (supported input must keep working; a failure here is a
  regression caused by an over-broad change): test_func_case_c, test_func_case_d,
  test_func_case_h, test_func_case_j, H05 (self-cancelling archive entry such as
  dist/../lib/x.js extracts to its normalized location), H07 ('..' as a filename
  substring is served), H08 (self-cancelling request path such as /dir/../file is
  served), H09 (literal backslash in a filename is served on Linux), H15 ('..'
  substring in a package file name is served).
Verifier-only rows H01-H15 are invisible to the candidate. An artifact claim about
how the baseline or the final code behaved on these inputs is supported only if the
trajectory shows the candidate observing that behavior; a claim of preserved
legitimate behavior or "no regression" is false whenever a legitimate-behavior row failed.
SEMANTICS
  copy_if_exists /app/output/plan.md "$destination/plan.md"
  copy_if_exists /app/output/findings.json "$destination/findings.json"

  # Give the judge the submitted change as evidence, without exposing the oracle.
  if [ -d /opt/task-baseline ]; then
    diff -ruN -x __pycache__ -x "*.pyc" \
      /opt/task-baseline "$WORKSPACE_DIR" \
      >"$destination/submission.diff" 2>/dev/null || true
    diff -rq -x __pycache__ -x "*.pyc" \
      /opt/task-baseline "$WORKSPACE_DIR" \
      >"$destination/submission-status.txt" 2>/dev/null || true
  fi

  if [ -n "${TRAJECTORY_SOURCE:-}" ] && [ -f "$TRAJECTORY_SOURCE" ]; then
    case "$TRAJECTORY_SOURCE" in
      *.json) cp "$TRAJECTORY_SOURCE" "$destination/trajectory.json" ;;
      *) cp "$TRAJECTORY_SOURCE" "$destination/trajectory.txt" ;;
    esac
  fi
}

merge_rewards() {
  "$PYTHON_BIN" "$TESTS_DIR/merge_rewards.py" "$VERIFIER_DIR" "$DETERMINISTIC_OUT/reward.json" \
    "$JUDGE_OUT/reward.json" "$ORACLE_RUN" "$DETERMINISTIC_OK"
}

find_trajectory || true
build_deterministic_suite

if ! run_deterministic_pass; then
  DETERMINISTIC_OK=0
  JUDGE_BACKEND=""
  JUDGE_PROVIDER="none"
  stage_judge_inputs
  printf '{"attempted":false,"status":"skipped","oracle_run":%s,"deterministic_completed":false,"api_key_present":false,"trajectory_present":%s,"judge_backend":"","judge_provider":"none"}\n' \
    "$([ "$ORACLE_RUN" = "1" ] && echo true || echo false)" \
    "$([ -n "${TRAJECTORY_SOURCE:-}" ] && [ -f "$TRAJECTORY_SOURCE" ] && echo true || echo false)" \
    >"$VERIFIER_DIR/semantic-execution.json"
  merge_rewards
  rm -rf "$DETERMINISTIC_SUITE" "$JUDGE_SUITE" "$DETERMINISTIC_OUT" "$JUDGE_OUT"
  exit 0
fi
DETERMINISTIC_OK=1
stage_judge_inputs

select_judge_backend || true

if [ "$ORACLE_RUN" = "0" ] \
   && [ -n "$JUDGE_BACKEND" ] \
   && [ -n "${TRAJECTORY_SOURCE:-}" ] \
   && [ -f "$TRAJECTORY_SOURCE" ]; then
  echo "=== RewardKit semantic judge pass ===" >>"$VERIFIER_DIR/rewardkit.log"
  SEMANTIC_STATUS="skipped"
  for candidate_backend in "${JUDGE_BACKENDS[@]}"; do
    JUDGE_BACKEND="$candidate_backend"
    JUDGE_PROVIDER="${JUDGE_BACKEND%%/*}"
    rm -rf "$JUDGE_SUITE" "$JUDGE_OUT"
    build_judge_suite
    echo "Trying semantic judge backend: $JUDGE_BACKEND" >>"$VERIFIER_DIR/rewardkit.log"
    if "$REWARDKIT_BIN" "$JUDGE_SUITE" --workspace "$WORKSPACE_DIR" \
         --output "$JUDGE_OUT/reward.json" >>"$VERIFIER_DIR/rewardkit.log" 2>&1 \
       && judge_output_complete; then
      SEMANTIC_STATUS="completed"
      break
    fi
    echo "Semantic judge backend unavailable or incomplete: $JUDGE_BACKEND" >>"$VERIFIER_DIR/rewardkit.log"
  done
  if [ "$SEMANTIC_STATUS" = "completed" ]; then
    copy_if_exists "$JUDGE_OUT/reward.json" "$VERIFIER_DIR/semantic_reward.json"
    copy_if_exists "$JUDGE_OUT/reward-details.json" "$VERIFIER_DIR/semantic-reward-details.json"
  else
    echo "No semantic judge backend completed; retaining deterministic result." >>"$VERIFIER_DIR/rewardkit.log"
    rm -rf "$JUDGE_OUT"
  fi
  printf '{"attempted":true,"status":"%s","oracle_run":false,"deterministic_completed":true,"api_key_present":true,"trajectory_present":true,"judge_backend":"%s","judge_provider":"%s"}\n' \
    "$SEMANTIC_STATUS" "$JUDGE_BACKEND" "$JUDGE_PROVIDER" \
    >"$VERIFIER_DIR/semantic-execution.json"
  merge_rewards
else
  printf '{"attempted":false,"status":"skipped","oracle_run":%s,"deterministic_completed":true,"api_key_present":%s,"trajectory_present":%s,"judge_backend":"%s","judge_provider":"%s"}\n' \
    "$([ "$ORACLE_RUN" = "1" ] && echo true || echo false)" \
    "$([ -n "$JUDGE_BACKEND" ] && echo true || echo false)" \
    "$([ -n "${TRAJECTORY_SOURCE:-}" ] && [ -f "$TRAJECTORY_SOURCE" ] && echo true || echo false)" \
    "$JUDGE_BACKEND" "$JUDGE_PROVIDER" \
    >"$VERIFIER_DIR/semantic-execution.json"
  merge_rewards
fi

rm -rf "$DETERMINISTIC_SUITE" "$JUDGE_SUITE" "$DETERMINISTIC_OUT" "$JUDGE_OUT"

exit 0
