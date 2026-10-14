"""Behavioral and artifact checks for go-esm-zone-boundary-defensive.

Mirrors the java-apinizer / python-optimate task check.py pattern:
  - _run_reproducer() calls the public, root-owned reproducer
    (/testbed/reproducer/run-all.sh); its integrity is protected by file
    ownership/mode (0555, root-owned), not by a separate private fixture set.
    It reports only five aggregate family results (Cases A-E). Its probe
    inputs travel through candidate-controlled code and are therefore
    observable by the candidate; nothing qualification-critical depends on
    them staying secret -- the granular, graded variants live only in the
    verifier-side hidden probe below. It is run as the unprivileged
    candidate identity with a minimal environment, exactly as the candidate
    runs it, so submitted code never executes with verifier credentials.
  - _run_hidden_verify() calls a verifier-only granular probe that lives
    entirely under tests/deterministic/support/ (never mounted into the
    candidate's own working session, only into the grading container). It
    reports the full per-case granular results (Cases A-Q) used for weighted
    deterministic scoring below.
  - _case_result() parses "Case X: PASS/FAIL/ERROR (detail)" lines.
  - Static/diff checks inspect /testbed against the /opt/task-baseline snapshot
    taken at image build time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path


class _LazyPytest:
    """Defer the optional pytest dependency until a test actually uses it.

    RewardKit discovery imports every Python file under ``tests/`` in a
    lightweight environment where verifier-only dependencies are not installed.
    The real verifier image installs and invokes pytest before any attribute on
    this proxy is accessed.
    """

    def __getattr__(self, name):
        import importlib

        return getattr(importlib.import_module("pytest"), name)


pytest = _LazyPytest()

TESTBED = Path(os.environ.get("TESTBED", "/testbed"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
REPRODUCER = TESTBED / "reproducer" / "run-all.sh"
HIDDEN_VERIFY = Path(__file__).resolve().parent / "support" / "hidden-verify.sh"
BASELINE_DIR = Path("/opt/task-baseline")
BASELINE_MODES = Path("/opt/task-baseline-modes.tsv")
CANDIDATE_USER = os.environ.get("ESM_CANDIDATE_USER", "agent")
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROTECTED_TESTS_MANIFEST = Path(__file__).resolve().parent / "support" / "protected_tests.sha256"

# Public family -> the granular hidden cases it aggregates, for the
# consistency check between the two probes. Deliberately cross-subsystem
# (not one family per boundary) so a public FAIL/PASS pattern does not by
# itself reveal which trust boundary is broken -- must mirror the grouping
# in environment/reproducer/harness.go's main() exactly.
PUBLIC_FAMILIES = {
    "A": ["A", "H"],
    "B": ["G", "D"],
    "C": ["I", "C"],
    "D": ["B", "J"],
    "E": ["A", "G", "I"],
}

# The whole source tree is production scope except test files, the sealed
# reproducer, and VCS/build ephemera. A Go source-audit task's legitimate fix
# can touch any production .go file, not just one predicted subsystem.
SCOPE_EXCLUDE_DIRS = {".git", "reproducer"}
EPHEMERAL_DIRS = {"__pycache__", ".git"}

REQUIRED_FINDING_KEYS = {
    "area", "trust_boundary", "root_cause", "security_impact", "severity",
    "affected_code", "behavior_before_fix", "remediation", "behavior_after_fix",
    "verification",
}
VALID_SEVERITIES = {"informational", "low", "moderate", "high", "critical"}
MIN_PLAN_WORDS = 60

# Reject test-runner / harness fingerprinting in production remediations:
# production behavior gated on the presence of a test runner or on this
# checker's own fixture names. Ordinary request logging is NOT treated as
# fingerprinting -- public reproducer inputs are observable by design.
SHORTCUT_PATTERNS = (
    re.compile(r"\bpytest\b", re.I),
    re.compile(r"\bPYTEST_[A-Z0-9_]+\b"),
    re.compile(r"\bgo\s+test\b", re.I),
    re.compile(r"\bGO_TEST[A-Z0-9_]*\b"),
    re.compile(r"esm-poc-|esm-det-|esm-secret-"),  # this checker's own fixture-path prefixes
)

# Lines describing the pre-remediation state, which test_func_artifact_truthfulness
# does not treat as final claims.
PRE_FIX_CONTEXT = re.compile(
    r"\b(before|baseline|original(ly)?|unpatched|pre-?fix|pre-?remediation|initial(ly)?|prior|previously|was)\b",
    re.I,
)

_REPRODUCER_OUTPUT: str | None = None
_REPRODUCER_RC: int | None = None
_HIDDEN_OUTPUT: str | None = None
_HIDDEN_RC: int | None = None


def _run_reproducer() -> tuple[str, int]:
    """Runs the public reproducer (Cases A-E, aggregate only)."""
    global _REPRODUCER_OUTPUT, _REPRODUCER_RC
    if _REPRODUCER_RC is None:
        cmd = [str(REPRODUCER)]
        env = {
            "PATH": SAFE_PATH,
            "GOPATH": "/go",
            "GOFLAGS": "-mod=mod",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "LANG": "C.UTF-8",
        }
        if os.geteuid() == 0:
            import pwd

            try:
                cand = pwd.getpwnam(CANDIDATE_USER)
            except KeyError:
                cand = None
            if cand is None or cand.pw_uid == 0:
                _REPRODUCER_OUTPUT = (
                    f"ERROR: unprivileged candidate user {CANDIDATE_USER!r} unavailable; "
                    "refusing to run candidate code as root"
                )
                _REPRODUCER_RC = 3
                return _REPRODUCER_OUTPUT, _REPRODUCER_RC
            home = cand.pw_dir
            cmd = ["runuser", "-u", CANDIDATE_USER, "--", *cmd]
        else:
            home = os.environ.get("HOME", "/tmp")
        env["HOME"] = home
        cand_tmp = Path(home) / ".tmp"
        env["TMPDIR"] = str(cand_tmp) if cand_tmp.is_dir() else "/tmp"
        result = subprocess.run(
            cmd,
            cwd=str(TESTBED),
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        _REPRODUCER_OUTPUT = result.stdout + result.stderr
        _REPRODUCER_RC = result.returncode
    return _REPRODUCER_OUTPUT, _REPRODUCER_RC


def _run_hidden_verify() -> tuple[str, int]:
    """Runs the verifier-only granular probe (Cases A-Q).

    Invoked via `/bin/sh <script>` rather than direct exec: the script's
    executable bit is not guaranteed to survive every packaging/mount path
    (e.g. zip extraction) that can land it here, and `sh` only needs read
    access, not the execute bit.
    """
    global _HIDDEN_OUTPUT, _HIDDEN_RC
    if _HIDDEN_RC is None:
        result = subprocess.run(
            ["/bin/sh", str(HIDDEN_VERIFY)],
            cwd=str(TESTBED),
            capture_output=True,
            text=True,
            timeout=600,
        )
        _HIDDEN_OUTPUT = result.stdout + result.stderr
        _HIDDEN_RC = result.returncode
    return _HIDDEN_OUTPUT, _HIDDEN_RC


def _status_from(output: str, case_id: str) -> str:
    matches = re.findall(rf"^Case {re.escape(case_id)}: ([A-Z]+)", output, re.MULTILINE)
    return matches[-1] if matches else "ERROR_NO_RESULTS"


def _case_status(case_id: str) -> str:
    output, _ = _run_hidden_verify()
    return _status_from(output, case_id)


def _assert_case(case_id: str, description: str) -> None:
    output, _ = _run_hidden_verify()
    status = _status_from(output, case_id)
    assert status == "PASS", f"{description}:\n{output[-3000:]}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_protected_tests() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in PROTECTED_TESTS_MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relpath = line.partition("  ")
        entries[relpath.strip()] = digest.strip()
    return entries


_STATUS_WORD = r"(?:PASS|FAIL|ERROR)"


def _cases_without_recorded_status(text: str) -> list[str]:
    """Public cases A-E that the text does not pair with an observed status.

    The findings.json schema only asks for an observed-result string, so any
    clear wording counts: "Case A: PASS", "A PASS (2/2)", "A: FAIL", or a range
    such as "A-E: PASS" / "Cases A through E all PASS". The case letter must be
    a standalone token (optionally prefixed by "Case"), followed by a status
    word within a short window on the same line."""
    if re.search(rf"(?<![A-Za-z])A\s*(?:-|–|—|to|through)\s*E(?![A-Za-z])[^\n]{{0,25}}?\b{_STATUS_WORD}\b", text, re.I):
        return []
    return [
        c for c in "ABCDE"
        if not re.search(rf"(?<![A-Za-z]){c}(?![A-Za-z])[^\n]{{0,15}}?\b{_STATUS_WORD}\b", text)
    ]


def _validate_deliverables() -> tuple[bool, str]:
    plan = OUTPUT_DIR / "plan.md"
    findings = OUTPUT_DIR / "findings.json"
    if not plan.is_file() or plan.is_symlink() or not plan.read_text(encoding="utf-8").strip():
        return False, "missing or empty /app/output/plan.md"
    plan_text = plan.read_text(encoding="utf-8")
    plan_words = len(plan_text.split())
    if plan_words < MIN_PLAN_WORDS:
        return False, f"plan.md must be a substantive write-up of at least {MIN_PLAN_WORDS} words (found {plan_words})"
    # instruction.md's plan.md template requires these four sections
    # (equivalent Markdown formatting is allowed, so this matches by
    # keyword/heading-ish presence, not an exact heading string).
    required_sections = {
        "root causes": r"root\s*cause",
        "production changes": r"production\s*change",
        "preserved legitimate behavior": r"preserved?\s*(legitimate\s*)?behavior",
        "validation": r"\bvalidation\b",
    }
    missing_sections = [
        name for name, pattern in required_sections.items()
        if not re.search(pattern, plan_text, re.I)
    ]
    if missing_sections:
        return False, f"plan.md is missing required section(s): {missing_sections}"
    if not findings.is_file() or findings.is_symlink():
        return False, "missing /app/output/findings.json"
    try:
        data = json.loads(findings.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"findings.json invalid JSON: {e}"
    if not isinstance(data, dict):
        return False, "findings.json must be a JSON object"
    required_top = {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    if set(data) != required_top:
        return False, f"findings.json top-level keys must be exactly {sorted(required_top)}"
    if not isinstance(data["summary"], str) or not data["summary"].strip():
        return False, "summary must be a non-empty string"
    fl = data.get("findings", [])
    if not isinstance(fl, list) or not fl:
        return False, "'findings' must be non-empty"
    for i, f in enumerate(fl):
        if not isinstance(f, dict) or set(f) != REQUIRED_FINDING_KEYS:
            return False, f"finding #{i} keys must be exactly {sorted(REQUIRED_FINDING_KEYS)}"
        sev = str(f.get("severity", "")).lower()
        if sev not in VALID_SEVERITIES:
            return False, f"finding #{i} invalid severity: {sev!r}"
        for key in REQUIRED_FINDING_KEYS - {"affected_code", "severity"}:
            if not isinstance(f[key], str) or not f[key].strip():
                return False, f"finding #{i}: {key} must be a non-empty string"
        aff = f.get("affected_code", [])
        if not isinstance(aff, list) or not aff or any(not isinstance(p, str) or not p.strip() for p in aff):
            return False, f"finding #{i}: affected_code must contain non-empty strings"
    runs = data.get("verification_runs", [])
    if not isinstance(runs, list) or not runs:
        return False, "verification_runs must be non-empty"
    last_run = runs[-1]
    if not isinstance(last_run, dict) or "run-all.sh" not in str(last_run.get("command", "")):
        return False, "the final verification_runs entry must record /testbed/reproducer/run-all.sh"
    last_result = str(last_run.get("result", ""))
    missing_cases = _cases_without_recorded_status(last_result)
    if missing_cases:
        return False, (
            "the final verification_runs entry's result must record the observed "
            f"status for every public case (A-E); missing: {missing_cases}"
        )
    preserved = data.get("preserved_legitimate_behaviors", [])
    if not isinstance(preserved, list) or not preserved:
        return False, "preserved_legitimate_behaviors must be non-empty"
    return True, ""


def _is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in {"tests", "test", "testdata"} for p in parts[:-1]) or parts[-1].endswith("_test.go")


def _in_production_scope(rel: str, is_dir: bool = False) -> bool:
    """instruction.md scopes the task to "Go production source"; enforce
    that here too, not just in _changed_production_files()'s .go-only walk,
    so a non-.go file (README, CI config, go.mod, etc.) is a scope
    violation if modified, exactly like any other out-of-scope change.
    Directories are exempt from the .go check (a directory has no
    extension of its own; it is in scope if it can contain in-scope
    files) -- only regular files are extension-checked."""
    top = rel.split("/", 1)[0]
    if top in SCOPE_EXCLUDE_DIRS:
        return False
    if _is_test_path(rel):
        return False
    if not is_dir and not rel.endswith(".go"):
        return False
    return True


def _scope_violations() -> list[str]:
    def snapshot(root: Path, mode_overrides: dict[str, int] | None = None) -> dict[str, tuple]:
        entries = {}
        for dirpath, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in EPHEMERAL_DIRS]
            for name in dirs + files:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                st = os.lstat(path)
                kind = stat.S_IFMT(st.st_mode)
                mode = (mode_overrides or {}).get(rel, stat.S_IMODE(st.st_mode))
                if stat.S_ISLNK(st.st_mode):
                    payload = ("link", os.readlink(path))
                elif stat.S_ISREG(st.st_mode):
                    payload = ("file", Path(path).read_bytes())
                else:
                    payload = ("other",)
                entries[rel] = (kind, mode, payload)
        return entries

    original_modes: dict[str, int] = {}
    if BASELINE_MODES.is_file():
        for line in BASELINE_MODES.read_text(encoding="utf-8").splitlines():
            rel, mode, _kind, _target = (line.split("\t", 3) + [""] * 4)[:4]
            original_modes[rel] = int(mode, 8)

    baseline = snapshot(BASELINE_DIR, original_modes)
    current = snapshot(TESTBED)
    violations = []
    for rel in sorted(set(baseline) | set(current)):
        kind = (current.get(rel) or baseline.get(rel))[0]
        if _in_production_scope(rel, is_dir=stat.S_ISDIR(kind)):
            continue
        if rel not in baseline:
            violations.append(f"created outside scope: {rel}")
        elif rel not in current:
            violations.append(f"deleted outside scope: {rel}")
        elif baseline[rel] != current[rel]:
            violations.append(f"modified outside scope: {rel}")
    return violations


def _changed_production_files() -> set[str]:
    if not BASELINE_DIR.is_dir():
        pytest.fail("baseline snapshot absent; cannot establish submitted changes")
    changed = set()
    for path in TESTBED.rglob("*.go"):
        rel = path.relative_to(TESTBED).as_posix()
        if not _in_production_scope(rel):
            continue
        baseline_file = BASELINE_DIR / rel
        if not baseline_file.is_file() or not path.is_file() or path.read_bytes() != baseline_file.read_bytes():
            changed.add(rel)
    for path in BASELINE_DIR.rglob("*.go"):
        rel = path.relative_to(BASELINE_DIR).as_posix()
        if not _in_production_scope(rel):
            continue
        current_file = TESTBED / rel
        if not current_file.is_file():
            changed.add(rel)
    return changed


# ── deliverable + scope ───────────────────────────────────────────────────

def test_func_deliverables():
    ok, msg = _validate_deliverables()
    assert ok, msg


def test_func_scope_compliance():
    if not BASELINE_DIR.is_dir():
        pytest.skip("baseline snapshot absent")
    v = _scope_violations()
    assert not v, "Changes outside scope:\n" + "\n".join(v[:20])


def test_func_protected_tests_unmodified():
    """Every baseline project test file (protected_tests.sha256) must be byte-identical.

    Subsumed by test_func_scope_compliance's whole-tree diff, but kept as an
    explicit, narrowly-scoped row so a *_test.go tamper always has a
    dedicated, unambiguous failure message.
    """
    protected = _load_protected_tests()
    problems = []
    for relpath, expected in protected.items():
        full = TESTBED / relpath
        if not full.is_file():
            problems.append(f"protected test file deleted or moved: {relpath}")
            continue
        if _sha256_file(full) != expected:
            problems.append(f"protected test file modified: {relpath}")
    assert not problems, "project test file(s) changed, which is out of scope: " + "; ".join(problems)


# ── aggregate behavioral contracts (public reproducer, real HTTP exploitation) ──

def test_sec_case_a():
    _assert_case("A", "Case A (traversal write blocked) aggregate contract must PASS")


def test_sec_case_b():
    _assert_case("B", "Case B (traversal read blocked) aggregate contract must PASS")


def test_func_case_c():
    _assert_case("C", "Case C (valid-domain zone preserved) aggregate contract must PASS")


def test_func_case_d():
    _assert_case("D", "Case D (no zone header preserved) aggregate contract must PASS")


def test_sec_case_g():
    _assert_case("G", "Case G (tarball entry-name traversal blocked) aggregate contract must PASS")


def test_func_case_h():
    _assert_case("H", "Case H (valid package tarball install preserved) aggregate contract must PASS")


def test_sec_case_i():
    _assert_case("I", "Case I (web-serving path traversal blocked) aggregate contract must PASS")


def test_func_case_j():
    _assert_case("J", "Case J (legitimate in-app file served) aggregate contract must PASS")


def test_sec_hidden_h01():
    _assert_case("H01", "H01: a non-domain, non-traversal garbage zone identifier must be handled safely -- a clean rejection (4xx) or a successful request are both fine, a server error (5xx) is not")


def test_sec_hidden_h02():
    _assert_case("H02", "H02: a crafted escape payload prefixed with an innocuous domain-like segment must still be blocked")


def test_sec_hidden_h03():
    _assert_case("H03", "H03: an artifact created under one valid zone must be stored in that zone's namespace and must not be retrievable via a different valid zone or without a zone")


def test_sec_hidden_h04():
    _assert_case("H04", "H04: a tarball containing both a malicious escaping entry and legitimate sibling entries must not corrupt or block the legitimate siblings, whether the malicious entry is skipped or the whole archive is safely rejected")


def test_func_hidden_h05():
    _assert_case("H05", "H05: a tarball entry name that lexically cancels itself out (e.g. dist/../lib/x.js) must still extract to its normalized in-package location and be servable")


def test_sec_hidden_h06():
    _assert_case("H06", "H06: a crafted escape payload prefixed with an innocuous same-app-looking segment must still be blocked")


def test_func_hidden_h07():
    _assert_case("H07", "H07: a request path whose filename merely contains '..' as a substring (not a path segment) must still be served normally")


def test_func_hidden_h08():
    _assert_case("H08", "H08: a request path that lexically cancels itself out (e.g. /dir/../file) must still resolve and serve normally")


def test_func_hidden_h09():
    _assert_case("H09", "H09: a request path whose filename contains a literal backslash must still be served normally on this Linux task environment")


def test_sec_hidden_h10():
    _assert_case("H10", "H10: a malicious/rejected package install must not leave a stray artifact directly under the OS temp directory")


def test_sec_hidden_h11():
    _assert_case("H11", "H11: a blocked/rejected transform request must not leave a stray artifact directly under the OS temp directory")


def test_sec_hidden_h12():
    _assert_case("H12", "H12: a lexical string-prefix relationship between two directory paths (e.g. /app vs /app-secret) must not be treated as filesystem containment")


def test_sec_hidden_h14():
    _assert_case("H14", "H14: a zone identifier consisting only of a dot segment ('..') must not write or read outside the storage root")


def test_func_hidden_h15():
    _assert_case("H15", "H15: a package file whose name merely contains '..' as a substring (e.g. foo..bar.js) must still be served through the server's package path")


def test_sec_hidden_h13():
    _assert_case("H13", "H13: a blocked/rejected web-serving request must not leave a stray artifact directly under the OS temp directory")


def test_func_public_reproducer_consistent():
    """The public aggregate probe (Cases A-E) must not report PASS for a
    family whose underlying granular checks (from the hidden verifier) show
    a real failure, and must not report FAIL/ERROR for a family whose
    underlying checks all genuinely pass."""
    hidden_output, _ = _run_hidden_verify()
    public_output, _ = _run_reproducer()
    mismatches = []
    for family_id, member_ids in PUBLIC_FAMILIES.items():
        member_statuses = [_status_from(hidden_output, m) for m in member_ids]
        family_should_pass = all(s == "PASS" for s in member_statuses)
        public_status = _status_from(public_output, family_id)
        if family_should_pass and public_status != "PASS":
            mismatches.append(
                f"public Case {family_id} reports {public_status} but all underlying "
                f"checks ({member_ids}) passed"
            )
        if not family_should_pass and public_status == "PASS":
            mismatches.append(
                f"public Case {family_id} reports PASS but underlying checks "
                f"{list(zip(member_ids, member_statuses))} show a failure"
            )
    assert not mismatches, "\n".join(mismatches)


# ── implementation-agnostic change evidence ───────────────────────────────

def test_func_in_scope_change_present():
    changed = _changed_production_files()
    assert changed, "no in-scope production source change was submitted"


def test_sec_no_testharness_fingerprinting():
    """Production remediations must not gate behavior on pytest/go-test/checker presence."""
    changed = _changed_production_files()
    offenders = []
    for rel in sorted(changed):
        try:
            text = (TESTBED / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern in SHORTCUT_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{rel} matches {pattern.pattern}")
                break
    assert not offenders, "production changes appear to fingerprint the test harness:\n" + "\n".join(offenders[:20])


# ── findings artifact ─────────────────────────────────────────────────────

def test_func_affected_code_in_scope():
    """Every affected_code entry a finding references must be a real,
    existing, in-scope production file. The public findings.json schema
    (instruction.md) never requires that an affected_code entry itself be a
    file the remediation modified -- a finding may legitimately cite a
    contextual/call-chain file it did not patch. Requiring it anyway is a
    false-negative risk, not a truthfulness requirement; truthfulness is
    covered separately (test_func_in_scope_change_present requires at least
    one real production change overall, and test_func_artifact_truthfulness
    checks PASS/FAIL claims against the actual reproducer replay)."""
    fp = OUTPUT_DIR / "findings.json"
    if not fp.exists():
        pytest.fail("findings.json absent")
    data = json.loads(fp.read_text(encoding="utf-8"))
    bad = []
    for f in data.get("findings", []):
        for p in f.get("affected_code", []):
            if not _in_production_scope(p):
                bad.append(p)
            if not (TESTBED / p).exists():
                bad.append(f"{p} (not found)")
    assert not bad, f"affected_code issues: {bad}"


def test_func_artifact_truthfulness():
    """Every final PASS or FAIL claim per public case in plan.md /
    findings.json must match the public reproducer's own replay, in both
    directions: an unsupported PASS and an unsupported FAIL are both
    rejected. Lines that describe pre-fix state (before/baseline/original/
    unpatched/...) are not final claims and are ignored; a case whose final
    claims say both PASS and FAIL is ambiguous to a line-level check and is
    left to the semantic judge, as are per-finding narrative claims. Checked
    against the public aggregate (Cases A-E) only -- that is the only
    reproducer output the candidate can actually observe, so it is the only
    thing a truthful claim can be about."""
    plan_path = OUTPUT_DIR / "plan.md"
    findings_path = OUTPUT_DIR / "findings.json"
    if not plan_path.exists() or not findings_path.exists():
        pytest.skip("deliverables absent; covered by test_func_deliverables")
    text = plan_path.read_text(encoding="utf-8") + "\n" + findings_path.read_text(encoding="utf-8")
    final_lines = [line for line in text.splitlines() if not PRE_FIX_CONTEXT.search(line)]
    public_output, _ = _run_reproducer()
    mismatches = []
    for case_id in "ABCDE":
        claims = set()
        for line in final_lines:
            for word in ("PASS", "FAIL"):
                if re.search(rf"\bCase\s+{case_id}\b.{{0,40}}\b{word}\b", line, re.I):
                    claims.add(word)
        if len(claims) != 1:
            continue
        claimed = claims.pop()
        actual = _status_from(public_output, case_id)
        if actual != claimed:
            mismatches.append(f"Case {case_id}: plan/findings claim {claimed} but reproducer reports {actual}")
    actual_status = {case_id: _status_from(public_output, case_id) for case_id in "ABCDE"}
    not_passing = sorted(c for c, s in actual_status.items() if s != "PASS")
    # A generic success claim about the public reproducer (no case named) is a
    # final claim about the whole run: it is false when any public case is not PASS.
    if not_passing:
        for line in _claim_lines(plan_path, findings_path):
            if _generic_reproducer_success(line):
                mismatches.append(
                    f"generic success claim {line.strip()[:120]!r} but reproducer reports "
                    f"{', '.join(f'Case {c}: {actual_status[c]}' for c in not_passing)}")
                break
    # The final verification_runs entry must report the statuses the replay observes.
    for case_id, claimed in _final_run_statuses(findings_path).items():
        if claimed != actual_status[case_id]:
            mismatches.append(
                f"final verification_runs entry records Case {case_id}: {claimed} "
                f"but reproducer reports {actual_status[case_id]}")
    assert not mismatches, "\n".join(mismatches)


_REPRODUCER_REF = re.compile(r"run-all\.sh|\breproducer\b|\bpublic (?:run|cases?|validation)\b", re.I)
_SUCCESS_WORD = re.compile(
    r"\b(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|clean(?:ly)?|green|all\s+(?:cases\s+|groups\s+)?ok)\b", re.I)
_NEGATIVE_WORD = re.compile(
    r"\b(?:not|never|no\s+longer|fail(?:s|ed|ing|ure)?|error|still|except|remaining|but)\b|n't\b", re.I)
_CASE_TOKEN = re.compile(r"\b(?i:cases?)\s+[A-E]\b|(?<![A-Za-z])[A-E]\s*(?:[:=(]|-\s*[A-E]\b)|\b[A-E]\s+(?:PASS|FAIL|ERROR)\b")


def _claim_lines(plan_path: Path, findings_path: Path) -> list[str]:
    """Final (non pre-fix) claim lines from plan.md plus every findings.json string."""
    lines = plan_path.read_text(encoding="utf-8").splitlines()
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
    except Exception:
        data = None

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)

    for value in strings(data):
        lines.extend(value.splitlines())
    return [line for line in lines if line.strip() and not PRE_FIX_CONTEXT.search(line)]


def _generic_reproducer_success(line: str) -> bool:
    """True for an unqualified success claim about the public run, e.g.
    '/testbed/reproducer/run-all.sh - Builds and passes cleanly.'"""
    return bool(_REPRODUCER_REF.search(line) and _SUCCESS_WORD.search(line)
                and not _NEGATIVE_WORD.search(line) and not _CASE_TOKEN.search(line))


def _final_run_statuses(findings_path: Path) -> dict[str, str]:
    """Per-case statuses stated by the final verification_runs result, where unambiguous."""
    try:
        runs = json.loads(findings_path.read_text(encoding="utf-8")).get("verification_runs") or []
        result = str(runs[-1].get("result", "")) if isinstance(runs[-1], dict) else ""
    except Exception:
        return {}
    statuses: dict[str, set[str]] = {}
    span = re.search(rf"(?<![A-Za-z])A\s*(?:-|–|—|to|through)\s*E(?![A-Za-z])[^\n]{{0,25}}?\b({_STATUS_WORD})\b", result, re.I)
    if span:
        for case_id in "ABCDE":
            statuses.setdefault(case_id, set()).add(span.group(1).upper())
    for case_id in "ABCDE":
        # Strict adjacency ("A: PASS", "Case A PASS (2/2)", "A (2/2) FAIL"); anything
        # looser is left to the semantic judge rather than risking a misattribution.
        pattern = rf"(?<![A-Za-z]){case_id}\s*[:=-]?\s*(?:\(\d+/\d+\)\s*)?({_STATUS_WORD})\b"
        for match in re.finditer(pattern, result):
            statuses.setdefault(case_id, set()).add(match.group(1).upper())
    return {case_id: next(iter(found)) for case_id, found in statuses.items() if len(found) == 1}
