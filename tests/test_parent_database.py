import json
from pathlib import Path
import sys

from test_parent import ParentTest, git
from ub_execute.store import WorkflowError


class ParentDatabaseTest(ParentTest):
    def config(self):
        self.probe_state = self.root / 'database.json'
        self.probe_state.write_text(json.dumps({'identity':{'host':'127.0.0.1','port':55432,'database':self.root.name},'schema_fingerprint':'schema-1','worker_restricted':True,'hazards':[]}))
        script = self.root / 'probe.py'
        script.write_text('from pathlib import Path\nimport sys\nprint(Path(sys.argv[1]).read_text())\n')
        worker = self.root / 'worker.json'
        worker.write_text(json.dumps({'DATABASE_URL':'postgresql://worker:private@127.0.0.1:55432/' + self.root.name}))
        worker.chmod(0o600)
        admin = self.root / 'admin.json'
        admin.write_text(json.dumps({'DATABASE_URL':'postgresql://owner:owner-secret@127.0.0.1:55432/' + self.root.name}))
        admin.chmod(0o600)
        return {'database':{'worker_env_file':str(worker),'admin_env_file':str(admin),'expected_identity':{'host':'127.0.0.1','port':55432,'database':self.root.name}, 'probe_argv':[sys.executable,str(script),str(self.probe_state)], 'schema_apply_argv':[sys.executable,'-c','pass']}}

    def test_restricted_environment_never_copies_admin_and_blocks_schema_commands(self):
        workspace = self.attach(**self.config())
        run, attempt, child = self.child(workspace)
        env = self.manager.worker_environment({'run_id':run['id'],'attempt_id':attempt['id']}, {'DATABASE_URL':'owner-secret','PGPASSWORD':'owner','PATH':'/bin'})
        self.assertIn('worker:private@',env['DATABASE_URL'])
        self.assertNotIn('PGPASSWORD',env)
        self.assertNotIn('owner-secret',json.dumps(self.manager.context(workspace['id'])))
        self.assertEqual(workspace['capability_level'],'cooperative')
        job = {'id':'bad','run_id':run['id'],'attempt_id':attempt['id'],'check':{'argv':['npm','run','db:push']}}
        with self.runtime.store.transaction() as db:
            with self.assertRaisesRegex(WorkflowError,'schema|coordinator'):
                self.manager.acquire_check(db,run,job,attempt)

    def test_rejects_conflicting_secondary_database_credentials(self):
        config = self.config()
        path = Path(config['database']['worker_env_file'])
        values = json.loads(path.read_text())
        values['DIRECT_URL'] = values['DATABASE_URL'].replace('worker:private','owner:owner-secret')
        path.write_text(json.dumps(values))
        with self.assertRaisesRegex(WorkflowError,'credential|conflicting'):
            self.attach(**config)

    def test_coordinator_rejects_a_remote_secondary_database_url(self):
        from ub_execute.parent_database import schema_command
        config = self.config()
        admin_file = Path(config['database']['admin_env_file'])
        values = json.loads(admin_file.read_text())
        values['DIRECT_URL'] = 'postgresql://owner:private@remote.invalid:5432/production'
        admin_file.write_text(json.dumps(values))
        with self.assertRaisesRegex(WorkflowError,'identity|local|conflicting'):
            schema_command(config,str(self.repo))

    def test_rejects_privileged_worker_and_wrong_database(self):
        config = self.config()
        value = json.loads(self.probe_state.read_text())
        value['worker_restricted'] = False
        value['hazards'] = ['object owner']
        self.probe_state.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkflowError,'privilege|restricted|owner'):
            self.attach(**config)
        value['worker_restricted'] = True
        value['hazards'] = []
        value['identity']['database'] = 'wrong'
        self.probe_state.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkflowError,'identity'):
            self.attach(**config)

    def test_schema_review_apply_recovery_and_stale_database_evidence(self):
        workspace = self.attach(**self.config())
        run, attempt, child = self.child(workspace)
        proposal = self.manager.schema_request(attempt['id'], {'commit':git(child,'rev-parse','HEAD'),'expected_fingerprint':'schema-2','description':'Add a column.'}, 'Request the schema change.')
        with self.assertRaisesRegex(WorkflowError,'review'):
            self.manager.schema_apply(proposal['id'],'wrong','Apply the change.')
        self.manager.schema_review(proposal['id'],'independent-reviewer','The schema proposal is safe.')
        with self.assertRaisesRegex(WorkflowError,'capability'):
            self.manager.schema_apply(proposal['id'],'wrong','Apply the change.')
        with self.runtime.store.transaction() as db:
            before = self.manager.environment_evidence(db,run,attempt,['database'])
        capability = (self.runtime.store.root / 'parents' / workspace['id'] / 'coordinator.capability').read_text().strip()
        # The fake apply exits before the changed schema becomes visible. Recovery must inspect it.
        with self.assertRaisesRegex(WorkflowError,'fingerprint|reconcil|schema'):
            self.manager.schema_apply(proposal['id'],capability,'Apply the change.')
        value = json.loads(self.probe_state.read_text())
        value['schema_fingerprint'] = 'schema-2'
        self.probe_state.write_text(json.dumps(value))
        recovered = self.manager.recover(workspace['id'],'Check the interrupted apply.')
        self.assertEqual(recovered['schema_generation'],2)
        with self.runtime.store.transaction() as db:
            self.assertFalse(self.manager.validate_evidence(db,run,before))
        self.assertEqual(self.manager.schema_apply(proposal['id'],capability,'Retry the recorded result.')['status'],'applied')

    def test_schema_apply_rejects_changed_database_before_privileged_dispatch(self):
        config = self.config()
        marker = self.root / 'privileged-command-ran'
        config['database']['schema_apply_argv'] = [sys.executable,'-c','from pathlib import Path; import sys; Path(sys.argv[1]).touch()',str(marker)]
        workspace = self.attach(**config)
        run, attempt, child = self.child(workspace)
        proposal = self.manager.schema_request(attempt['id'],{'commit':git(child,'rev-parse','HEAD'),'expected_fingerprint':'schema-2'},'Request the schema change.')
        self.manager.schema_review(proposal['id'],'reviewer','Approve the schema proposal.')
        value = json.loads(self.probe_state.read_text())
        value['schema_fingerprint'] = 'schema-other-parent'
        self.probe_state.write_text(json.dumps(value))
        capability = (self.runtime.store.root / 'parents' / workspace['id'] / 'coordinator.capability').read_text().strip()
        staged = []
        def stage(record, parent):
            staged.append(record['id'])
        try:
            with self.assertRaisesRegex(WorkflowError,'fingerprint|schema|baseline'):
                self.manager.schema_apply(proposal['id'],capability,'Apply the reviewed schema.',stage=stage)
        finally:
            with self.runtime.store.transaction() as db:
                current = self.runtime.store.get(db,'schema_proposal',proposal['id'])
            self.manager.leases.release(current.get('lease_id'))
        self.assertFalse(marker.exists(),'The privileged schema command ran against an unreviewed database baseline.')
        self.assertEqual(staged,[],'Source staging ran before checking the actual database baseline.')

    def test_two_runtime_instances_serialize_database_leases(self):
        workspace = self.attach(**self.config())
        run, attempt, child = self.child(workspace)
        job = {'id':'reader','run_id':run['id'],'attempt_id':attempt['id'],'check':{'environment_dependencies':['database']}}
        with self.runtime.store.transaction() as db:
            self.manager.acquire_check(db,run,job,attempt)
        self.addCleanup(self.manager.leases.release,job['environment_lease_id'])
        proposal = self.manager.schema_request(attempt['id'],{'commit':git(child,'rev-parse','HEAD'),'expected_fingerprint':'schema-2'},'Request a schema change.')
        self.manager.schema_review(proposal['id'],'reviewer','Approve the proposal.')
        capability = (self.runtime.store.root / 'parents' / workspace['id'] / 'coordinator.capability').read_text().strip()
        with self.assertRaisesRegex(WorkflowError,'lease|active'):
            self.manager.schema_apply(proposal['id'],capability,'Apply with another reader.')


    def test_data_writer_invalidates_prior_data_evidence_even_after_failure(self):
        workspace = self.attach(**self.config())
        run, attempt, child = self.child(workspace)
        with self.runtime.store.transaction() as db:
            old = self.manager.environment_evidence(db,run,attempt,['data'])
            job = {'id':'data-writer','run_id':run['id'],'attempt_id':attempt['id'],'check':{'environment_dependencies':['data']},'status':'failed'}
            self.manager.acquire_check(db,run,job,attempt)
            concurrent = {'id':'another-writer','run_id':run['id'],'attempt_id':attempt['id'],'check':{'environment_dependencies':['data']}}
            with self.assertRaisesRegex(WorkflowError,'lease|active'):
                self.manager.acquire_check(db,run,concurrent,attempt)
            self.manager.release_check(db,job)
            self.assertFalse(self.manager.validate_evidence(db,run,old))
            self.assertTrue(self.manager.validate_evidence(db,run,job['environment']))
            generation = job['environment']['data_generation']
            self.manager.release_check(db,job)
            self.assertEqual(job['environment']['data_generation'],generation)


    def test_data_writer_invalidates_evidence_in_another_store_and_parent(self):
        from ub_execute.parent import ParentManager
        from ub_execute.runtime import Runtime
        config = self.config()
        workspace = self.attach(**config)
        run, attempt, child = self.child(workspace)
        with self.runtime.store.transaction() as db:
            old = self.manager.environment_evidence(db,run,attempt,['data'])
        other_parent = self.root / 'other-parent'
        git(self.repo,'worktree','add','-qb','other-parent',str(other_parent),'HEAD')
        (other_parent / 'node_modules').symlink_to(self.repo / 'node_modules',target_is_directory=True)
        other_runtime = Runtime(self.root / 'other-state')
        other_manager = ParentManager(other_runtime)
        other_workspace = other_manager.attach(str(other_parent),config,'Attach the second parent to the shared database.')
        other_run = {'id':'other-run','workspace_id':other_workspace['id'],'worktree':str(other_parent)}
        with other_runtime.store.transaction() as db:
            other_runtime.store.put(db,'run',other_run)
            writer = {'id':'other-writer','run_id':'other-run','attempt_id':None,'check':{'environment_dependencies':['data']},'status':'failed'}
            other_manager.acquire_check(db,other_run,writer)
            self.addCleanup(other_manager.leases.release,writer['environment_lease_id'])
            other_manager.release_check(db,writer)
            self.assertTrue(other_manager.validate_evidence(db,other_run,writer['environment']))
        with self.runtime.store.transaction() as db:
            self.assertFalse(self.manager.validate_evidence(db,run,old))
            current = self.manager.environment_evidence(db,run,attempt,['data'])
            self.assertEqual(current['data_generation'],writer['environment']['data_generation'])
        self.assertEqual(self.manager.context(workspace['id'])['data_generation'],writer['environment']['data_generation'])


import os
from urllib.parse import urlsplit, urlunsplit
import uuid
import unittest


@unittest.skipUnless(os.environ.get('UB_PARENT_TEST_ADMIN_DSN'), 'Set the disposable PostgreSQL test DSN.')
class RealPostgresParentTest(ParentTest):
    def test_actual_role_blocks_schema_sql_and_inherited_owner_rights(self):
        import psycopg
        from psycopg import sql
        from ub_execute.parent_database import connect, inspect_database, read_environment
        admin_url = os.environ['UB_PARENT_TEST_ADMIN_DSN']
        parsed = urlsplit(admin_url)
        self.assertIn(parsed.hostname, {'localhost','127.0.0.1','::1'})
        name = 'ub_parent_test_' + uuid.uuid4().hex[:16]
        with connect(admin_url) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        role = None
        try:
            database_url = urlunsplit((parsed.scheme,parsed.netloc,'/' + name,'',''))
            with connect(database_url) as admin:
                admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
                admin.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                admin.execute('CREATE TABLE fixture (id integer)')
                admin.execute('INSERT INTO fixture VALUES (7)')
            private = self.root / 'real-admin.json'
            private.write_text(json.dumps({'DATABASE_URL':database_url}))
            private.chmod(0o600)
            workspace = self.attach(database={'admin_env_file':str(private)})
            with self.runtime.store.transaction() as db:
                record = self.manager._workspace(db,workspace['id'])
            config = self.manager._config(record)['database']
            worker = read_environment(config['worker_env_file'])['DATABASE_URL']
            role = urlsplit(worker).username
            self.assertEqual(workspace['database_enforcement'],'postgres_role')
            with connect(worker) as connection:
                self.assertEqual(connection.execute('SELECT id FROM fixture').fetchone()[0],7)
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute('CREATE TABLE public.forbidden (id integer)')
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute('ALTER TABLE fixture ADD COLUMN forbidden integer')
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute('INSERT INTO fixture VALUES (8)')
            with connect(database_url) as admin:
                owner = admin.execute('SELECT current_user').fetchone()[0]
                admin.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(owner),sql.Identifier(role)))
            with self.assertRaisesRegex(WorkflowError,'privilege|restricted'):
                inspect_database(config,str(self.repo))
        finally:
            with connect(admin_url) as admin:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                if role:
                    admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

    def test_schema_fingerprint_detects_rls_view_trigger_and_sequence_changes(self):
        from psycopg import sql
        from ub_execute.parent_database import connect, inspect_database
        admin_url = os.environ['UB_PARENT_TEST_ADMIN_DSN']
        parsed = urlsplit(admin_url)
        self.assertIn(parsed.hostname, {'localhost','127.0.0.1','::1'})
        name = 'ub_parent_test_' + uuid.uuid4().hex[:16]
        with connect(admin_url) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        role = None
        try:
            database_url = urlunsplit((parsed.scheme,parsed.netloc,'/' + name,'',''))
            with connect(database_url) as admin:
                admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
                admin.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                admin.execute('CREATE TABLE fixture (id integer)')
                admin.execute('CREATE VIEW projected AS SELECT id FROM fixture')
                admin.execute('CREATE SEQUENCE fixture_sequence')
                admin.execute("CREATE FUNCTION fixture_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$")
            private = self.root / 'fingerprint-admin.json'
            private.write_text(json.dumps({'DATABASE_URL':database_url}))
            private.chmod(0o600)
            workspace = self.attach(database={'admin_env_file':str(private)})
            with self.runtime.store.transaction() as db:
                record = self.manager._workspace(db,workspace['id'])
            config = self.manager._config(record)['database']
            role = config['provisioned_role']
            previous = inspect_database(config,str(self.repo))['schema_fingerprint']
            commands = [
                'ALTER TABLE fixture ENABLE ROW LEVEL SECURITY',
                'CREATE POLICY fixture_read ON fixture FOR SELECT USING (id > 0)',
                'CREATE OR REPLACE VIEW projected AS SELECT id + 1 AS id FROM fixture',
                'CREATE TRIGGER fixture_changed BEFORE INSERT ON fixture FOR EACH ROW EXECUTE FUNCTION fixture_trigger()',
                'ALTER SEQUENCE fixture_sequence INCREMENT BY 2',
            ]
            for command in commands:
                with connect(database_url) as admin:
                    admin.execute(command)
                current = inspect_database(config,str(self.repo))['schema_fingerprint']
                self.assertNotEqual(current,previous,command)
                previous = current
        finally:
            with connect(admin_url) as admin:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                if role:
                    admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
