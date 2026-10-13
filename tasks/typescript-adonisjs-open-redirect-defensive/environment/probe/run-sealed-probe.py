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

HERE = Path(__file__).resolve().parent

def run(command, cwd, timeout):
    env = {"PATH": "/opt/task-venv/bin:/opt/rewardkit-venv/bin:/usr/local/bin:/usr/bin:/bin",
           "HOME": "/home/agent", "CI": "1", "NO_COLOR": "1",
           "NODE_OPTIONS": "--dns-result-order=ipv4first"}
    for name in ("MAVEN_OPTS", "GRADLE_USER_HOME", "JAVA_TOOL_OPTIONS"):
        if name in os.environ:
            env[name] = os.environ[name]
    child = subprocess.Popen(
        ["/usr/sbin/runuser", "-u", "agent", "--", "/bin/bash", "-lc", command],
        cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = child.wait(timeout=timeout)
        # The project runner exits 1 for failing assertions; anything else is incomplete.
        return "PASS" if code == 0 else "FAIL" if code == 1 else "ERROR"
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return "ERROR"
    finally:
        # A successful public command may still have spawned background helpers.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

def run_project_tests(arguments):
    """Run only the supplied project suite as agent in a disposable writable copy.

    Unlike the grouped public probe, this path adds no patches or overlay tests
    and streams normal project-test output. Never execute an agent command as root.
    The original test tree remains root-owned and read-only to the agent.
    """
    with tempfile.TemporaryDirectory(prefix="project-tests-") as tmp:
        os.chmod(tmp, 0o755)
        work = Path(tmp) / "source"
        subprocess.run(["cp", "-a", "--reflink=auto", "/testbed/.", str(work)], check=True)
        shutil.rmtree(work / "reproducer", ignore_errors=True)
        subprocess.run(["chown", "-R", "agent:agent", str(work)], check=True)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/agent", "CI": "1",
               "NO_COLOR": "1", "NODE_OPTIONS": "--dns-result-order=ipv4first"}
        child = subprocess.Popen(
            ["/usr/sbin/runuser", "-u", "agent", "--", "npm", "run", "quick:test", "--", *arguments],
            cwd=work, env=env, start_new_session=True,
        )
        try:
            return child.wait(timeout=1200)
        except subprocess.TimeoutExpired:
            print("PROJECT TESTS: ERROR (timeout)", flush=True)
            return 124
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def main():
    if len(sys.argv) < 2 or os.geteuid() != 0:
        print("SUMMARY: ERROR")
        return 2
    if sys.argv[1] == "project-tests":
        try:
            return run_project_tests(sys.argv[2:])
        except Exception as exc:
            print(f"PROJECT TESTS: ERROR ({type(exc).__name__})", flush=True)
            return 2
    if sys.argv[1:] != ["public"]:
        print("SUMMARY: ERROR")
        return 2
    try:
        cfg = json.loads((HERE / "groups.json").read_text())
        with tempfile.TemporaryDirectory(prefix="public-probe-") as tmp:
            os.chmod(tmp, 0o755)
            work = Path(tmp) / "source"
            subprocess.run(["cp", "-a", "--reflink=auto", "/testbed/.", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(work / "reproducer", ignore_errors=True)
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
            subprocess.run(["chown", "-R", "agent:agent", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            prep = cfg.get("prepare")
            if prep and run(prep, work, cfg.get("prepare_timeout", 300)) != "PASS":
                print("VALIDATION ERROR: the project build (lint, type declarations, bundle) did not complete; "
                      "fix build errors before interpreting behavioral results.", flush=True)
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
                print(f"GROUP {group['id']}: {status} score={passed}/{possible} area={group['area']}", flush=True)
                totals.append((passed, possible))
            passed = sum(x for x, _ in totals)
            possible = sum(x for _, x in totals)
            status = "ERROR" if errors else "PASS" if passed == possible else "FAIL"
            print(f"SUMMARY: {status} score={passed}/{possible}", flush=True)
            if errors:
                print("VALIDATION ERROR: a command crashed or timed out; this is not a demonstrated behavioral result.",
                      flush=True)
            return 2 if errors else 0 if passed == possible else 1
    except Exception:
        print("SUMMARY: ERROR", flush=True)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
