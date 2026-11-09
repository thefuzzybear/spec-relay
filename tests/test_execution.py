import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import os
import signal

from ub_execute.runtime import Runtime, WorkflowError


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Execution Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "calc.py").write_text("def add(a, b):\n    return 0\n")
        (self.repo / "SPEC.md").write_text("# Addition\n\nREQ-1: Add two integers.\n")
        (self.repo / ".gitignore").write_text("__pycache__/\n.metrics/\n.coverage\ncoverage/\ntarget/\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "Establish the example")
        self.state = self.root / "state"
        self.runtime = Runtime(self.state)
        self.bundle = {
            "version": 1,
            "spec": str(self.repo / "SPEC.md"),
            "repository": str(self.repo),
            "requirements": [{"id": "REQ-1", "text": "Add two integers."}],
            "readiness": {"status": "ready", "open_findings": []},
            "checks": {"test": {"argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "kind": "test"}},
            "tasks": [{"id": "add", "title": "Add two integers", "requirements": ["REQ-1"],
                       "acceptance": ["Adding 2 and 3 returns 5."], "depends_on": [],
                       "scope": ["calc.py", "tests/"], "test_paths": ["tests/"], "checks": ["test"]}],
            "final_checks": ["test"],
        }

    def create(self):
        return self.runtime.run_create(bundle=self.bundle, reason="Execute the reviewed addition spec.")

    def register(self, run, agent_id, role, target_id, model=None):
        requirements = self.runtime.agent_requirements(run["id"], role, target_id)
        if requirements["policy_mode"] == "legacy":
            return None
        return self.runtime.agent_register(run["id"], agent_id, model or requirements["model"], role, target_id,
                                           reason="Assign the native agent to its current model role.")

    def claim(self, run, agent_id="worker-1"):
        self.register(run, agent_id, "implementation", "add")
        return self.runtime.task_claim(run_id=run["id"], task_id="add", agent_id=agent_id, reason="Implement addition.")

    def check(self, run, attempt, phase):
        job = self.runtime.check_start(run_id=run["id"], attempt_id=attempt["id"], check_id="test", phase=phase, reason="Check addition.")
        for _ in range(150):
            result = self.runtime.job_status(job_id=job["id"])
            if result["status"] not in {"queued", "running"}:
                return result
            time.sleep(0.03)
        self.fail("The check did not finish.")

    def implement(self, run, attempt):
        tree = Path(attempt["worktree"])
        (tree / "tests").mkdir()
        (tree / "tests/test_calc.py").write_text("import unittest\nfrom calc import add\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        red = self.check(run, attempt, "red")
        self.assertEqual(red["status"], "failed")
        self.runtime.red_confirm(job_id=red["id"], explanation="The assertion expected 5 and received 0.", agent_id="worker-1")
        (tree / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        self.assertEqual(self.check(run, attempt, "green")["status"], "passed")
        git(tree, "add", ".")
        git(tree, "commit", "-qm", "Implement addition and prove the result")
        return tree

    def review(self, run, attempt, decision="accept"):
        self.register(run, "reviewer-1", "task_review", attempt["id"])
        return self.runtime.review_record(run_id=run["id"], attempt_id=attempt["id"], reviewer_id="reviewer-1",
                                          decision=decision, findings=[], reason="The requirement and behavior test agree.")

    def test_complete_flow_survives_runtime_restart(self):
        run = self.create()
        attempt = self.claim(run)
        self.implement(run, attempt)
        self.runtime = Runtime(self.state)
        self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Submit the tested change.")
        self.review(run, attempt)
        self.runtime.task_accept(run_id=run["id"], attempt_id=attempt["id"], reason="All task evidence is current.")
        self.runtime.task_integrate(run_id=run["id"], task_id="add", reason="Integrate the accepted change.")
        final = self.runtime.check_start(run_id=run["id"], check_id="test", phase="final", reason="Check the combined revision.")
        for _ in range(150):
            if self.runtime.job_status(job_id=final["id"])["status"] not in {"queued", "running"}:
                break
            time.sleep(0.03)
        self.register(run, "final-reviewer", "final_review", run["id"])
        self.runtime.run_review_record(run_id=run["id"], reviewer_id="final-reviewer", verified_requirements=["REQ-1"], findings=[], reason="The combined behavior meets the spec.")
        result = self.runtime.run_complete(run_id=run["id"], reason="The integrated behavior meets the spec.")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), run["base_commit"])
        self.assertTrue(self.runtime.run_events(run_id=run["id"])["events"])

    def test_two_concurrent_claims_have_one_owner(self):
        run = self.create()
        def claim(_):
            try:
                return self.claim(run)["id"]
            except WorkflowError:
                return None
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            claims = list(pool.map(claim, range(2)))
        self.assertEqual(sum(value is not None for value in claims), 1)

    def test_changed_source_invalidates_passing_evidence(self):
        run = self.create()
        attempt = self.claim(run)
        tree = self.implement(run, attempt)
        (tree / "calc.py").write_text("def add(a, b):\n    return 9\n")
        git(tree, "add", ".")
        git(tree, "commit", "-qm", "Break the candidate after its check")
        with self.assertRaisesRegex(WorkflowError, "evidence|check"):
            self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Attempt stale submission.")

    def test_review_is_bound_to_the_candidate(self):
        run = self.create()
        attempt = self.claim(run)
        tree = self.implement(run, attempt)
        self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Submit the candidate.")
        self.review(run, attempt)
        (tree / "calc.py").write_text("def add(a, b):\n    return 9\n")
        git(tree, "add", ".")
        git(tree, "commit", "-qm", "Change the reviewed revision")
        with self.assertRaises(WorkflowError):
            self.runtime.task_accept(run_id=run["id"], attempt_id=attempt["id"], reason="Attempt stale acceptance.")

    def test_test_edits_invalidate_the_red_cycle(self):
        run = self.create()
        attempt = self.claim(run)
        tree = self.implement(run, attempt)
        (tree / "tests/test_calc.py").write_text("import unittest\nclass Fake(unittest.TestCase):\n    def test_fake(self):\n        self.assertTrue(True)\n")
        self.assertEqual(self.check(run, attempt, "green")["status"], "passed")
        git(tree, "add", ".")
        git(tree, "commit", "-qm", "Replace the original behavior test")
        with self.assertRaisesRegex(WorkflowError, "red|test"):
            self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Attempt to replace the red test.")

    def test_missing_requirement_and_cycles_fail_intake(self):
        self.bundle["tasks"][0]["requirements"] = []
        with self.assertRaisesRegex(WorkflowError, "requirement"):
            self.create()
        self.bundle["tasks"][0]["requirements"] = ["REQ-1"]
        self.bundle["tasks"][0]["depends_on"] = ["add"]
        with self.assertRaisesRegex(WorkflowError, "cycl"):
            self.create()

    def test_open_blocker_prevents_execution(self):
        self.bundle["readiness"]["open_findings"] = [{"severity": "blocker", "text": "The behavior is undecided."}]
        with self.assertRaisesRegex(WorkflowError, "finding|ready"):
            self.create()

    def test_pause_prevents_claims_and_check_dispatch(self):
        run = self.create()
        self.runtime.run_control(run_id=run["id"], action="pause", reason="The user paused execution.")
        with self.assertRaises(WorkflowError):
            self.claim(run)

    def test_recovery_preserves_uncommitted_work(self):
        run = self.create()
        attempt = self.claim(run)
        tree = Path(attempt["worktree"])
        (tree / "calc.py").write_text("# Work in progress\n")
        self.assertTrue(self.runtime.run_recover(run["id"])["attempts"][0]["dirty"])
        self.runtime.task_abandon(run_id=run["id"], attempt_id=attempt["id"], reason="The worker stopped.")
        recovered = self.claim(run, "worker-2")
        self.assertNotEqual(recovered["worktree"], str(tree))
        self.assertEqual((tree / "calc.py").read_text(), "# Work in progress\n")

    def test_command_timeout_is_not_a_behavior_failure(self):
        self.bundle["checks"]["test"]["argv"] = [sys.executable, "-c", "import time; time.sleep(10)"]
        self.bundle["checks"]["test"]["timeout_seconds"] = 0.15
        run = self.create()
        attempt = self.claim(run)
        result = self.check(run, attempt, "red")
        self.assertEqual(result["status"], "timeout")
        with self.assertRaises(WorkflowError):
            self.runtime.red_confirm(job_id=result["id"], explanation="A timeout is not a failing assertion.", agent_id="worker-1")

    def test_generated_metrics_do_not_block_a_source_submission(self):
        run = self.create()
        attempt = self.claim(run)
        tree = self.implement(run, attempt)
        # Simulate a target repository without generated metrics in its ignore rules.
        (tree / "target/report").mkdir(parents=True)
        (tree / "target/report/result.json").write_text("{}")
        result = self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Submit source while preserving generated artifacts.")
        self.assertEqual(result["status"], "submitted")

    def test_final_completion_requires_a_specification_review(self):
        run = self.create()
        attempt = self.claim(run)
        self.implement(run, attempt)
        self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Submit the candidate.")
        self.review(run, attempt)
        self.runtime.task_accept(run_id=run["id"], attempt_id=attempt["id"], reason="Accept the reviewed candidate.")
        self.runtime.task_integrate(run_id=run["id"], task_id="add", reason="Integrate the candidate.")
        with self.assertRaises(WorkflowError):
            self.runtime.run_complete(run_id=run["id"], reason="Attempt completion without final evidence.")

    def test_new_source_after_green_is_not_ignored(self):
        run = self.create()
        attempt = self.claim(run)
        tree = self.implement(run, attempt)
        (tree / "new.py").write_text("raise RuntimeError('untested')\n")
        with self.assertRaises(WorkflowError):
            self.runtime.task_submit(run_id=run["id"], attempt_id=attempt["id"], reason="Attempt a dirty submission.")

    def test_task_context_preserves_spec_and_review_artifacts(self):
        report = self.root / "review.md"
        report.write_text("The specification is ready.\n")
        self.bundle["review_artifacts"] = [str(report)]
        run = self.create()
        attempt = self.claim(run)
        report.write_text("Changed after intake.\n")
        context = self.runtime.task_context(run["id"], "add", attempt["id"])
        self.assertEqual(Path(context["review_artifacts"][0]["snapshot"]).read_text(), "The specification is ready.\n")
        self.assertIn("REQ-1", Path(context["spec_snapshot"]).read_text())
        self.assertEqual(self.runtime.run_status(run["id"])["tasks"][0]["status"], "running")
        note = self.runtime.action_record(run["id"], "worker-1", "Read repository instructions.", "Establish the task boundaries.", {"path": "AGENTS.md"})
        self.assertEqual(self.runtime.run_events(run["id"])["events"][-1]["sequence"], note["sequence"])

    def test_existing_runs_replay_without_new_optional_fields(self):
        run = self.create()
        with self.runtime.store.transaction() as db:
            stored = self.runtime.store.get(db, "run", run["id"])
            stored.pop("review_artifacts")
            self.runtime.store.put(db, "run", stored)
        self.assertEqual(self.runtime.task_context(run["id"], "add")["review_artifacts"], [])

    def test_a_runtime_change_invalidates_old_passing_checks(self):
        from unittest.mock import patch
        run = self.create()
        attempt = self.claim(run)
        self.implement(run, attempt)
        with patch("ub_execute.runtime.runner_fingerprint", return_value="another-runtime"):
            with self.assertRaisesRegex(WorkflowError, "evidence"):
                self.runtime.task_submit(run["id"], attempt["id"], reason="Attempt submission after the runtime changes.")

    def test_recovery_stops_an_orphan_after_a_worker_crash(self):
        from ub_execute.processes import is_locked
        proof = self.root / "orphan-proof.txt"
        code = f"import time; from pathlib import Path; time.sleep(1.5); Path({str(proof)!r}).write_text('The orphan continued.'); time.sleep(10)"
        self.bundle["checks"]["test"]["argv"] = [sys.executable, "-c", code]
        run = self.create()
        attempt = self.claim(run)
        job = self.runtime.check_start(run["id"], "test", "green", reason="Exercise interrupted process recovery.", attempt_id=attempt["id"])
        for _ in range(100):
            job = self.runtime.job_status(job["id"])
            if job.get("command_pid"):
                break
            time.sleep(0.03)
        self.assertTrue(job.get("command_pid"))
        os.kill(job["pid"], signal.SIGKILL)
        for _ in range(100):
            if not self.runtime._worker_alive(job):
                break
            time.sleep(0.03)
        with self.runtime.store.transaction() as db:
            job = self.runtime.store.get(db, "job", job["id"])
            job["created_at"] -= 10
            self.runtime.store.put(db, "job", job)
        result = Runtime(self.state).job_status(job["id"])
        self.assertEqual(result["status"], "interrupted")
        for _ in range(100):
            if not is_locked(Path(job["directory"]) / "command.lock"):
                break
            time.sleep(0.03)
        self.assertFalse(is_locked(Path(job["directory"]) / "command.lock"))
        time.sleep(1.6)
        self.assertFalse(proof.exists(), "The orphan command continued after recovery.")


if __name__ == "__main__":
    unittest.main()
