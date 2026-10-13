from pathlib import Path
import re
import shutil
import subprocess
import sys


def copy_libraries(binary: Path, destination: Path) -> None:
    libraries = subprocess.run(["ldd", str(binary)], check=False, capture_output=True, text=True).stdout
    for library in re.findall(r"(/[^\s()]+)", libraries):
        source = Path(library)
        # ldd also echoes the queried path in error lines (e.g. foreign-libc
        # optional bindings); only real shared objects are copied.
        if not source.is_file():
            continue
        target = destination / source.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def build_runtime(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for binary in ("/usr/local/bin/node", "/bin/bash", "/bin/dash", "/usr/bin/env", "/usr/bin/sed", "/usr/bin/dirname", "/usr/bin/uname"):
        target = destination / binary.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, target)
        copy_libraries(Path(binary), destination)
    for native in Path("/testbed/node_modules").rglob("*"):
        if native.is_file() and (native.suffix in {".node", ".so"} or native.stat().st_mode & 0o111):
            with native.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    copy_libraries(native, destination)
    shutil.copy2("/etc/ld.so.cache", destination / "etc-ld.so.cache")
    (destination / "usr/bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2("/usr/bin/ldd", destination / "usr/bin/ldd")
    shutil.copytree("/usr/local/lib/node_modules/pnpm", destination / "usr/local/lib/node_modules/pnpm", symlinks=True)
    (destination / "usr/local/bin/pnpm").symlink_to("/usr/local/lib/node_modules/pnpm/bin/pnpm.cjs")
    (destination / "bin/sh").symlink_to("dash")
    (destination / "dev").mkdir()
    (destination / "etc").mkdir()
    (destination / "etc-ld.so.cache").rename(destination / "etc/ld.so.cache")
    (destination / "etc/passwd").write_text("checker:x:10002:10002::/home/checker:/bin/bash\n")
    (destination / "etc/group").write_text("checker:x:10002:\n")


if __name__ == "__main__":
    build_runtime(Path(sys.argv[1]))
