"""Process liveness through kernel locks, without process-list permissions."""

from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def held_lock(path):
    with Path(path).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def is_locked(path):
    path = Path(path)
    if not path.exists():
        return False
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False
