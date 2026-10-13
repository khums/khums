"""Canonical RewardKit adapter for the deterministic pytest prepass."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("VERIFIER_DIR", os.environ.get("RESULT_DIR", "/logs/verifier")))
CONFIG = json.loads((HERE / "verifier.json").read_text(encoding="utf-8"))

NAMES = [f"test_explicit_public_group[{group}]" for group in CONFIG["public_groups"]]
NAMES += [f"test_implicit_upstream_behavior[{case['id']}]" for case in CONFIG["implicit"]]
NAMES += [f"test_private_url_behavior[{kind}]" for kind in CONFIG["private_cases"]]
NAMES += [f"test_implicit_redirect_behavior[{kind}]" for kind in CONFIG["implicit_redirect_cases"]]
NAMES += [f"test_private_http_behavior[{case}]" for case in CONFIG.get("private_http_cases", [])]
NAMES += ["test_required_artifacts"]
# Trajectory scope is reviewed by the validity judge (trajectory_scope_evidence.txt
# plus scope_helper.txt), not as a deterministic gate: static command parsing
# cannot see equivalent reads such as a recursive grep over /testbed.


def weight_for(name: str) -> int:
    if "explicit_public_group" in name:
        return 4 if "[G2]" in name else 8
    if "implicit_upstream" in name or "implicit_redirect_behavior" in name:
        return 4
    if "private_http_behavior" in name:
        return 6
    if "private_url_behavior" in name:
        case = name.partition("[")[2].removesuffix("]")
        return {"named_segment": 14, "wildcard_segment": 14,
                "supported_segment": 6, "nested_query": 6,
                "optional_segment": 4, "wildcard_unicode": 6}[case]
    return 2


WEIGHTS = {name: weight_for(name) for name in NAMES}


def _summary(rows: list[dict], reason: str) -> dict:
    total = sum(row["weight"] for row in rows)
    earned = sum(row["weight"] * row["score"] for row in rows)
    passed = sum(row["status"] == "passed" for row in rows)
    return {
        "strict_pass": bool(rows) and passed == len(rows),
        "strict_score": float(bool(rows) and passed == len(rows)),
        "deterministic_score": round(earned / total, 6) if total else 0.0,
        "passed_checks": passed,
        "failed_checks": sum(row["status"] == "failed" for row in rows),
        "error_checks": sum(row["status"] == "error" for row in rows),
        "skipped_checks": sum(row["status"] == "skipped" for row in rows),
        "total_checks": len(rows),
        "tests": rows,
        "by_name": {row["name"]: row for row in rows},
        "test_weights": WEIGHTS,
        "reason": reason,
    }


def _category(name: str, status: str, message: str, has_issue: bool) -> tuple[str, str]:
    if "SUBMISSION_ERROR:" in message:
        return "failed", "submission_scope_or_replay_error"
    if "VALIDATION_ERROR:" in message or "TimeoutExpired" in message:
        return "error", "validation_execution_error"
    if status == "error":
        return status, "setup_or_execution_error"
    if status == "skipped":
        return status, "not_executed"
    if name == "test_required_artifacts":
        return status, "artifact_contract" if has_issue else "passed"
    if "[production_build]" in name or "[type_contract]" in name:
        return status, "build_failure" if has_issue else "passed"
    return status, "behavioral_failure" if has_issue else "passed"


def run_prepass() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    junit = OUT / "deterministic-junit.xml"
    cmd = [sys.executable, "-m", "pytest", str(HERE / "pytest_checks.py"),
           "--junit-xml", str(junit), "-q", "-p", "no:cacheprovider", "--tb=short"]
    try:
        junit.unlink(missing_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1100)
        (OUT / "deterministic-pytest.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        root = ET.parse(junit).getroot()
        found = {}
        for case in root.iter("testcase"):
            name = case.get("name", "")
            failure, error, skipped = (case.find(tag) for tag in ("failure", "error", "skipped"))
            issue = failure if failure is not None else error if error is not None else skipped
            message = "" if issue is None else (issue.get("message") or "")[:1600]
            status = ("passed" if issue is None else "skipped" if skipped is not None
                      else "error" if error is not None else "failed")
            status, category = _category(name, status, message, issue is not None)
            found[name] = {
                "name": name, "status": status,
                "score": 1.0 if status == "passed" else 0.0,
                "weight": WEIGHTS.get(name, 0), "message": message,
                "failure_category": category,
            }
        rows = [found.get(name, {"name": name, "status": "error", "score": 0.0, "weight": weight,
                                "message": "pytest row missing",
                                "failure_category": "setup_or_execution_error"})
                for name, weight in WEIGHTS.items()]
        result = _summary(rows, "pytest completed" if proc.returncode == 0 else "pytest failures")
    except Exception as exc:
        rows = [{"name": name, "status": "error", "score": 0.0, "weight": weight,
                 "message": f"prepass infrastructure error: {exc}",
                 "failure_category": "setup_or_execution_error"} for name, weight in WEIGHTS.items()]
        result = _summary(rows, "deterministic prepass infrastructure error")
    (OUT / "deterministic_reward.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = [f"deterministic score={result['deterministic_score']:.6f}",
             f"{result['passed_checks']} passed; {result['failed_checks']} failed; "
             f"{result['error_checks']} errors; {result['skipped_checks']} skipped"]
    lines.extend(f"{row['name']}: {row['status']} [{row.get('failure_category', '')}] {row.get('message', '')}"
                 for row in rows)
    (OUT / "test_output.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


# The observation prepass needs pytest, not RewardKit discovery. In particular,
# retain its evidence even when the scoring dependency is unavailable.
if __name__ == "__main__" and "--prepass" in sys.argv:
    summary = run_prepass()
    raise SystemExit(0 if summary["strict_pass"] else 1)

import rewardkit as rk  # noqa: E402
from rewardkit import criterion  # noqa: E402


@criterion(description="Precomputed deterministic behavior {check_name}.")
def deterministic_check(workspace: Path, check_name: str) -> float:
    try:
        data = json.loads((OUT / "deterministic_reward.json").read_text(encoding="utf-8"))
        return float(data["by_name"][check_name]["score"])
    except Exception:
        return 0.0


for _name, _weight in WEIGHTS.items():
    rk.deterministic_check(_name, weight=_weight, name=_name)
