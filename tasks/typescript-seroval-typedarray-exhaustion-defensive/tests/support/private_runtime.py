from __future__ import annotations

import os
from pathlib import Path
import pwd
import signal
import subprocess


def run_private(command: str, work: Path, timeout: int, compilation: bool = False):
    checker = pwd.getpwnam("checker")
    jail = work.parent

    def enter_runtime():
        os.chroot(jail)
        os.chdir("/source")
        os.setgroups([])
        os.setgid(checker.pw_gid)
        os.setuid(checker.pw_uid)

    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/checker", "TMPDIR": "/tmp", "CI": "1", "NO_COLOR": "1"}
    invocation = ["/bin/bash", "-c", command]
    setup = enter_runtime
    working_directory = None
    if compilation:
        invocation = ["/usr/sbin/runuser", "-u", "checker", "--", *invocation]
        env["HOME"] = str(jail / "home/checker")
        env["TMPDIR"] = str(jail / "tmp")
        setup = None
        working_directory = work
    process = subprocess.Popen(invocation, env=env, text=True, cwd=working_directory,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               preexec_fn=setup, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    logs = Path(os.environ["VERIFIER_DIR"]) / "internal"
    logs.mkdir(parents=True, exist_ok=True)
    # The replay worktree is shared by every row; keep each command's output.
    with (logs / (work.parent.parent.name + ".log")).open("a", encoding="utf-8") as stream:
        stream.write(f"$ {command}\nexit={process.returncode}\n{stdout}{stderr}\n")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
