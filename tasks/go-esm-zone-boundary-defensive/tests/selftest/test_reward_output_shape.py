"""Verifier self-test: canonical output shape of tests/test.sh's reward merge.

Not part of grading (test.sh never runs it). Run it after any change to
tests/test.sh or tests/merge_rewards.py:

    python3 tests/selftest/test_reward_output_shape.py

It drives the real tests/test.sh with stub RewardKit and prepass binaries
(no network, no reproducer, no API calls) and asserts that the root
reward-details.json contains exactly the dimensions and process criteria
produced by that invocation:

  1. deterministic-only execution;
  2. deterministic plus a completed semantic judge pass;
  3. reuse of an existing verifier output directory (a judged run followed by
     a deterministic-only run) carries no stale judge artifacts;
  4. a semantic pass that ran but did not complete leaks nothing.

Needs bash and write access to /app/output (test.sh creates it); run it as
root in the task image or any disposable container.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
TEST_SH = TESTS_DIR / "test.sh"

DET_DIMS = {"deterministic", "process"}
JUDGE_DIMS = {"process", "output_quality", "validity", "task_quality"}
DET_PROCESS_CRITERIA = ["det_process_plan"]
JUDGE_PROCESS_CRITERIA = ["judge_process_review"]
JUDGE_ONLY_FILES = ("semantic_reward.json", "semantic-reward-details.json")

# Stub RewardKit: writes a deterministic or judge result shaped like the real
# tool's (reward.json plus a sibling reward-details.json). STUB_JUDGE_MODE=
# "partial" simulates a judge pass that returns without every dimension.
REWARDKIT_STUB = textwrap.dedent(
    """\
    import json, os, sys
    from pathlib import Path
    suite = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("--output") + 1])
    out.parent.mkdir(parents=True, exist_ok=True)
    def crit(name):
        return {"name": name, "score": 1.0}
    if "judge" in suite.name:
        dims = ["process", "output_quality", "validity", "task_quality"]
        if os.environ.get("STUB_JUDGE_MODE") == "partial":
            dims = ["process", "output_quality"]
        reward = {d: 1.0 for d in dims}
        details = {d: {"score": 1.0, "kind": "llm",
                       "criteria": [crit("judge_process_review" if d == "process" else "judge_" + d)]}
                   for d in dims}
    else:
        reward = {"deterministic": 1.0, "process": 1.0}
        details = {
            "deterministic": {"score": 1.0, "kind": "programmatic", "criteria": [crit("det_case_a")]},
            "process": {"score": 1.0, "kind": "programmatic", "criteria": [crit("det_process_plan")]},
        }
    out.write_text(json.dumps(reward))
    (out.parent / "reward-details.json").write_text(json.dumps(details))
    """
)

# Stub python: intercepts only the deterministic prepass; everything else
# (merge_rewards.py, inline helpers) runs on the real interpreter.
PYTHON_STUB = textwrap.dedent(
    """\
    #!/bin/sh
    case "$1" in
      */deterministic/check.py)
        printf '{"deterministic_score": 1.0, "strict_pass": true}\\n' > "$VERIFIER_DIR/deterministic_reward.json"
        printf '{"plan_md": {"score": 1.0}}\\n' > "$VERIFIER_DIR/process_artifacts.json"
        exit 0 ;;
    esac
    exec "{real_python}" "$@"
    """
)


class RewardOutputShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="reward-shape-"))
        self.result_dir = self.tmp / "verifier"
        self.workspace = self.tmp / "testbed"
        self.workspace.mkdir()
        rk = self.tmp / "rewardkit"
        rk.write_text(f"#!{sys.executable}\n" + REWARDKIT_STUB)
        rk.chmod(0o755)
        py = self.tmp / "python"
        py.write_text(PYTHON_STUB.replace("{real_python}", sys.executable))
        py.chmod(0o755)
        trajectory = self.tmp / "trajectory.json"
        trajectory.write_text("[]")
        self.base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "RESULT_DIR": str(self.result_dir),
            "TESTBED": str(self.workspace),
            "REWARDKIT_BIN": str(rk),
            "PYTHON_BIN": str(py),
            "TRAJECTORY_PATH": str(trajectory),
            "RUN_ROLE": "agent",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verifier(self, *, judge: bool, judge_mode: str = "complete") -> dict:
        env = dict(self.base_env)
        if judge:
            env["ANTHROPIC_API_KEY"] = "selftest-not-a-real-key"
            env["STUB_JUDGE_MODE"] = judge_mode
        proc = subprocess.run(["bash", str(TEST_SH)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.result_dir / "reward-details.json").read_text())

    def semantic_status(self) -> str:
        return json.loads((self.result_dir / "semantic-execution.json").read_text())["status"]

    def process_criteria(self, details: dict) -> list[str]:
        return [c["name"] for c in details["process"]["criteria"]]

    def assert_deterministic_only(self, details: dict) -> None:
        self.assertEqual(set(details), DET_DIMS)
        self.assertEqual(self.process_criteria(details), DET_PROCESS_CRITERIA)
        reward = json.loads((self.result_dir / "reward.json").read_text())
        self.assertFalse(set(reward) & {"output_quality", "validity", "task_quality"}, reward)
        for name in JUDGE_ONLY_FILES:
            self.assertFalse((self.result_dir / name).exists(), f"stale {name}")
        self.assertFalse((self.result_dir / "rewardkit_judge").exists())

    def test_1_deterministic_only(self) -> None:
        details = self.run_verifier(judge=False)
        self.assertEqual(self.semantic_status(), "skipped")
        self.assert_deterministic_only(details)

    def test_2_deterministic_plus_semantic(self) -> None:
        details = self.run_verifier(judge=True)
        self.assertEqual(self.semantic_status(), "completed")
        self.assertEqual(set(details), DET_DIMS | JUDGE_DIMS)
        self.assertEqual(self.process_criteria(details), DET_PROCESS_CRITERIA + JUDGE_PROCESS_CRITERIA)
        for name in JUDGE_ONLY_FILES:
            self.assertTrue((self.result_dir / name).exists(), name)

    def test_3_reused_output_dir_has_no_stale_judge_artifacts(self) -> None:
        self.run_verifier(judge=True)
        details = self.run_verifier(judge=False)
        self.assertEqual(self.semantic_status(), "skipped")
        self.assert_deterministic_only(details)

    def test_4_incomplete_semantic_pass_leaks_nothing(self) -> None:
        details = self.run_verifier(judge=True, judge_mode="partial")
        self.assertEqual(self.semantic_status(), "skipped")
        self.assert_deterministic_only(details)


if __name__ == "__main__":
    unittest.main(verbosity=2)
