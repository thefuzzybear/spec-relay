import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ub_execute.runtime import Runtime
from ub_execute.store import WorkflowError


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


class ParentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'parent'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'user.name', 'Parent Test')
        (self.repo / '.gitignore').write_text('node_modules\n.env*\n.ub-parent\n.next\n')
        (self.repo / 'package.json').write_text('{"name":"app","dependencies":{}}')
        (self.repo / 'package-lock.json').write_text('{"lockfileVersion":3}')
        (self.repo / 'app.js').write_text('module.exports = "parent";')
        (self.repo / 'node_modules').mkdir()
        (self.repo / '.env').write_text('DATABASE_URL=owner-secret\nADMIN_TOKEN=secret\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'Prepare the parent')
        self.runtime = Runtime(self.root / 'state')
        try:
            module = importlib.import_module('ub_execute.parent')
        except ModuleNotFoundError:
            module = None
        self.assertIsNotNone(module, 'The parent attachment API must create a controlled child environment.')
        self.manager = module.ParentManager(self.runtime)

    def attach(self, **config):
        return self.manager.attach(str(self.repo), config, 'Attach the prepared environment.')

    def child(self, workspace):
        path = self.root / ('child-' + str(len(list(self.root.glob('child-*')))))
        git(self.repo, 'worktree', 'add', '-qb', path.name, str(path), 'HEAD')
        run = {'id':'run-one', 'workspace_id':workspace['id'], 'worktree':str(self.repo)}
        attempt = {'id':path.name, 'run_id':run['id'], 'worktree':str(path), 'base_commit':git(path,'rev-parse','HEAD')}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, 'run', run)
            self.manager.prepare_child(db, run, attempt)
            self.runtime.store.put(db, 'attempt', attempt)
        return run, attempt, path

    def test_child_borrows_dependencies_and_uses_its_own_source(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        self.assertEqual((child / 'node_modules').resolve(), (self.repo / 'node_modules').resolve())
        (child / 'app.js').write_text('module.exports = "child";')
        output = subprocess.check_output(['node', '-e', 'process.stdout.write(require("./app.js"))'], cwd=child, text=True)
        self.assertEqual(output, 'child')
        environment = self.manager.worker_environment({'run_id':run['id'], 'attempt_id':attempt['id']}, {'DATABASE_URL':'owner', 'ADMIN_TOKEN':'owner', 'PATH':os.environ['PATH']})
        self.assertNotIn('DATABASE_URL', environment)
        self.assertNotIn('ADMIN_TOKEN', environment)
        self.assertTrue(environment['NEXT_BUILD_DIR'].startswith(str(child.resolve())))
        envfile = child / '.ub-parent' / 'worker.env.json'
        self.assertEqual(envfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(git(child, 'check-ignore', str(envfile)), str(envfile))
        self.assertNotIn('secret', json.dumps(self.manager.context(workspace['id'])))

    def test_attachment_rejects_workspace_dependencies_that_resolve_parent_code(self):
        (self.repo / 'package.json').write_text('{"workspaces":["packages/*"]}')
        git(self.repo,'add','package.json')
        git(self.repo,'commit','-qm','Add linked packages')
        with self.assertRaisesRegex(WorkflowError, 'workspace|local package'):
            self.attach()

    def test_child_rejects_changed_manifest_and_existing_dependency_target(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        (child / 'package.json').write_text('{"dependencies":{"other":"1"}}')
        with self.runtime.store.transaction() as db:
            with self.assertRaisesRegex(WorkflowError, 'manifest|dependency'):
                self.manager.prepare_child(db, run, attempt)
        (child / 'package.json').write_bytes((self.repo / 'package.json').read_bytes())
        (child / 'node_modules').unlink()
        (child / 'node_modules').mkdir()
        with self.runtime.store.transaction() as db:
            with self.assertRaisesRegex(WorkflowError, 'overwrite|exists'):
                self.manager.prepare_child(db, run, attempt)

    def test_changed_parent_dependency_invalidates_only_affected_evidence(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        with self.runtime.store.transaction() as db:
            pure = self.manager.environment_evidence(db, run, attempt, [])
            dependency = self.manager.environment_evidence(db, run, attempt, ['dependencies'])
        (self.repo / 'package-lock.json').write_text('{"lockfileVersion":4}')
        with self.runtime.store.transaction() as db:
            self.assertTrue(self.manager.validate_evidence(db, run, pure))
            self.assertFalse(self.manager.validate_evidence(db, run, dependency))

    def test_dirty_refresh_preserves_child_and_release_preserves_parent(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        (child / 'app.js').write_text('dirty child')
        with self.assertRaisesRegex(WorkflowError, 'uncommitted|dirty|changes'):
            self.manager.refresh(run['id'], attempt['id'], 'Refresh the child.')
        self.assertEqual((child / 'app.js').read_text(), 'dirty child')
        with self.runtime.store.transaction() as db:
            result = self.manager.release_child(db, attempt)
        self.assertTrue((self.repo / 'node_modules').is_dir())
        self.assertTrue((self.repo / '.env').is_file())
        self.assertEqual(result['released'], True)

    def test_dependency_reader_blocks_shared_dependency_transition(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        job = {'id':'job-one','run_id':run['id'],'attempt_id':attempt['id'],'check':{'environment_dependencies':['dependencies']}}
        with self.runtime.store.transaction() as db:
            self.manager.acquire_check(db, run, job, attempt)
        self.addCleanup(self.manager.leases.release,job['environment_lease_id'])
        (self.repo / 'package-lock.json').write_text('changed')
        git(self.repo,'add','package-lock.json')
        git(self.repo,'commit','-qm','Update the dependency manifest')
        with self.assertRaisesRegex(WorkflowError, 'lease|reader|active'):
            self.manager.attach(str(self.repo), {}, 'Update the dependency generation.')
        with self.runtime.store.transaction() as db:
            self.manager.release_check(db, job)

    def test_children_receive_distinct_ports_and_http_evidence_tracks_child(self):
        workspace = self.attach(app={'port':43000,'port_count':100,'origin':'http://127.0.0.1:43000'})
        run, first, child = self.child(workspace)
        _, second, _ = self.child(workspace)
        self.addCleanup(self.manager.leases.release,first['parent_environment']['app']['lease_id'])
        self.addCleanup(self.manager.leases.release,second['parent_environment']['app']['lease_id'])
        self.assertNotEqual(first['parent_environment']['app']['port'],second['parent_environment']['app']['port'])
        first['app_server'] = {'id':'owned-server','origin':first['parent_environment']['app']['origin']}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db,'attempt',first)
            evidence = self.manager.environment_evidence(db,run,first,['http'])
            self.assertTrue(self.manager.validate_evidence(db,run,evidence))
            first['app_server']['id'] = 'replacement'
            self.runtime.store.put(db,'attempt',first)
            self.assertFalse(self.manager.validate_evidence(db,run,evidence))

    def test_refresh_updates_integration_base_after_clean_git_reconciliation(self):
        workspace = self.attach()
        run, attempt, child = self.child(workspace)
        original = attempt['base_commit']
        (self.repo / 'integrated.js').write_text('module.exports = 1;')
        git(self.repo,'add','integrated.js')
        git(self.repo,'commit','-qm','Integrate another task')
        parent_head = git(self.repo,'rev-parse','HEAD')
        git(child,'merge','--ff-only',parent_head)
        self.manager.refresh(run['id'],attempt['id'],'Adopt the reconciled parent commit.')
        with self.runtime.store.transaction() as db:
            refreshed = self.runtime.store.get(db,'attempt',attempt['id'])
        self.assertEqual(refreshed['base_commit'],parent_head)
        self.assertEqual(refreshed['original_base_commit'],original)
