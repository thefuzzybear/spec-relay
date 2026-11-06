"""A valid foreign archive must not replace the recorded recovery copy."""

from pathlib import Path
import shutil
import tarfile
import unittest
from unittest.mock import patch

import test_cleanup
from ub_execute.store import WorkflowError
from ub_execute.workspace import git


class CleanupArchiveBindingTests(unittest.TestCase):
    def fixture(self, other=False):
        fixture = test_cleanup.CleanupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if other:
            (fixture.tree / 'source').write_bytes(b'foreign committed content')
            (fixture.tree / '.env').write_text('foreign private data')
            git(fixture.tree, 'add', 'source')
            git(fixture.tree, 'commit', '-m', 'Other disposable fixture')
            fixture.run['integration_commit'] = git(fixture.tree, 'rev-parse', 'HEAD')
            with fixture.runtime.store.transaction() as db:
                fixture.runtime.store.put(db, 'run', fixture.run)
        manager = fixture.manager()
        fixture.release(manager)
        return fixture, manager

    @staticmethod
    def queued(fixture, manager):
        with patch.object(manager, '_launch', return_value=None):
            return manager.apply(manager.plan(fixture.run['id'])['id'], 'Test the recovery archive identity.')

    @staticmethod
    def archive(fixture, archive_id):
        with fixture.runtime.store.transaction() as db:
            return fixture.runtime.store.get(db, 'cleanup_archive', archive_id)

    def foreign_archive(self):
        fixture, manager = self.fixture(other=True)
        job = self.queued(fixture, manager)
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'complete', status)
        return self.archive(fixture, status['resources'][0]['archive_id'])

    @staticmethod
    def substitute(original, foreign):
        for name in ('manifest.json', 'history.bundle', 'files.tar'):
            shutil.copy2(Path(foreign['directory']) / name, Path(original['directory']) / name)

    def test_uninterrupted_removal_rejects_substitution_after_archive_check(self):
        foreign = self.foreign_archive()
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        original_start = manager._start_removal

        def replace_before_removal(current, item):
            original_start(current, item)
            original = self.archive(fixture, item['archive_id'])
            self.substitute(original, foreign)

        with patch.object(manager, '_start_removal', side_effect=replace_before_removal):
            status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked', 'A changed recovery copy must block ordinary removal.')
        self.assertEqual((fixture.tree / 'source').read_bytes(), b'source\x00data')

    def test_resumed_removal_rejects_an_internally_valid_foreign_archive(self):
        foreign = self.foreign_archive()
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        original_save = manager._save

        class Interruption(BaseException):
            pass

        def stop_after_verification(current, item=None):
            original_save(current, item)
            if current['resources'][0]['stage'] == 'restore_verified':
                raise Interruption()

        with patch.object(manager, '_save', side_effect=stop_after_verification):
            with self.assertRaises(Interruption):
                manager.execute(job['id'])
        interrupted = manager.status(job['id'])
        original = self.archive(fixture, interrupted['resources'][0]['archive_id'])
        self.substitute(original, foreign)
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked', 'Resume must check the retained archive identity.')
        self.assertEqual((fixture.tree / 'source').read_bytes(), b'source\x00data')

    def test_restore_rejects_substitution_without_creating_destination(self):
        foreign = self.foreign_archive()
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'complete', status)
        archive_id = status['resources'][0]['archive_id']
        original = self.archive(fixture, archive_id)
        self.substitute(original, foreign)
        destination = fixture.root / 'restored'
        with self.assertRaises(WorkflowError):
            manager.restore(archive_id, str(destination))
        self.assertFalse(destination.exists())

    def test_reconciliation_rejects_substitution_after_git_removal(self):
        foreign = self.foreign_archive()
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        original_save = manager._save

        class Interruption(BaseException):
            pass

        def stop_after_removal(current, item=None):
            if current['resources'][0]['stage'] == 'removed':
                raise Interruption()
            original_save(current, item)

        with patch.object(manager, '_save', side_effect=stop_after_removal):
            with self.assertRaises(Interruption):
                manager.execute(job['id'])
        interrupted = manager.status(job['id'])
        self.assertFalse(fixture.tree.exists())
        original = self.archive(fixture, interrupted['resources'][0]['archive_id'])
        self.substitute(original, foreign)
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked', 'Reconciliation must not certify a foreign recovery copy.')
        with fixture.runtime.store.transaction() as db:
            resource = fixture.runtime.store.list(db, 'cleanup_resource', fixture.run['id'])[0]
        self.assertFalse(resource.get('removed', False))

    def test_malformed_manifest_recovery_blocks_and_releases_cleanup_lease(self):
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        original_save = manager._save

        class Interruption(BaseException):
            pass

        def stop_after_verification(current, item=None):
            original_save(current, item)
            if current['resources'][0]['stage'] == 'restore_verified':
                raise Interruption()

        with patch.object(manager, '_save', side_effect=stop_after_verification):
            with self.assertRaises(Interruption):
                manager.execute(job['id'])
        interrupted = manager.status(job['id'])
        original = self.archive(fixture, interrupted['resources'][0]['archive_id'])
        (Path(original['directory']) / 'manifest.json').write_text('{truncated')
        status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual((fixture.tree / 'source').read_bytes(), b'source\x00data')
        with fixture.runtime.store.transaction() as db:
            lease = fixture.runtime.store.list(db, 'cleanup_lease', fixture.run['id'])[0]
        self.assertFalse(lease['active'])

    def test_truncated_tar_during_restore_check_preserves_source_and_releases_lease(self):
        fixture, manager = self.fixture()
        job = self.queued(fixture, manager)
        original_open = tarfile.open

        def truncate_before_read(name, mode='r', *args, **kwargs):
            if mode == 'r':
                path = Path(name)
                path.write_bytes(path.read_bytes()[:100])
            return original_open(name, mode, *args, **kwargs)

        with patch('ub_execute.cleanup_archive.tarfile.open', side_effect=truncate_before_read):
            status = manager.execute(job['id'])
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual((fixture.tree / 'source').read_bytes(), b'source\x00data')
        with fixture.runtime.store.transaction() as db:
            lease = fixture.runtime.store.list(db, 'cleanup_lease', fixture.run['id'])[0]
        self.assertFalse(lease['active'])


if __name__ == '__main__':
    unittest.main()
