"""Shared structural scope policy for the source snapshot, not a subsystem allowlist.

Deterministic checks enforce containment, regular files and explicit exclusions.
Whether a finding is genuinely production-relevant is a semantic judgment, not
an inferred filename extension or a requirement to mention one package.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
import stat

# These are test/harness/history/generated-output locations, not production
# packages. In particular, devel-common supplies the trusted pytest harness.
EXCLUDED_PARTS = frozenset({
    '.git', '.hg', '.svn', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    '__pycache__', '.venv', 'node_modules', 'test', 'tests', '__tests__',
    'fixtures', 'test-fixtures', 'task-tests', 'task_tests', 'solution',
    'solutions', 'verifier', 'scoring', 'reproducer', 'devel-common',
})
EXCLUDED_FILES = frozenset({
    'conftest.py', 'pytest.ini', 'tox.ini', '.coverage', 'coverage.xml',
    'junit.xml', 'reward.json', 'reward.txt', 'deterministic_reward.json',
})


class SourceScopeError(AssertionError):
    """The submitted filesystem cannot safely represent permitted source."""


def is_permitted_path(relative: str | Path) -> bool:
    """Reject only structural exclusions; do not impose a package/extension."""
    path = PurePosixPath(str(relative))
    if path.is_absolute() or '..' in path.parts:
        return False
    parts = tuple(part.lower() for part in path.parts)
    if not parts:
        return True
    if any(part in EXCLUDED_PARTS for part in parts):
        return False
    return not any(
        name in EXCLUDED_FILES or name.startswith('test_')
        or name.endswith(('_test.py', '.pyc', '.pyo', '.class', '.o'))
        or '.test.' in name or '.spec.' in name
        for name in parts
    )


def _relative_target(root: Path, path: Path) -> Path:
    try:
        target = path.resolve(strict=True)
        relative = target.relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceScopeError('source path is missing, cyclic, or outside /testbed') from exc
    if not is_permitted_path(relative):
        raise SourceScopeError('source path resolves to excluded material')
    return relative


def validate_affected_path(testbed: Path, value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise SourceScopeError('affected_code must contain non-empty path strings')
    path = Path(value)
    if path.is_absolute():
        raise SourceScopeError('affected_code must be relative to /testbed')
    normalized = Path(os.path.normpath(value))
    if not is_permitted_path(normalized):
        raise SourceScopeError('affected_code refers to excluded material or escapes /testbed')
    relative = _relative_target(testbed, testbed / normalized)
    resolved = testbed.resolve() / relative
    if not resolved.is_file():
        raise SourceScopeError('affected_code must resolve to an existing regular file')
    return resolved


def _open_beneath(root_fd: int, relative: Path, *, directory: bool = False) -> int:
    """Do not follow mutable ancestor links while a root-owned checker copies."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(relative.parts):
            if part in ('..', '/'):
                raise SourceScopeError('invalid source path')
            is_dir = directory or index < len(relative.parts) - 1
            new_fd = os.open(part, flags | (os.O_DIRECTORY if is_dir else 0), dir_fd=fd)
            os.close(fd)
            fd = new_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _entries(root: Path):
    """Yield entries without ever traversing symlinked directories."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        pending = [Path('.')]
        while pending:
            relative = pending.pop()
            fd = _open_beneath(root_fd, relative, directory=True)
            try:
                with os.scandir(fd) as listing:
                    names = sorted(entry.name for entry in listing)
                for name in names:
                    child = relative / name
                    if not is_permitted_path(child):
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    yield child, info
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(child)
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)


def overlay_submitted_source(testbed: Path, work: Path) -> dict:
    """Mirror all structurally permitted source, preserving trusted exclusions.

    Additions, edits and deletions are replayed. In-tree symlinks are rebased
    into the verifier snapshot; escaping/test-target links and special files
    are rejected. Excluded tests and harness configuration remain baseline-owned.
    No submitted code is imported by this root-owned orchestration process.
    """
    if testbed.is_symlink() or not testbed.is_dir():
        raise SourceScopeError('submitted source root is missing or is a symlink')
    work = work.resolve(strict=True)
    if work == testbed.resolve() or work.is_relative_to(testbed.resolve()):
        raise SourceScopeError('snapshot destination must be separate from submitted source')

    # First remove eligible baseline entries, including deleted source files.
    old_directories = []
    for relative, info in list(_entries(work)):
        target = work / relative
        if stat.S_ISDIR(info.st_mode):
            old_directories.append(target)
        else:
            target.unlink()
    for target in sorted(old_directories, key=lambda p: len(p.parts), reverse=True):
        try:
            target.rmdir()
        except OSError:
            pass  # A protected test/harness child must remain in place.

    source_root = testbed.resolve(strict=True)
    source_fd = os.open(source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    counts = {'files': 0, 'directories': 0, 'symlinks': 0}
    try:
        for relative, info in _entries(source_root):
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(exist_ok=True)
                counts['directories'] += 1
            elif stat.S_ISREG(info.st_mode):
                fd = _open_beneath(source_fd, relative)
                with os.fdopen(fd, 'rb') as src:
                    checked = os.fstat(src.fileno())
                    if not stat.S_ISREG(checked.st_mode):
                        raise SourceScopeError('submitted source contains a special file')
                    with target.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(0o755 if checked.st_mode & 0o111 else 0o644)
                counts['files'] += 1
            elif stat.S_ISLNK(info.st_mode):
                linked = _relative_target(source_root, source_root / relative)
                # Validate every target ancestor with no-follow opens too.
                linked_fd = _open_beneath(source_fd, linked)
                try:
                    mode = os.fstat(linked_fd).st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise SourceScopeError('source link targets a special file')
                finally:
                    os.close(linked_fd)
                target.symlink_to(os.path.relpath(work / linked, target.parent))
                counts['symlinks'] += 1
            else:
                raise SourceScopeError('submitted source contains a special file')
    except (OSError, RuntimeError) as exc:
        raise SourceScopeError('submitted source changed while being snapshotted or is unreadable') from exc
    finally:
        os.close(source_fd)
    return counts


def configure_permissions(root: Path, uid: int, gid: int) -> None:
    """Make permitted source editable while protecting test/reproducer entries.

    Root-owned sticky directories let the agent create source/helper files and
    atomically replace agent-owned source files, but not rename or replace
    root-owned excluded trees. The agent does not need unrestricted sudo.
    """
    if root.is_symlink():
        raise SourceScopeError('source root cannot be a symlink')
    for directory, directories, files in os.walk(root, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(root)
        permitted = is_permitted_path(relative)
        os.chown(current, 0, gid if permitted else 0)
        current.chmod(0o1775 if permitted else 0o555)
        for name in directories + files:
            path = current / name
            if path.is_symlink():
                os.lchown(path, 0, 0)
                continue
            if path.is_dir():
                continue
            mode = path.stat().st_mode
            allowed = is_permitted_path(path.relative_to(root))
            os.chown(path, uid if allowed else 0, gid if allowed else 0)
            path.chmod((0o755 if mode & 0o111 else 0o644) if allowed
                       else (0o555 if mode & 0o111 else 0o444))


def prepare_trusted_pytest_config(work: Path, baseline: Path) -> None:
    """Keep production metadata editable without accepting submitted pytest options."""
    source = baseline / 'pyproject.toml'
    destination = work / '.checker-pytest.toml'
    # Always overwrite after the submission overlay; agents cannot control it.
    if destination.is_symlink():
        destination.unlink()
    if source.is_file():
        shutil.copyfile(source, destination)
    else:
        destination.write_text('[tool.pytest.ini_options]\n', encoding='utf-8')
    destination.chmod(0o444)
