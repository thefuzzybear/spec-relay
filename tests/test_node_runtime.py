import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_parent
from ub_execute.node_runtime import inspect_node
from ub_execute.store import WorkflowError


class NodeRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_parent.ParentTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.repo = self.fixture.repo

    def declarations(self):
        package = self.repo/'package.json'
        manifest = json.loads(package.read_text())
        manifest['engines'] = {'node':'>=22 <23'}
        package.write_text(json.dumps(manifest))
        (self.repo/'.nvmrc').write_text('22\n')
        (self.repo/'.node-version').write_text('22.14.0\n')
        test_parent.git(self.repo,'add','package.json','.nvmrc','.node-version')
        test_parent.git(self.repo,'commit','-qm','Declare the Node runtime')

    def test_inspector_records_actual_executable_version_and_declarations(self):
        self.declarations()
        executable = self.root/'node-example'
        executable.write_text('#!' + sys.executable + '\nprint("v22.14.0")\n')
        executable.chmod(0o755)
        alias = self.root/'node-alias'
        alias.symlink_to(executable)
        with patch('ub_execute.node_runtime.shutil.which',return_value=str(alias)):
            result = inspect_node(self.repo,self.repo/'node_modules')
        self.assertEqual(result,{'executable':str(executable.resolve()),'version':'v22.14.0',
                                 'declared':{'engines':'>=22 <23','version_files':{'.nvmrc':'22','.node-version':'22.14.0'}}})

    def test_inspector_skips_non_node_environments_and_rejects_missing_node(self):
        with patch('ub_execute.node_runtime.shutil.which',return_value=None):
            self.assertIsNone(inspect_node(self.repo,None))
            with self.assertRaisesRegex(WorkflowError,'Node runtime'):
                inspect_node(self.repo,self.repo/'node_modules')

    def test_inspector_rejects_failed_version_probe(self):
        result = subprocess.CompletedProcess(['/example/node','--version'],1,stdout='',stderr='private details')
        with patch('ub_execute.node_runtime.shutil.which',return_value='/example/node'):
            with patch('ub_execute.node_runtime.subprocess.run',return_value=result):
                with self.assertRaisesRegex(WorkflowError,'Node runtime version probe failed'):
                    inspect_node(self.repo,self.repo/'node_modules')

    def test_parent_records_node_identity_and_rejects_changed_runtime_evidence(self):
        self.declarations()
        identity = {'executable':'/example/node','version':'v22.14.0',
                    'declared':{'engines':'>=22 <23','version_files':{'.nvmrc':'22','.node-version':'22.14.0'}}}
        with patch('ub_execute.parent.inspect_node',create=True,return_value=identity):
            workspace = self.fixture.attach()
            self.assertEqual(workspace.get('node_runtime'),identity)
            self.assertIn('.nvmrc',workspace['manifest_digests'])
            self.assertIn('.node-version',workspace['manifest_digests'])
            run, attempt, child = self.fixture.child(workspace)
            self.assertEqual(attempt['parent_environment'].get('node_runtime'),identity)
            with self.fixture.runtime.store.transaction() as db:
                evidence = self.fixture.manager.environment_evidence(db,run,attempt,['dependencies'])
                self.assertEqual(evidence.get('node_runtime'),identity)
                self.assertTrue(self.fixture.manager.validate_evidence(db,run,evidence))
        changed = {**identity,'version':'v24.0.0'}
        with patch('ub_execute.parent.inspect_node',create=True,return_value=changed):
            with self.fixture.runtime.store.transaction() as db:
                self.assertFalse(self.fixture.manager.validate_evidence(db,run,evidence))
            updated = self.fixture.manager.attach(str(self.repo),{},'Adopt the changed Node runtime.')
            self.assertEqual(updated['node_runtime'],changed)
            self.assertEqual(updated['dependency_generation'],workspace['dependency_generation'] + 1)
            with self.fixture.runtime.store.transaction() as db:
                with self.assertRaisesRegex(WorkflowError,'stale|Refresh|runtime|generation'):
                    self.fixture.manager.environment_evidence(db,run,attempt,['dependencies'])
            context = self.fixture.manager.refresh(run['id'],attempt['id'],'Acknowledge the current Node runtime.')
            self.assertEqual(context['node_runtime'],changed)
            self.assertEqual(context['dependency_generation'],updated['dependency_generation'])
            with self.fixture.runtime.store.transaction() as db:
                refreshed = self.fixture.runtime.store.get(db,'attempt',attempt['id'])
                current = self.fixture.manager.environment_evidence(db,run,refreshed,['dependencies'])
                self.assertEqual(current['node_runtime'],changed)
                self.assertTrue(self.fixture.manager.validate_evidence(db,run,current))
