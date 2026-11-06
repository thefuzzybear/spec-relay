"""Cleanup must preserve work unless recovery and release are certain."""

import importlib.util
import os
import pwd
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ub_execute.runtime import Runtime
from ub_execute.store import WorkflowError
from ub_execute.workspace import git
from ub_execute.processes import held_lock


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / '.gitignore').write_text('ignored/\n.env\nnode_modules\n')
        (self.repo / 'source').write_bytes(b'source\x00data')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Fixture')
        self.tree = self.root / 'managed'
        git(self.repo, 'worktree', 'add', '-b', 'result', str(self.tree))
        self.runtime = Runtime(self.root / 'state')
        self.run = {'id': 'run-fixture', 'status': 'complete', 'repository': str(self.repo),
                    'worktree': str(self.tree), 'integration_commit': git(self.tree, 'rev-parse', 'HEAD'),
                    'integration_branch': 'result', 'base_commit': git(self.tree, 'rev-parse', 'HEAD'),
                    'bundle': {'cleanup_policy': {'authorized': True, 'on_complete': True}}}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, 'run', self.run)

    def manager(self):
        self.assertIsNotNone(importlib.util.find_spec('ub_execute.cleanup'),
                             'Completed runs need a preservation-first cleanup coordinator.')
        from ub_execute.cleanup import CleanupManager, resource_manifest
        manager = CleanupManager(self.runtime)
        with self.runtime.store.transaction() as db:
            resources = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])
            if not resources:
                self.runtime.store.put(db, 'cleanup_resource', resource_manifest(self.run))
        return manager

    def release(self, manager):
        return manager.release(self.run['id'], self.run['id'] + '/integration', 'coordinator',
                               'Workers and owned services stopped after native completion.')

    def finish(self, manager, plan):
        result = manager.apply(plan['id'], 'Remove the checked disposable fixture.')
        for unused in range(300):
            status = manager.status(result['id'])
            if status['status'] in {'complete', 'blocked'}:
                return status
            time.sleep(.02)
        self.fail('Cleanup did not finish.')

    def test_round_trip_ignored_files_modes_empty_dirs_and_borrowed_link(self):
        manager = self.manager()
        (self.tree / '.env').write_bytes(b'private\x00credentials')
        ignored = self.tree / 'ignored'
        ignored.mkdir()
        (ignored / 'empty').mkdir()
        name = 'name with\nnewline'
        (ignored / name).write_bytes(bytes(range(256)))
        (ignored / name).chmod(0o751)
        borrowed = self.root / 'borrowed'
        borrowed.mkdir()
        (borrowed / 'keep').write_text('parent data')
        (self.tree / 'node_modules').symlink_to(borrowed, target_is_directory=True)
        self.release(manager)
        status = self.finish(manager, manager.plan(self.run['id']))
        self.assertEqual(status['status'], 'complete', status)
        self.assertFalse(self.tree.exists())
        self.assertEqual((borrowed / 'keep').read_text(), 'parent data')
        self.assertEqual(git(self.repo, 'rev-parse', 'result'), self.run['integration_commit'])
        archive = status['resources'][0]['archive_id']
        restored = self.root / 'restored'
        manager.restore(archive, str(restored))
        self.assertEqual((restored / '.env').read_bytes(), b'private\x00credentials')
        self.assertEqual((restored / 'ignored' / name).read_bytes(), bytes(range(256)))
        self.assertEqual((restored / 'ignored' / name).stat().st_mode & 0o777, 0o751)
        self.assertTrue((restored / 'ignored' / 'empty').is_dir())
        self.assertEqual(os.readlink(restored / 'node_modules'), str(borrowed))
        self.assertEqual(git(restored, 'rev-parse', 'HEAD'), self.run['integration_commit'])
        with self.assertRaises(WorkflowError):
            manager.restore(archive, str(restored))
        replay = manager.apply(status['plan_id'], 'Repeat the same checked request.')
        self.assertEqual(replay['id'], status['id'])

    def test_dirty_unknown_untracked_and_unreleased_are_blocked(self):
        manager = self.manager()
        self.assertTrue(manager.plan(self.run['id'])['blocked'])
        self.release(manager)
        (self.tree / 'source').write_text('dirty')
        self.assertTrue(manager.plan(self.run['id'])['blocked'])
        git(self.tree, 'checkout', '--', 'source')
        (self.tree / 'unknown').write_text('valuable work')
        plan = manager.plan(self.run['id'])
        self.assertTrue(plan['blocked'])
        status = self.finish(manager, plan)
        self.assertTrue(self.tree.exists())
        self.assertEqual((self.tree / 'unknown').read_text(), 'valuable work')
        self.assertEqual(status['status'], 'blocked')

    def test_stale_plan_and_changed_ref_preserve_source(self):
        manager = self.manager()
        self.release(manager)
        plan = manager.plan(self.run['id'])
        (self.tree / '.env').write_text('late secret')
        status = self.finish(manager, plan)
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual((self.tree / '.env').read_text(), 'late secret')
        git(self.tree, 'commit', '--allow-empty', '-m', 'Unexpected commit')
        self.assertTrue(manager.plan(self.run['id'])['blocked'])

    def test_active_job_and_controller_lock_block(self):
        manager = self.manager()
        self.release(manager)
        directory = self.runtime.store.root / 'runs' / self.run['id']
        directory.mkdir(parents=True)
        with held_lock(directory / 'controller.lock'):
            self.assertTrue(manager.plan(self.run['id'])['blocked'])
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, 'job', {'id': 'job-live', 'run_id': self.run['id'],
                                   'status': 'running', 'directory': str(directory)})
        self.assertTrue(manager.plan(self.run['id'])['blocked'])

    def test_unowned_primary_nested_and_borrowed_resources_are_protected(self):
        manager = self.manager()
        self.release(manager)
        nested = self.tree / 'ignored' / 'nested'
        nested.mkdir(parents=True)
        git(nested, 'init')
        self.assertTrue(manager.plan(self.run['id'])['blocked'])
        with self.runtime.store.transaction() as db:
            resource = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])[0]
            resource['ownership'] = 'borrowed'
            self.runtime.store.put(db, 'cleanup_resource', resource)
        self.assertTrue(manager.plan(self.run['id'])['blocked'])
        self.assertTrue(self.tree.exists())

    def test_archive_failure_preserves_source(self):
        manager = self.manager()
        self.release(manager)
        plan = manager.plan(self.run['id'])
        # Run in this process so the injected storage failure reaches the worker.
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(plan['id'], 'Test an archive storage failure.')
        with patch('ub_execute.cleanup.create_archive', side_effect=OSError('No space left')):
            manager.execute(job['id'])
        self.assertEqual(manager.status(job['id'])['status'], 'blocked')
        self.assertEqual((self.tree / 'source').read_bytes(), b'source\x00data')

    def test_integrated_accepted_attempt_is_eligible(self):
        manager = self.manager()
        from ub_execute.cleanup import resource_manifest
        attempt_tree = self.root / 'attempt'
        git(self.repo, 'worktree', 'add', '-b', 'task-result', str(attempt_tree))
        attempt = {'id': 'attempt-fixture', 'run_id': self.run['id'], 'task_id': 'task',
                   'status': 'accepted', 'worktree': str(attempt_tree), 'branch': 'task-result',
                   'base_commit': self.run['base_commit'], 'candidate_commit': self.run['base_commit']}
        task = {'id': self.run['id'] + '/task', 'run_id': self.run['id'], 'status': 'integrated',
                'attempts': [attempt['id']]}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, 'attempt', attempt)
            self.runtime.store.put(db, 'task', task)
            self.runtime.store.put(db, 'cleanup_resource', resource_manifest(self.run, attempt))
        self.release(manager)
        manager.release(self.run['id'], attempt['id'], 'coordinator', 'The accepted worker stopped after integration.')
        plan = manager.plan(self.run['id'])
        self.assertEqual(len(plan['candidates']), 2, plan)
        status = self.finish(manager, plan)
        self.assertEqual(status['status'], 'complete', status)
        self.assertFalse(attempt_tree.exists())

    def test_crash_after_removal_reconciles_without_repeating_removal(self):
        manager = self.manager()
        self.release(manager)
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(manager.plan(self.run['id'])['id'], 'Test interrupted reconciliation.')
        original = manager._save
        class Interruption(BaseException):
            pass
        def stop_after_removal(current, item=None):
            if current['resources'][0]['stage'] == 'removed':
                raise Interruption()
            original(current, item)
        with patch.object(manager, '_save', side_effect=stop_after_removal):
            with self.assertRaises(Interruption):
                manager.execute(job['id'])
        self.assertFalse(self.tree.exists())
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'complete', status)
        self.assertEqual(status['resources'][0]['stage'], 'reconciled')

    def test_archive_checksum_failure_rejects_restore_and_preserves_destination(self):
        manager = self.manager()
        self.release(manager)
        status = self.finish(manager, manager.plan(self.run['id']))
        self.assertEqual(status['status'], 'complete', status)
        archive_id = status['resources'][0]['archive_id']
        with self.runtime.store.transaction() as db:
            archive = self.runtime.store.get(db, 'cleanup_archive', archive_id)
        directory = Path(archive['directory'])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'files.tar').stat().st_mode & 0o777, 0o600)
        with (directory / 'files.tar').open('ab') as stream:
            stream.write(b'corruption')
        destination = self.root / 'no-restore'
        with self.assertRaises(WorkflowError):
            manager.restore(archive_id, str(destination))
        self.assertFalse(destination.exists())

    def test_registered_output_failure_restores_exact_files(self):
        manager = self.manager()
        self.release(manager)
        output = self.tree / 'owned-output'
        output.write_bytes(b'generated data')
        from ub_execute.cleanup_inventory import file_hash
        with self.runtime.store.transaction() as db:
            resource = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])[0]
            resource['generated_artifacts'] = {'owned-output': file_hash(output)}
            self.runtime.store.put(db, 'cleanup_resource', resource)
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(manager.plan(self.run['id'])['id'], 'Test ordinary Git removal failure.')
        from ub_execute.cleanup import git as original_git
        def fail_remove(root, *args):
            if args[:2] == ('worktree', 'remove'):
                raise WorkflowError('Injected ordinary removal failure.')
            return original_git(root, *args)
        with patch('ub_execute.cleanup.git', side_effect=fail_remove):
            manager.execute(job['id'])
        self.assertEqual(manager.status(job['id'])['status'], 'blocked')
        self.assertEqual(output.read_bytes(), b'generated data')

    def test_hidden_index_flags_preserve_tracked_edits_without_changing_index(self):
        manager = self.manager()
        self.release(manager)
        for flag in ('assume-unchanged', 'skip-worktree'):
            with self.subTest(flag=flag):
                git(self.tree, 'update-index', '--' + flag, 'source')
                (self.tree / 'source').write_bytes(b'hidden valuable edit')
                before = git(self.tree, 'ls-files', '-v', 'source')
                self.assertFalse(git(self.tree, 'status', '--porcelain', '--untracked-files=no'))
                plan = manager.plan(self.run['id'])
                self.assertTrue(plan['blocked'], 'Cleanup must reject index flags that hide tracked edits.')
                self.assertFalse(plan['candidates'])
                self.assertEqual(git(self.tree, 'ls-files', '-v', 'source'), before)
                self.assertEqual((self.tree / 'source').read_bytes(), b'hidden valuable edit')
                git(self.tree, 'update-index', '--no-' + flag, 'source')
                git(self.tree, 'checkout', '--', 'source')

    def test_nested_registered_output_crash_restores_ancestor_metadata_and_resumes(self):
        manager = self.manager()
        self.release(manager)
        output = self.tree / 'output' / 'data'
        output.parent.mkdir()
        output.write_bytes(b'generated nested data')
        from ub_execute.cleanup_inventory import file_hash, inventory
        with self.runtime.store.transaction() as db:
            resource = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])[0]
            resource['generated_artifacts'] = {'output/data': file_hash(output)}
            self.runtime.store.put(db, 'cleanup_resource', resource)
        before = inventory(self.tree)
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(manager.plan(self.run['id'])['id'], 'Test interrupted nested output removal.')
        from ub_execute.cleanup import git as original_git
        class Interruption(BaseException):
            pass
        def interrupt_remove(root, *args):
            if args[:2] == ('worktree', 'remove'):
                raise Interruption()
            return original_git(root, *args)
        with patch('ub_execute.cleanup.git', side_effect=interrupt_remove):
            with self.assertRaises(Interruption):
                manager.execute(job['id'])
        self.assertEqual(inventory(self.tree), before, 'Owned rollback must restore directory metadata.')
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'complete', status)
        self.assertFalse(self.tree.exists())

    def test_nested_output_process_crash_resumes_from_checked_intermediate(self):
        manager = self.manager()
        self.release(manager)
        output = self.tree / 'output' / 'data'
        output.parent.mkdir()
        output.write_bytes(b'generated nested data')
        from ub_execute.cleanup_inventory import file_hash
        with self.runtime.store.transaction() as db:
            resource = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])[0]
            resource['generated_artifacts'] = {'output/data': file_hash(output)}
            self.runtime.store.put(db, 'cleanup_resource', resource)
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(manager.plan(self.run['id'])['id'], 'Test process death after owned unlink.')
        script = """
import os, sys
from unittest.mock import patch
from ub_execute.runtime import Runtime
from ub_execute.cleanup import CleanupManager, git
manager = CleanupManager(Runtime(sys.argv[1]))
def stop(root, *args):
    if args[:2] == ('worktree', 'remove'):
        os._exit(91)
    return git(root, *args)
with patch('ub_execute.cleanup.git', side_effect=stop):
    manager.execute(sys.argv[2])
"""
        child = subprocess.run([sys.executable, '-c', script, str(self.runtime.store.root), job['id']])
        self.assertEqual(child.returncode, 91)
        self.assertFalse(output.exists())
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'complete', status)
        self.assertFalse(self.tree.exists())

    def test_registered_output_recovery_preserves_external_changes(self):
        manager = self.manager()
        self.release(manager)
        output = self.tree / 'output' / 'data'
        output.parent.mkdir()
        output.write_bytes(b'owned generated data')
        from ub_execute.cleanup_inventory import file_hash
        with self.runtime.store.transaction() as db:
            resource = self.runtime.store.list(db, 'cleanup_resource', self.run['id'])[0]
            resource['generated_artifacts'] = {'output/data': file_hash(output)}
            self.runtime.store.put(db, 'cleanup_resource', resource)
        with patch.object(manager, '_launch', return_value=None):
            job = manager.apply(manager.plan(self.run['id'])['id'], 'Test external changes during failed removal.')
        from ub_execute.cleanup import git as original_git
        external_time = 1000000000000000000
        def external_change(root, *args):
            if args[:2] == ('worktree', 'remove'):
                (self.tree / '.env').write_text('late private data')
                os.utime(output.parent, ns=(external_time, external_time))
                raise WorkflowError('Injected removal failure with an external writer.')
            return original_git(root, *args)
        with patch('ub_execute.cleanup.git', side_effect=external_change):
            status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual((self.tree / '.env').read_text(), 'late private data')
        self.assertEqual(output.parent.stat().st_mtime_ns, external_time)
        self.assertTrue(self.tree.exists())
        self.assertTrue(status['resources'][0]['archive_id'])

    @unittest.skipUnless(sys.platform == 'darwin', 'The fixture uses macOS file access controls.')
    def test_macos_access_controls_block_removal(self):
        manager = self.manager()
        self.release(manager)
        user = pwd.getpwuid(os.getuid()).pw_name
        subprocess.run(['chmod', '+a', 'user:' + user + ' allow read', str(self.tree / 'source')], check=True)
        plan = manager.plan(self.run['id'])
        self.assertTrue(plan['blocked'], 'Cleanup must preserve files with unsupported access controls.')
        self.assertFalse(plan['candidates'])

    def test_absent_authorization_does_not_remove(self):
        manager = self.manager()
        self.release(manager)
        with self.runtime.store.transaction() as db:
            self.run['bundle']['cleanup_policy'] = {}
            self.runtime.store.put(db, 'run', self.run)
        plan = manager.plan(self.run['id'])
        with self.assertRaises(WorkflowError):
            manager.apply(plan['id'], 'No policy authorizes this removal.')
        self.assertTrue(self.tree.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'The fixture uses macOS directory access controls.')
    def test_worktree_directory_access_controls_block_removal(self):
        manager = self.manager()
        self.release(manager)
        user = pwd.getpwuid(os.getuid()).pw_name
        subprocess.run(['chmod','+a','user:' + user + ' allow read',str(self.tree)],check=True)
        plan = manager.plan(self.run['id'])
        self.assertFalse(plan['candidates'], 'Cleanup must preserve unsupported access controls on the worktree directory.')
        self.assertTrue(self.tree.exists())


if __name__ == '__main__':
    unittest.main()
