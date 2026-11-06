"""Exercise the public runtime with borrowed parents and completed cleanup."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import test_execution
from ub_execute.runtime import WorkflowError
from ub_execute.tools import catalog, call


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_execution.ExecutionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime

    def attach(self, dependencies=False):
        fixture = self.fixture
        config = {}
        if dependencies:
            (fixture.repo / 'package.json').write_text('{"name":"fixture"}')
            (fixture.repo / 'package-lock.json').write_text('{}')
            (fixture.repo / 'node_modules').mkdir()
            (fixture.repo / 'node_modules' / 'proof.txt').write_text('borrowed')
            test_execution.git(fixture.repo, 'add', 'package.json', 'package-lock.json')
            test_execution.git(fixture.repo, 'commit', '-qm', 'Record prepared dependencies')
            config['dependency_dir'] = 'node_modules'
        workspace = self.runtime.workspace_attach(str(fixture.repo), config, 'Use the prepared feature workspace.')
        fixture.bundle.update(workspace_id=workspace['id'], workspace_mode='attached_parent')
        return workspace

    def wait(self, job):
        for _ in range(300):
            current = self.runtime.job_status(job['id'])
            if current['status'] not in {'queued', 'running'}:
                return current
            time.sleep(0.02)
        self.fail('The detached check did not finish.')

    def integrate(self, run, attempt):
        self.fixture.implement(run, attempt)
        self.runtime.task_submit(run['id'], attempt['id'], 'Submit the tested addition.')
        self.fixture.review(run, attempt)
        self.runtime.task_accept(run['id'], attempt['id'], 'Accept the reviewed addition.')
        return self.runtime.task_integrate(run['id'], 'add', 'Integrate the reviewed addition.')

    def complete(self, run):
        final = self.runtime.check_start(run['id'], 'test', 'final', 'Check the integrated result.')
        self.assertEqual(self.wait(final)['status'], 'passed')
        self.runtime.run_review_record(run['id'], 'independent-final', ['REQ-1'], [], 'Verify the complete requirement.')
        return self.runtime.run_complete(run['id'], 'The reviewed specification is complete.')

    def test_attached_run_uses_prepared_parent_and_child_source(self):
        fixture = self.fixture
        self.attach(dependencies=True)
        run = fixture.create()
        self.assertFalse(run['integration_owned'])
        self.assertEqual(run['worktree'], str(fixture.repo.resolve()))
        attempt = fixture.claim(run)
        child = Path(attempt['worktree'])
        self.assertEqual((child / 'node_modules').resolve(), (fixture.repo / 'node_modules').resolve())
        self.assertNotIn('node_modules', test_execution.git(child, 'status', '--porcelain'))
        self.assertEqual(test_execution.git(child, 'rev-parse', 'HEAD'), run['integration_commit'])
        (child / 'calc.py').write_text('def add(a,b):\n    return 101\n')
        fixture.bundle['checks']['test']['argv'] = [sys.executable, '-c', 'from calc import add; assert add(1,2)==101']
        with self.runtime.store.transaction() as db:
            stored = self.runtime.store.get(db, 'run', run['id'])
            stored['bundle']['checks']['test']['argv'] = fixture.bundle['checks']['test']['argv']
            self.runtime.store.put(db, 'run', stored)
        check = self.runtime.check_start(run['id'], 'test', 'green', 'Verify child application source.', attempt['id'])
        self.assertEqual(self.wait(check)['status'], 'passed')
        self.assertIn('return 0', (fixture.repo / 'calc.py').read_text())
        self.assertEqual((child / '.ub-parent' / 'worker.env.json').stat().st_mode & 0o777, 0o600)

    def test_completed_cleanup_restores_files_and_preserves_parent(self):
        fixture = self.fixture
        self.attach(dependencies=True)
        fixture.bundle['cleanup_policy'] = {'authorized': True, 'on_complete': True}
        run = fixture.create()
        attempt = fixture.claim(run)
        self.integrate(run, attempt)
        child = Path(attempt['worktree'])
        (child / '.ub-parent' / 'retained-secret').write_bytes(b'private\0bytes')
        self.runtime.writer_release(run['id'], attempt['id'], 'orchestrator', 'The worker and reviewer finished. No child services remain.')
        result = self.complete(run)
        self.assertEqual(result['status'], 'complete')
        cleanup = result['cleanup']
        for _ in range(300):
            cleanup = self.runtime.cleanup_status(cleanup['id'])
            if cleanup['status'] in {'complete', 'blocked'}:
                break
            time.sleep(0.03)
        resources = {item['resource_id']: item for item in cleanup['resources']}
        removed = resources[attempt['id']]
        self.assertEqual(removed['stage'], 'reconciled', removed)
        self.assertEqual(resources[run['id'] + '/integration']['stage'], 'blocked')
        self.assertFalse(child.exists())
        self.assertEqual((fixture.repo / 'node_modules' / 'proof.txt').read_text(), 'borrowed')
        self.assertIn('a + b', (fixture.repo / 'calc.py').read_text())
        restored = fixture.root / 'restored'
        self.runtime.cleanup_restore(removed['archive_id'], str(restored.resolve()))
        self.assertEqual((restored / '.ub-parent' / 'retained-secret').read_bytes(), b'private\0bytes')
        self.assertEqual(test_execution.git(restored, 'rev-parse', 'HEAD'), test_execution.git(fixture.repo, 'rev-parse', attempt['branch']))
        packet = self.runtime.task_context(run['id'], 'add', attempt['id'])
        self.assertEqual(packet['resource']['archive_id'], removed['archive_id'])
        self.assertEqual(self.runtime.run_recover(run['id'])['run']['status'], 'complete')

    def test_cleanup_failure_does_not_revert_completed_execution(self):
        fixture = self.fixture
        fixture.bundle['cleanup_policy'] = {'authorized': True, 'on_complete': True}
        run = fixture.create()
        attempt = fixture.claim(run)
        self.integrate(run, attempt)
        with patch.object(self.runtime, 'cleanup_apply', side_effect=OSError('fixture storage is unavailable')):
            result = self.complete(run)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['cleanup']['status'], 'blocked')
        self.assertEqual(self.runtime.run_status(run['id'])['status'], 'complete')

    def test_cleanup_lease_prevents_dispatch_and_reconciliation(self):
        run = self.fixture.create()
        attempt = self.fixture.claim(run)
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, 'cleanup_lease', {'id': attempt['id'], 'run_id': run['id'], 'active': True, 'cleanup_id': 'fixture-cleanup'})
        with self.assertRaisesRegex(WorkflowError, 'cleanup lease'):
            self.runtime.check_start(run['id'], 'test', 'green', 'Try dispatch during cleanup.', attempt['id'])
        with self.assertRaisesRegex(WorkflowError, 'cleanup lease'):
            self.runtime.integration_reconcile(run['id'], 'Try reconciliation during cleanup.')

    def test_catalog_exposes_lifecycle_but_keeps_apply_coordinator_only(self):
        names = {item['name'] for item in catalog()}
        self.assertTrue({'cleanup_plan', 'cleanup_apply', 'cleanup_status', 'cleanup_restore', 'cleanup_authorize', 'writer_release', 'workspace_attach', 'workspace_context', 'workspace_refresh', 'schema_change_request', 'schema_review'} <= names)
        self.assertNotIn('coordinator_apply', names)
        with self.assertRaisesRegex(WorkflowError, 'Unknown'):
            call(self.runtime, 'coordinator_apply', {})

    def test_attached_schema_commands_are_rejected_before_launch(self):
        self.attach()
        self.fixture.bundle['checks']['test']['argv'] = ['npm', 'run', 'db:push']
        run = self.fixture.create()
        attempt = self.fixture.claim(run)
        with self.assertRaisesRegex(WorkflowError, 'coordinator'):
            self.runtime.check_start(run['id'], 'test', 'green', 'Try a worker schema command.', attempt['id'])

    def test_queued_check_rejects_changed_dependency_generation(self):
        self.attach(dependencies=True)
        run = self.fixture.create()
        attempt = self.fixture.claim(run)
        with patch.object(self.runtime, '_launch_job', side_effect=lambda job: job):
            job = self.runtime.check_start(run['id'], 'test', 'green', 'Queue an environment-dependent check.', attempt['id'])
        (self.fixture.repo / 'package-lock.json').write_text('{"changed":true}')
        from ub_execute.worker import run_job
        run_job(self.fixture.state, job['id'])
        result = self.runtime.job_status(job['id'])
        self.assertEqual(result['status'], 'invalidated')
        self.assertIn('environment', result['error'].lower())
        self.assertFalse(self.runtime.workspace_context(run['workspace_id'])['leases'])

    def test_invalid_lifecycle_policies_fail_before_worktree_creation(self):
        for extra in ({'workspace_mode': 'attached_parent'}, {'cleanup_policy': {'authorized': 'yes', 'on_complete': True}}, {'workspace_mode': 'unknown'}):
            with self.subTest(extra=extra):
                bundle = dict(self.fixture.bundle, **extra)
                with self.assertRaises(WorkflowError):
                    self.runtime.run_create(bundle, 'Reject an invalid lifecycle policy.')

    def attach_database(self):
        fixture = self.fixture
        self.probe = fixture.root / 'probe.json'
        identity = {'host':'127.0.0.1','port':5432,'database':'relay_fixture'}
        evidence = {'identity':identity,'worker_restricted':True,'hazards':[],'schema_fingerprint':'before'}
        self.probe.write_text(json.dumps(evidence))
        worker = fixture.root / 'worker.json'
        admin = fixture.root / 'admin.json'
        worker.write_text(json.dumps({'DATABASE_URL':'postgresql://worker:private@127.0.0.1:5432/relay_fixture'}))
        admin.write_text(json.dumps({'DATABASE_URL':'postgresql://owner:private@127.0.0.1:5432/relay_fixture'}))
        worker.chmod(0o600)
        admin.chmod(0o600)
        self.counter = fixture.root / 'push-count'
        after = dict(evidence, schema_fingerprint='after')
        command = f'from pathlib import Path; Path({str(self.probe)!r}).write_text({json.dumps(after)!r}); p=Path({str(self.counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
        config = {'schema_paths':['schema/'],'database':{'worker_env_file':str(worker),'admin_env_file':str(admin),'expected_identity':identity,
                  'probe_argv':[sys.executable,'-c',f'from pathlib import Path; print(Path({str(self.probe)!r}).read_text())'],
                  'schema_apply_argv':[sys.executable,'-c',command]}}
        workspace = self.runtime.workspace_attach(str(fixture.repo), config, 'Attach the controlled fixture database.')
        fixture.bundle.update(workspace_id=workspace['id'],workspace_mode='attached_parent')
        fixture.bundle['tasks'][0]['scope'].append('schema/')
        return workspace

    def test_coordinator_stages_schema_once_and_final_task_avoids_duplicate_commit(self):
        fixture = self.fixture
        workspace = self.attach_database()
        first, second = fixture.create(), fixture.create()
        attempt = fixture.claim(first)
        child = Path(attempt['worktree'])
        (child / 'schema').mkdir()
        (child / 'schema' / 'feature.sql').write_text('CREATE TABLE feature (id integer);\n')
        test_execution.git(child,'add','schema')
        test_execution.git(child,'commit','-qm','Propose the feature schema')
        original = test_execution.git(child,'rev-parse','HEAD')
        proposal = self.runtime.schema_change_request(attempt['id'],{'commit':original,'expected_fingerprint':'after','schema_paths':['schema/']},'Request parent schema application.')
        self.runtime.schema_review(proposal['id'],'schema-reviewer','Review the schema-only commit.')
        capability = (fixture.state/'parents'/workspace['id']/'coordinator.capability').read_text().strip()
        applied = self.runtime.coordinator_apply(proposal['id'],capability,'Apply the reviewed schema from the parent.')
        self.assertEqual(applied['status'],'applied')
        staged = test_execution.git(fixture.repo,'rev-parse','HEAD')
        self.assertEqual(self.runtime.run_status(second['id'])['integration_commit'],staged)
        self.assertEqual(self.runtime.workspace_context(workspace['id'])['schema_generation'],2)
        self.runtime.coordinator_apply(proposal['id'],capability,'Retry the completed coordinator operation.')
        self.assertEqual(self.counter.read_text(),'1')
        test_execution.git(child,'rebase',staged)
        self.runtime.workspace_refresh(first['id'],attempt['id'],'Refresh clean child code and environment.')
        self.integrate(first,attempt)
        self.assertEqual(test_execution.git(fixture.repo,'log','--format=%s').count('Propose the feature schema'),1)
        self.assertIn('a + b',(fixture.repo/'calc.py').read_text())

    def test_stale_database_check_cannot_satisfy_submission(self):
        fixture = self.fixture
        workspace = self.attach_database()
        run = fixture.create()
        attempt = fixture.claim(run)
        child = fixture.implement(run,attempt)
        evidence = json.loads(self.probe.read_text())
        evidence['schema_fingerprint'] = 'unrecorded-change'
        self.probe.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(WorkflowError,'evidence|check'):
            self.runtime.task_submit(run['id'],attempt['id'],'Reject stale database evidence.')
        self.assertTrue(child.is_dir())

    def test_owned_service_lease_requires_stop_before_release(self):
        fixture = self.fixture
        self.attach()
        # Reattach with an explicit child app range.
        workspace = self.runtime.workspace_attach(str(fixture.repo),{'app':{'port':45000,'port_count':100,'origin':'http://127.0.0.1:45000'}},'Assign child app ports.')
        run = fixture.create()
        attempt = fixture.claim(run)
        app = attempt['parent_environment']['app']
        with patch('ub_execute.resources._alive',return_value=True):
            record = self.runtime.service_register(attempt['id'],{'pid':os.getpid(),'port':app['port'],'origin':app['origin'],'environment_dependencies':['dependencies','http']},'Record the owned child server.')
            with self.assertRaisesRegex(WorkflowError,'Stop'):
                self.runtime.service_release(record['id'],'Try release before stopping.')
        with patch('ub_execute.resources._alive',return_value=False):
            self.assertEqual(self.runtime.service_release(record['id'],'The owned child server stopped.')['status'],'released')
        with self.runtime.store.transaction() as db:
            current = self.runtime.store.get(db,'attempt',attempt['id'])
            self.runtime._parent_manager().release_child(db,current)

    def test_generated_registration_binds_exact_file_bytes(self):
        run = self.fixture.create()
        attempt = self.fixture.claim(run)
        output = Path(attempt['worktree'])/'target'/'result.bin'
        output.parent.mkdir()
        output.write_bytes(b'exact output\0')
        record = self.runtime.artifact_register(run['id'],attempt['id'],['target/result.bin'],'The configured report check created this file.')
        self.assertEqual(record['generated_artifacts']['target/result.bin'],hashlib.sha256(output.read_bytes()).hexdigest())
        with self.assertRaises(WorkflowError):
            self.runtime.artifact_register(run['id'],attempt['id'],['calc.py'],'Reject tracked source ownership.')

    def test_task_named_integration_has_a_distinct_branch_namespace(self):
        self.fixture.bundle['tasks'][0]['id'] = 'integration'
        run = self.fixture.create()
        attempt = self.runtime.task_claim(run['id'], 'integration', 'worker', 'Use a task ID that matches the integration label.')
        self.assertTrue(Path(attempt['worktree']).is_dir())
        self.assertNotEqual(attempt['branch'], run['integration_branch'])

    def child_service(self):
        fixture = self.fixture
        workspace = self.runtime.workspace_attach(str(fixture.repo),{'app':{'port':46000,'port_count':100,'origin':'http://127.0.0.1:46000'}},'Assign isolated child app ports.')
        fixture.bundle.update(workspace_id=workspace['id'],workspace_mode='attached_parent')
        run = fixture.create()
        attempt = fixture.claim(run)
        self.addCleanup(self.runtime._parent_manager().leases.release,attempt['parent_environment']['app']['lease_id'])
        return run, attempt

    def test_default_service_registration_supports_database_free_parent(self):
        run, attempt = self.child_service()
        app = attempt['parent_environment']['app']
        try:
            with patch('ub_execute.resources._alive',return_value=True):
                service = self.runtime.service_register(attempt['id'],{'pid':os.getpid(),'port':app['port'],'origin':app['origin']},'Record a database-free child app.')
        except WorkflowError as error:
            self.fail('A child app must support a database-free parent: ' + str(error))
        with patch('ub_execute.resources._alive',return_value=False):
            self.runtime.service_release(service['id'],'The fixture child app stopped.')
        with self.runtime.store.transaction() as db:
            self.runtime._parent_manager().release_child(db,self.runtime.store.get(db,'attempt',attempt['id']))

    def test_http_evidence_rejects_source_changed_after_server_registration(self):
        run, attempt = self.child_service()
        app = attempt['parent_environment']['app']
        with patch('ub_execute.resources._alive',return_value=True):
            service = self.runtime.service_register(attempt['id'],{'pid':os.getpid(),'port':app['port'],'origin':app['origin'],'environment_dependencies':['http']},'Record the child server revision.')
        self.addCleanup(self.runtime._parent_manager().leases.release,service['environment_lease_id'])
        child = Path(attempt['worktree'])
        (child/'calc.py').write_text('def add(a,b):\n    return 999\n')
        with self.runtime.store.transaction() as db:
            current = self.runtime.store.get(db,'attempt',attempt['id'])
            stored = self.runtime.store.get(db,'run',run['id'])
            with self.assertRaisesRegex(WorkflowError,'server|source|revision'):
                self.runtime._parent_manager().environment_evidence(db,stored,current,['http'])
            self.runtime._parent_manager().release_child(db,current)

    def test_schema_staging_recovers_after_git_commit_before_bookkeeping(self):
        fixture = self.fixture
        workspace = self.attach_database()
        run = fixture.create()
        attempt = fixture.claim(run)
        child = Path(attempt['worktree'])
        (child/'schema').mkdir()
        (child/'schema/feature.sql').write_text('CREATE TABLE feature (id integer);\n')
        test_execution.git(child,'add','schema')
        test_execution.git(child,'commit','-qm','Propose recoverable schema')
        proposal = self.runtime.schema_change_request(attempt['id'],{'commit':test_execution.git(child,'rev-parse','HEAD'),'expected_fingerprint':'after','schema_paths':['schema/']},'Request recoverable schema application.')
        self.runtime.schema_review(proposal['id'],'reviewer','Review the recoverable schema change.')
        capability = (fixture.state/'parents'/workspace['id']/'coordinator.capability').read_text().strip()
        with patch.object(self.runtime,'_advance_parent',side_effect=RuntimeError('simulated bookkeeping interruption')):
            with self.assertRaisesRegex(RuntimeError,'interruption'):
                self.runtime.coordinator_apply(proposal['id'],capability,'Exercise the Git staging journal.')
        self.assertFalse(self.counter.exists())
        self.runtime.coordinator_recover(workspace['id'],'Reconcile completed Git staging before retry.','retry')
        self.runtime.coordinator_apply(proposal['id'],capability,'Continue the checked schema intent.')
        self.assertEqual(self.counter.read_text(),'1')
        self.assertEqual(test_execution.git(fixture.repo,'log','--format=%s').count('Propose recoverable schema'),1)

    def test_schema_only_task_can_finish_after_provisional_parent_application(self):
        fixture = self.fixture
        workspace = self.attach_database()
        fixture.bundle['tasks'][0]['tdd_exception'] = 'This fixture tests schema workflow state rather than application behavior.'
        fixture.bundle['checks']['test']['argv'] = [sys.executable,'-c','print("schema fixture")']
        run = fixture.create()
        attempt = fixture.claim(run)
        child = Path(attempt['worktree'])
        (child/'schema').mkdir()
        (child/'schema/only.sql').write_text('CREATE TABLE feature (id integer);\n')
        test_execution.git(child,'add','schema')
        test_execution.git(child,'commit','-qm','Deliver schema-only task')
        proposal = self.runtime.schema_change_request(attempt['id'],{'commit':test_execution.git(child,'rev-parse','HEAD'),'expected_fingerprint':'after','schema_paths':['schema/']},'Request schema-only task application.')
        self.runtime.schema_review(proposal['id'],'reviewer','Review the schema-only requirement.')
        capability = (fixture.state/'parents'/workspace['id']/'coordinator.capability').read_text().strip()
        self.runtime.coordinator_apply(proposal['id'],capability,'Apply the schema-only task from the parent.')
        test_execution.git(child,'rebase',test_execution.git(fixture.repo,'rev-parse','HEAD'))
        self.runtime.workspace_refresh(run['id'],attempt['id'],'Acknowledge the applied schema generation.')
        check = self.runtime.check_start(run['id'],'test','green','Verify the schema-only task environment.',attempt['id'])
        self.assertEqual(self.wait(check)['status'],'passed')
        try:
            self.runtime.task_submit(run['id'],attempt['id'],'Submit the applied schema-only task.')
        except WorkflowError as error:
            self.fail('The applied schema-only task must remain submittable: ' + str(error))
        fixture.review(run,attempt)
        self.runtime.task_accept(run['id'],attempt['id'],'Accept the reviewed schema-only task.')
        integrated = self.runtime.task_integrate(run['id'],'add','Finish the already applied schema-only task.')
        self.assertEqual(integrated['status'],'integrated')
