"""RewardKit adapter. Pytest runs only in the explicit prepass."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get('VERIFIER_DIR', os.environ.get('RESULT_DIR', '/logs/verifier')))

# Core security effects: rows whose failure means unsafe behavior reaches a client
# or the request's authority/origin can be spoofed or exhausted. Each weight is
# assigned by the behavior the row observes, not by a shared group label.
CORE = {
    # unsafe-response prevention: no clickable unsafe-scheme redirect body (18 pts)
    'p05': ('safe_response', 6), 'h04': ('safe_response', 6), 'h17': ('safe_response', 6),
    # authority-identity protection: userinfo/malformed authority never becomes the host (18 pts)
    'p10': ('authority_identity', 6), 'h09': ('authority_identity', 6), 'h18': ('authority_identity', 6),
    # bounded metadata parsing: attacker-sized proxy/host headers parse in bounded time (24 pts)
    'p01': ('bounded_metadata', 6), 'p02': ('bounded_metadata', 6), 'p03': ('bounded_metadata', 6),
    'h01': ('bounded_metadata', 6),
    # navigation origin: back-redirects never leave the request origin (39 pts)
    'p07': ('navigation_origin', 7), 'p09': ('navigation_origin', 7), 'h07': ('navigation_origin', 7),
    'h08': ('navigation_origin', 6), 'h15': ('navigation_origin', 6), 'h16': ('navigation_origin', 6),
    # spoofed authority combined with an off-origin referrer (6 pts)
    'h11': ('interaction', 6),
}
# Supported behavior a broad denial would break (50 pts).
SUPPORTED = {
    'p06': ('safe_response', 4), 'h05': ('safe_response', 4),
    'p08': ('navigation_origin', 4), 'h06': ('navigation_origin', 4),
    'h10': ('navigation_origin', 4), 'h19': ('navigation_origin', 4),
    'p11': ('authority_identity', 4), 'p12': ('authority_identity', 4),
    'p04': ('bounded_metadata', 3), 'h02': ('bounded_metadata', 3), 'h03': ('bounded_metadata', 3),
    'h12': ('navigation_origin', 3), 'h13': ('authority_identity', 3), 'h14': ('safe_response', 3),
}
HARNESS = {'test_build': 2, 'test_scope': 2, 'test_protected_tests': 2, 'test_artifacts': 2}

WEIGHTS = {f'test_{case}': weight for case, (_, weight) in sorted({**CORE, **SUPPORTED}.items())}
WEIGHTS.update(HARNESS)
CLASS = {f'test_{c}': 'core_security' for c in CORE}
CLASS.update({f'test_{c}': 'supported_behavior' for c in SUPPORTED})
CLASS.update({name: 'harness_artifacts' for name in HARNESS})
EFFECT = {f'test_{c}': e for c, (e, _) in {**CORE, **SUPPORTED}.items()}

assert len(CORE) == 17 and len(SUPPORTED) == 14 and len(HARNESS) == 4
assert sum(w for _, w in CORE.values()) == 105
assert sum(w for _, w in SUPPORTED.values()) == 50
assert sum(HARNESS.values()) == 8
for _effect in ('safe_response', 'authority_identity'):
    assert sum(w for e, w in CORE.values() if e == _effect) >= 17


def _summary(tests: list[dict], reason: str) -> dict:
    total = sum(item['weight'] for item in tests)
    earned = sum(item['weight'] * item['score'] for item in tests)
    passed = sum(item['status'] == 'passed' for item in tests)
    by_class = {}
    for item in tests:
        bucket = by_class.setdefault(CLASS.get(item['name'], 'other'), {'earned': 0.0, 'possible': 0})
        bucket['earned'] += item['weight'] * item['score']
        bucket['possible'] += item['weight']
    return {
        'strict_pass': bool(tests) and passed == len(tests),
        'strict_score': float(bool(tests) and passed == len(tests)),
        'deterministic_score': round(earned / total, 6) if total else 0.0,
        'passed_checks': passed,
        'failed_checks': sum(item['status'] == 'failed' for item in tests),
        'error_checks': sum(item['status'] == 'error' for item in tests),
        'skipped_checks': sum(item['status'] == 'skipped' for item in tests),
        'total_checks': len(tests), 'tests': tests, 'by_name': {x['name']: x for x in tests},
        'test_weights': WEIGHTS, 'weight_classes': by_class, 'reason': reason,
        'effect_groups': {name: EFFECT.get(name, 'secondary') for name in WEIGHTS},
    }


def _category(name: str, status: str, message: str, has_issue: bool) -> str:
    if not has_issue:
        return 'passed'
    if 'exceeded isolated process deadline' in message or 'exceeded deadline' in message:
        return 'timeout'
    if status == 'error':
        return 'setup_or_execution_error'
    if status == 'skipped':
        return 'not_executed'
    if name == 'test_artifacts':
        return 'artifact_contract'
    if name in ('test_scope', 'test_protected_tests'):
        return 'scope_or_test_tree_change'
    if name == 'test_build':
        return 'build_failure'
    return 'security_miss' if CLASS.get(name) == 'core_security' else 'supported_behavior_regression'


def run_prepass() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    junit = OUT / 'deterministic-junit.xml'
    cmd = [sys.executable, '-m', 'pytest', str(HERE / 'pytest_checks.py'),
           '--junit-xml', str(junit), '-q', '-p', 'no:cacheprovider', '--tb=short']
    try:
        junit.unlink(missing_ok=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1100)
        (OUT / 'deterministic-pytest.log').write_text(proc.stdout + proc.stderr, encoding='utf-8')
        root = ET.parse(junit).getroot()
        found = {}
        for case in root.iter('testcase'):
            name = case.get('name', '')
            failure, error, skipped = (case.find(tag) for tag in ('failure', 'error', 'skipped'))
            issue = failure if failure is not None else error if error is not None else skipped
            message = '' if issue is None else (issue.get('message') or '')[:1200]
            status = ('passed' if issue is None else 'skipped' if skipped is not None
                      else 'error' if error is not None else 'failed')
            found[name] = {
                'name': name, 'status': status, 'score': 1.0 if status == 'passed' else 0.0,
                'weight': WEIGHTS.get(name, 0), 'message': message,
                'weight_class': CLASS.get(name, 'other'),
                'failure_category': _category(name, status, message, issue is not None),
            }
        tests = [found.get(name, {'name': name, 'status': 'error', 'score': 0.0, 'weight': weight,
                                  'message': 'pytest row missing', 'weight_class': CLASS.get(name, 'other'),
                                  'failure_category': 'setup_or_execution_error'})
                 for name, weight in WEIGHTS.items()]
        summary = _summary(tests, 'pytest completed' if proc.returncode == 0 else 'pytest failures')
    except Exception as exc:
        tests = [{'name': name, 'status': 'error', 'score': 0.0, 'weight': weight,
                  'message': f'deterministic prepass infrastructure error: {exc}',
                  'weight_class': CLASS.get(name, 'other'),
                  'failure_category': 'setup_or_execution_error'} for name, weight in WEIGHTS.items()]
        summary = _summary(tests, 'deterministic prepass infrastructure error')
    (OUT / 'deterministic_reward.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    lines = [f"deterministic score={summary['deterministic_score']:.6f}",
             f"{summary['passed_checks']} passed; {summary['failed_checks']} failed; "
             f"{summary['error_checks']} errors; {summary['skipped_checks']} skipped"]
    lines.extend(f"{row['name']}: {row['status']} [{row.get('weight_class','')}/{row.get('failure_category','')}] "
                 f"{row.get('message','')}" for row in summary['tests'])
    (OUT / 'test_output.log').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return summary


# The observation prepass needs pytest, not RewardKit discovery.
if __name__ == '__main__' and '--prepass' in sys.argv:
    result = run_prepass()
    raise SystemExit(0 if result['strict_pass'] else 1)

import rewardkit as rk  # noqa: E402
from rewardkit import criterion  # noqa: E402


@criterion(description='Precomputed deterministic behavior {check_name}.')
def deterministic_check(workspace: Path, check_name: str) -> float:
    try:
        data = json.loads((OUT / 'deterministic_reward.json').read_text(encoding='utf-8'))
        return float(data['by_name'][check_name]['score'])
    except Exception:
        return 0.0


for _name, _weight in WEIGHTS.items():
    rk.deterministic_check(_name, weight=_weight, name=_name)
