"""Verifier-only behavioral and scope checks for the Koa task.

Trust boundary: case definitions, expected observations and every pass/fail
decision live in this root-owned process. Candidate JavaScript runs only as the
dedicated unprivileged `checker` account, inside a disposable copy of the
submission, with an allowlisted environment (no credentials or verifier
configuration). Each run receives one behavioral input on stdin and returns raw
observations from a fixture-free worker.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

try:
    import pytest
except ModuleNotFoundError:
    # RewardKit discovery imports every module under tests/ on hosts without
    # pytest; keep this file import-safe there. Pytest runs only in the prepass.
    class _ImportSafePytest:
        @staticmethod
        def fixture(*args, **kwargs):
            if args and callable(args[0]) and not kwargs:
                return args[0]
            return lambda fn: fn
    pytest = _ImportSafePytest()

TESTBED = Path(os.environ.get('TESTBED', '/testbed'))
BASELINE = Path(os.environ.get('BASELINE_DIR', '/opt/task-baseline'))
OUTPUT = Path(os.environ.get('OUTPUT_DIR', '/app/output'))
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'support' / 'protected_tests.sha256'
WORKER_SOURCE = HERE / 'support' / 'worker.js'
WORKER_USER = os.environ.get('KOA_WORKER_USER', 'checker')
HOST = 'good.example'
LONG = ' ' * 14000
COMMON = {'host': HOST, 'accept': 'text/html'}

# --- private case definitions (never sent to the candidate) -------------------
# Each spec: worker input fields plus the expected observation for this process.
SPECS = {
    'p01': dict(direct=True, proxy=True, headers={'host': f'{HOST}{LONG}'}, mode='request', field='host', expect=HOST, bounded=True),
    'p02': dict(direct=True, proxy=True, headers={'host': HOST, 'x-forwarded-proto': f'https{LONG}'}, mode='request', field='protocol', expect='https', bounded=True),
    'p03': dict(direct=True, proxy=True, headers={'host': HOST, 'x-forwarded-for': f'198.51.100.10{LONG}'}, mode='request', field='ips', expect=['198.51.100.10'], bounded=True),
    'p04': dict(proxy=True, headers={'host': HOST, 'x-forwarded-host': 'proxy.example, other.example'}, mode='request', field='host', expect='proxy.example'),
    'p05': dict(headers=dict(COMMON), mode='redirect', target='javascript:alert(1)', expect_location='javascript:alert(1)', no_clickable=True),
    'p06': dict(headers=dict(COMMON), mode='redirect', target='/safe?x=1&y=2', expect_location='/safe?x=1&y=2', safe_body=True),
    'p07': dict(headers={**COMMON, 'referrer': 'https://outside.example/path'}, mode='back', expect_location='/fallback'),
    'p08': dict(headers={**COMMON, 'referrer': 'http://good.example/path'}, mode='back', expect_location='http://good.example/path'),
    'p09': dict(headers={**COMMON, 'referrer': '//outside.example/path'}, mode='back', expect_confined=True),
    'p10': dict(headers={'host': 'user@spoof.example@good.example'}, mode='request', field='hostname', expect_one_of=[HOST, '']),
    'p11': dict(headers={'host': 'good.example:8080'}, mode='request', field='hostname', expect=HOST),
    'p12': dict(headers={'host': '[::1]:8080'}, mode='request', field='hostname', expect='[::1]'),
    'h01': dict(direct=True, proxy=True, headers={'host': HOST, 'x-forwarded-host': f'proxy.example{LONG}'}, mode='request', field='host', expect='proxy.example', bounded=True),
    'h02': dict(proxy=True, headers={'host': HOST, 'x-forwarded-proto': 'https, http'}, mode='request', field='protocol', expect='https'),
    'h03': dict(proxy=True, headers={'host': HOST, 'x-forwarded-for': '198.51.100.10, 198.51.100.11'}, mode='request', field='ips', expect=['198.51.100.10', '198.51.100.11']),
    'h04': dict(headers=dict(COMMON), mode='redirect', target='JaVaScRiPt:alert(2)', expect_location='JaVaScRiPt:alert(2)', no_clickable=True),
    'h05': dict(headers=dict(COMMON), mode='redirect', target='https://safe.example/a?x=1&y=2', expect_location='https://safe.example/a?x=1&y=2', safe_body=True),
    'h06': dict(headers={**COMMON, 'referrer': '/local/path'}, mode='back', expect_location='/local/path'),
    'h07': dict(headers={**COMMON, 'referrer': '///outside.example/path'}, mode='back', expect_confined=True),
    'h08': dict(headers={**COMMON, 'referrer': 'https://good.example/path'}, mode='back', expect_location='/fallback'),
    'h09': dict(proxy=True, headers={'host': HOST, 'x-forwarded-host': 'user@spoof.example@good.example'}, mode='request', field='hostname', expect_one_of=[HOST, '']),
    'h10': dict(headers={'host': HOST, 'referrer': 'http://GOOD.EXAMPLE:80/path'}, mode='back', expect_equivalent='http://good.example/path'),
    'h11': dict(headers={'host': 'evil.example@good.example', 'referrer': 'http://evil.example/path'}, mode='back', expect_location='/fallback'),
    'h12': dict(headers={'host': HOST}, mode='back', expect_location='/fallback'),
    'h13': dict(direct=True, http_version=2, headers={'host': 'fallback.example', ':authority': 'h2.example:8443'}, mode='request', field='hostname', expect='h2.example'),
    'h14': dict(headers={'host': HOST, 'accept': 'text/plain'}, mode='redirect', target='javascript:alert(1)', expect_location='javascript:alert(1)', plain=True),
    'h15': dict(proxy=True, headers={'host': HOST, 'x-forwarded-proto': 'https', 'referrer': 'http://good.example/path'}, mode='back', expect_location='/fallback'),
    'h16': dict(headers={'host': HOST, 'referrer': 'http://good.example:8080/path'}, mode='back', expect_location='/fallback'),
    'h17': dict(headers=dict(COMMON), mode='redirect', target='data:text/html,unsafe', expect_location='data:text/html,unsafe', no_clickable=True),
    'h18': dict(headers={'host': 'user@'}, mode='request', field='hostname', expect=''),
    'h19': dict(proxy=True, headers={'host': HOST, 'x-forwarded-proto': 'https', 'referrer': 'https://good.example/path'}, mode='back', expect_location='https://good.example/path'),
}
CASES = [f'p{i:02d}' for i in range(1, 13)] + [f'h{i:02d}' for i in range(1, 20)]
assert sorted(SPECS) == sorted(CASES)
FALLBACK = '/fallback'


# --- isolated candidate execution ----------------------------------------------
def _quiesce_worker():
    subprocess.run(['pkill', '-KILL', '-u', WORKER_USER], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _worker_env(home: Path) -> dict:
    # Allowlist only. Nothing from the verifier environment (API keys, judge or
    # verifier configuration, PYTHONPATH, TESTS_DIR, ...) reaches candidate code.
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home),
            'LANG': 'C.UTF-8', 'NODE_ENV': 'test'}


@pytest.fixture(scope='session')
def worktree():
    """Disposable, worker-owned copy of the submission; trusted files stay root-only."""
    _quiesce_worker()
    root = Path(tempfile.mkdtemp(prefix='koa-verifier-'))
    root.chmod(0o711)  # traverse only; the worker cannot list sibling verifier state
    source = root / 'source'
    shutil.copytree(TESTBED, source, symlinks=True,
                    ignore=shutil.ignore_patterns('reproducer', '.git'))
    home = root / 'home'
    home.mkdir()
    worker = root / 'worker.js'
    shutil.copyfile(WORKER_SOURCE, worker)
    worker.chmod(0o444)
    subprocess.run(['chown', '-R', f'{WORKER_USER}:{WORKER_USER}', str(source), str(home)], check=True)
    try:
        yield {'root': root, 'source': source, 'home': home, 'worker': worker}
    finally:
        _quiesce_worker()
        shutil.rmtree(root, ignore_errors=True)


def _as_worker(argv: list[str], tree: dict, *, input_text: str | None = None, timeout: float):
    child = subprocess.Popen(
        ['/usr/sbin/runuser', '-u', WORKER_USER, '--', *argv],
        cwd=tree['source'], env=_worker_env(tree['home']), text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = child.communicate(input=input_text, timeout=timeout)
        return child.returncode, out, err
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.communicate()
        raise
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _quiesce_worker()


def _observe(case: str, tree: dict) -> dict:
    spec = SPECS[case]
    payload = {
        'root': str(tree['source']), 'transport': 'direct' if spec.get('direct') else 'http',
        'mode': spec['mode'], 'proxy': bool(spec.get('proxy')), 'headers': spec['headers'],
        'field': spec.get('field'), 'target': spec.get('target'),
        'alt': FALLBACK if spec['mode'] in ('back', 'redirect') else None,
        'httpVersionMajor': spec.get('http_version', 1),
    }
    deadline = 5 if spec.get('bounded') else 8
    try:
        code, out, err = _as_worker(['node', str(tree['worker'])], tree,
                                    input_text=json.dumps(payload), timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f'{case} exceeded isolated process deadline') from exc
    assert code == 0, f'{case}: worker exited {code}: {(err or out)[-600:]}'
    data = json.loads(out)
    assert 'error' not in data, f"{case}: candidate raised: {data['error']}"
    return data['observed']


# --- trusted assertions -----------------------------------------------------------
def _confined_local(location) -> bool:
    """The fallback, or a same-origin path that cannot leave the host."""
    if location == FALLBACK:
        return True
    if not isinstance(location, str) or not location.startswith('/') or location.startswith('//'):
        return False
    if '\\' in location or re.search(r'[\x00-\x20\x7f]', location):
        return False
    resolved = urlsplit(urljoin(f'http://{HOST}/', location))
    return resolved.scheme == 'http' and resolved.netloc == HOST


def _decode_html(value: str) -> str:
    for entity, char in (('&quot;', '"'), ('&#39;', "'"), ('&lt;', '<'), ('&gt;', '>'), ('&amp;', '&')):
        value = value.replace(entity, char)
    return value


def _check(case: str, o: dict) -> None:
    spec = SPECS[case]
    if spec['mode'] == 'request':
        actual = o.get('value')
        if 'expect_one_of' in spec:
            assert actual in spec['expect_one_of'], f'{case}: unexpected metadata value {actual!r}'
        else:
            assert actual == spec['expect'], f'{case}: unexpected metadata value {actual!r}'
        if spec.get('bounded'):
            elapsed = o.get('elapsed')
            assert isinstance(elapsed, (int, float)) and elapsed <= 250, f'{case}: unbounded metadata parsing: {elapsed!r}ms'
        return
    status, location, body = o.get('status'), o.get('location'), o.get('body') or ''
    assert status == 302, f'{case}: status {status!r}'
    if 'expect_location' in spec:
        assert location == spec['expect_location'], f'{case}: unexpected location {location!r}'
    if spec.get('expect_confined'):
        assert _confined_local(location), f'{case}: location {location!r} is not the fallback or a same-origin path'
    if 'expect_equivalent' in spec:
        got, want = urlsplit(location or ''), urlsplit(spec['expect_equivalent'])
        assert got.scheme and got.netloc, f'{case}: invalid absolute redirect location {location!r}'
        norm = lambda u: (u.scheme.lower(), (u.hostname or '').lower(), u.port or {'http': 80, 'https': 443}.get(u.scheme.lower()), u.path or '/', u.query)
        assert norm(got) == norm(want), f'{case}: unexpected location {location!r}'
    if spec.get('no_clickable'):
        assert not re.search(r'<a\s', body, re.I), f'{case}: unsafe clickable response'
    if spec.get('safe_body'):
        anchors = re.findall(r'<a\s[^>]*href\s*=\s*"([^"]*)"[^>]*>', body, re.I)
        assert len(anchors) == 1, f'{case}: safe destination lost its single clickable response'
        base = f'http://{HOST}/'
        assert urljoin(base, _decode_html(anchors[0])) == urljoin(base, location or ''), \
            f'{case}: clickable response points away from redirect destination'
    if spec.get('plain'):
        assert re.search(r'text/plain', o.get('contentType') or '', re.I) and not re.search(r'<a\s', body, re.I), \
            f'{case}: plain response changed'


def _make(case: str):
    def test(worktree):
        _check(case, _observe(case, worktree))
    test.__name__ = f'test_{case}'
    return test


for _case in CASES:
    globals()[f'test_{_case}'] = _make(_case)


def test_build(worktree):
    try:
        code, out, err = _as_worker(['npm', 'run', 'build'], worktree, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError('build exceeded deadline') from exc
    assert code == 0, out[-1000:] + err[-1000:]


# --- trusted, non-executing checks (read candidate files as data only) ---------
def test_protected_tests():
    entries = {}
    for line in MANIFEST.read_text(encoding='utf-8').splitlines():
        digest, rel = line.split('  ', 1)
        entries[rel] = digest
    problems = []
    for rel, digest in entries.items():
        target = TESTBED / rel
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            problems.append(rel)
    for directory in ('__tests__', 'test-helpers'):
        for target in (TESTBED / directory).rglob('*'):
            if target.is_file() and target.relative_to(TESTBED).as_posix() not in entries:
                problems.append(target.relative_to(TESTBED).as_posix())
    assert not problems, f'project test files altered: {problems[:8]}'


def test_scope():
    if not BASELINE.is_dir():
        raise AssertionError('baseline snapshot unavailable')
    excluded = {'node_modules', '.git', 'testdata', 'dist'}
    # Any production JavaScript under lib is a valid place for an alternative fix.
    def allowed(rel):
        return rel.startswith('lib/') and rel.endswith('.js')
    def snapshot(root):
        result = {}
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in excluded]
            for name in files:
                file = Path(dirpath) / name
                rel = file.relative_to(root).as_posix()
                if rel.startswith('reproducer/'):
                    continue
                result[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
        return result
    before, after = snapshot(BASELINE), snapshot(TESTBED)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    violations = sorted(p for p in changed if not allowed(p))
    assert not violations, f'changes outside source scope: {violations[:8]}'


def test_artifacts():
    plan, findings = OUTPUT / 'plan.md', OUTPUT / 'findings.json'
    assert plan.is_file() and plan.read_text(encoding='utf-8').strip(), 'plan.md missing or empty'
    plan_text = plan.read_text(encoding='utf-8')
    for concept in (r'root\s+causes?', r'production\s+changes?',
                    r'preserv\w*\s+(?:legitimate\s+)?behavio\w*', r'validat\w*'):
        assert re.search(concept, plan_text, re.I), f'plan.md missing required section concept: {concept}'
    assert findings.is_file(), 'findings.json missing'
    data = json.loads(findings.read_text(encoding='utf-8'))
    assert isinstance(data, dict) and set(data) == {
        'summary', 'findings', 'preserved_legitimate_behaviors', 'verification_runs'
    }, 'findings.json top-level schema mismatch'
    def text(value):
        return isinstance(value, str) and bool(value.strip())
    assert text(data['summary']), 'summary must be nonempty text'
    assert isinstance(data['findings'], list) and data['findings'], 'findings must be a nonempty array'
    assert isinstance(data['preserved_legitimate_behaviors'], list) and data['preserved_legitimate_behaviors']
    assert all(text(v) for v in data['preserved_legitimate_behaviors'])
    required = {'area', 'trust_boundary', 'root_cause', 'security_impact', 'severity',
                'affected_code', 'behavior_before_fix', 'remediation', 'behavior_after_fix',
                'verification'}
    for i, finding in enumerate(data['findings']):
        assert isinstance(finding, dict) and set(finding) == required, f'finding {i} schema mismatch'
        assert finding['severity'] in {'informational', 'low', 'moderate', 'high', 'critical'}
        assert all(text(finding[k]) for k in required - {'severity', 'affected_code'}), f'finding {i} has empty field'
        paths = finding['affected_code']
        assert isinstance(paths, list) and paths and all(text(p) for p in paths), f'finding {i} affected_code invalid'
        for rel in paths:
            path = Path(rel)
            assert not path.is_absolute() and '..' not in path.parts, f'finding {i} affected_code outside /testbed'
            resolved = (TESTBED / path).resolve()
            assert resolved.is_relative_to(TESTBED.resolve()) and resolved.is_file(), f'finding {i} affected_code missing'
            assert resolved.relative_to(TESTBED.resolve()).as_posix().startswith('lib/') and resolved.suffix == '.js', f'finding {i} affected_code out of production scope'
    runs = data['verification_runs']
    assert isinstance(runs, list) and runs, 'verification_runs must be a nonempty array'
    assert all(isinstance(r, dict) and set(r) == {'command', 'result'} and text(r['command']) and text(r['result']) for r in runs), 'verification_runs entries invalid'
    assert '/testbed/reproducer/run-all.sh' in runs[-1]['command'], 'final verification run must record public reproducer'
