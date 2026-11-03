"""Git workspaces and content fingerprints."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess

from .store import WorkflowError


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise WorkflowError(result.stderr.strip() or "Git failed.")
    return result.stdout.strip()


def relative_path(value):
    if not isinstance(value, str) or not value:
        raise WorkflowError("Provide a repository-relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in value or path == PurePosixPath("."):
        raise WorkflowError(f"Unsafe repository path: {value}")
    return value


def inside(root, value):
    relative_path(value)
    path = Path(root) / value
    if not path.resolve().is_relative_to(Path(root).resolve()):
        raise WorkflowError(f"The path escapes its worktree: {value}")
    return path


def selected(path, prefixes):
    return any(path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)


def file_names(root):
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-c", "-z"])
    other = subprocess.check_output(["git", "-C", str(root), "ls-files", "-o", "--exclude-standard", "-z"])
    paths = {os.fsdecode(item) for item in tracked.split(b"\0") if item}
    artifacts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "node_modules", ".metrics", "coverage", "target"}
    for item in other.split(b"\0"):
        if item:
            name = os.fsdecode(item)
            if not (set(Path(name).parts) & artifacts) and Path(name).name != ".coverage":
                paths.add(name)
    return sorted(paths)


def fingerprint(root, prefixes=None):
    digest = hashlib.sha256()
    for name in file_names(root):
        if prefixes is not None and not selected(name, prefixes):
            continue
        path = inside(root, name)
        digest.update(name.encode() + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"exec:" if os.access(path, os.X_OK) else b"file:")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        elif path.is_dir():
            raise WorkflowError(f"Submodules require a repository adapter: {name}")
        else:
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def require_clean(root):
    tracked = set(git(root, "ls-files").splitlines())
    untracked_source = set(file_names(root)) - tracked
    if git(root, "status", "--porcelain", "--untracked-files=no") or untracked_source:
        raise WorkflowError("Commit the candidate before submission. The worktree contains changes.")


def changed_paths(root, base):
    output = subprocess.check_output(["git", "-C", str(root), "diff", "--name-only", "--no-renames", "-z", base])
    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def add_worktree(repo, path, branch, base):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return str(path)
