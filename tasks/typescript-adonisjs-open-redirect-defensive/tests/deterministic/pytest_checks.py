from __future__ import annotations
import json, os, re, secrets, shutil, subprocess, tempfile
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit
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
CONFIG = json.loads((Path(__file__).with_name("verifier.json")).read_text())

def _quiesce_agent_processes():
    subprocess.run(["pkill", "-KILL", "-u", "agent"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _run(command: str, cwd: Path, timeout: int = 600):
    env = {"PATH": "/opt/task-venv/bin:/opt/rewardkit-venv/bin:/usr/local/bin:/usr/bin:/bin",
           "HOME": "/home/agent", "CI": "1", "NO_COLOR": "1",
           "NODE_OPTIONS": "--dns-result-order=ipv4first"}
    try:
        return subprocess.run(["/usr/sbin/runuser", "-u", "agent", "--", "/bin/bash", "-lc", command],
                              cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"VALIDATION_ERROR: TimeoutExpired after {timeout}s: {command}")
    finally:
        _quiesce_agent_processes()

@pytest.fixture(scope="session")
def verifier_worktree():
    # The solving trajectory has ended. Remove agent-owned observers before
    # creating a scoring checkout or invoking private candidate behavior.
    _quiesce_agent_processes()
    root = Path(tempfile.mkdtemp(prefix="cyberbench-private-"))
    root.chmod(0o755)
    work = root / "source"
    shutil.copytree(TESTBED, work, symlinks=True)
    shutil.rmtree(work / "reproducer", ignore_errors=True)
    # Private expectations never enter the candidate checkout or its process.
    # The runner contains no fixtures; it reports only observed output.
    runner = root / "candidate_runner.mjs"
    shutil.copyfile(Path(__file__).with_name("candidate_runner.mjs"), runner)
    runner.chmod(0o644)
    subprocess.run(["chown", "-R", "agent:agent", str(work)], check=True)
    try:
        yield work, runner
    finally:
        shutil.rmtree(root, ignore_errors=True)

@pytest.fixture(scope="session")
def public_results():
    try:
        proc = subprocess.run(["/testbed/reproducer/run-all.sh"], cwd=TESTBED, text=True,
                              capture_output=True, timeout=CONFIG.get("public_timeout", 1200))
    except subprocess.TimeoutExpired as exc:
        proc = subprocess.CompletedProcess(exc.cmd, 2, "SUMMARY: ERROR\nVALIDATION ERROR: public probe timed out\n", "")
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
        pytest.fail(f"VALIDATION_ERROR: public validation did not complete normally; {proc.stdout[-1800:]}")
    assert group in rows, f"VALIDATION_ERROR: {group} missing; public exit={proc.returncode}"
    status, earned, total = rows[group]
    assert status == "PASS" and total > 0 and earned == total, f"{group}: observed {status} {earned}/{total}"

@pytest.mark.parametrize("case", CONFIG["implicit"], ids=lambda c: c["id"])
def test_implicit_upstream_behavior(case, verifier_worktree):
    work, _ = verifier_worktree
    proc = _run(case["command"], work, case.get("timeout", 600))
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]

@pytest.mark.parametrize("case", CONFIG["private_http_cases"])
def test_private_http_behavior(case, fresh_private_build):
    # Depending on the build fixture guarantees the candidate build ran before any
    # private spec exists in the worktree (the build lints the tests tree).
    work, _ = fresh_private_build
    # Private test sources are stored solely in verifier-owned files and are
    # staged into the disposable copy only after the agent has finished.
    private_fixture = Path(__file__).with_name("private_http_contract.spec.ts")
    target = work / "tests" / "private_http_contract.spec.ts"
    shutil.copyfile(private_fixture, target)
    target.chmod(0o644)
    try:
        proc = _run(f"npm run quick:test -- --files tests/private_http_contract.spec.ts --tests={case}", work, 180)
    finally:
        target.unlink(missing_ok=True)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1800:]

def _observe(work: Path, runner: Path, payload: dict) -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/agent", "CI": "1", "NO_COLOR": "1",
           "NODE_OPTIONS": "--dns-result-order=ipv4first"}
    _quiesce_agent_processes()
    try:
        proc = subprocess.run(["/usr/sbin/runuser", "-u", "agent", "--", "node", str(runner)],
                              cwd=work, env=env, input=json.dumps(payload), text=True,
                              capture_output=True, timeout=60)
    finally:
        _quiesce_agent_processes()
    assert proc.returncode == 0, (proc.stderr or proc.stdout)[-500:]
    return json.loads(proc.stdout)

@pytest.fixture(scope="session")
def fresh_private_build(verifier_worktree):
    work, runner = verifier_worktree
    proc = _run("npm run build", work, 240)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]
    return work, runner

@pytest.mark.parametrize("kind", CONFIG["private_cases"],
                         ids=lambda kind: kind)
def test_private_url_behavior(kind, fresh_private_build):
    work, runner = fresh_private_build
    nonce = secrets.token_hex(5)
    if kind == "named_segment":
        value = f"/{nonce}.example?next=/#part"
        payload = {"pattern": "/:page", "tokens": [{"type": 1, "val": "page", "end": ""}],
                   "params": {"page": value}}
        expected = "/" + quote(value, safe="-_.!~*'()")
    elif kind == "wildcard_segment":
        values = [f"dir/{nonce}", "?next/#fragment"]
        payload = {"pattern": "/*", "tokens": [{"type": 2, "val": "*", "end": ""}],
                   "params": {"*": values}}
        expected = "/" + "/".join(quote(value, safe="-_.!~*'()") for value in values)
    elif kind == "supported_segment":
        value = f"post-{nonce}"
        payload = {"pattern": "/:id", "tokens": [{"type": 1, "val": "id", "end": ""}],
                   "params": {"id": value}, "options": {"prefixUrl": "https://app.example.test"}}
        expected = "https://app.example.test/" + value
    elif kind == "nested_query":
        team = f"team/{nonce}"
        member = f"member?{nonce}"
        payload = {"pattern": "/teams/:team/members/:member",
                   "tokens": [{"type": 0, "val": "teams", "end": ""},
                              {"type": 1, "val": "team", "end": ""},
                              {"type": 0, "val": "members", "end": ""},
                              {"type": 1, "val": "member", "end": ""}],
                   "params": {"team": team, "member": member},
                   "options": {"qs": {"view": "full"}}, "query": "view=full"}
        expected = "/teams/" + quote(team, safe="-_.!~*'()") + "/members/" + quote(member, safe="-_.!~*'()") + "?view=full"
    elif kind == "optional_segment":
        payload = {"pattern": "/users/:id?",
                   "tokens": [{"type": 0, "val": "users", "end": ""},
                              {"type": 3, "val": "id", "end": ""}], "params": {}}
        expected = "/users"
    elif kind == "wildcard_unicode":
        values = [f"café-{nonce}", "space here", "x/y"]
        payload = {"pattern": "/files/*",
                   "tokens": [{"type": 0, "val": "files", "end": ""},
                              {"type": 2, "val": "*", "end": ""}], "params": {"*": values}}
        expected = "/files/" + "/".join(quote(value, safe="-_.!~*'()") for value in values)
    else:
        raise AssertionError(f"unknown private case: {kind}")
    observed = _observe(work, runner, payload)
    assert isinstance(observed, dict) and isinstance(observed.get("value"), str), f"{kind}: observed {observed!r}"
    assert _equivalent_url(observed["value"], expected), f"{kind}: observed {observed!r}; expected {expected!r}"

_PCHAR_SEGMENT = re.compile(r"^(?:[A-Za-z0-9\-._~!$&'()*+,;=:@]|%[0-9A-Fa-f]{2})*$")


def _equivalent_url(actual: str, expected: str) -> bool:
    """Same destination under any valid encoder; never decode across segment boundaries.

    Each path segment must be RFC 3986 pchar-only (raw '/', '?', '#', '\\', spaces and
    non-ASCII are impossible), segment counts must match, and segments must be equal
    after percent-decoding. Query order is not part of the contract.
    """
    got, want = urlsplit(actual), urlsplit(expected)
    if (got.scheme, got.netloc, got.fragment) != (want.scheme, want.netloc, want.fragment):
        return False
    got_segments, want_segments = got.path.split("/"), want.path.split("/")
    if len(got_segments) != len(want_segments):
        return False
    if not all(_PCHAR_SEGMENT.match(segment) for segment in got_segments):
        return False
    if [unquote(s) for s in got_segments] != [unquote(s) for s in want_segments]:
        return False
    return sorted(parse_qsl(got.query, keep_blank_values=True)) == sorted(
        parse_qsl(want.query, keep_blank_values=True))


def _redirect_case(kind: str, nonce: str) -> tuple[dict, str, int]:
    """Build inputs and independent expected results; never send expectations to the candidate."""
    host = f"app-{nonce}.example.test"
    external = f"accounts-{nonce}.example.test"
    prefix = f"https://{external}"
    payload = {"operation": "redirect", "headers": {"host": host},
               "allowedHosts": [], "requestUrl": "/current", "method": "path"}
    status = 302
    if kind == "explicit_external_https":
        payload.update(method="shortcut", target=f"{prefix}/login/{nonce}")
        expected = payload["target"]
    elif kind == "explicit_external_http_port":
        payload["target"] = f"http://{external}:8088/login/{nonce}"
        expected = payload["target"]
    elif kind == "explicit_external_outside_referrer_allowlist":
        payload.update(target=f"{prefix}/login/{nonce}",
                       allowedHosts=[f"referrer-{nonce}.example.test"])
        expected = payload["target"]
    elif kind == "fluent_external_query_status":
        payload.update(target=f"{prefix}/login", query={"flow": nonce}, status=307)
        expected = f"{prefix}/login?flow={nonce}"
        status = 307
    elif kind.startswith("route_external_prefix"):
        payload.update(method="route", identifier="records.show", pattern="/records/:id",
                       params={"id": nonce}, options={"prefixUrl": prefix})
        expected = f"{prefix}/records/{nonce}"
        if kind == "route_external_prefix_named_segment":
            value = f"/{nonce}.example?next=/#part"
            payload.update(pattern="/:id", params={"id": value})
            expected = prefix + "/" + quote(value, safe="-_.!~*'()")
        elif kind == "route_external_prefix_wildcard":
            values = [f"folder/{nonce}", "?next/#fragment"]
            payload.update(pattern="/files/*", params={"*": values})
            expected = prefix + "/files/" + "/".join(quote(v, safe="-_.!~*'()") for v in values)
        elif kind == "route_external_prefix_query_status":
            value = f"team/{nonce}"
            payload.update(params={"id": value}, status=303, forwardQueryString=True,
                           requestUrl=f"/current?from={nonce}&view=old")
            payload["options"]["qs"] = {"view": "full"}
            expected = prefix + "/records/" + quote(value, safe="-_.!~*'()") + f"?from={nonce}&view=full"
            status = 303
        elif kind != "route_external_prefix":
            raise AssertionError(f"unknown private redirect case: {kind}")
    elif kind == "relative_destination":
        payload["target"] = f"/dashboard/{nonce}"
        expected = payload["target"]
    elif kind == "same_host_absolute_destination":
        payload["target"] = f"https://{host}/dashboard/{nonce}"
        expected = payload["target"]
    elif kind in {"referrer_external_rejected", "referrer_same_host_preserved",
                  "referrer_allowlisted_external_preserved"}:
        fallback = f"/safe/{nonce}"
        referrer_host = host if kind == "referrer_same_host_preserved" else external
        referrer = f"https://{referrer_host}/previous/{nonce}"
        payload.update(method="back", fallback=fallback)
        payload["headers"]["referer"] = referrer
        if kind == "referrer_allowlisted_external_preserved":
            payload["allowedHosts"] = [external]
        expected = fallback if kind == "referrer_external_rejected" else referrer
    elif kind in {
        "back_query_disabled_config", "back_query_disabled_fluent", "back_query_cleared",
        "back_custom_query_without_forwarding", "back_query_enabled_config",
        "back_query_enabled_fluent", "back_query_forwarded_merge", "back_query_rejected_referrer",
    }:
        base = f"https://{host}/search/{nonce}"
        payload.update(method="back", fallback=f"/safe/{nonce}")
        payload["headers"]["referer"] = base + f"?term={nonce}&view=old"
        expected = base
        if kind == "back_query_disabled_config":
            payload["forwardQueryString"] = False
        elif kind == "back_query_disabled_fluent":
            payload.update(forwardQueryString=True,
                           queryActions=[{"operation": "withQs", "args": [False]}])
        elif kind == "back_query_cleared":
            payload.update(forwardQueryString=True, query={"discarded": nonce},
                           queryActions=[{"operation": "clearQs"}])
        elif kind == "back_custom_query_without_forwarding":
            payload.update(forwardQueryString=False, query={"notice": nonce}, status=303)
            expected = base + f"?notice={nonce}"
            status = 303
        elif kind in {"back_query_enabled_config", "back_query_enabled_fluent"}:
            if kind == "back_query_enabled_config":
                payload["forwardQueryString"] = True
            else:
                payload["queryActions"] = [{"operation": "withQs", "args": []}]
            expected = payload["headers"]["referer"]
        elif kind == "back_query_forwarded_merge":
            payload.update(forwardQueryString=True, query={"view": "full", "notice": nonce})
            expected = base + f"?term={nonce}&view=full&notice={nonce}"
        else:
            payload["headers"]["referer"] = f"{prefix}/search?term={nonce}"
            payload["forwardQueryString"] = True
            expected = payload["fallback"]
    elif kind in {
        "back_encoded_root_relative", "back_encoded_root_relative_query",
        "back_encoded_interior_control", "back_encoded_absolute_control",
    }:
        base = f"/%2Fcatalog-{nonce}"
        if kind == "back_encoded_interior_control":
            base = f"/files/a%2Fb-{nonce}"
        elif kind == "back_encoded_absolute_control":
            base = f"https://{host}" + base
        payload.update(method="back", fallback=f"/safe/{nonce}")
        payload["headers"]["referer"] = base
        expected = base
        if kind == "back_encoded_root_relative_query":
            payload["headers"]["referer"] += f"?view={nonce}"
            payload["forwardQueryString"] = True
            expected = payload["headers"]["referer"]
    elif kind in {"back_generated_named_destination", "back_generated_wildcard_destination"}:
        value = f"/catalog-{nonce}"
        payload.update(method="back", fallback=f"/safe/{nonce}")
        if kind == "back_generated_named_destination":
            payload["previousRoute"] = {"identifier": "previous.show", "pattern": "/:page",
                                        "params": {"page": value}}
            expected = "/" + quote(value, safe="-_.!~*'()")
        else:
            values = [value, f"item-{nonce}"]
            payload["previousRoute"] = {"identifier": "previous.show", "pattern": "/*",
                                        "params": {"*": values}}
            expected = "/" + "/".join(quote(v, safe="-_.!~*'()") for v in values)
    elif kind in {
        "location_parent_relative", "location_query_only", "header_parent_relative", "header_query_only",
        "location_external_outside_referrer_allowlist", "header_external_outside_referrer_allowlist",
    }:
        method = kind.partition("_")[0]
        payload.update(method=method, allowedHosts=[f"referrer-{nonce}.example.test"])
        status = 200  # Setting a header does not itself send a redirect response.
        if kind.endswith("parent_relative"):
            expected = f"../login-{nonce}"
        elif kind.endswith("query_only"):
            expected = f"?view={nonce}"
        else:
            expected = f"{prefix}/login/{nonce}"
        payload["target"] = expected
    else:
        raise AssertionError(f"unknown private redirect case: {kind}")
    return payload, expected, status


def _assert_redirect_observation(kind: str, observed: dict, expected: str, status: int) -> None:
    assert isinstance(observed, dict), f"{kind}: response observation is not an object"
    assert "error" not in observed, f"{kind}: candidate error: {observed.get('error')}"
    actual = observed.get("location")
    assert isinstance(actual, str), f"{kind}: missing Location header: {observed!r}"
    assert observed.get("status") == status, f"{kind}: observed status {observed.get('status')!r}"
    # Query order and percent-escape form are not part of this contract; decoding
    # never crosses segment boundaries, so the original injection still fails.
    if kind.startswith(("location_", "header_")):
        # Direct setters must store the caller's reference verbatim.
        assert actual == expected, f"{kind}: Location {actual!r}; expected {expected!r}"
    else:
        assert _equivalent_url(actual, expected), f"{kind}: Location {actual!r}; expected {expected!r}"


@pytest.mark.parametrize("kind", CONFIG["implicit_redirect_cases"], ids=lambda kind: kind)
def test_implicit_redirect_behavior(kind, fresh_private_build):
    work, runner = fresh_private_build
    payload, expected, status = _redirect_case(kind, secrets.token_hex(5))
    observed = _observe(work, runner, payload)
    _assert_redirect_observation(kind, observed, expected, status)


def _plan_headings(text: str) -> set[str]:
    """ATX or setext headings outside fences, emphasis stripped, case-folded."""
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
    assert plan.is_file()
    plan_text = plan.read_text(encoding="utf-8")
    assert plan_text.strip()
    headings = _plan_headings(plan_text)
    for heading in ("remediation plan", "root causes identified", "production changes",
                    "preserved legitimate behavior", "validation"):
        assert heading in headings, f"missing plan section: {heading}"
    data = json.loads(findings.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert set(data) == {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    assert isinstance(data["summary"], str) and data["summary"].strip()
    assert isinstance(data["findings"], list) and data["findings"]
    fields = {"area", "trust_boundary", "root_cause", "security_impact", "severity",
              "affected_code", "behavior_before_fix", "remediation", "behavior_after_fix", "verification"}
    allowed_severities = {"informational", "low", "moderate", "high", "critical"}
    root = TESTBED.resolve()
    for finding in data["findings"]:
        assert isinstance(finding, dict) and set(finding) == fields
        for key in fields - {"severity", "affected_code"}:
            assert isinstance(finding[key], str) and finding[key].strip(), key
        assert finding["severity"] in allowed_severities
        paths = finding["affected_code"]
        assert isinstance(paths, list) and paths
        for rel in paths:
            assert isinstance(rel, str) and rel.strip()
            path = Path(rel)
            assert not path.is_absolute() and ".." not in path.parts
            assert path.parts[0] == "src" and not any(part.lower() in {"test", "tests", "__tests__"} for part in path.parts)
            resolved = (root / path).resolve()
            assert resolved.is_relative_to(root) and resolved.is_file(), rel
    preserved = data["preserved_legitimate_behaviors"]
    assert isinstance(preserved, list) and preserved
    assert all(isinstance(item, str) and item.strip() for item in preserved)
    runs = data["verification_runs"]
    assert isinstance(runs, list) and runs
    for run in runs:
        assert isinstance(run, dict) and set(run) == {"command", "result"}
        assert all(isinstance(run[key], str) and run[key].strip() for key in ("command", "result"))
    assert "/testbed/reproducer/run-all.sh" in runs[-1]["command"]
