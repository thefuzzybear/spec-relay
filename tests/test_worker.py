import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_execution
from ub_execute.worker import run_job


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_execution.ExecutionTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def queued(self, argv, kind="test", phase="green", timeout=3):
        fixture = self.fixture
        fixture.bundle["checks"]["test"] = {"argv": argv, "kind": kind, "timeout_seconds": timeout}
        if kind != "test":
            fixture.bundle["tasks"][0]["tdd_exception"] = "This fixture checks the quality report transport."
        run = fixture.create()
        attempt = fixture.claim(run)
        with patch.object(fixture.runtime, "_launch_job", side_effect=lambda job: job):
            job = fixture.runtime.check_start(run_id=run["id"], attempt_id=attempt["id"], check_id="test", phase=phase, reason="Exercise the check worker.")
        return job, attempt

    def execute(self, job):
        run_job(self.fixture.state, job["id"])
        return self.fixture.runtime.job_status(job["id"])

    def test_records_output_and_exit_status(self):
        job, _ = self.queued([sys.executable, "-c", "print('proof')"])
        result = self.execute(job)
        self.assertEqual(result["status"], "passed")
        self.assertIn("proof", result["log_tails"]["stdout.log"])

    def test_missing_executable_is_an_environment_error(self):
        job, _ = self.queued(["/missing/ub-execute-command"])
        self.assertEqual(self.execute(job)["status"], "error")

    def test_timeout_does_not_count_as_a_test_failure(self):
        job, _ = self.queued([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.03)
        self.assertEqual(self.execute(job)["status"], "timeout")

    def test_changed_source_prevents_execution(self):
        job, attempt = self.queued([sys.executable, "-c", "raise AssertionError('must not execute')"])
        (Path(attempt["worktree"]) / "calc.py").write_text("# Changed after dispatch.\n")
        self.assertEqual(self.execute(job)["status"], "invalidated")

    def test_quality_reports_are_required_and_bound_to_the_tool(self):
        report = json.dumps({"version": 1, "tool": "dryer", "status": "failed", "candidates": [{"score": 1}]})
        job, _ = self.queued([sys.executable, "-c", f"print({report!r})"], kind="dryer", phase="quality")
        self.assertEqual(self.execute(job)["status"], "failed")

    def test_non_json_quality_output_is_an_error(self):
        job, _ = self.queued([sys.executable, "-c", "print('No report')"], kind="dryer", phase="quality")
        self.assertEqual(self.execute(job)["status"], "error")

    def test_wrong_tool_report_cannot_pass(self):
        report = json.dumps({"version": 1, "tool": "crapper", "status": "passed"})
        job, _ = self.queued([sys.executable, "-c", f"print({report!r})"], kind="dryer", phase="quality")
        self.assertEqual(self.execute(job)["status"], "error")

    def test_nonzero_command_cannot_claim_passing_quality(self):
        report = json.dumps({"version": 1, "tool": "dryer", "status": "passed"})
        job, _ = self.queued([sys.executable, "-c", f"print({report!r}); raise SystemExit(1)"], kind="dryer", phase="quality")
        self.assertEqual(self.execute(job)["status"], "error")

    def test_cancelled_job_does_not_execute(self):
        job, _ = self.queued([sys.executable, "-c", "raise RuntimeError('must not execute')"])
        self.fixture.runtime.job_cancel(job["id"], reason="Cancel before the worker starts.")
        self.assertEqual(self.execute(job)["status"], "cancelled")

    def test_pause_between_queue_and_start_prevents_execution(self):
        job, _ = self.queued([sys.executable, "-c", "raise RuntimeError('must not execute')"])
        with self.fixture.runtime.store.transaction() as db:
            run = self.fixture.runtime.store.get(db, "run", job["run_id"])
            run["status"] = "paused"
            self.fixture.runtime.store.put(db, "run", run)
        self.assertEqual(self.execute(job)["status"], "cancelled")

    def test_changed_runtime_prevents_execution(self):
        job, _ = self.queued([sys.executable, "-c", "raise RuntimeError('must not execute')"])
        with patch("ub_execute.worker.runner_fingerprint", return_value="changed"):
            self.assertEqual(self.execute(job)["status"], "invalidated")

    def test_source_changes_during_a_check_invalidate_the_result(self):
        code = "from pathlib import Path; Path('calc.py').write_text('# Changed by the check.\\n')"
        job, _ = self.queued([sys.executable, "-c", code])
        self.assertEqual(self.execute(job)["status"], "invalidated")


if __name__ == "__main__":
    unittest.main()
