from __future__ import annotations
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

# Support direct collection and file-based discovery without test.sh's
# PYTHONPATH setup, including a copied verifier-only tests directory.
TESTS_ROOT = str(Path(__file__).resolve().parents[1])
if TESTS_ROOT not in sys.path:
    sys.path.insert(0, TESTS_ROOT)

from support.artifact_contract import validate_artifacts, validate_public_evidence
from support.private_runtime import run_private
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

def _run(command: str, cwd: Path, timeout: int = 600, compilation: bool = False):
    try:
        return run_private(command, cwd, timeout, compilation)
    except subprocess.TimeoutExpired:
        # A timeout is an incomplete observation, not a demonstrated defect.
        pytest.fail(f"VALIDATION_ERROR: TimeoutExpired after {timeout}s: {command}")

# One trusted replay of the frozen submission serves every implicit/private row.
# Only dist/.cache/tmp/home are writable by the unprivileged checker.
@pytest.fixture(scope="session")
def verifier_worktree():
    root = Path(tempfile.mkdtemp(prefix="cyberbench-private-", dir="/opt/task-policy/private"))
    shutil.chown(root, user="root", group="checker")
    root.chmod(0o750)
    jail = root / "jail"
    shutil.copytree("/opt/checker-runtime", jail, symlinks=True)
    work = jail / "source"
    shutil.copytree(BASELINE, work, symlinks=True)
    submitted = TESTBED / "packages/seroval/src"
    assert submitted.is_dir(), "submitted production source is missing"
    assert not any(path.is_symlink() for path in submitted.rglob("*")), "submitted source contains a symlink"
    destination = work / "packages/seroval/src"
    shutil.rmtree(destination)
    shutil.copytree(submitted, destination, symlinks=False)
    private_patch = Path(__file__).with_name("private.patch")
    if private_patch.is_file():
        cmd = ["git", "-C", str(work), "apply", "--whitespace=nowarn"]
        cmd.extend(f"--include={pattern}" for pattern in CONFIG.get("private_test_includes", []))
        cmd.append(str(private_patch))
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    # Candidate node_modules, package scripts, test runners and reproducer files
    # never enter this worktree. Keep trusted dependencies root-owned.
    subprocess.run(["chmod", "-R", "a-w,go+rX", str(jail)], check=True)
    sink = jail / "dev/null"
    sink.touch()
    sink.chmod(0o666)
    package_dir = work / "packages/seroval"
    package_dir.chmod(0o1777)
    for rel in ("dist", ".cache"):
        path = package_dir / rel
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["chown", "-R", "checker:checker", str(path)], check=True)
    for relative in ("tmp", "home/checker"):
        path = jail / relative
        path.mkdir(parents=True, exist_ok=True)
        shutil.chown(path, user="checker", group="checker")
        path.chmod(0o700)
    try:
        yield work
    finally:
        shutil.rmtree(root, ignore_errors=True)

@pytest.fixture(scope="session")
def public_results():
    env = dict(os.environ, TASK_SUBMITTED_SOURCE=str(TESTBED / "packages/seroval/src"))
    try:
        proc = subprocess.run(["/opt/task-checker/run-sealed-probe.py", "public"], cwd=TESTBED, text=True, env=env,
                              capture_output=True, timeout=CONFIG.get("public_timeout", 1200))
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(exc.cmd, 2, "SUMMARY: ERROR\nVALIDATION ERROR: public probe timed out\n", "")
    stopped = subprocess.run(["pkill", "-KILL", "-u", "agent"], check=False)
    assert stopped.returncode in (0, 1), "could not terminate public-probe workers"
    log_dir = Path(os.environ.get("VERIFIER_DIR", os.environ.get("RESULT_DIR", "/logs/verifier")))
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "public_validation.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    rows = {}
    for group, status, earned, total in re.findall(r"GROUP (G\d+): (PASS|FAIL|ERROR)(?: score=(\d+)/(\d+))?", proc.stdout):
        rows[group] = (status, int(earned or 0), int(total or 0))
    return proc, rows

@pytest.mark.parametrize("group", CONFIG["public_groups"])
def test_explicit_public_group(group, public_results):
    proc, rows = public_results
    group_error = group in rows and rows[group][0] == "ERROR"
    global_error = proc.returncode == 2 and not any(row[0] == "ERROR" for row in rows.values())
    if group_error or global_error:
        kind = "SUBMISSION_ERROR" if "SUBMISSION ERROR:" in proc.stdout else "VALIDATION_ERROR"
        pytest.fail(f"{kind}: public validation did not complete normally; {proc.stdout[-1800:]}")
    assert group in rows, f"VALIDATION_ERROR: {group} missing; public exit={proc.returncode}"
    status, earned, total = rows[group]
    assert status == "PASS" and total > 0 and earned == total, (
        f"{group}: observed {status} {earned}/{total}; public validation exercises typed-array/DataView "
        "encoding and decoding bounds, backing-source validation and supported-input compatibility"
    )

@pytest.mark.parametrize("case", CONFIG["implicit"], ids=lambda c: c["id"])
def test_implicit_upstream_behavior(case, verifier_worktree):
    proc = _run(case["command"], verifier_worktree, case.get("timeout", 600), case.get("compilation", False))
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]

@pytest.mark.parametrize("case", CONFIG["private_cases"], ids=lambda c: c["id"])
def test_private_behavior(case, verifier_worktree):
    proc = _run(case["command"], verifier_worktree, case.get("timeout", 180))
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]

def test_required_artifacts():
    data = validate_artifacts(TESTBED, OUTPUT)
    plan_text = (OUTPUT / "plan.md").read_text(encoding="utf-8")
    assert "/testbed/reproducer/run-all.sh" in plan_text, "plan.md Validation must record the reproducer run"
    evidence = Path(os.environ["VERIFIER_DIR"]) / "internal/validation-evidence.jsonl"
    digest = (Path(os.environ["VERIFIER_DIR"]) / "internal/source-sha256.txt").read_text().strip()
    validate_public_evidence(data, evidence, digest)
