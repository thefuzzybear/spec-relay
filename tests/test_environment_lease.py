"""Queued and completed checks must retain their actual environment lease."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_execution
from ub_execute.store import WorkflowError
from ub_execute.worker import run_job, execute


class EnvironmentLeaseTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_execution.ExecutionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        workspace = self.runtime.workspace_attach(str(self.fixture.repo),{},'Attach the fixture environment.')
        self.fixture.bundle.update(workspace_mode='attached_parent',workspace_id=workspace['id'])
        self.fixture.bundle['tasks'][0]['tdd_exception'] = 'This fixture checks environment lease evidence.'
        self.proof = self.fixture.root/'command-ran'
        self.fixture.bundle['checks']['test']['argv'] = [sys.executable,'-c',f'from pathlib import Path; Path({str(self.proof)!r}).write_text("executed")']
        self.run = self.fixture.create()
        self.attempt = self.fixture.claim(self.run)
        tree = Path(self.attempt['worktree'])
        (tree/'calc.py').write_text('def add(a,b):\n    return a + b\n')
        test_execution.git(tree,'add','calc.py')
        test_execution.git(tree,'commit','-qm','Prepare the lease fixture candidate')
        with patch.object(self.runtime,'_launch_job',side_effect=lambda job:job):
            self.job = self.runtime.check_start(self.run['id'],'test','green','Queue the owned environment check.',self.attempt['id'])
        self.manager = self.runtime._parent_manager()
        self.addCleanup(self.manager.leases.release,self.job['environment_lease_id'])

    def replace_with_exclusive_owner(self):
        with self.runtime.store.transaction() as db:
            workspace = self.runtime.store.get(db,'parent_workspace',self.run['workspace_id'])
        resource = self.manager._dependency_resource(workspace)
        self.manager.leases.release(self.job['environment_lease_id'])
        exclusive = self.manager.leases.acquire([(resource,'exclusive')],{'operation':'dependency_update'})
        self.addCleanup(self.manager.leases.release,exclusive)
        return resource, exclusive

    def test_missing_queued_lease_prevents_command_dispatch(self):
        resource, exclusive = self.replace_with_exclusive_owner()
        run_job(self.fixture.state,self.job['id'])
        result = self.runtime.job_status(self.job['id'])
        self.assertEqual(result['status'],'invalidated','A check without its recorded lease must not execute.')
        self.assertFalse(self.proof.exists())
        self.assertEqual(len(self.manager.leases.active([resource])),1)

    def test_lease_lost_during_command_invalidates_result_without_releasing_new_owner(self):
        current = {}
        def lose_lease(job,store):
            result = execute(job,store)
            current['resource'], current['lease'] = self.replace_with_exclusive_owner()
            return result
        with patch('ub_execute.worker.execute',side_effect=lose_lease):
            run_job(self.fixture.state,self.job['id'])
        result = self.runtime.job_status(self.job['id'])
        self.assertEqual(result['status'],'invalidated','A result cannot survive loss of its environment lease.')
        self.assertTrue(self.proof.exists())
        self.assertEqual(len(self.manager.leases.active([current['resource']])),1)

    def test_completed_evidence_requires_dispatch_and_result_lease_attestation(self):
        run_job(self.fixture.state,self.job['id'])
        result = self.runtime.job_status(self.job['id'])
        self.assertEqual(result['status'],'passed')
        self.assertTrue(result.get('environment_lease_verified'),'A passing attached check must retain its lease verification receipt.')
        with self.runtime.store.transaction() as db:
            stored = self.runtime.store.get(db,'job',self.job['id'])
            stored['environment_lease_verified'] = False
            self.runtime.store.put(db,'job',stored)
        with self.assertRaisesRegex(WorkflowError,'evidence|check'):
            self.runtime.task_submit(self.run['id'],self.attempt['id'],'Reject a result without verified lease ownership.')

    def test_changed_lease_owner_prevents_dispatch_and_preserves_foreign_claim(self):
        import json
        with self.manager.leases.transaction() as db:
            db.execute('UPDATE leases SET owner=? WHERE id LIKE ?',
                       (json.dumps({'job_id':'another-job','run_id':'another-run','state':'another-store'}),self.job['environment_lease_id'] + ':%'))
        run_job(self.fixture.state,self.job['id'])
        result = self.runtime.job_status(self.job['id'])
        self.assertEqual(result['status'],'invalidated')
        self.assertFalse(self.proof.exists())
        with self.manager.leases.transaction() as db:
            rows = db.execute('SELECT id FROM leases WHERE id LIKE ?',(self.job['environment_lease_id'] + ':%',)).fetchall()
        self.assertTrue(rows,'A failed check must not release a different lease owner.')

    def test_lost_data_writer_invalidates_data_without_releasing_new_owner(self):
        import test_parent_database
        fixture = test_parent_database.ParentDatabaseTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        workspace = fixture.attach(**fixture.config())
        run, attempt, child = fixture.child(workspace)
        manager = fixture.manager
        job = {'id':'lost-data-job','run_id':run['id'],'attempt_id':attempt['id'],
               'check':{'argv':[],'environment_dependencies':['data']}}
        with fixture.runtime.store.transaction() as db:
            manager.acquire_check(db,run,job,attempt)
        resource = manager._database_resource(workspace)
        before = manager.leases.data_generation(resource)
        manager.leases.release(job['environment_lease_id'])
        other = manager.leases.acquire([(resource,'exclusive')],{'operation':'new-writer'})
        self.addCleanup(manager.leases.release,other)
        job.update(status='passed',started_at=1,command_pid=12345)
        try:
            with fixture.runtime.store.transaction() as db:
                manager.release_check(db,job)
        except WorkflowError as error:
            self.fail('Lost writer recovery must record invalidation without failing finalization: ' + str(error))
        self.assertEqual(job['status'],'invalidated')
        self.assertGreater(manager.leases.data_generation(resource),before)
        self.assertEqual(len(manager.leases.active([resource])),1)
