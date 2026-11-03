"""Preserve pinned upstream sources and licenses without nested Git repositories."""

import io
import json
from pathlib import Path
import subprocess
import tarfile


def main():
    root = Path(__file__).resolve().parents[1]
    lock = {}
    for name in ("ub-mode", "crapper", "dryer", "mutator"):
        source = root / ".upstream" / name
        if not source.exists():
            raise SystemExit(f"Clone unclebob/{name} into {source} first.")
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        archive = subprocess.check_output(["git", "-C", str(source), "archive", revision])
        destination = root / "vendor" / name
        if destination.exists():
            raise SystemExit(f"The vendor directory already exists: {destination}")
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(destination, filter="data")
        lock[name] = {"repository": f"https://github.com/unclebob/{name}", "commit": revision}
    (root / "upstream.lock.json").write_text(json.dumps(lock, indent=2) + "\n")


if __name__ == "__main__":
    main()
