"""Serialize access to shared parent resources across runtime stores."""

import json
import os
from pathlib import Path
import tempfile
import uuid

from .store import WorkflowError, sqlite_transaction


class ParentLeases:
    def __init__(self):
        self.path = Path(tempfile.gettempdir()) / f'ub-execute-parent-leases-{os.getuid()}.sqlite3'
        if self.path.is_symlink():
            raise WorkflowError('The parent lease database must not be a symlink.')
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, resource TEXT, mode TEXT, owner TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS generations (resource TEXT PRIMARY KEY, value INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS completed_writers (lease_id TEXT PRIMARY KEY, resource TEXT NOT NULL, generation INTEGER NOT NULL)')

    def transaction(self):
        return sqlite_transaction(self.path)

    def acquire(self, resources, owner):
        lease_id = 'lease-' + uuid.uuid4().hex
        with self.transaction() as db:
            for resource, mode in resources:
                rows = db.execute('SELECT mode FROM leases WHERE resource=?', (resource,)).fetchall()
                if rows and (mode == 'exclusive' or any(row[0] == 'exclusive' for row in rows)):
                    raise WorkflowError('An active resource lease blocks this operation.')
            for resource, mode in resources:
                db.execute('INSERT INTO leases VALUES(?,?,?,?)', (lease_id + ':' + resource, resource, mode, json.dumps(owner)))
        return lease_id

    def owns(self, lease_id, resources, owner):
        if not lease_id:
            return False
        with self.transaction() as db:
            rows = db.execute('SELECT resource,mode,owner FROM leases WHERE id LIKE ?', (lease_id + ':%',)).fetchall()
        try:
            actual = {resource:(mode,json.loads(actor)) for resource,mode,actor in rows}
        except (ValueError,TypeError):
            return False
        expected = {resource:(mode,owner) for resource,mode in resources}
        return actual == expected

    def invalidate_data(self, resource):
        """Advance the data epoch when a writer loses its ownership proof."""
        with self.transaction() as db:
            return self._advance_data(db, resource)

    @staticmethod
    def _advance_data(db, resource):
        db.execute('INSERT INTO generations VALUES (?,1) ON CONFLICT(resource) DO UPDATE SET value=value+1',(resource,))
        return db.execute('SELECT value FROM generations WHERE resource=?',(resource,)).fetchone()[0]

    def release(self, lease_id):
        if lease_id:
            with self.transaction() as db:
                db.execute('DELETE FROM leases WHERE id LIKE ?', (lease_id + ':%',))

    def active(self, resources):
        with self.transaction() as db:
            return [{'resource':r, 'mode':m, 'owner':json.loads(o)} for r,m,o in db.execute('SELECT resource,mode,owner FROM leases') if r in resources]

    def data_generation(self, resource):
        with self.transaction() as db:
            row = db.execute('SELECT value FROM generations WHERE resource=?',(resource,)).fetchone()
            return row[0] if row else 0

    def finish_data_writer(self, lease_id, resource):
        """Advance the shared baseline once, before releasing an exclusive writer."""
        with self.transaction() as db:
            completed = db.execute('SELECT resource,generation FROM completed_writers WHERE lease_id=?',(lease_id,)).fetchone()
            if completed:
                if completed[0] != resource:
                    raise WorkflowError('The completed data lease belongs to another resource.')
                return completed[1]
            lease = db.execute('SELECT mode FROM leases WHERE id=? AND resource=?',(lease_id + ':' + resource,resource)).fetchone()
            if not lease or lease[0] != 'exclusive':
                raise WorkflowError('An exclusive database lease is required to finish a data writer.')
            generation = self._advance_data(db,resource)
            db.execute('INSERT INTO completed_writers VALUES (?,?,?)',(lease_id,resource,generation))
            db.execute('DELETE FROM leases WHERE id LIKE ?',(lease_id + ':%',))
            return generation
