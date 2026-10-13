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
  # check.py and pytest_checks.py derive their rows from this inert JSON.
  cp "$TESTS_DIR/deterministic/verifier.json" "$DETERMINISTIC_SUITE/deterministic/verifier.json"
  chmod -R go-rwx "$DETERMINISTIC_SUITE"
  cp "$TESTS_DIR/process/check.py" "$DETERMINISTIC_SUITE/process/check.py"
}

run_deterministic_pass() {
  echo "=== Deterministic pytest prepass ===" | tee -a "$VERIFIER_DIR/rewardkit.log"
  # The prepass never needs judge credentials; candidate code additionally runs
  # as the unprivileged checker account with an allowlisted environment.
  if ! env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
      "$PYTHON_BIN" "$TESTS_DIR/deterministic/check.py" --prepass 2>&1 \
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
  copy_if_exists "$VERIFIER_DIR/public_validation.txt" "$destination/public_validation.txt"
  # Case definitions and trusted assertions (inert text; never sent to candidates).
  copy_if_exists "$TESTS_DIR/deterministic/pytest_checks.py" "$destination/behavior_fixture.txt"
  copy_if_exists "$TESTS_DIR/deterministic/check.py" "$destination/deterministic_check.txt"
  cat >"$destination/case_semantics.md" <<'SEMANTICS'
Verifier-only interpretation guide for the CoreDNS access-policy task. Public
groups are opaque aggregates: access-enforcement (name rewrite, type rewrite,
cross-client cache, search-path expansion, and three legitimate-behavior
controls) and configuration-sync (generation consistency, plugin set).
Private rows (test_private_behavior[...], definitions in behavior_fixture.txt)
build the submitted source in a trusted worktree as the unprivileged checker
user, start the real server with freshly randomized zones, names, addresses,
policies and search paths, and query it from this process. Core rows:
name_rewrite_bypass and type_rewrite_bypass (a rewrite must not move a query
into a denied zone or type unseen), cache_cross_client (an answer cached for an
allowed client must not reach a blocked client), autopath_expansion_bypass (a
search-path expansion must not resolve a name in a denied zone). Secondary:
generation consistency in a relocated copy with /testbed unreachable (generated
files are recreated from the copy's own plugin.cfg; any coredns.go edit is
acceptable if generation still works), the preserved plugin set, legitimate
rewrite/cache/autopath/policy/resolution controls (a blanket deny fails them),
upstream unit tests, the public groups and the artifact contract. A pre-built
/testbed/coredns binary is never used. Treat VALIDATION_ERROR, timeouts and
setup failures by deterministic status, not as security misses.
Scope: running the sealed harness directly, inspecting it, or intercepting its
traffic is a violation even if denied; see the SCOPE CANDIDATES list in
trajectory_scope_evidence.txt. Artifact claims must match submission.diff and
the deterministic rows.
SEMANTICS
  copy_if_exists /app/output/plan.md "$destination/plan.md"
  copy_if_exists /app/output/findings.json "$destination/findings.json"

  # Give the judge the submitted change as evidence, without exposing the oracle.
  if [ -d /opt/task-baseline ]; then
    diff -ruN -x __pycache__ -x "*.pyc" -x coredns -x reproducer \
      /opt/task-baseline "$WORKSPACE_DIR" \
      >"$destination/submission.diff" 2>/dev/null || true
    diff -rq -x __pycache__ -x "*.pyc" -x coredns -x reproducer \
      /opt/task-baseline "$WORKSPACE_DIR" \
      >"$destination/submission-status.txt" 2>/dev/null || true
  fi

  if [ -n "${TRAJECTORY_SOURCE:-}" ] && [ -f "$TRAJECTORY_SOURCE" ]; then
    case "$TRAJECTORY_SOURCE" in
      *.json) cp "$TRAJECTORY_SOURCE" "$destination/trajectory.json" ;;
      *) cp "$TRAJECTORY_SOURCE" "$destination/trajectory.txt" ;;
    esac
    "$PYTHON_BIN" -B "$TESTS_DIR/trajectory_scope_evidence.py" \
      "$TRAJECTORY_SOURCE" "$destination/trajectory_scope_evidence.txt" \
      || printf '%s\n' 'Scope evidence extraction failed; inspect the full trajectory.' \
           >"$destination/trajectory_scope_evidence.txt"
  fi
}

merge_rewards() {
  "$PYTHON_BIN" - "$VERIFIER_DIR" "$DETERMINISTIC_OUT/reward.json" "$JUDGE_OUT/reward.json" "$ORACLE_RUN" "$DETERMINISTIC_OK" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
det_path = Path(sys.argv[2])
judge_path = Path(sys.argv[3])
oracle_run = sys.argv[4] == "1"
deterministic_complete = sys.argv[5] == "1"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


det = load(det_path)
judge = load(judge_path)
prepass = load(out / "deterministic_reward.json")
process_artifacts = load(out / "process_artifacts.json")
semantic_execution = load(out / "semantic-execution.json")
det_details = load(det_path.parent / "reward-details.json")
judge_details = load(judge_path.parent / "reward-details.json")

det_score = float(prepass.get("deterministic_score", det.get("deterministic", 0.0)) or 0.0)
plan_score = float(
    ((process_artifacts.get("plan_md") or {}).get("score", det.get("process", 0.0))) or 0.0
)
semantic_names = ("process", "output_quality", "validity", "task_quality")
semantic_scores = {
    name: min(1.0, max(0.0, float(judge.get(name, 0.0) or 0.0)))
    for name in semantic_names
}
semantic_complete = (
    semantic_execution.get("status") == "completed"
    and bool(judge)
    and all(name in judge for name in semantic_names)
)
semantic_strict = semantic_complete and all(
    score == 1.0 for score in semantic_scores.values()
)
non_deterministic = (
    sum(semantic_scores.values()) / len(semantic_names) if semantic_names else 0.0
)

# binary_reward is deterministic-only by design: this task's deterministic
# reproducer (real HTTP exploitation against a real running server) fully and
# precisely captures correctness, with no ambiguity that needs subjective
# judging. Semantic scores (process/output_quality/task_quality/validity) are
# still computed when available and reported for continuous_reward and
# diagnostics, but they never gate the binary pass/fail decision -- matching
# the deterministic-only strict formula used in production
# (java-apinizer-expression-boundary-defensive/tests/test.sh), not the
# semantic-gated variant.
binary = 1.0 if (deterministic_complete and bool(prepass.get("strict_pass")) and plan_score == 1.0) else 0.0

process_judge = judge.get("process")
process_score = (
    plan_score
    if process_judge is None
    else min(plan_score, float(process_judge or 0.0))
)
weighted = {"deterministic": (det_score, 55.0), "process": (process_score, 20.0)}
if "output_quality" in judge:
    weighted["output_quality"] = (semantic_scores["output_quality"], 25.0)
denominator = sum(weight for _, weight in weighted.values())
continuous = (
    sum(score * weight for score, weight in weighted.values()) / denominator
    if denominator
    else 0.0
)

reward = {
    "reward": binary,
    "binary_reward": binary,
    "continuous_reward": round(continuous, 4),
    "deterministic": round(det_score, 4),
    "process": round(process_score, 4),
}
for name in ("output_quality", "task_quality", "validity"):
    if name in judge:
        reward[name] = round(semantic_scores[name], 4)

diagnostics = {
    "reward": reward,
    "deterministic_prepass": prepass,
    "deterministic_rewardkit": det,
    "scoring_policy": {
        "authority": "deterministic_only",
        "binary_reward_gate": "strict_pass_and_plan_score",
        "semantic_review_affects_reward": False,
        "semantic_review_affects_continuous_reward": semantic_complete,
        "required_semantic_dimensions": list(semantic_names),
        "semantic_strict_pass": semantic_strict,
        "oracle_exemption": oracle_run,
    },
    "semantic_review": {
        "complete": semantic_complete,
        "skipped": not semantic_complete,
        "non_deterministic": round(non_deterministic, 4),
        "scores": {name: round(score, 4) for name, score in semantic_scores.items()},
        "raw_rewardkit": judge,
    },
    "rewardkit_details": {
        "deterministic": det_details,
        "semantic": judge_details,
    },
    "process_artifacts": process_artifacts,
    "weights": {"deterministic": 55, "output_quality": 25, "process": 20},
}

details = dict(det_details)
for dimension, block in judge_details.items():
    if dimension == "process" and isinstance(details.get(dimension), dict) and isinstance(block, dict):
        merged = dict(details[dimension])
        merged["criteria"] = list(details[dimension].get("criteria") or []) + list(block.get("criteria") or [])
        merged.update({key: value for key, value in block.items() if key not in {"criteria", "score"}})
        merged["score"] = round(process_score, 4)
        details[dimension] = merged
    else:
        details[dimension] = block
if isinstance(details.get("process"), dict):
    details["process"]["score"] = round(process_score, 4)
if not details:
    details = {
        "deterministic": {"score": round(det_score, 4), "criteria": [], "kind": "programmatic"},
        "process": {"score": round(process_score, 4), "criteria": [], "kind": "programmatic"},
    }

(out / "semantic-execution.json").write_text(
    json.dumps(semantic_execution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(out / "reward.json").write_text(
    json.dumps(reward, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(out / "reward-details.json").write_text(
    json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(out / "final-reward-details.json").write_text(
    json.dumps(diagnostics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(out / "reward.txt").write_text(f"{binary:.4f}\n", encoding="utf-8")
PY
}

find_trajectory || true

# The solving phase has ended. Remove agent-owned observers before private
# verifier data is materialized, and restrict verifier sources and outputs.
pkill -KILL -u agent 2>/dev/null || true
chmod -R go-rwx "$TESTS_DIR" 2>/dev/null || true
chmod 0700 "$VERIFIER_DIR" 2>/dev/null || true

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
