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
from source_digest import source_digest

HERE = Path(__file__).resolve().parent
BASELINE = Path('/opt/checker-baseline')
SUBMITTED_SOURCE = Path(os.environ.get('TASK_SUBMITTED_SOURCE', '/testbed/packages/seroval/src'))

def record_result(work, status, passed, total):
    record = {"command": "/testbed/reproducer/run-all.sh", "status": status,
              "passed": passed, "total": total,
              "source_sha256": source_digest(work / "packages/seroval/src")}
    with Path('/opt/task-policy/public-runs.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')

class SubmissionError(RuntimeError):
    """The submitted tree cannot be replayed as permitted production source."""

def copy_submitted_source(work):
    if SUBMITTED_SOURCE.is_symlink() or not SUBMITTED_SOURCE.is_dir():
        raise SubmissionError('submitted source is missing')
    if any(path.is_symlink() for path in SUBMITTED_SOURCE.rglob('*')):
        raise SubmissionError('submitted source contains a symlink')
    destination = work / 'packages/seroval/src'
    shutil.rmtree(destination)
    shutil.copytree(SUBMITTED_SOURCE, destination, symlinks=False)

def run(command, cwd, timeout):
    env = {"PATH": "/opt/task-venv/bin:/opt/rewardkit-venv/bin:/usr/local/bin:/usr/bin:/bin",
           "HOME": "/home/agent", "CI": "1", "NO_COLOR": "1"}
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
        # vitest/pnpm exit 1 for failing assertions; anything else is incomplete.
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
            patch = HERE / "test.patch"
            if patch.exists():
                cmd = ["git", "-C", str(work), "apply", "--whitespace=nowarn"]
                for pattern in cfg.get("test_includes", []):
                    cmd.append(f"--include={pattern}")
                cmd.append(str(patch))
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            overlay = HERE / "overlay"
            if overlay.exists():
                subprocess.run(["cp", "-a", f"{overlay}/.", str(work)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["chmod", "-R", "a-w,go+rX", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # The sticky package directory lets the unprivileged build replace
            # its generated output but not root-owned dependencies or runners.
            package_dir = work / 'packages/seroval'
            package_dir.chmod(0o1777)
            writable = [package_dir / 'dist', package_dir / '.cache']
            for path in writable:
                if path.exists():
                    shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)
                subprocess.run(["chown", "-R", "agent:agent", str(path)], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            prep = cfg.get("prepare")
            if prep and run(prep, work, cfg.get("prepare_timeout", 300)) != "PASS":
                record_result(work, "ERROR", 0, 0)
                print('VALIDATION ERROR: the package build did not complete; check TypeScript/build errors before attributing a behavioral defect.', flush=True)
                print("SUMMARY: ERROR", flush=True)
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
            record_result(work, status, passed, possible)
            print(f"SUMMARY: {status} score={passed}/{possible}", flush=True)
            if status == "FAIL" and cfg.get("failure_context"):
                print("INVESTIGATION: " + cfg["failure_context"], flush=True)
            if errors:
                print('VALIDATION ERROR: a command did not complete normally (crash or timeout); this is not a demonstrated behavioral result.', flush=True)
            return 2 if errors else 0 if passed == possible else 1
    except SubmissionError as exc:
        print('SUBMISSION ERROR: ' + str(exc), flush=True)
        print("SUMMARY: ERROR", flush=True)
        return 2
    except Exception:
        print('VALIDATION ERROR: the trusted validation workspace could not be prepared or executed.', flush=True)
        print("SUMMARY: ERROR", flush=True)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
