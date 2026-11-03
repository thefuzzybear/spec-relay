"""Identify the runtime and analyzer source used by a check."""

import hashlib
import importlib.metadata
from pathlib import Path
import sys


def runner_fingerprint():
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256(sys.version.encode())
    digest.update(sys.executable.encode())
    files = list((root / "src").rglob("*.py"))
    files.extend((root / "vendor").glob("*/src/**/*.py"))
    files.extend([root / "upstream.lock.json", root / "pyproject.toml"])
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes())
    for name in ("tree-sitter", "tree-sitter-language-pack", "coverage"):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = "absent"
        digest.update((name + ":" + version).encode())
    return digest.hexdigest()
