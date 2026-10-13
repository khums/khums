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
    env = {
        "PATH": "/opt/task-venv/bin:/opt/rewardkit-venv/bin:/opt/java/openjdk/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/agent",
        "CI": "1",
        "NO_COLOR": "1",
        "JAVA_HOME": os.environ.get("JAVA_HOME", "/opt/java/openjdk"),
        "MAVEN_HOME": os.environ.get("MAVEN_HOME", "/usr/share/maven"),
        "MAVEN_OPTS": os.environ.get("MAVEN_OPTS", "-Dmaven.repo.local=/opt/maven-cache"),
    }
    for name in ("GRADLE_USER_HOME", "JAVA_TOOL_OPTIONS"):
        if name in os.environ:
            env[name] = os.environ[name]
    child = subprocess.Popen(
            ["/usr/sbin/runuser", "-u", "agent", "--", "/bin/bash", "-c", command],
        cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return child.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return None

def main():
    if len(sys.argv) != 2 or sys.argv[1] != "public" or os.geteuid() != 0:
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
            # Overlay copies must not carry restrictive source modes into the worktree.
            subprocess.run(["chmod", "-R", "u+rwX", str(work)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            prep = cfg.get("prepare")
            if prep and not run(prep, work, cfg.get("prepare_timeout", 300)):
                print("SUMMARY: ERROR")
                return 2
            totals, errored = [], False
            for group in cfg["groups"]:
                results = [run(case, work, group.get("timeout", 180)) for case in group["commands"]]
                passed = sum(bool(r) for r in results)
                possible = len(results)
                if any(r is None for r in results):
                    status, errored = "ERROR", True
                else:
                    status = "PASS" if passed == possible else "FAIL"
                print(f"GROUP {group['id']}: {status} score={passed}/{possible}", flush=True)
                totals.append((passed, possible))
            passed = sum(x for x, _ in totals)
            possible = sum(x for _, x in totals)
            status = "ERROR" if errored else "PASS" if passed == possible else "FAIL"
            print(f"SUMMARY: {status} score={passed}/{possible}", flush=True)
            return 2 if errored else 0 if passed == possible else 1
    except Exception:
        print("SUMMARY: ERROR", flush=True)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
