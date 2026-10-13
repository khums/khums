#!/usr/bin/env python3
"""Run public behavioral checks without exposing case-level fixtures or logs."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from source_scope import SourceScopeError, overlay_submitted_source, prepare_trusted_pytest_config

HERE = Path(__file__).resolve().parent
BASELINE = Path('/opt/checker-baseline')
SUBMITTED_SOURCE = Path('/testbed')

def copy_submitted_source(work):
    return overlay_submitted_source(SUBMITTED_SOURCE, work)

def run(command, cwd, timeout):
    env = {"PATH": "/opt/task-venv/bin:/opt/rewardkit-venv/bin:/usr/local/bin:/usr/bin:/bin",
           "PYTHONPATH": f"{cwd / 'shared/secrets_masker/src'}:{cwd / 'devel-common/src'}",
           "PYTHONPYCACHEPREFIX": f"/tmp/{cwd.parent.name}-pycache",
           "HOME": "/home/agent", "CI": "1", "NO_COLOR": "1",
           "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1"}
    for name in ("MAVEN_OPTS", "GRADLE_USER_HOME", "JAVA_TOOL_OPTIONS"):
        if name in os.environ:
            env[name] = os.environ[name]
    child = subprocess.Popen(
            ["/usr/sbin/runuser", "-u", "agent", "--", "/bin/bash", "-c", command],
        cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = child.wait(timeout=timeout)
        return 'PASS' if code == 0 else 'FAIL' if code == 1 else 'ERROR'
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return 'ERROR'

def main():
    if len(sys.argv) != 2 or sys.argv[1] != "public" or os.geteuid() != 0:
        print("SUMMARY: ERROR")
        return 2
    try:
        cfg = json.loads((HERE / "groups.json").read_text())
        if cfg.get('context'):
            print('CONTEXT: ' + cfg['context'], flush=True)
        with tempfile.TemporaryDirectory(prefix="public-probe-") as tmp:
            os.chmod(tmp, 0o755)
            work = Path(tmp) / "source"
            subprocess.run(["cp", "-a", "--reflink=auto", f"{BASELINE}/.", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            copy_submitted_source(work)
            prepare_trusted_pytest_config(work, BASELINE)
            patch = HERE / "test.patch"
            if patch.exists():
                cmd = ["git", "-C", str(work), "apply", "--whitespace=nowarn"]
                for pattern in cfg.get("test_includes", []):
                    cmd.append(f"--include={pattern}")
                cmd.append(str(patch))
                # Source is overlaid separately; only trusted test hunks apply.
                # A patch failure is infrastructure failure, never a passing probe.
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            overlay = HERE / "overlay"
            if overlay.exists():
                subprocess.run(["cp", "-a", f"{overlay}/.", str(work)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["chmod", "-R", "a-w,go+rX", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            prep = cfg.get("prepare")
            if prep and run(prep, work, cfg.get("prepare_timeout", 300)) != "PASS":
                print("SUMMARY: ERROR")
                return 2
            totals = []
            errors = False
            for group in cfg["groups"]:
                results = [run(case, work, group.get("timeout", 180)) for case in group["commands"]]
                passed = results.count("PASS")
                possible = len(results)
                status = "ERROR" if "ERROR" in results else "PASS" if passed == possible else "FAIL"
                errors = errors or status == "ERROR"
                print(f"GROUP {group['id']}: {status} score={passed}/{possible}", flush=True)
                totals.append((passed, possible))
            passed = sum(x for x, _ in totals)
            possible = sum(x for _, x in totals)
            status = "ERROR" if errors else "PASS" if passed == possible else "FAIL"
            print(f"SUMMARY: {status} score={passed}/{possible}", flush=True)
            if status == 'FAIL' and cfg.get('failure_context'):
                print('INVESTIGATION: ' + cfg['failure_context'], flush=True)
            if errors:
                print('VALIDATION ERROR: a command did not complete normally; check import/build failures or timeouts before attributing a masking defect.', flush=True)
            return 2 if errors else 0 if passed == possible else 1
    except SourceScopeError as exc:
        print('SUBMISSION ERROR: ' + str(exc), flush=True)
        print('SUMMARY: ERROR', flush=True)
        return 2
    except Exception:
        print('VALIDATION ERROR: the trusted validation workspace could not be prepared or executed.', flush=True)
        print("SUMMARY: ERROR", flush=True)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
