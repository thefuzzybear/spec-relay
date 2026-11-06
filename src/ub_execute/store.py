"""Transactional state and an ordered action record."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class WorkflowError(ValueError):
    """An operation does not satisfy the execution contract."""


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite3"
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(kind, id));
                CREATE INDEX IF NOT EXISTS objects_run ON objects(run_id, kind);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    time REAL NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                    data TEXT NOT NULL);
            """)

    def transaction(self):
        return sqlite_transaction(self.path, wal=True)

    def put(self, db, kind, value):
        db.execute("INSERT INTO objects VALUES (?, ?, ?, ?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                   (kind, value["id"], value.get("run_id", value["id"]), encode(value)))

    def get(self, db, kind, key):
        row = db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise WorkflowError(f"Unknown {kind}: {key}")
        return json.loads(row[0])

    def list(self, db, kind, run_id):
        return [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM objects WHERE kind=? AND run_id=? ORDER BY rowid", (kind, run_id))]

    def event(self, db, run_id, action, reason, data):
        if not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("Provide a reason for the action.")
        db.execute("INSERT INTO events(run_id,time,action,reason,data) VALUES(?,?,?,?,?)",
                   (run_id, time.time(), action, reason, encode(data)))


@contextmanager
def sqlite_transaction(path, wal=False):
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        if wal:
            db.execute('PRAGMA busy_timeout=30000')
            db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
