"""Deterministic checks for the CoreDNS access-policy task.

Trust boundary: case generation, expected outcomes and every pass/fail decision
live in this root-owned process. Submitted code is built and executed only as
the unprivileged `checker` account, from a disposable root-assembled worktree:
the trusted baseline, overlaid with the submitted non-test source from
/testbed (baseline test files are restored, a pre-built binary in /testbed is
never used). Candidate processes receive an allowlisted environment with no
credentials or verifier configuration. DNS queries are sent from this process,
so policies are observed end to end on a live server.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import socket
import string
import subprocess
import tempfile
import time

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

TESTBED = Path(os.environ.get("TESTBED_DIR", os.environ.get("TESTBED", "/testbed")))
OUTPUT = Path(os.environ.get("OUTPUT_DIR", "/app/output"))
BASELINE = Path(os.environ.get("TASK_BASELINE_DIR", "/opt/task-baseline"))
HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "verifier.json").read_text(encoding="utf-8"))
WORKER_USER = os.environ.get("TASK_WORKER_USER", "checker")
AGENT_USER = os.environ.get("TASK_AGENT_USER", "agent")
GO_BIN = os.environ.get("TASK_GO_BIN", "/usr/local/go/bin")
GOCACHE = os.environ.get("TASK_CHECKER_GOCACHE", "/opt/checker-gocache")
GENERATED = ("core/dnsserver/zdirectives.go", "core/plugin/zplugin.go")
DRAWS = 3


# --- submission scope ---------------------------------------------------------
def _is_test_path(rel: Path) -> bool:
    parts = rel.parts
    return (not parts or parts[0] in {"test", "reproducer", ".git", ".github"} or "testdata" in parts
            or rel.name.endswith("_test.go") or rel.as_posix() == "coredns")


def _go_env(home: Path) -> dict:
    # Allowlist only: nothing from the verifier environment reaches submitted code.
    return {"PATH": f"{GO_BIN}:/usr/local/bin:/usr/bin:/bin", "HOME": str(home),
            "TMPDIR": str(home / "tmp"), "LANG": "C.UTF-8", "GOPATH": "/go", "GOMODCACHE": "/go/pkg/mod",
            "GOCACHE": GOCACHE, "GOFLAGS": "-mod=mod", "GOPROXY": "off", "GOSUMDB": "off",
            "GONOSUMDB": "*", "GOTOOLCHAIN": "local", "CGO_ENABLED": "0"}


def _quiesce(user: str = WORKER_USER):
    subprocess.run(["pkill", "-KILL", "-u", user], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _assemble() -> dict:
    """Trusted baseline + submitted non-test source, owned by the worker account."""
    _quiesce()
    root = Path(tempfile.mkdtemp(prefix="coredns-verifier-"))
    root.chmod(0o711)
    work = root / "src"
    shutil.copytree(BASELINE, work, symlinks=False,
                    ignore=lambda d, names: [n for n in names if n in {"coredns", "reproducer", ".git"}])
    submitted = set()
    for path in sorted(TESTBED.rglob("*")):
        rel = path.relative_to(TESTBED)
        if _is_test_path(rel) or any(p in {"reproducer", ".git"} for p in rel.parts):
            continue
        if path.is_symlink():
            # Upstream ships a few symlinks; only an unchanged baseline link is accepted.
            original = BASELINE / rel
            if original.is_symlink() and os.readlink(original) == os.readlink(path):
                submitted.add(rel)
                continue
            raise AssertionError(f"SUBMISSION_ERROR: submitted source adds or changes a symlink: {rel}")
        if path.is_file():
            submitted.add(rel)
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, work / rel)
    for path in sorted(work.rglob("*")):
        rel = path.relative_to(work)
        if path.is_file() and not _is_test_path(rel) and rel not in submitted:
            path.unlink()  # mirror production files the submission removed
    home = root / "home"
    (home / "tmp").mkdir(parents=True)
    subprocess.run(["chown", "-R", f"{WORKER_USER}:{WORKER_USER}", str(work), str(home)], check=True)
    return {"root": root, "work": work, "home": home}


def _dispose(tree: dict) -> None:
    _quiesce()
    shutil.rmtree(tree["root"], ignore_errors=True)


def _as_user(user: str, argv: list[str], cwd: Path, env: dict, timeout: int):
    child = subprocess.Popen(["/usr/sbin/runuser", "-u", user, "--", *argv], cwd=cwd, env=env, text=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             start_new_session=True)
    try:
        out, _ = child.communicate(timeout=timeout)
        return child.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        pytest.fail(f"VALIDATION_ERROR: TimeoutExpired after {timeout}s: {' '.join(argv[:3])}")
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _go(tree: dict, args: list[str], timeout: int):
    return _as_user(WORKER_USER, [f"{GO_BIN}/go", *args], tree["work"], _go_env(tree["home"]), timeout)


@pytest.fixture(scope="session")
def built_tree():
    tree = _assemble()
    try:
        binary = tree["home"] / "coredns"
        code, out = _go(tree, ["build", "-tags", "grpcnotrace", "-o", str(binary), "."], 900)
        tree["build"] = (code, out[-1500:])
        tree["binary"] = binary
        yield tree
    finally:
        _dispose(tree)


def _require_build(tree: dict) -> None:
    code, log = tree["build"]
    assert code == 0, f"submitted source failed to build: {log}"


# --- live server --------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, tree: dict, body: str):
        self.dir = Path(tempfile.mkdtemp(prefix="corefile-", dir=tree["home"]))
        self.port = _free_port()
        (self.dir / "Corefile").write_text(f".:{self.port} {{\n    bind 127.0.0.1\n{body}\n}}\n", encoding="utf-8")
        subprocess.run(["chown", "-R", f"{WORKER_USER}:{WORKER_USER}", str(self.dir)], check=True)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(tree["home"]), "TMPDIR": str(tree["home"] / "tmp")}
        self.proc = subprocess.Popen(["/usr/sbin/runuser", "-u", WORKER_USER, "--", str(tree["binary"]),
                                      "-conf", str(self.dir / "Corefile")], cwd=self.dir, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.stop()
        pytest.fail("VALIDATION_ERROR: candidate server did not start for a verifier configuration")

    def query(self, source: str, name: str, qtype: str):
        import dns.message
        import dns.query
        import dns.rdatatype
        q = dns.message.make_query(name, dns.rdatatype.from_text(qtype))
        q.flags &= ~dns.flags.RD
        return dns.query.udp(q, "127.0.0.1", port=self.port, source=source, timeout=3)

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()


# --- private scenario generators (definitions and expectations stay here) -----
def _label(rng: random.Random) -> str:
    return rng.choice(string.ascii_lowercase) + "".join(rng.choice(string.ascii_lowercase + string.digits)
                                                         for _ in range(rng.randint(5, 9)))


def _zone(rng: random.Random) -> str:
    return f"{_label(rng)}.{rng.choice(['corp', 'lan', 'example', 'internal', 'test'])}."


def _ip4(rng: random.Random) -> str:
    return f"{rng.choice(['192.0.2', '198.51.100', '203.0.113'])}.{rng.randint(1, 254)}"


def _ip6(rng: random.Random) -> str:
    return f"2001:db8::{rng.randint(1, 0xffff):x}"


def _clients(rng: random.Random) -> tuple[str, str]:
    allowed, blocked = rng.sample(range(2, 250), 2)
    return f"127.0.0.{allowed}", f"127.0.0.{blocked}"


def _draw(kind: str, rng: random.Random, workdir: Path) -> dict:
    """Return {'body': Corefile body, 'steps': [(source, qname, qtype, expectation)]}."""
    other = "127.0.0.1"
    if kind == "name_rewrite_bypass":
        blocked, public = _zone(rng), _zone(rng)
        ip = _ip4(rng)
        if rng.random() < 0.5:
            src, dst = f"{_label(rng)}.{public}", f"{_label(rng)}.{blocked}"
            rule = f"rewrite name exact {src} {dst}"
        else:
            host = _label(rng)
            src, dst = f"{host}.{public}", f"{host}.{blocked}"
            rule = f"rewrite name suffix .{public} .{blocked}"
        body = f"    {rule}\n    acl {blocked} {{\n        block\n    }}\n    hosts {{\n        {ip} {dst}\n        fallthrough\n    }}"
        return {"body": body, "steps": [(other, src, "A", "REFUSED")]}
    if kind == "type_rewrite_bypass":
        name = f"{_label(rng)}.{_zone(rng)}"
        if rng.random() < 0.5:
            rule, blocked_type, asked, record = "rewrite type A AAAA", "AAAA", "A", _ip6(rng)
        else:
            rule, blocked_type, asked, record = "rewrite type AAAA A", "A", "AAAA", _ip4(rng)
        zone = rng.choice([".", name.split(".", 1)[1]])
        body = (f"    {rule}\n    acl {zone} {{\n        block type {blocked_type}\n    }}\n"
                f"    hosts {{\n        {record} {name}\n        fallthrough\n    }}")
        return {"body": body, "steps": [(other, name, asked, "REFUSED")]}
    if kind == "cache_cross_client":
        zone = _zone(rng)
        allowed, blocked = _clients(rng)
        names = [(f"{_label(rng)}.{zone}", _ip4(rng)) for _ in range(rng.randint(2, 3))]
        hosts = "\n".join(f"        {ip} {name}" for name, ip in names)
        body = (f"    acl {zone} {{\n        allow net {allowed}/32\n        block\n    }}\n    cache {rng.randint(30, 300)}\n"
                f"    hosts {{\n{hosts}\n        fallthrough\n    }}")
        steps = []
        for name, ip in names:
            steps += [(allowed, name, "A", f"ANSWER:{ip}"), (blocked, name, "A", "REFUSED"),
                      (allowed, name, "A", f"ANSWER:{ip}")]
        return {"body": body, "steps": steps}
    if kind in ("autopath_expansion_bypass", "autopath_allowed_control"):
        zones = [_zone(rng) for _ in range(3)]
        if kind == "autopath_expansion_bypass":
            protected = target = rng.choice(zones[1:])
        else:  # the allowed expansion is reached before any protected search domain
            target, protected = zones[1], zones[2]
        host, ip = _label(rng), _ip4(rng)
        resolv = workdir / f"resolv-{secrets.token_hex(4)}.conf"
        resolv.write_text("search " + " ".join(z.rstrip(".") for z in zones) + "\n", encoding="utf-8")
        body = (f"    acl {protected} {{\n        block\n    }}\n    autopath {resolv}\n"
                f"    hosts {{\n        {ip} {host}.{target}\n    }}")
        expect = "REFUSED" if kind == "autopath_expansion_bypass" else f"ANSWER:{ip}"
        return {"body": body, "steps": [(other, f"{host}.{zones[0]}", "A", expect)], "files": [resolv]}
    if kind == "rewrite_allowed_control":
        blocked, public, allowed_zone = _zone(rng), _zone(rng), _zone(rng)
        if rng.random() < 0.5:
            src, dst, ip = f"{_label(rng)}.{public}", f"{_label(rng)}.{allowed_zone}", _ip4(rng)
            rule, step = f"rewrite name exact {src} {dst}", (other, src, "A", f"ANSWER:{ip}")
        else:
            dst, ip = f"{_label(rng)}.{allowed_zone}", _ip6(rng)
            rule, step = "rewrite type A AAAA", (other, dst, "A", f"ANSWER:{ip}")
        body = (f"    {rule}\n    acl {blocked} {{\n        block\n    }}\n"
                f"    hosts {{\n        {ip} {dst}\n        fallthrough\n    }}")
        return {"body": body, "steps": [step]}
    if kind == "cache_allowed_hit_control":
        zone, blocked_zone = _zone(rng), _zone(rng)
        allowed, _ = _clients(rng)
        name, ip = f"{_label(rng)}.{zone}", _ip4(rng)
        body = (f"    acl {zone} {{\n        allow net {allowed}/32\n        block\n    }}\n    acl {blocked_zone} {{\n        block\n    }}\n"
                f"    cache 120\n    hosts {{\n        {ip} {name}\n        fallthrough\n    }}")
        return {"body": body, "steps": [(allowed, name, "A", f"ANSWER:{ip}")] * 3}
    if kind == "policy_direct_control":
        zone = _zone(rng)
        allowed, blocked = _clients(rng)
        name, ip = f"{_label(rng)}.{zone}", _ip4(rng)
        body = (f"    acl {zone} {{\n        allow net {allowed}/32\n        block\n    }}\n"
                f"    hosts {{\n        {ip} {name}\n        fallthrough\n    }}")
        return {"body": body, "steps": [(blocked, name, "A", "REFUSED"), (allowed, name, "A", f"ANSWER:{ip}")]}
    if kind == "unrelated_resolution_control":
        zone = _zone(rng)
        name, ip = f"{_label(rng)}.{zone}", _ip4(rng)
        body = f"    hosts {{\n        {ip} {name}\n        fallthrough\n    }}"
        return {"body": body, "steps": [(other, name, "A", f"ANSWER:{ip}"),
                                        (other, f"{_label(rng)}.{zone}", "A", "NOT_REFUSED")]}
    raise AssertionError(f"unknown generator {kind}")


def _check(kind: str, step: tuple, response) -> None:
    import dns.rcode
    source, name, qtype, expect = step
    rcode = dns.rcode.to_text(response.rcode())
    if expect == "REFUSED":
        assert rcode == "REFUSED", f"{kind}: a query the policy must deny was answered ({rcode})"
    elif expect == "NOT_REFUSED":
        assert rcode != "REFUSED", f"{kind}: an unrelated query was refused"
    else:
        want = expect.split(":", 1)[1]
        values = {rr.to_text() for rrset in response.answer for rr in rrset}
        assert rcode == "NOERROR" and want in values, \
            f"{kind}: a query the policy allows did not receive its answer ({rcode})"


@pytest.mark.parametrize("case", CONFIG["private_cases"], ids=lambda c: c["id"])
def test_private_behavior(case, built_tree):
    _require_build(built_tree)
    rng = random.Random(secrets.randbits(64))
    for _ in range(DRAWS):
        scenario = _draw(case["generator"], rng, built_tree["home"])
        for extra in scenario.get("files", []):
            os.chmod(extra, 0o644)
        with Server(built_tree, scenario["body"]) as server:
            for step in scenario["steps"]:
                _check(case["generator"], step, server.query(step[0], step[1], step[2]))


def test_plugin_set_preserved(built_tree):
    _require_build(built_tree)
    code, out = _as_user(WORKER_USER, [str(built_tree["binary"]), "-plugins"], built_tree["home"],
                         {"PATH": "/usr/bin:/bin", "HOME": str(built_tree["home"])}, 30)
    assert code == 0, "server binary could not list its plugins"
    names = {line.strip().rsplit(".", 1)[-1] for line in out.splitlines() if line.strip()}
    baseline = {line.split(":", 1)[0].strip() for line in (BASELINE / "plugin.cfg").read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")}
    missing = sorted(p for p in baseline | {"proxyproto"} if p not in names)
    assert not missing, f"plugins previously available are missing from the built server: {missing[:8]}"


# --- generation consistency (behavioral, relocated) -----------------------------
def _digest(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and not any(p in {"tmp"} for p in rel.parts[:1]):
            result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_generation_consistency():
    """Regenerate in a fresh tree at a different path, with /testbed unreachable.

    Generated files are removed first, so they must be recreated inside this
    copy from its own generator input; the result must equal the submitted
    files, and nothing else in the tree may change.
    """
    tree = _assemble()
    try:
        expected = {rel: (tree["work"] / rel).read_bytes() for rel in GENERATED}
        for rel in GENERATED:
            (tree["work"] / rel).unlink()
        before = _digest(tree["work"])
        # The copy must regenerate from its own inputs: hide the agent workspace
        # from the worker while generating, so no step can reach back into it.
        mode = TESTBED.stat().st_mode & 0o7777
        TESTBED.chmod(0o700)
        try:
            code, out = _go(tree, ["generate", "coredns.go"], 300)
        finally:
            TESTBED.chmod(mode)
        assert code == 0, f"go generate failed in a relocated copy of the submitted source: {out[-1200:]}"
        for rel in GENERATED:
            path = tree["work"] / rel
            assert path.is_file(), f"go generate did not recreate {rel} inside the relocated copy"
            assert path.read_bytes() == expected[rel], f"{rel} differs from a fresh generation of the submitted generator input"
        after = _digest(tree["work"])
        changed = sorted(k for k in set(before) | set(after) if k not in GENERATED and before.get(k) != after.get(k))
        assert not changed, f"go generate changed other files: {changed[:6]}"
    finally:
        _dispose(tree)


def test_upstream_unit_tests(built_tree):
    _require_build(built_tree)
    code, out = _go(built_tree, ["test", "-count=1", "-timeout=300s", *CONFIG["upstream_packages"]], 600)
    assert code == 0, out[-1500:]


# --- public reproducer ----------------------------------------------------------
@pytest.fixture(scope="session")
def public_results():
    _quiesce(AGENT_USER)
    env = {"PATH": f"{GO_BIN}:/usr/local/bin:/usr/bin:/bin", "HOME": f"/home/{AGENT_USER}",
           "TMPDIR": f"/home/{AGENT_USER}/.tmp", "LANG": "C.UTF-8", "GOPATH": "/go", "GOMODCACHE": "/go/pkg/mod",
           "GOFLAGS": "-mod=mod", "GOPROXY": "off", "GOSUMDB": "off", "GONOSUMDB": "*", "GOTOOLCHAIN": "local"}
    try:
        code, out = _as_user(AGENT_USER, [str(TESTBED / "reproducer/run-all.sh")], TESTBED, env,
                             CONFIG.get("public_timeout", 900))
    finally:
        _quiesce(AGENT_USER)
    log_dir = Path(os.environ.get("VERIFIER_DIR", os.environ.get("RESULT_DIR", "/logs/verifier")))
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "public_validation.txt").write_text(out, encoding="utf-8")
    rows = {m.group(1): (m.group(2), int(m.group(3)), int(m.group(4)))
            for m in re.finditer(r"^([\w-]+): (PASS|FAIL) \((\d+)/(\d+)\)", out, re.M)}
    return code, out, rows


@pytest.mark.parametrize("group", CONFIG["public_groups"])
def test_public_group(group, public_results):
    code, out, rows = public_results
    assert group in rows, f"VALIDATION_ERROR: {group} missing from public output: {out[-600:]}"
    status, passed, total = rows[group]
    assert status == "PASS" and total and passed == total, f"{group}: observed {status} {passed}/{total}"


# --- artifact contract ------------------------------------------------------------
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
    plan_text = plan.read_text(encoding="utf-8") if plan.is_file() else ""
    assert plan_text.strip(), "plan.md missing or empty"
    headings = _plan_headings(plan_text)
    for heading in ("remediation plan", "root causes identified", "production changes",
                    "preserved legitimate behavior", "validation"):
        assert heading in headings, f"missing plan heading: {heading}"
    data = json.loads((OUTPUT / "findings.json").read_text(encoding="utf-8"))
    required = {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    assert isinstance(data, dict) and set(data) == required, "findings.json has an invalid shape"
    assert isinstance(data["summary"], str) and data["summary"].strip()
    assert isinstance(data["findings"], list) and data["findings"]
    fields = {"area", "trust_boundary", "root_cause", "security_impact", "severity", "affected_code",
              "behavior_before_fix", "remediation", "behavior_after_fix", "verification"}
    root = TESTBED.resolve()
    for finding in data["findings"]:
        assert isinstance(finding, dict) and set(finding) == fields, "finding has an invalid shape"
        assert all(isinstance(finding[k], str) and finding[k].strip() for k in fields - {"affected_code"})
        assert finding["severity"] in {"informational", "low", "moderate", "high", "critical"}
        assert isinstance(finding["affected_code"], list) and finding["affected_code"]
        for rel in finding["affected_code"]:
            path = Path(rel)
            assert isinstance(rel, str) and not path.is_absolute() and ".." not in path.parts
            resolved = (root / path).resolve()
            assert resolved.is_file() and resolved.is_relative_to(root) and not _is_test_path(resolved.relative_to(root)), \
                "affected_code must name an existing production source file under /testbed"
    preserved = data["preserved_legitimate_behaviors"]
    assert isinstance(preserved, list) and preserved and all(isinstance(v, str) and v.strip() for v in preserved)
    runs = data["verification_runs"]
    assert isinstance(runs, list) and runs
    assert all(isinstance(x, dict) and set(x) == {"command", "result"} and
               all(isinstance(x[k], str) and x[k].strip() for k in ("command", "result")) for x in runs)
    assert "/testbed/reproducer/run-all.sh" in runs[-1]["command"]
