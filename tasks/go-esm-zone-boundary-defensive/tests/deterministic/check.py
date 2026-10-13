"""RewardKit deterministic adapter for go-esm-zone-boundary-defensive.

Mirrors the java-apinizer / python-optimate check.py pattern exactly.
Called by test.sh with --prepass to run pytest and write deterministic_reward.json.
Called by RewardKit to read precomputed results.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

import rewardkit as rk
from rewardkit import criterion

VERIFIER_ROOT_DEFAULT = "/logs/verifier"
TESTS_DIR = Path(os.environ.get("TESTS_DIR", str(Path(__file__).resolve().parents[1])))
DEFAULT_TARGET = TESTS_DIR / "deterministic" / "pytest_checks.py"
PYTEST_TARGETS = [
    Path(p) for p in os.environ.get("DETERMINISTIC_PYTEST_TARGETS", str(DEFAULT_TARGET)).split(os.pathsep)
    if p
]
PYTEST_TIMEOUT_SEC = int(os.environ.get("DETERMINISTIC_PYTEST_TIMEOUT_SEC", "1200"))

TEST_WEIGHTS = {
    # deliverable + scope
    "test_func_deliverables": 3,
    "test_func_scope_compliance": 4,
    "test_func_protected_tests_unmodified": 4,
    # aggregate behavioral contracts (public reproducer, core cases)
    "test_sec_case_a": 15,
    "test_sec_case_b": 15,
    "test_func_case_c": 5,
    "test_func_case_d": 5,
    "test_sec_case_g": 15,
    "test_func_case_h": 5,
    "test_sec_case_i": 15,
    "test_func_case_j": 5,
    # hidden verifier-only behavioral invariants
    "test_sec_hidden_h01": 5,
    "test_sec_hidden_h02": 12,
    "test_sec_hidden_h03": 15,
    "test_sec_hidden_h04": 12,
    "test_func_hidden_h05": 5,
    "test_sec_hidden_h06": 12,
    "test_func_hidden_h07": 5,
    "test_func_hidden_h08": 5,
    "test_func_hidden_h09": 5,
    "test_sec_hidden_h10": 8,
    "test_sec_hidden_h11": 8,
    "test_sec_hidden_h12": 15,
    "test_sec_hidden_h13": 8,
    "test_sec_hidden_h14": 15,
    "test_func_hidden_h15": 5,
    "test_func_public_reproducer_consistent": 3,
    # implementation-agnostic change evidence
    "test_func_in_scope_change_present": 5,
    "test_sec_no_testharness_fingerprinting": 5,
    # findings artifact
    "test_func_affected_code_in_scope": 5,
    "test_func_artifact_truthfulness": 3,
}


def _verifier_dir() -> Path:
    out = Path(os.environ.get("VERIFIER_DIR", os.environ.get("RESULT_DIR", VERIFIER_ROOT_DEFAULT)))
    out.mkdir(parents=True, exist_ok=True)
    return out


def _internal_dir() -> Path:
    path = _verifier_dir() / "internal"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_test_name(name: str) -> str:
    leaf = name.rsplit("::", 1)[-1]
    leaf = leaf.split(".", 1)[-1] if "." in leaf and not leaf.startswith("test_") else leaf
    return re.sub(r"\[.*\]$", "", leaf)


def _case_status(case: ET.Element) -> tuple[str, str, str]:
    failure = case.find("failure")
    error = case.find("error")
    skipped = case.find("skipped")
    if failure is not None:
        return "failed", failure.get("message", "") or (failure.text or ""), failure.text or ""
    if error is not None:
        return "failed", error.get("message", "") or (error.text or ""), error.text or ""
    if skipped is not None:
        return "skipped", skipped.get("message", "") or (skipped.text or ""), skipped.text or ""
    return "passed", "", ""


def _parse_junit(junit_path: Path) -> list[dict]:
    if not junit_path.exists():
        raise FileNotFoundError(f"JUnit XML not created: {junit_path}")
    root = ET.parse(junit_path).getroot()
    cases = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        full_name = f"{classname}::{name}" if classname else name
        status, message, details = _case_status(case)
        cases.append({
            "name": full_name,
            "base_name": _base_test_name(name),
            "status": status,
            "message": " ".join((message or "").split())[:800],
            "details": details or "",
            "duration": int(float(case.get("time", "0") or 0) * 1000),
        })
    return cases


def _summarize_cases(cases: list[dict], *, pytest_exit_code: int, reason: str) -> dict:
    by_base: dict[str, list[dict]] = {name: [] for name in TEST_WEIGHTS}
    unknown = []
    for case in cases:
        base = case["base_name"]
        if base in by_base:
            by_base[base].append(case)
        else:
            unknown.append(case)

    tests = []
    for name, weight in TEST_WEIGHTS.items():
        rows = by_base.get(name, [])
        if not rows:
            tests.append({"name": name, "status": "failed", "score": 0.0, "weight": weight,
                          "message": f"no pytest rows collected for {name}", "details": "", "cases": []})
            continue
        passed = sum(1 for r in rows if r["status"] == "passed")
        failed = sum(1 for r in rows if r["status"] == "failed")
        skipped = sum(1 for r in rows if r["status"] == "skipped")
        score = passed / len(rows) if rows else 0.0
        status = "passed" if passed == len(rows) else ("failed" if failed else "skipped")
        first_bad = next((r for r in rows if r["status"] != "passed"), None)
        tests.append({
            "name": name, "status": status, "score": round(score, 4), "weight": weight,
            "message": (first_bad or {}).get("message", ""),
            "details": (first_bad or {}).get("details", ""), "cases": rows,
        })

    total_weight = sum(float(t["weight"]) for t in tests)
    earned_weight = sum(float(t["weight"]) * float(t["score"]) for t in tests)
    det_score = earned_weight / total_weight if total_weight else 0.0
    strict = (bool(tests) and all(t["status"] == "passed" and float(t["score"]) == 1.0 for t in tests)
              and pytest_exit_code == 0)

    return {
        "strict_pass": strict,
        "strict_score": 1.0 if strict else 0.0,
        "deterministic_score": round(det_score, 4),
        "earned_weight": round(earned_weight, 4),
        "total_weight": round(total_weight, 4),
        "passed_checks": sum(1 for t in tests if t["status"] == "passed"),
        "failed_checks": sum(1 for t in tests if t["status"] == "failed"),
        "skipped_checks": sum(1 for t in tests if t["status"] == "skipped"),
        "total_checks": len(tests),
        "test_weights": TEST_WEIGHTS,
        "tests": tests,
        "by_name": {t["name"]: t for t in tests},
        "failed_tests": [
            {"name": t["name"], "message": t["message"], "weight": t["weight"], "score": t["score"]}
            for t in tests if t["status"] != "passed" or float(t["score"]) != 1.0
        ],
        "unknown_pytest_rows": unknown,
        "reason": reason,
        "exit_code": pytest_exit_code,
    }


def _write_outputs(summary: dict, *, junit_path: Path | None = None) -> None:
    out = _verifier_dir()
    _write_json(out / "deterministic_reward.json", summary)
    lines = ["Deterministic pytest prepass — go-esm-zone-boundary-defensive", ""]
    if junit_path:
        lines.append(f"junit_xml: {junit_path}")
    lines.append(
        f"summary: {summary['passed_checks']} passed, {summary['failed_checks']} failed, "
        f"{summary['skipped_checks']} skipped, score={summary['deterministic_score']:.4f}"
        f"  strict={'YES' if summary['strict_pass'] else 'NO'}"
    )
    lines.append("")
    for t in summary.get("tests", []):
        lines.append(f"{t['status'].upper():7} {t['name']} [score={t['score']}, weight={t['weight']}]")
        if t.get("message"):
            lines.append(f"  {t['message'][:200]}")
    text = "\n".join(lines) + "\n"
    (out / "test_output.log").write_text(text, encoding="utf-8")
    print(text)


def _preflight_failure(message: str, details: str = "") -> dict:
    tests = [{"name": n, "status": "failed", "score": 0.0, "weight": w,
              "message": message, "details": details, "cases": []}
             for n, w in TEST_WEIGHTS.items()]
    total = sum(float(v) for v in TEST_WEIGHTS.values())
    return {
        "strict_pass": False, "strict_score": 0.0, "deterministic_score": 0.0,
        "earned_weight": 0.0, "total_weight": round(total, 4),
        "passed_checks": 0, "failed_checks": len(tests), "skipped_checks": 0,
        "total_checks": len(tests), "test_weights": TEST_WEIGHTS, "tests": tests,
        "by_name": {t["name"]: t for t in tests},
        "failed_tests": [{"name": t["name"], "message": message, "weight": t["weight"], "score": 0.0}
                         for t in tests],
        "unknown_pytest_rows": [], "reason": message, "exit_code": 1,
    }


def run_pytest_prepass() -> dict:
    out = _verifier_dir()
    junit_path = _internal_dir() / "deterministic_junit.xml"
    targets = [str(p) for p in PYTEST_TARGETS if p.exists()]
    if not targets:
        summary = _preflight_failure(f"no pytest targets found; expected {DEFAULT_TARGET}")
        _write_outputs(summary, junit_path=junit_path)
        return summary

    cmd = [sys.executable, "-m", "pytest", *targets,
           "--junit-xml", str(junit_path), "-q", "-p", "no:cacheprovider", "--tb=short"]
    started = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(TESTS_DIR), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    duration_ms = int((time.perf_counter() - started) * 1000)
    raw_log = proc.stdout or ""
    (out / "pytest_output.log").write_text(raw_log, encoding="utf-8")

    try:
        cases = _parse_junit(junit_path)
        reason = "all checks passed" if proc.returncode == 0 else "pytest check failures"
        summary = _summarize_cases(cases, pytest_exit_code=proc.returncode, reason=reason)
        summary["pytest"] = {"command": cmd, "duration_ms": duration_ms,
                              "junit_xml": str(junit_path), "raw_output": str(out / "pytest_output.log")}
    except Exception as exc:
        summary = _preflight_failure(f"failed to parse JUnit XML: {exc}", traceback.format_exc())
        summary["pytest"] = {"command": cmd, "duration_ms": duration_ms,
                              "junit_xml": str(junit_path), "raw_output": str(out / "pytest_output.log")}

    _write_outputs(summary, junit_path=junit_path)
    return summary


def _load_precomputed() -> dict:
    path = _verifier_dir() / "deterministic_reward.json"
    if not path.exists():
        return _preflight_failure("deterministic prepass did not produce deterministic_reward.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _preflight_failure(f"deterministic_reward.json invalid JSON: {exc}", traceback.format_exc())


@criterion(description="Precomputed deterministic pytest check {check_name}.")
def deterministic_check(workspace: Path, check_name: str) -> float:
    summary = _load_precomputed()
    row = (summary.get("by_name") or {}).get(check_name)
    if not isinstance(row, dict):
        return 0.0
    try:
        return float(row.get("score", 1.0 if row.get("status") == "passed" else 0.0))
    except Exception:
        return 0.0


for _check_name, _weight in TEST_WEIGHTS.items():
    rk.deterministic_check(_check_name, weight=_weight, name=_check_name)


if __name__ == "__main__":
    if "--prepass" in sys.argv:
        run_pytest_prepass()
