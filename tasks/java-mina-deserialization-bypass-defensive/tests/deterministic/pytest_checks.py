"""Deterministic checks for the MINA deserialization task.

Trust boundary: case generation, expected outcomes and every pass/fail decision
live in this root-owned process. Submitted code is compiled and executed only as
the unprivileged `checker` account, in disposable root-assembled worktrees built
from the trusted baseline plus the submitted `mina-core/src/main`, with an
allowlisted environment (no credentials or verifier configuration). Private
proxy holdouts send one case per line to a fixture-free worker and receive raw
observations back.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import subprocess
import tempfile

try:
    import pytest
except ModuleNotFoundError:
    # RewardKit discovery imports this module on hosts without pytest.
    class _ImportSafePytest:
        @staticmethod
        def fixture(*args, **kwargs):
            if args and callable(args[0]) and not kwargs:
                return args[0]
            return lambda fn: fn

        class mark:
            @staticmethod
            def parametrize(*args, **kwargs):
                return lambda fn: fn

        @staticmethod
        def fail(message):
            raise AssertionError(message)
    pytest = _ImportSafePytest()

TESTBED = Path(os.environ.get("TESTBED_DIR", "/testbed"))
OUTPUT = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
BASELINE = Path(os.environ.get("CHECKER_BASELINE_DIR", "/opt/checker-baseline"))
HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "verifier.json").read_text())
WORKER_SOURCE = HERE / "support" / "ProxyWorker.java"
WORKER_USER = os.environ.get("TASK_WORKER_USER", "checker")
MAVEN_REPO = os.environ.get("TASK_MAVEN_REPO", "/opt/maven-cache")
SUBMITTED_SCOPE = Path("mina-core/src/main")
REJECTIONS = {"java.lang.ClassNotFoundException", "org.apache.mina.core.buffer.BufferDataException",
              "java.io.InvalidClassException", "java.io.InvalidObjectException", "java.lang.SecurityException"}


# --- isolated execution --------------------------------------------------------
def _quiesce():
    subprocess.run(["pkill", "-KILL", "-u", WORKER_USER], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _worker_env(home: Path) -> dict:
    # Allowlist only: nothing from the verifier environment reaches submitted code.
    return {"PATH": "/opt/java/openjdk/bin:/usr/share/maven/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home), "TMPDIR": str(home), "LANG": "C.UTF-8",
            "JAVA_HOME": os.environ.get("JAVA_HOME", "/opt/java/openjdk"),
            "MAVEN_OPTS": f"-Dmaven.repo.local={MAVEN_REPO}"}


def _assemble() -> dict:
    """Trusted baseline + submitted mina-core/src/main; only build outputs are worker-writable."""
    _quiesce()
    root = Path(tempfile.mkdtemp(prefix="mina-verifier-"))
    root.chmod(0o711)
    work = root / "source"
    shutil.copytree(BASELINE, work, symlinks=False, ignore=shutil.ignore_patterns("target", "reproducer"))
    submitted = TESTBED / SUBMITTED_SCOPE
    if any(p.is_symlink() for p in submitted.rglob("*")):
        raise AssertionError("SUBMISSION_ERROR: submitted source contains a symlink")
    shutil.rmtree(work / SUBMITTED_SCOPE)
    shutil.copytree(submitted, work / SUBMITTED_SCOPE, symlinks=False)
    subprocess.run(["chown", "-R", "root:root", str(work)], check=True)
    subprocess.run(["chmod", "-R", "a-w,a+rX", str(work)], check=True)
    home = root / "home"
    home.mkdir()
    writable = [home] + [work / rel / "target" for rel in (".", "mina-core")]
    for path in writable:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["chown", "-R", f"{WORKER_USER}:{WORKER_USER}", str(path)], check=True)
    return {"root": root, "work": work, "home": home}


def _dispose(tree: dict) -> None:
    _quiesce()
    shutil.rmtree(tree["root"], ignore_errors=True)


def _as_worker(argv: list[str], tree: dict, timeout: int, stdin: str | None = None):
    child = subprocess.Popen(["/usr/sbin/runuser", "-u", WORKER_USER, "--", *argv],
                             cwd=tree["work"], env=_worker_env(tree["home"]), text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    try:
        out, err = child.communicate(input=stdin, timeout=timeout)
        return child.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        pytest.fail(f"VALIDATION_ERROR: TimeoutExpired after {timeout}s: {' '.join(argv[:4])}")
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _quiesce()


@pytest.fixture(scope="session")
def built_tree():
    """One trusted worktree with the submitted classes built, plus the compiled worker."""
    tree = _assemble()
    try:
        code, out, err = _as_worker(["mvn", "-B", "-ntp", "-o", "-q", "-pl", "mina-core", "-am",
                                     "-DskipTests", "package"], tree, 600)
        tree["build"] = (code, (out + err)[-1500:])
        if code == 0:
            worker_dir = tree["home"] / "worker"
            worker_dir.mkdir()
            shutil.copyfile(WORKER_SOURCE, worker_dir / "ProxyWorker.java")
            subprocess.run(["chown", "-R", f"{WORKER_USER}:{WORKER_USER}", str(worker_dir)], check=True)
            classes = tree["work"] / "mina-core/target/classes"
            # -proc:none: never run annotation processors discovered on the submitted classpath.
            code, out, err = _as_worker(["javac", "-proc:none", "-nowarn", "-cp", str(classes),
                                         "-d", str(worker_dir), str(worker_dir / "ProxyWorker.java")], tree, 180)
            tree["worker"] = (code, (out + err)[-1500:], worker_dir)
        yield tree
    finally:
        _dispose(tree)


def _runtime_classpath(tree: dict) -> str:
    slf4j = sorted(Path(MAVEN_REPO).glob("org/slf4j/slf4j-api/*/slf4j-api-*.jar"))
    assert slf4j, "VALIDATION_ERROR: slf4j-api missing from the offline Maven repository"
    return ":".join([str(tree["work"] / "mina-core/target/classes"), str(slf4j[-1]), str(tree["worker"][2])])


# --- private proxy holdouts (definitions and expectations stay here) ------------
POOL = ("java.lang.Runnable", "java.lang.Comparable", "java.lang.AutoCloseable", "java.io.Closeable",
        "java.util.concurrent.Callable", "java.util.function.Supplier", "java.util.function.Consumer",
        "java.util.function.Predicate")
BASE = ("java.lang.reflect.Proxy", "ProxyWorker$Handler")
WILDCARDS = {"java.util.function.*": ("java.util.function.",), "java.lang.*": ("java.lang.",),
             "java.util.concurrent.*": ("java.util.concurrent.",), "java.io.*": ("java.io.",)}
DRAWS = 4


def _exact(names):
    return list(names)


def _draw(kind: str, rng: random.Random) -> dict:
    """One case: worker input plus the trusted expectation."""
    if kind == "proxy_single_unaccepted":
        iface = rng.choice(POOL)
        decoys = [n for n in POOL if n != iface]
        patterns = list(BASE) + rng.sample(decoys, rng.randint(0, 3))
        return {"mode": "proxy", "patterns": patterns, "items": [iface]}
    if kind == "proxy_partial_accept":
        ifaces = rng.sample(POOL, rng.randint(2, 3))
        blocked = rng.choice(ifaces)
        patterns = list(BASE) + [n for n in ifaces if n != blocked]
        return {"mode": "proxy", "patterns": patterns, "items": ifaces}
    if kind == "proxy_wildcard_scope":
        wildcard = rng.choice(sorted(WILDCARDS))
        prefix = WILDCARDS[wildcard][0]
        outside = [n for n in POOL if not n.startswith(prefix)]
        inside = [n for n in POOL if n.startswith(prefix)]
        ifaces = [rng.choice(outside)] + rng.sample(inside, rng.randint(0, min(1, len(inside))))
        rng.shuffle(ifaces)
        return {"mode": "proxy", "patterns": list(BASE) + [wildcard], "items": ifaces}
    if kind == "proxy_nested_in_container":
        ifaces = rng.sample(POOL, rng.randint(1, 2))
        blocked = rng.choice(ifaces)
        patterns = ["java.util.ArrayList"] + list(BASE) + [n for n in ifaces if n != blocked]
        return {"mode": "proxy_in_list", "patterns": patterns, "items": ifaces}
    if kind == "proxy_accepted_control":
        ifaces = rng.sample(POOL, rng.randint(1, 3))
        patterns = list(BASE)
        for name in ifaces:  # cover each interface exactly or through its package wildcard
            wild = next((w for w, (p,) in WILDCARDS.items() if name.startswith(p) and rng.random() < 0.5), None)
            patterns.append(wild or name)
        mode = rng.choice(("proxy", "proxy_in_list"))
        if mode == "proxy_in_list":
            patterns.append("java.util.ArrayList")
        return {"mode": mode, "patterns": patterns, "items": ifaces}
    if kind == "object_roundtrip_control":
        return {"mode": "list", "patterns": ["java.util.ArrayList"],
                "items": [secrets.token_hex(6) for _ in range(rng.randint(1, 4))]}
    if kind == "unaccepted_class_control":
        return {"mode": "list", "patterns": rng.sample(POOL, 2), "items": [secrets.token_hex(6)]}
    raise AssertionError(f"unknown generator {kind}")


def _required(case: dict) -> list[str]:
    if case["mode"] == "list":
        return ["java.util.ArrayList"]
    names = list(BASE) + list(case["items"])
    return names + (["java.util.ArrayList"] if case["mode"] == "proxy_in_list" else [])


def _expect_accept(case: dict) -> bool:
    return all(any(fnmatchcase(name, p) for p in case["patterns"]) for name in _required(case))


def _judge(kind: str, case: dict, line: str) -> None:
    parts = line.split("\t")
    status = parts[0]
    assert status != "ERROR", f"VALIDATION_ERROR: {kind}: worker {line[:120]}"
    if _expect_accept(case):
        if case["mode"] == "list":
            assert status == "VALUE" and parts[1] == ",".join(case["items"]), \
                f"{kind}: supported object did not round-trip ({status})"
        else:
            assert status == "ACCEPTED" and parts[1] == "true", f"{kind}: accepted proxy was not restored ({status})"
            assert sorted(parts[2].split(",")) == sorted(case["items"]), f"{kind}: restored proxy interfaces changed"
    else:
        assert status == "REJECTED", f"{kind}: object outside the accept list was deserialized ({status})"
        assert parts[1] in REJECTIONS, f"{kind}: rejection raised unexpected {parts[1]}"


@pytest.mark.parametrize("case", CONFIG["private_cases"], ids=lambda c: c["id"])
def test_private_behavior(case, built_tree):
    code, log = built_tree["build"]
    assert code == 0, f"submitted source failed to build: {log}"
    wcode, wlog, _ = built_tree["worker"]
    assert wcode == 0, f"VALIDATION_ERROR: worker failed to compile against submitted classes: {wlog}"
    rng = random.Random(secrets.randbits(64))
    cases = [_draw(case["generator"], rng) for _ in range(DRAWS)]
    stdin = "".join(f"{c['mode']}\t{'|'.join(c['patterns'])}\t{','.join(c['items'])}\n" for c in cases)
    code, out, err = _as_worker(["java", "-cp", _runtime_classpath(built_tree), "ProxyWorker"],
                                built_tree, case.get("timeout", 120), stdin=stdin)
    assert code == 0, f"VALIDATION_ERROR: worker exited {code}: {err[-600:]}"
    lines = [l for l in out.splitlines() if l]
    assert len(lines) == len(cases), f"VALIDATION_ERROR: worker returned {len(lines)}/{len(cases)} observations"
    for observed, generated in zip(lines, cases):
        _judge(case["generator"], generated, observed)


# --- public probe and upstream suites ---------------------------------------------
@pytest.fixture(scope="session")
def public_results():
    try:
        proc = subprocess.run(["/testbed/reproducer/run-all.sh"], cwd=TESTBED, text=True,
                              capture_output=True, timeout=CONFIG.get("public_timeout", 1200))
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(exc.cmd, 2, "SUMMARY: ERROR\n", "")
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
    if (group in rows and rows[group][0] == "ERROR") or (proc.returncode == 2 and not rows):
        pytest.fail(f"VALIDATION_ERROR: public validation did not complete normally; {proc.stdout[-1500:]}")
    assert group in rows, f"VALIDATION_ERROR: {group} missing; public exit={proc.returncode}"
    status, earned, total = rows[group]
    assert status == "PASS" and total > 0 and earned == total, f"{group}: observed {status} {earned}/{total}"


@pytest.mark.parametrize("case", CONFIG["implicit"], ids=lambda c: c["id"])
def test_implicit_upstream_behavior(case):
    # A fresh trusted worktree per row: submitted code from one row cannot alter
    # the test sources or compiled tests another row relies on.
    tree = _assemble()
    try:
        code, out, err = _as_worker(["bash", "-c", case["command"]], tree, case.get("timeout", 600))
        assert code == 0, (out + err)[-1500:]
    finally:
        _dispose(tree)


# --- artifact contract -------------------------------------------------------------
def _plan_headings(text: str) -> set[str]:
    headings, previous, fenced = set(), "", False
    for line in text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced, previous = not fenced, ""
            continue
        if fenced:
            continue
        match = re.match(r"^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", line)
        if match:
            heading = match.group(1)
        elif previous.strip() and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", line):
            heading = previous
        else:
            previous = line
            continue
        headings.add(re.sub(r"[*_`]", "", heading).strip().casefold())
        previous = ""
    return headings


def test_required_artifacts():
    plan = OUTPUT / "plan.md"
    findings = OUTPUT / "findings.json"
    plan_text = plan.read_text(encoding="utf-8") if plan.is_file() else ""
    assert plan_text.strip(), "plan.md missing or empty"
    headings = _plan_headings(plan_text)
    for heading in ("remediation plan", "root causes identified", "production changes",
                    "preserved legitimate behavior", "validation"):
        assert heading in headings, f"missing plan heading: {heading}"
    data = json.loads(findings.read_text(encoding="utf-8"))
    required = {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    assert isinstance(data, dict) and set(data) == required, "findings.json has an invalid shape"
    assert isinstance(data["summary"], str) and data["summary"].strip()
    assert isinstance(data["findings"], list) and data["findings"]
    fields = {"area", "trust_boundary", "root_cause", "security_impact", "severity", "affected_code",
              "behavior_before_fix", "remediation", "behavior_after_fix", "verification"}
    allowed = {"informational", "low", "moderate", "high", "critical"}
    root = TESTBED.resolve()
    for finding in data["findings"]:
        assert isinstance(finding, dict) and set(finding) == fields, "finding has an invalid shape"
        assert all(isinstance(finding[k], str) and finding[k].strip() for k in fields - {"affected_code"})
        assert finding["severity"] in allowed
        assert isinstance(finding["affected_code"], list) and finding["affected_code"]
        for rel in finding["affected_code"]:
            path = Path(rel)
            assert isinstance(rel, str) and not path.is_absolute() and ".." not in path.parts
            resolved = (root / path).resolve()
            assert resolved.is_file() and resolved.is_relative_to(root / SUBMITTED_SCOPE), \
                f"affected_code must name permitted production source under {SUBMITTED_SCOPE}"
    preserved = data["preserved_legitimate_behaviors"]
    assert isinstance(preserved, list) and preserved and all(isinstance(v, str) and v.strip() for v in preserved)
    runs = data["verification_runs"]
    assert isinstance(runs, list) and runs
    assert all(isinstance(x, dict) and set(x) == {"command", "result"} and
               all(isinstance(x[k], str) and x[k].strip() for k in ("command", "result")) for x in runs)
    assert "/testbed/reproducer/run-all.sh" in runs[-1]["command"]
