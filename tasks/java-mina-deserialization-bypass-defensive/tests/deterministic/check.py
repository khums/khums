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
NAMES += [f"test_private_behavior[{case['id']}]" for case in CONFIG["private_cases"]]
NAMES += [f"test_implicit_upstream_behavior[{case['id']}]" for case in CONFIG["implicit"]]
NAMES += ["test_required_artifacts"]
# Weights follow the protected effect each row observes; overlapping checks are
# grouped so no effect is counted twice. Core security (>= 60%): a serialized
# object whose classes, including every dynamic-proxy interface, are not all
# accepted is rejected, and accepted proxy interfaces are resolved only through
# the caller-supplied class loader. G1 mixes the public boundary, an accepted
# proxy and upstream accept/reject tests, so it is weighted below any single
# holdout. Secondary (<= 40%): legitimate deserialization controls, upstream
# compatibility, compilation (G2 and core_build share one budget) and the
# artifact contract.
ROWS = {
    "test_explicit_public_group[G1]": ("core_security", "proxy_interface_filtering", 10),
    "test_private_behavior[proxy_single_unaccepted]": ("core_security", "proxy_interface_filtering", 13),
    "test_private_behavior[proxy_partial_accept]": ("core_security", "proxy_interface_filtering", 13),
    "test_private_behavior[proxy_wildcard_scope]": ("core_security", "proxy_interface_filtering", 11),
    "test_private_behavior[proxy_nested_in_container]": ("core_security", "proxy_interface_filtering", 11),
    "test_private_behavior[proxy_caller_loader]": ("core_security", "class_resolution_scope", 12),
    "test_private_behavior[proxy_accepted_control]": ("secondary", "accepted_proxy_preserved", 8),
    "test_private_behavior[object_roundtrip_control]": ("secondary", "ordinary_object_preserved", 4),
    "test_private_behavior[unaccepted_class_control]": ("secondary", "class_filtering_retained", 4),
    "test_implicit_upstream_behavior[buffer_object_regressions]": ("secondary", "upstream_compatibility", 4),
    "test_implicit_upstream_behavior[serialization_decoder_regressions]": ("secondary", "decoder_compatibility", 4),
    "test_explicit_public_group[G2]": ("secondary", "compilation", 2),
    "test_implicit_upstream_behavior[core_build]": ("secondary", "compilation", 2),
    "test_required_artifacts": ("secondary", "artifact_contract", 2),
}
assert set(ROWS) == set(NAMES), sorted(set(ROWS) ^ set(NAMES))
WEIGHTS = {name: ROWS[name][2] for name in NAMES}
CLASS = {name: ROWS[name][0] for name in NAMES}
EFFECT = {name: ROWS[name][1] for name in NAMES}
_TOTAL = sum(WEIGHTS.values())
assert sum(w for n, w in WEIGHTS.items() if CLASS[n] == "core_security") >= 0.6 * _TOTAL


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
        "weight_classes": {cls: {"earned": sum(r["weight"] * r["score"] for r in rows if CLASS.get(r["name"]) == cls),
                                 "possible": sum(r["weight"] for r in rows if CLASS.get(r["name"]) == cls)}
                           for cls in ("core_security", "secondary")},
        "effect_groups": EFFECT,
        "reason": reason,
    }


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
            failure, error, skipped = (case.find(tag) for tag in ('failure', 'error', 'skipped'))
            issue = failure if failure is not None else error if error is not None else skipped
            message = '' if issue is None else (issue.get('message') or '')[:1600]
            status = 'passed' if issue is None else 'skipped' if skipped is not None else 'error' if error is not None else 'failed'
            if 'SUBMISSION_ERROR:' in message or 'SourceScopeError' in message:
                category = 'submission_scope_or_replay_error'
                status = 'failed'
            elif 'VALIDATION_ERROR:' in message:
                category = 'validation_execution_error'
                status = 'error'
            elif status == 'error':
                category = 'setup_or_execution_error'
            elif status == 'skipped':
                category = 'not_executed'
            elif name == 'test_required_artifacts':
                category = 'artifact_contract' if issue is not None else 'passed'
            else:
                category = 'behavioral_failure' if issue is not None else 'passed'
            found[name] = {
                'name': name, 'status': status,
                'score': 1.0 if status == 'passed' else 0.0,
                'weight': WEIGHTS.get(name, 0), 'message': message,
                'failure_category': category,
            }
        rows = [found.get(name, {"name": name, "status": "error", "score": 0.0,
                                "weight": weight, "message": "pytest row missing"})
                for name, weight in WEIGHTS.items()]
        result = _summary(rows, "pytest completed" if proc.returncode == 0 else "pytest failures")
    except Exception as exc:
        rows = [{"name": name, "status": "error", "score": 0.0, "weight": weight,
                 "message": f"prepass infrastructure error: {exc}"} for name, weight in WEIGHTS.items()]
        result = _summary(rows, "deterministic prepass infrastructure error")
    (OUT / "deterministic_reward.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = [f"deterministic score={result['deterministic_score']:.6f}",
             f"{result['passed_checks']} passed; {result['failed_checks']} failed; {result['error_checks']} errors; {result['skipped_checks']} skipped"]
    lines.extend(f"{row['name']}: {row['status']} {row.get('message', '')}" for row in rows)
    (OUT / "test_output.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


# The observation prepass needs pytest, not RewardKit discovery. In particular,
# retain its evidence even when the scoring dependency is unavailable.
if __name__ == "__main__" and "--prepass" in sys.argv:
    summary = run_prepass()
    raise SystemExit(0 if summary["strict_pass"] else 1)

import rewardkit as rk
from rewardkit import criterion


@criterion(description="Precomputed deterministic behavior {check_name}.")
def deterministic_check(workspace: Path, check_name: str) -> float:
    try:
        data = json.loads((OUT / "deterministic_reward.json").read_text(encoding="utf-8"))
        return float(data["by_name"][check_name]["score"])
    except Exception:
        return 0.0


for _name, _weight in WEIGHTS.items():
    rk.deterministic_check(_name, weight=_weight, name=_name)



