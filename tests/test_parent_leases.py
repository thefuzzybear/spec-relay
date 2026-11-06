"""Shared environment leases must serialize readers, writers, and retries."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ub_execute.parent_leases import ParentLeases
from ub_execute.store import WorkflowError


class ParentLeaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='relay-lease-regression-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch('ub_execute.parent_leases.tempfile.gettempdir', return_value=str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.first = ParentLeases()
        self.second = ParentLeases()

    def test_exclusive_owner_blocks_new_readers_and_shared_owners_coexist(self):
        exclusive = self.first.acquire([('database:fixture','exclusive')], {'operation':'schema_apply'})
        with self.assertRaises(WorkflowError):
            self.second.acquire([('database:fixture','shared')], {'operation':'read_check'})
        self.first.release(exclusive)
        one = self.first.acquire([('database:fixture','shared')], {'operation':'first_reader'})
        two = self.second.acquire([('database:fixture','shared')], {'operation':'second_reader'})
        self.assertEqual(len(self.first.active(['database:fixture'])),2)
        with self.assertRaises(WorkflowError):
            self.second.acquire([('database:fixture','exclusive')], {'operation':'schema_apply'})
        self.first.release(one)
        self.second.release(two)
        self.assertEqual(self.first.active(['database:fixture']),[])

    def test_data_writer_completion_retries_once_and_rejects_another_resource(self):
        lease = self.first.acquire([('database:fixture','exclusive')], {'operation':'data_test'})
        generation = self.first.finish_data_writer(lease,'database:fixture')
        self.assertEqual(generation,1)
        self.assertEqual(self.second.finish_data_writer(lease,'database:fixture'),generation)
        self.assertEqual(self.first.data_generation('database:fixture'),1)
        with self.assertRaises(WorkflowError):
            self.second.finish_data_writer(lease,'database:other')
        self.assertEqual(self.first.active(['database:fixture']),[])

    def test_missing_or_shared_lease_cannot_complete_a_data_writer(self):
        with self.assertRaises(WorkflowError):
            self.first.finish_data_writer('missing','database:fixture')
        reader = self.first.acquire([('database:fixture','shared')], {'operation':'read_check'})
        with self.assertRaises(WorkflowError):
            self.second.finish_data_writer(reader,'database:fixture')
        self.assertEqual(self.first.data_generation('database:fixture'),0)
        self.first.release(reader)
