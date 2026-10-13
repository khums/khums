from pathlib import Path
import os
import shutil
import subprocess
import sys


def freeze(testbed: Path, output: Path, verifier: Path, trajectory: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError("verification requires root")
    # RewardKit imports helpers during host-side discovery. The digest module
    # exists only in the Docker checker; require it when freezing, not importing.
    sys.path.insert(0, "/opt/task-checker")
    from source_digest import source_digest

    stopped = subprocess.run(["pkill", "-KILL", "-u", "agent"], check=False)
    if stopped.returncode not in (0, 1):
        raise RuntimeError("could not terminate solver processes")
    verifier.chmod(0o700)
    internal = verifier / "internal"
    internal.mkdir(exist_ok=True)
    internal.chmod(0o700)
    snapshot = Path("/opt/task-policy/submission")
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir(mode=0o700)
    submitted = testbed / "packages/seroval/src"
    digest = source_digest(submitted)
    destination = snapshot / "packages/seroval/src"
    destination.parent.mkdir(parents=True)
    shutil.copytree(submitted, destination, symlinks=False)
    assert source_digest(destination) == digest
    if output.is_symlink() or any(path.is_symlink() for path in output.rglob("*")):
        raise ValueError("output artifacts must not contain symlinks")
    shutil.copytree(output, snapshot / "output", symlinks=False)
    subprocess.run(["chown", "-R", "root:root", str(snapshot)], check=True)
    subprocess.run(["chmod", "-R", "a-w", str(snapshot)], check=True)
    (internal / "source-sha256.txt").write_text(digest + "\n")
    evidence = Path("/opt/task-policy/public-runs.jsonl")
    (internal / "validation-evidence.jsonl").write_bytes(evidence.read_bytes() if evidence.exists() else b"")
    if trajectory:
        shutil.copyfile(trajectory, internal / "trajectory.json")


if __name__ == "__main__":
    freeze(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
