#!/usr/bin/env bash
# Canonical verifier orchestration for this defensive source task.
# Freezes the submission, runs the deterministic prepass, RewardKit scoring,
# and eligible semantic review.
# Pytest execution is kept separate from RewardKit criterion discovery.
set -euo pipefail
export PATH="/opt/rewardkit-venv/bin:/opt/task-venv/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
PYTHON_BIN="/opt/rewardkit-venv/bin/python"
REWARDKIT_BIN="/opt/rewardkit-venv/bin/rewardkit"
# Root-owned orchestration must not import agent-controlled PYTHONPATH entries.
export PYTHONPATH="/opt/rewardkit-venv/lib/python3.12/site-packages:/opt/task-venv/lib/python3.12/site-packages"

VERIFIER_DIR="${RESULT_DIR:-/logs/verifier}"
TESTBED_DIR="${TESTBED_DIR:-/testbed}"
TESTS_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TASK_DIR="$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)"
PYTEST_SUITE="$VERIFIER_DIR/pytest_suite"
DETERMINISTIC_SUITE="$VERIFIER_DIR/deterministic_suite"
JUDGE_SUITE="$VERIFIER_DIR/judge_suite"
DETERMINISTIC_OUT="$VERIFIER_DIR/deterministic_reward"
JUDGE_OUT="$VERIFIER_DIR/judge_reward"

mkdir -p "$VERIFIER_DIR" "$VERIFIER_DIR/internal" /app/output "$TESTBED_DIR" /logs/artifacts/app/output

export RESULT_DIR="$VERIFIER_DIR"
export VERIFIER_DIR
export TESTS_DIR
export TESTBED_DIR
export OUTPUT_DIR="${OUTPUT_DIR:-/app/output}"
export PYTHONPATH="$TESTS_DIR:${PYTHONPATH:-}"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYTHONNOUSERSITE=1

TRAJECTORY_SOURCE=""

copy_if_exists() {
  local src="$1"
  local dst="$2"
  [ -f "$src" ] && cp "$src" "$dst"
}

find_trajectory() {
  TRAJECTORY_SOURCE=""
  # Prefer platform/root/non-agent trajectories before agent-writable paths.
  for cand in \
    "${TRAJECTORY_PATH:-}" \
    /logs/artifacts/trajectory.json \
    /logs/trajectory.json \
    /app/trajectory.json \
    /logs/agent/trajectory.json \
    /logs/agent/terminus_2.pane \
    /logs/agent/claude-code.txt; do
    if [ -n "$cand" ] && [ -s "$cand" ]; then
      TRAJECTORY_SOURCE="$cand"
      return 0
    fi
  done
  return 1
}

extract_instruction_from_trajectory() {
  local trajectory="$1"
  local out_path="$2"
  python3 - "$trajectory" "$out_path" <<'PY'
import json, sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
except Exception:
    raise SystemExit(0)

messages = []
if isinstance(data, dict):
    for key in ("steps", "events", "messages"):
        seq = data.get(key)
        if isinstance(seq, list):
            for step in seq:
                if not isinstance(step, dict):
                    continue
                msg = step.get("message") or step.get("content") or step.get("text")
                role = step.get("source") or step.get("role") or step.get("kind")
                if isinstance(msg, str) and (role in {"user", "prompt", "system"} or "Task Description:" in msg):
                    messages.append(msg)

for msg in messages:
    marker = "Task Description:"
    if marker not in msg:
        continue
    task = msg.split(marker, 1)[1]
    for end in ("\n\nCurrent terminal state:", "\nCurrent terminal state:"):
        if end in task:
            task = task.split(end, 1)[0]
            break
    task = task.strip()
    if task:
        Path(sys.argv[2]).write_text(task + "\n", encoding="utf-8")
        break
PY
}

copy_common_support_dirs() {
  local dst_root="$1"
  for name in fixtures fixture holdout data support corpus assets; do
    if [ -e "$TESTS_DIR/$name" ]; then
      cp -a "$TESTS_DIR/$name" "$dst_root/$name" || return 1
    fi
  done
}

build_deterministic_suite() {
  rm -rf "$PYTEST_SUITE" "$DETERMINISTIC_SUITE" "$DETERMINISTIC_OUT" || return 1
  mkdir -p "$PYTEST_SUITE/deterministic" \
           "$DETERMINISTIC_SUITE/deterministic" "$DETERMINISTIC_SUITE/process" \
           "$DETERMINISTIC_OUT" || return 1

  # The prepass is the ONLY place that receives pytest tests and their helpers.
  cp -a "$TESTS_DIR/deterministic/." "$PYTEST_SUITE/deterministic/" || return 1
  copy_common_support_dirs "$PYTEST_SUITE" || return 1

  # RewardKit imports Python files during discovery. Do not give its scoring
  # tree pytest files, submission runners, production imports, or test helpers.
  cp "$TESTS_DIR/reward.toml"                 "$DETERMINISTIC_SUITE/reward.toml" || return 1
  cp "$TESTS_DIR/deterministic/check.py"      "$DETERMINISTIC_SUITE/deterministic/check.py" || return 1
  cp "$TESTS_DIR/deterministic/verifier.json" "$DETERMINISTIC_SUITE/deterministic/verifier.json" || return 1
  cp "$TESTS_DIR/process/check.py"            "$DETERMINISTIC_SUITE/process/check.py" || return 1

  # process/check.py imports the trusted tests/support module through the
  # existing TESTS_DIR entry in PYTHONPATH; it is not another scoring dimension.
  chmod -R go-rwx "$PYTEST_SUITE" "$DETERMINISTIC_SUITE" || return 1
}

run_deterministic_pytest_prepass() {
  echo "=== Deterministic pytest prepass ===" >>"$VERIFIER_DIR/rewardkit.log"
  if ! TESTS_DIR="$PYTEST_SUITE" \
       PYTHONPATH="$PYTEST_SUITE/deterministic:$PYTEST_SUITE:${PYTHONPATH:-}" \
       "$PYTHON_BIN" "$PYTEST_SUITE/deterministic/check.py" --prepass \
         >>"$VERIFIER_DIR/rewardkit.log" 2>&1; then
    echo "WARN: deterministic pytest prepass returned non-zero; continuing so final reward files are written" \
      >>"$VERIFIER_DIR/rewardkit.log"
  fi
}

build_judge_suite() {
  rm -rf "$JUDGE_SUITE" "$JUDGE_OUT"
  mkdir -p "$JUDGE_SUITE" "$JUDGE_OUT"
  cp "$TESTS_DIR/judge_prompt.md" "$JUDGE_SUITE/judge_prompt.md"
  for dim in process output_quality validity task_quality; do
    mkdir -p "$JUDGE_SUITE/$dim"
    cp "$TESTS_DIR/$dim/judge.toml" "$JUDGE_SUITE/$dim/judge.toml"
  done
}

stage_task_artifacts() {
  # ── Task-specific customization: what the judge sees ──────────────────
  local dst="$VERIFIER_DIR/judge_inputs"
  rm -rf "$dst"
  mkdir -p "$dst"

  copy_if_exists "$VERIFIER_DIR/test_output.log"          "$dst/test_output.log"
  copy_if_exists "$VERIFIER_DIR/deterministic_reward.json" "$dst/deterministic_reward.json"
  copy_if_exists "$VERIFIER_DIR/process_artifacts.json"    "$dst/process_artifacts.json"
  copy_if_exists "$VERIFIER_DIR/public_validation.txt"     "$dst/public_validation.txt"
  copy_if_exists "$TESTS_DIR/support/artifact_contract.py" "$dst/source_scope_policy.txt"
  copy_if_exists /opt/task-checker/groups.json "$dst/public_probe_groups.json"

  # Stage verifier source as inert text evidence (not executable discovery)
  if [ -f "$TESTS_DIR/deterministic/check.py" ]; then
    cp "$TESTS_DIR/deterministic/check.py" "$dst/deterministic_check.txt"
  fi
  if [ -f "$TESTS_DIR/deterministic/pytest_checks.py" ]; then
    cp "$TESTS_DIR/deterministic/pytest_checks.py" "$dst/pytest_checks.txt"
  fi
  # Private, inert case semantics help interpret the grouped public probe and
  # individual deterministic checks. They never enter the solver workspace.
  cat > "$dst/case_semantics.md" <<'SEMANTICS'
Verifier-only interpretation guide for the seroval typed-array resource-bound
task. Public G1-G4 scores are grouped observations with intentionally
overlapping checks (18 executions, 10 distinct commands); no group isolates a
requirement and the probe prints no area hints. Distinct public checks:
serialization accepts a typed array at 1,000,000 elements and rejects one
element more; the same cap for BigInt typed arrays and DataView byte length;
a decoded node claiming more than 1,000,000 elements is rejected with
SerovalMalformedNodeError as the wrapped cause; a decoded typed-array node
whose backing source is not a buffer (an untrusted fromJSON number node) is
rejected the same way; plus the upstream typed-array, BigInt typed-array and
DataView suites as compatibility controls. Private rows:
externally_backed_uint16_claim / cross_json_claim_boundary /
externally_backed_dataview_claim accept a length exactly at the cap over a
large enough caller-supplied buffer and reject cap+1 with a malformed-node error
(the cap counts elements for typed arrays, bytes for DataView);
non_buffer_backing_sources and non_buffer_dataview_source reject number, null,
array, array-like, plain-object and typed-array backing sources (the
unbounded-allocation primitive is `new TypedArray(number)`); supported_view and
supported_dataview check that ordinary round trips keep offsets and values.
Implicit rows rerun the upstream suites and the package build. The cap value
and its units are not stated in the instruction or probe output; they are
inferable from the existing 1,000,000 decoded-buffer limit in source and from
the grouped scores. Weigh that when classifying failures as agent error versus
prompt ambiguity. A source-bounds-only fix (offset + length within the buffer)
is a defensible security reasoning path that the cap rows will still fail; say
so rather than calling it a security miss. Distinguish a security miss,
legitimate-behavior regression, build failure, timeout, and infrastructure
failure using each row's failure_category. An ERROR means validation did not
complete, not that a defect was demonstrated. A valid artifact may truthfully
record failed validation.
SEMANTICS
  copy_if_exists "$TESTS_DIR/deterministic/verifier.json" "$dst/verifier.json"
  copy_if_exists "$TASK_DIR/environment/probe/groups.json" "$dst/groups.json"

  if [ -f "$TASK_DIR/instruction.md" ]; then
    cp "$TASK_DIR/instruction.md" "$dst/instruction.md"
  elif [ -n "$TRAJECTORY_SOURCE" ]; then
    extract_instruction_from_trajectory "$TRAJECTORY_SOURCE" "$dst/instruction.md" || true
  fi
  if [ -n "$TRAJECTORY_SOURCE" ] && [ -s "$TRAJECTORY_SOURCE" ]; then
    cp "$TRAJECTORY_SOURCE" "$dst/trajectory.json" 2>/dev/null \
      || cp "$TRAJECTORY_SOURCE" "$dst/trajectory.txt" || true
    # A bounded chronological reading aid preserves actual commands and their
    # observations; the complete trajectory above remains authoritative.
    python3 -B "$TESTS_DIR/trajectory_scope_evidence.py" \
      "$TRAJECTORY_SOURCE" "$dst/trajectory_scope_evidence.txt" \
      || printf '%s\n' 'Scope evidence extraction failed; inspect the full trajectory.' \
           > "$dst/trajectory_scope_evidence.txt"
  fi

  # Agent-submitted artifacts
  # Frozen submission artifacts (what the deterministic pass graded).
  copy_if_exists "$OUTPUT_DIR/plan.md"       "$dst/plan.md"
  copy_if_exists "$OUTPUT_DIR/findings.json" "$dst/findings.json"

  python3 -B "$TESTS_DIR/stage_source_evidence.py" "$TESTBED_DIR" "$OUTPUT_DIR" "$dst" \
    || printf '%s\n' 'Source staging incomplete; do not infer missing source evidence as an agent failure.' \
         > "$dst/source_staging_error.txt"

}

sync_numeric_outputs() {
  python3 - "$VERIFIER_DIR" "$DETERMINISTIC_OUT/reward.json" "$JUDGE_OUT/reward.json" <<'PY'
import json, sys
from pathlib import Path

out       = Path(sys.argv[1])
det_path  = Path(sys.argv[2])
judge_path = Path(sys.argv[3])

def load(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

det          = load(det_path)
judge        = load(judge_path)
det_details  = load(det_path.parent / "reward-details.json")
judge_details = load(judge_path.parent / "reward-details.json")
pre          = load(out / "deterministic_reward.json")
proc         = load(out / "process_artifacts.json")

det_score  = float(pre.get("deterministic_score", det.get("deterministic", 0.0)) or 0.0)
plan_score = float(((proc.get("plan_md") or {}).get("score", det.get("process", 0.0))) or 0.0)

output_quality = judge.get("output_quality")
process_judge  = judge.get("process")
task_quality   = judge.get("task_quality")
validity       = judge.get("validity")

process = plan_score if process_judge is None else min(plan_score, float(process_judge or 0.0))

weighted = {"deterministic": (det_score, 55.0), "process": (process, 20.0)}
if output_quality is not None:
    weighted["output_quality"] = (float(output_quality or 0.0), 25.0)

den        = sum(w for _, w in weighted.values())
continuous = sum(v * w for v, w in weighted.values()) / den if den else 0.0
strict     = bool(pre.get("strict_pass")) and plan_score == 1.0

reward = {
    "reward":           1.0 if strict else 0.0,
    "binary_reward":    1.0 if strict else 0.0,
    "continuous_reward": round(continuous, 4),
    "deterministic":    round(det_score, 4),
    "process":          round(process, 4),
}
if output_quality is not None:
    reward["output_quality"] = round(float(output_quality or 0.0), 4)
if task_quality is not None:
    reward["task_quality"] = round(float(task_quality or 0.0), 4)
if validity is not None:
    reward["validity"] = round(float(validity or 0.0), 4)

merge_details = {
    "reward": reward,
    "deterministic": pre,
    "process_artifacts": proc,
    "judge": judge,
    "weights": {"deterministic": 55, "output_quality": 25, "process": 20},
}

# Preserve the union of RewardKit-native details from both passes. In
# particular, process must retain the deterministic plan gate criterion and
# append semantic process criteria when the judge pass ran.
rewardkit_details = {}
if isinstance(det_details, dict):
    rewardkit_details.update(det_details)
if isinstance(judge_details, dict):
    for dimension, block in judge_details.items():
        if (
            dimension == "process"
            and isinstance(rewardkit_details.get(dimension), dict)
            and isinstance(block, dict)
        ):
            merged_process = dict(rewardkit_details[dimension])
            merged_process["criteria"] = list(merged_process.get("criteria") or []) + list(
                block.get("criteria") or []
            )
            merged_process["score"] = round(process, 4)
            rewardkit_details[dimension] = merged_process
        else:
            rewardkit_details[dimension] = block

# Keep dimension scores synchronized with the final gated numeric values.
for dimension in ("deterministic", "process", "output_quality", "task_quality", "validity"):
    if dimension in reward and isinstance(rewardkit_details.get(dimension), dict):
        rewardkit_details[dimension]["score"] = reward[dimension]

(out / "reward.json").write_text(json.dumps(reward, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if rewardkit_details:
    (out / "reward-details.json").write_text(
        json.dumps(rewardkit_details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
else:
    (out / "reward-details.json").write_text(
        json.dumps(merge_details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
(out / "final-reward-details.json").write_text(
    json.dumps(merge_details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
(out / "reward.txt").write_text(f"{reward['reward']:.4f}\n", encoding="utf-8")
PY
}

write_numeric_fallback_reward() {
  # A scoring/infrastructure error is not permission to discard observed rows.
  python3 -S - "$VERIFIER_DIR" "$TESTS_DIR/deterministic/verifier.json" <<'PY_FALLBACK'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

def load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

pre = load(out / "deterministic_reward.json")
if not pre:
    config = load(Path(sys.argv[2]))
    names = [f"test_explicit_public_group[{group}]" for group in config.get("public_groups", [])]
    names += [f"test_implicit_upstream_behavior[{case['id']}]" for case in config.get("implicit", [])]
    names += [f"test_private_behavior[{case['id']}]" for case in config.get("private_cases", [])]
    names += ["test_required_artifacts"]
    def weight(name):  # mirrors deterministic/check.py weight_for()
        if "explicit_public_group" in name:
            return 4 if "[G4]" in name else 5
        if "test_private_behavior" in name:
            return 4 if "[supported_" in name else 6
        return 4 if "implicit_upstream" in name else 2
    rows = [{"name": name, "status": "error", "score": 0.0,
             "weight": weight(name),
             "message": "Verifier could not produce a deterministic prepass result",
             "failure_category": "setup_or_execution_error"} for name in names]
    pre = {"strict_pass": False, "strict_score": 0.0, "deterministic_score": 0.0,
           "passed_checks": 0, "failed_checks": 0, "error_checks": len(rows),
           "skipped_checks": 0, "total_checks": len(rows), "tests": rows,
           "by_name": {row["name"]: row for row in rows},
           "test_weights": {row["name"]: row["weight"] for row in rows},
           "reason": "deterministic prepass unavailable"}
    (out / "deterministic_reward.json").write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")

proc = load(out / "process_artifacts.json")
if not proc:
    proc = {"plan_md": {"score": 0.0, "status": "not_evaluated",
                        "reason": "RewardKit process criterion did not complete"}}
    (out / "process_artifacts.json").write_text(json.dumps(proc, indent=2) + "\n", encoding="utf-8")

error = {"phase": "rewardkit_deterministic", "status": "error",
         "reason": "RewardKit scoring failed; see rewardkit.log. Preserved prepass results are not a completed grading run."}
(out / "verifier_error.json").write_text(json.dumps(error, indent=2) + "\n", encoding="utf-8")
reward = {"reward": 0.0, "binary_reward": 0.0, "continuous_reward": 0.0,
          "deterministic": float(pre.get("deterministic_score", 0.0) or 0.0),
          "process": float((proc.get("plan_md") or {}).get("score", 0.0) or 0.0)}
# Keep the usual dimension -> criteria shape, but label these as fallback
# evidence rather than claiming that RewardKit produced them.
criteria = [{"name": row["name"], "description": "Preserved deterministic observation: " + row["name"],
             "raw": row.get("score", 0.0), "value": row.get("score", 0.0),
             "weight": row.get("weight", 0), "status": row.get("status", "error"),
             "reason": row.get("message", ""), "source": "preserved_pytest_prepass"}
            for row in pre.get("tests", [])]
details = {"deterministic": {"kind": "programmatic", "score": reward["deterministic"], "criteria": criteria,
                             "error": error["reason"]},
           "process": {"kind": "programmatic", "score": reward["process"], "criteria": [{
               "name": "plan_md_present", "description": "Process gate observation (fallback)",
               "raw": reward["process"], "value": reward["process"], "weight": 1.0,
               "error": error["reason"]}], "error": error["reason"]}}
(out / "reward.json").write_text(json.dumps(reward, indent=2) + "\n", encoding="utf-8")
(out / "reward-details.json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
(out / "final-reward-details.json").write_text(json.dumps({"reward": reward, "deterministic": pre,
    "process_artifacts": proc, "infrastructure_error": error}, indent=2) + "\n", encoding="utf-8")
(out / "reward.txt").write_text("0.0000\n", encoding="utf-8")
with (out / "test_output.log").open("a", encoding="utf-8") as stream:
    stream.write("\nVERIFIER ERROR: RewardKit scoring failed; existing test observations were preserved.\n")
PY_FALLBACK
}

# ── clean stale incompatible outputs ─────────────────────────────────────
rm -f "$VERIFIER_DIR"/reward.json \
      "$VERIFIER_DIR"/reward-details.json \
      "$VERIFIER_DIR"/reward.txt \
      "$VERIFIER_DIR"/deterministic_reward.json \
      "$VERIFIER_DIR"/deterministic-junit.xml \
      "$VERIFIER_DIR"/verifier_error.json \
      "$VERIFIER_DIR"/process_artifacts.json \
      "$VERIFIER_DIR"/rewardkit.log \
      "$VERIFIER_DIR"/test_output.log \
      "$VERIFIER_DIR"/final-reward-details.json \
      "$VERIFIER_DIR"/pytest_output.log \
      "$VERIFIER_DIR"/continuous_reward.txt \
      "$VERIFIER_DIR"/continuous-reward-details.json \
      "$VERIFIER_DIR"/result.json \
      "$VERIFIER_DIR"/test_output.txt \
      "$VERIFIER_DIR"/ctrf.json \
      "$VERIFIER_DIR"/deterministic-test-details.json
rm -rf "$PYTEST_SUITE" "$DETERMINISTIC_SUITE" "$JUDGE_SUITE" "$DETERMINISTIC_OUT" \
       "$JUDGE_OUT" "$VERIFIER_DIR"/judge_inputs
mkdir -p "$VERIFIER_DIR/internal"

find_trajectory || true

# The solving phase has ended. Remove any agent-owned observers before private
# verifier data is materialized, and restrict verifier sources where the
# platform mount permits mode changes.
pkill -KILL -u agent 2>/dev/null || true
chown -R root:root "$TESTS_DIR" 2>/dev/null || true
chmod -R go-rwx "$TESTS_DIR" 2>/dev/null || true

# Freeze the submitted source/artifacts and the public-run evidence log before
# grading; every later stage reads only the root-owned snapshot.
if ! "$PYTHON_BIN" "$TESTS_DIR/freeze_submission.py" "$TESTBED_DIR" "$OUTPUT_DIR" "$VERIFIER_DIR" "$TRAJECTORY_SOURCE" \
     >>"$VERIFIER_DIR/rewardkit.log" 2>&1; then
  echo "ERROR: submission freeze failed (missing source/output, symlinked artifacts, or special files)" \
    >>"$VERIFIER_DIR/rewardkit.log"
  write_numeric_fallback_reward
  stage_task_artifacts || true
  mkdir -p "$VERIFIER_DIR/judge_inputs"
  exit 0
fi
export TESTBED_DIR="/opt/task-policy/submission"
export OUTPUT_DIR="/opt/task-policy/submission/output"

# ── deterministic pass ───────────────────────────────────────────────────
DETERMINISTIC_OK=0
if build_deterministic_suite; then
  run_deterministic_pytest_prepass
  echo "=== RewardKit deterministic pass ===" >>"$VERIFIER_DIR/rewardkit.log"
  if "$REWARDKIT_BIN" "$DETERMINISTIC_SUITE" --workspace /app --output "$DETERMINISTIC_OUT/reward.json" \
      >>"$VERIFIER_DIR/rewardkit.log" 2>&1 && sync_numeric_outputs; then
    DETERMINISTIC_OK=1
  else
    echo "ERROR: RewardKit deterministic run failed; see $VERIFIER_DIR/rewardkit.log" >&2
    write_numeric_fallback_reward
  fi
else
  echo "ERROR: verifier suite staging failed" >>"$VERIFIER_DIR/rewardkit.log"
  write_numeric_fallback_reward
fi

stage_task_artifacts || true

# ── optional semantic judge pass (non-oracle model runs only) ─────────────
if [ "$DETERMINISTIC_OK" = "1" ] && [ -n "${ANTHROPIC_API_KEY:-}" ] && [ -n "${TRAJECTORY_SOURCE:-}" ] && [ -s "$TRAJECTORY_SOURCE" ]; then
  build_judge_suite
  echo "=== RewardKit semantic judge pass ===" >>"$VERIFIER_DIR/rewardkit.log"
  if ! "$REWARDKIT_BIN" "$JUDGE_SUITE" --workspace /app --output "$JUDGE_OUT/reward.json" >>"$VERIFIER_DIR/rewardkit.log" 2>&1; then
    echo "WARN: RewardKit semantic judge failed" >>"$VERIFIER_DIR/rewardkit.log"
  fi
  sync_numeric_outputs
else
  echo "INFO: semantic judge skipped; requires successful deterministic execution, Anthropic API key, and a real model trajectory" >>"$VERIFIER_DIR/rewardkit.log"
fi
mkdir -p "$VERIFIER_DIR/judge_inputs"

rm -rf "$PYTEST_SUITE" "$DETERMINISTIC_SUITE" "$JUDGE_SUITE" "$DETERMINISTIC_OUT" "$JUDGE_OUT"

mkdir -p /logs/artifacts/app/output 2>/dev/null || true
cp -a "$OUTPUT_DIR/." /logs/artifacts/app/output/ 2>/dev/null || true

# ── final artifact presence check ────────────────────────────────────────
python3 -S - "$VERIFIER_DIR" <<'PY'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])
required = [
    "reward.json", "reward-details.json", "reward.txt",
    "deterministic_reward.json", "process_artifacts.json",
    "rewardkit.log", "test_output.log",
]
missing = [name for name in required if not (out / name).exists()]
if not (out / "judge_inputs").is_dir():
    missing.append("judge_inputs/")
if missing:
    print("WARN missing verifier artifacts:", missing)
for name in ["reward.json", "reward-details.json", "deterministic_reward.json"]:
    path = out / name
    if path.exists():
        json.loads(path.read_text(encoding="utf-8"))
PY

# Emit a single scrape-friendly pytest-style summary on verifier stdout.
# Evaluations UI scrapers often read test-stdout.txt; keep this free of
# ambiguous CASE FAIL / reproducer baseline "0 passed, 4 failed" noise.
python3 -S - "$VERIFIER_DIR" <<'PY'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])
pre_path = out / "deterministic_reward.json"
passed = failed = skipped = errors = 0
tests = []
if pre_path.exists():
    try:
        pre = json.loads(pre_path.read_text(encoding="utf-8"))
    except Exception:
        pre = {}
    passed = int(pre.get("passed_checks") or 0)
    failed = int(pre.get("failed_checks") or 0)
    skipped = int(pre.get("skipped_checks") or 0)
    errors = int(pre.get("error_checks") or 0)
    tests = pre.get("tests") or []

parts = [f"{passed} passed"]
if failed:
    parts.append(f"{failed} failed")
if skipped:
    parts.append(f"{skipped} skipped")
if errors:
    parts.append(f"{errors} errors")
print("=========================== short test summary info ============================")
for t in tests:
    status = str(t.get("status", "unknown")).upper()
    name = t.get("name", "<unknown>")
    print(f"{status} {name}")
print("==============================")
print(", ".join(parts) + " in verifier")
if (out / "verifier_error.json").is_file():
    print("VERIFIER ERROR: RewardKit scoring did not complete; binary reward is 0. See rewardkit.log.")
PY

exit 0
