from __future__ import annotations
import json, os, random, re, secrets, shutil, subprocess, tempfile
from pathlib import Path
from support.source_scope import (
    SourceScopeError, overlay_submitted_source, prepare_trusted_pytest_config,
    validate_affected_path,
)
from support.artifact_contract import required_plan_sections
try:
    import pytest
except ModuleNotFoundError:
    class _ImportSafePytest:
        @staticmethod
        def fixture(*args, **kwargs):
            return lambda fn: fn
        class mark:
            @staticmethod
            def parametrize(*args, **kwargs):
                return lambda fn: fn
    pytest = _ImportSafePytest()

TESTBED = Path(os.environ.get("TESTBED_DIR", "/testbed"))
OUTPUT = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
BASELINE = Path(os.environ.get("CHECKER_BASELINE_DIR", "/opt/checker-baseline"))
CONFIG = json.loads((Path(__file__).with_name("verifier.json")).read_text())

def _run(command: str, cwd: Path, timeout: int = 600):
    env = {"PATH": "/opt/task-venv/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
           "CI": "1", "NO_COLOR": "1", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1", "PYTHONPYCACHEPREFIX": f"/tmp/{cwd.parent.name}-pycache",
           "PYTHONPATH": f"{cwd / 'shared/secrets_masker/src'}:{cwd / 'devel-common/src'}"}
    return subprocess.run(["/usr/sbin/runuser", "-u", "checker", "--", "/bin/bash", "-c", command],
                          cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)

@pytest.fixture(scope="session")
def verifier_worktree():
    root = Path(tempfile.mkdtemp(prefix="cyberbench-private-"))
    root.chmod(0o755)
    work = root / "source"
    try:
        shutil.copytree(BASELINE, work, symlinks=True)
        overlay_submitted_source(TESTBED, work)
        prepare_trusted_pytest_config(work, BASELINE)
        private_patch = Path(__file__).with_name("private.patch")
        if private_patch.is_file():
            cmd = ["git", "-C", str(work), "apply", "--whitespace=nowarn"]
            cmd.extend(f"--include={pattern}" for pattern in CONFIG.get("private_test_includes", []))
            cmd.append(str(private_patch))
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        runner = Path(__file__).parent / "support/submission_runner.py"
        shutil.copy2(runner, work / ".submission_runner.py")
        subprocess.run(["chmod", "-R", "a-w,go+rX", str(work)], check=True)
        yield work
    finally:
        shutil.rmtree(root, ignore_errors=True)

@pytest.fixture(scope="session")
def public_results():
    proc = subprocess.run([str(TESTBED / "reproducer/run-all.sh")], cwd=TESTBED, text=True,
                          capture_output=True, timeout=CONFIG.get("public_timeout", 1200))
    log_dir = Path(os.environ.get('VERIFIER_DIR', os.environ.get('RESULT_DIR', '/logs/verifier')))
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / 'public_validation.txt').write_text(proc.stdout + proc.stderr, encoding='utf-8')
    rows = {}
    for group, status, earned, total in re.findall(r"GROUP (G\d+): (PASS|FAIL|ERROR)(?: score=(\d+)/(\d+))?", proc.stdout):
        rows[group] = (status, int(earned or 0), int(total or 0))
    return proc, rows

@pytest.mark.parametrize("group", CONFIG["public_groups"])
def test_explicit_public_group(group, public_results):
    proc, rows = public_results
    group_error = group in rows and rows[group][0] == 'ERROR'
    global_error = proc.returncode == 2 and not any(row[0] == 'ERROR' for row in rows.values())
    if group_error or global_error:
        kind = 'SUBMISSION_ERROR' if 'SUBMISSION ERROR:' in proc.stdout else 'VALIDATION_ERROR'
        pytest.fail(f'{kind}: public validation did not complete normally; {proc.stdout[-1800:]}')
    assert group in rows, f"VALIDATION_ERROR: {group} missing; public exit={proc.returncode}"
    status, earned, total = rows[group]
    assert status == "PASS" and total > 0 and earned == total, (
        f'{group}: observed {status} {earned}/{total}; '
        'public validation exercises secrets masking of nested values and supported-input compatibility'
    )

# --- fresh, verifier-owned masking cases ---------------------------------------
# Every run draws new secret values, nesting depths and key spellings. Secret and
# public control values come from the same random alphabet, so no value-based
# shortcut can tell them apart. Only the payload crosses into the candidate
# process; expectations and assertions stay here.
_FIELDS = ("password", "api_key", "token", "passwd", "private_key")


def _rand(n: int = 12) -> str:
    return secrets.token_hex(n)


def _public_key(rng) -> str:
    # Hex-only suffixes can never contain a sensitive field name.
    return rng.choice(("node", "layer", "cfg", "item", "meta", "passage", "api_version")) + "_" + _rand(3)


def _sensitive_key(rng, field: str) -> str:
    variants = (field, field.upper(), field.title(), f"db_{field}", f"{field}_hash",
                f"  {field.upper()}  ", f"X-{field.title()}")
    return rng.choice(variants)


def _chain(rng, length: int) -> list[str]:
    return [_public_key(rng) for _ in range(length)]


def _put(root: dict, path: list[str], value) -> None:
    node = root
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _get(root, path):
    node = root
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def _generate(kind: str) -> dict:
    """Return {'request': ..., 'secrets': [...], 'sensitive_paths': [...], 'public': [(path, value)], 'exact': bool}."""
    rng = random.Random(secrets.randbits(64))
    max_depth = rng.choice((3, 4, 5))
    fields = sorted(rng.sample(_FIELDS, 3))
    payload: dict = {}
    case = {"secrets": [], "sensitive_paths": [], "public": [], "exact": False}

    def deep_secret(prefix: list[str], depth: int, value=None):
        field = rng.choice(fields)
        path = prefix + _chain(rng, depth - len(prefix) - 1) + [_sensitive_key(rng, field)]
        secret = value if value is not None else _rand()
        _put(payload, path, secret)
        case["sensitive_paths"].append(path)
        if isinstance(secret, str):
            case["secrets"].append(secret)
        # A public sibling at the same depth must survive untouched.
        sibling = path[:-1] + [_public_key(rng)]
        public = _rand()
        _put(payload, sibling, public)
        case["public"].append((sibling, public))
        return path

    if kind == "deep_secret_fresh":
        deep_secret([], rng.randint(max_depth + 1, max_depth + 4))
    elif kind == "deep_key_variants":
        for _ in range(3):
            deep_secret([], rng.randint(max_depth + 1, max_depth + 4))
    elif kind == "deep_nested_secret_value":
        inner_a, inner_b = _rand(), _rand()
        nested = {_public_key(rng): inner_a, _public_key(rng): {_public_key(rng): inner_b}}
        deep_secret([], rng.randint(max_depth + 1, max_depth + 3), value=nested)
        case["secrets"] += [inner_a, inner_b]
    elif kind == "deep_secret_via_list":
        # A list inside the depth bound whose dict elements nest past it.
        holder = _public_key(rng)
        element: dict = {}
        saved = payload
        payload = element
        path = deep_secret([], rng.randint(max_depth, max_depth + 3))
        payload = saved
        public_item = _rand()
        payload[holder] = [element, public_item]
        case["sensitive_paths"] = [[holder, 0] + path]
        case["public"] = [([holder, 0] + p, v) for p, v in case["public"]] + [([holder, 1], public_item)]
    elif kind == "shallow_secret_control":
        # Within the depth bound: masking and structure preservation already work.
        deep_secret([], rng.randint(1, max_depth))
        deep_secret([], rng.randint(1, max_depth))
    elif kind == "deep_public_preserved":
        # Deep non-sensitive data, including containers past the bound, is returned unchanged.
        for _ in range(3):
            path = _chain(rng, rng.randint(max_depth + 1, max_depth + 4))
            _put(payload, path, rng.choice((_rand(), [_rand(), _rand()], {_public_key(rng): _rand()})))
        case["exact"] = True
    else:
        raise AssertionError(f"unknown generator {kind}")
    case["request"] = {"operation": "redact", "sensitive_fields": fields,
                       "payload": payload, "max_depth": max_depth}
    return case


def _get_any(root, path):
    node = root
    for key in path:
        if isinstance(key, int):
            if not isinstance(node, list) or key >= len(node):
                raise KeyError(path)
        elif not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


def _assert_case(kind: str, case: dict, observed) -> None:
    if case["exact"]:
        assert observed == case["request"]["payload"], f"{kind}: non-sensitive data changed"
        return
    rendered = json.dumps(observed, sort_keys=True)
    leaked = sum(s in rendered for s in case["secrets"])
    if leaked:  # report a count only; never echo secret values into logs
        raise AssertionError(f"{kind}: {leaked} of {len(case['secrets'])} fresh secret value(s) exposed in redacted output")
    for path in case["sensitive_paths"]:
        try:
            _get_any(observed, path)
        except KeyError:
            raise AssertionError(f"{kind}: sensitive key removed instead of masked") from None
    for path, value in case["public"]:
        try:
            got = _get_any(observed, path)
        except KeyError:
            raise AssertionError(f"{kind}: public control path missing") from None
        assert got == value, f"{kind}: public control value altered"


@pytest.mark.parametrize("case", CONFIG["implicit"], ids=lambda c: c["id"])
def test_implicit_upstream_behavior(case, verifier_worktree):
    if "generator" in case:
        generated = _generate(case["generator"])
        request = json.dumps(generated["request"], separators=(",", ":"))
        # The child receives only the behavioral input. Expected values remain
        # in this verifier process and are never imported with submitted code.
        env = {"PATH": "/opt/task-venv/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
               "CI": "1", "NO_COLOR": "1", "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1", "PYTHONPYCACHEPREFIX": f"/tmp/{verifier_worktree.parent.name}-pycache",
               "PYTHONPATH": f"{verifier_worktree / 'shared/secrets_masker/src'}:{verifier_worktree / 'devel-common/src'}"}
        proc = subprocess.run(["/usr/sbin/runuser", "-u", "checker", "--", "/opt/task-venv/bin/python", ".submission_runner.py"],
                              cwd=verifier_worktree, env=env, input=request, text=True,
                              capture_output=True, timeout=case.get("timeout", 600))
        assert proc.returncode == 0, proc.stderr[-1000:]
        _assert_case(case["generator"], generated, json.loads(proc.stdout))
        return
    proc = _run(case["command"], verifier_worktree, case.get("timeout", 600))
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]

def test_required_artifacts():
    plan = OUTPUT / "plan.md"
    findings = OUTPUT / "findings.json"
    plan_text = plan.read_text(encoding="utf-8") if plan.is_file() else ""
    assert plan_text.strip()
    missing = [heading for heading, present in required_plan_sections(plan_text).items() if not present]
    assert not missing, f"missing plan sections: {missing}"
    data = json.loads(findings.read_text(encoding="utf-8"))
    required = {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    assert set(data) == required and isinstance(data["summary"], str) and data["summary"].strip()
    assert isinstance(data["findings"], list) and data["findings"]
    fields = {"area", "trust_boundary", "root_cause", "security_impact", "severity", "affected_code", "behavior_before_fix", "remediation", "behavior_after_fix", "verification"}
    allowed = {"informational", "low", "moderate", "high", "critical"}
    for finding in data["findings"]:
        assert isinstance(finding, dict) and set(finding) == fields
        assert all(isinstance(finding[k], str) and finding[k].strip() for k in fields - {"affected_code"})
        assert finding["severity"] in allowed
        assert isinstance(finding["affected_code"], list) and finding["affected_code"]
        for rel in finding['affected_code']:
            validate_affected_path(TESTBED, rel)
    assert isinstance(data["preserved_legitimate_behaviors"], list) and data["preserved_legitimate_behaviors"]
    assert all(isinstance(value, str) and value.strip() for value in data["preserved_legitimate_behaviors"])
    runs = data["verification_runs"]
    assert isinstance(runs, list) and runs
    assert all(
        isinstance(x, dict) and set(x) == {'command', 'result'}
        and all(isinstance(x[k], str) and x[k].strip() for k in ('command', 'result'))
        for x in runs
    )
    assert '/testbed/reproducer/run-all.sh' in runs[-1]['command']
    assert '/testbed/reproducer/run-all.sh' in plan_text
    # The existing semantic judge checks whether these were actual observed
    # results, not just whether a narrative happens to contain PASS/FAIL words.
