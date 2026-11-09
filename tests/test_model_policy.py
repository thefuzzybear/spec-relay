import sys
import tempfile
import unittest
from pathlib import Path

from ub_execute.runtime import Runtime, WorkflowError


class ModelPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        import subprocess

        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

        self.git = git
        git("init", "-q")
        git("config", "user.name", "Model Policy Test")
        git("config", "user.email", "test@example.invalid")
        (self.repo / "SPEC.md").write_text("# Policy\n")
        git("add", ".")
        git("commit", "-qm", "Create model policy fixture")
        self.runtime = Runtime(self.root / "state")
        self.bundle = {
            "version": 1,
            "spec": str(self.repo / "SPEC.md"),
            "repository": str(self.repo),
            "requirements": [{"id": "R1", "text": "Use model assignments."}],
            "readiness": {"status": "ready", "open_findings": []},
            "checks": {"unit": {"argv": [sys.executable, "-c", "pass"], "kind": "test"}},
            "tasks": [{"id": "policy", "title": "Apply model policy", "requirements": ["R1"],
                       "acceptance": ["A model assignment gates each role."], "depends_on": [],
                       "scope": ["src/", "tests/"], "test_paths": ["tests/"], "checks": ["unit"]}],
            "final_checks": ["unit"],
        }

    def test_new_run_normalizes_default_policy_and_registration_gates_claim(self):
        validation = self.runtime.plan_validate(self.bundle)
        run = self.runtime.run_create(self.bundle, "Create a run with model defaults.")
        expected = {
            "implementation": "gpt-6-luna",
            "task_review": "gpt-6.1-sol",
            "schema_review": "gpt-6.1-sol",
            "final_review": "gpt-6.1-sol",
        }
        self.assertEqual(validation["model_policy"], expected)
        self.assertEqual(self.runtime.run_status(run["id"])["model_policy"], expected)
        with self.assertRaisesRegex(WorkflowError, "register|assignment"):
            self.runtime.task_claim(run["id"], "policy", "worker", "Claim without a model assignment.")

    def test_agent_requirements_and_wrong_model_registration(self):
        run = self.runtime.run_create(self.bundle, "Create a run with model defaults.")
        requirements = self.runtime.agent_requirements(run["id"], "implementation", "policy")
        self.assertEqual(requirements["model"], "gpt-6-luna")
        self.assertEqual(requirements["spawn"], {"model": "gpt-6-luna", "fork_turns": "none"})
        context = self.runtime.task_context(run["id"], "policy")
        self.assertEqual(context["implementation_agent"]["model"], "gpt-6-luna")
        self.assertEqual(context["implementation_agent"]["target"]["base_commit"], run["base_commit"])
        with self.assertRaisesRegex(WorkflowError, "model"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6.1-sol", "implementation", "policy",
                                        reason="Reject a model mismatch.")

    def test_registration_is_idempotent_for_one_current_assignment(self):
        run = self.runtime.run_create(self.bundle, "Create an idempotent registration check.")
        first = self.runtime.agent_register(run["id"], "same-worker", "gpt-6-luna", "implementation", "policy",
                                            reason="Register the worker once.")
        second = self.runtime.agent_register(run["id"], "same-worker", "gpt-6-luna", "implementation", "policy",
                                             reason="Repeat the same current assignment.")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.runtime.run_status(run["id"])["agent_assignments"]), 1)

    def test_luna_agent_can_keep_its_model_across_distinct_tasks(self):
        self.bundle["requirements"].append({"id": "R2", "text": "Keep the same model for the second task."})
        self.bundle["tasks"].append({"id": "second", "title": "Apply the second task", "requirements": ["R2"],
                                     "acceptance": ["The second task uses Luna."], "depends_on": [],
                                     "scope": ["src/", "tests/"], "test_paths": ["tests/"], "checks": ["unit"]})
        run = self.runtime.run_create(self.bundle, "Create a cross-task model identity check.")
        self.runtime.agent_register(run["id"], "same-worker", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign Luna to the first task.")
        first = self.runtime.task_claim(run["id"], "policy", "same-worker", "Claim the first task.")
        self.runtime.agent_register(run["id"], "same-worker", "gpt-6-luna", "implementation", "second",
                                    reason="Keep Luna on the second task.")
        second = self.runtime.task_claim(run["id"], "second", "same-worker", "Claim the second task.")
        self.assertNotEqual(first["worktree"], second["worktree"])

    def test_registration_rejects_paused_run(self):
        run = self.runtime.run_create(self.bundle, "Create a paused run validation check.")
        self.runtime.run_control(run["id"], "pause", "Pause before model registration.")
        with self.assertRaisesRegex(WorkflowError, "paused"):
            self.runtime.agent_register(run["id"], "paused-worker", "gpt-6-luna", "implementation", "policy",
                                        reason="Reject model registration while paused.")
        with self.assertRaisesRegex(WorkflowError, "paused"):
            self.runtime.task_escalate(run["id"], "policy", "orchestrator", "Reject escalation while paused.")

    def test_host_metadata_requires_a_nonempty_reference(self):
        run = self.runtime.run_create(self.bundle, "Create a host metadata reference check.")
        with self.assertRaisesRegex(WorkflowError, "reference"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                        reported_model="gpt-6-luna", model_source="host_metadata",
                                        reason="Reject host model metadata without a reference.")
        assignment = self.runtime.agent_register(run["id"], "worker-with-metadata", "gpt-6-luna", "implementation", "policy",
                                                 reported_model="gpt-6-luna", model_source="host_metadata",
                                                 model_reference="native-agent://worker-with-metadata",
                                                 reason="Record the referenced host model metadata.")
        self.assertEqual(assignment["model_reference"], "native-agent://worker-with-metadata")

    def test_model_source_without_reported_model_is_rejected(self):
        run = self.runtime.run_create(self.bundle, "Create a partial provenance rejection check.")
        with self.assertRaisesRegex(WorkflowError, "source|reported model"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                        model_source="agent_report", reason="Reject a source without a report.")

    def test_old_legacy_task_without_generation_reports_zero(self):
        run = self.runtime.run_create(self.bundle, "Create a legacy task shape check.")
        with self.runtime.store.transaction() as db:
            stored_run = self.runtime.store.get(db, "run", run["id"])
            stored_run["bundle"].pop("model_policy")
            self.runtime.store.put(db, "run", stored_run)
            task = self.runtime.store.get(db, "task", run["id"] + "/policy")
            task.pop("generation")
            self.runtime.store.put(db, "task", task)
        requirements = self.runtime.agent_requirements(run["id"], "implementation", "policy")
        context = self.runtime.task_context(run["id"], "policy")
        self.assertEqual(requirements["target"]["generation"], 0)
        self.assertEqual(context["implementation_agent"]["target"]["generation"], 0)

    def test_stale_registration_cannot_be_refreshed_for_same_target(self):
        run = self.runtime.run_create(self.bundle, "Create a stale registration replacement check.")
        self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign the worker at the first generation.")
        with self.runtime.store.transaction() as db:
            task = self.runtime.store.get(db, "task", run["id"] + "/policy")
            task["generation"] = 1
            self.runtime.store.put(db, "task", task)
        with self.assertRaisesRegex(WorkflowError, "new native agent ID"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                        reason="Reject a stale registration refresh.")

    def test_registration_rejects_unknown_role_and_target(self):
        run = self.runtime.run_create(self.bundle, "Create a target validation check.")
        with self.assertRaisesRegex(WorkflowError, "role"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "unknown", "policy",
                                        reason="Reject an unknown role.")
        with self.assertRaises(WorkflowError):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "missing",
                                        reason="Reject an unknown task target.")

    def test_reported_model_mismatch_is_rejected(self):
        run = self.runtime.run_create(self.bundle, "Create a run with model defaults.")
        with self.assertRaisesRegex(WorkflowError, "reported|model"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                        reported_model="gpt-6.1-sol", model_source="host_metadata",
                                        model_reference="agent://worker", reason="Reject mismatched metadata.")

    def test_model_reference_must_be_text(self):
        run = self.runtime.run_create(self.bundle, "Create a metadata reference validation check.")
        with self.assertRaisesRegex(WorkflowError, "reference"):
            self.runtime.agent_register(run["id"], "worker", "gpt-6-luna", "implementation", "policy",
                                        reported_model="gpt-6-luna", model_source="agent_report",
                                        model_reference={"id": "invalid"}, reason="Reject a non-text metadata reference.")

    def test_policy_rejects_unknown_roles_and_unavailable_models(self):
        self.bundle["model_policy"] = {"implementation": "gpt-6-luna", "task_review": "gpt-6.1-sol",
                                       "schema_review": "gpt-6.1-sol", "final_review": "gpt-6.1-sol",
                                       "fallback": "gpt-6.1-sol"}
        with self.assertRaisesRegex(WorkflowError, "unknown|missing|policy"):
            self.runtime.plan_validate(self.bundle)

    def test_native_agent_cannot_change_models_between_task_assignments(self):
        self.bundle["requirements"].append({"id": "R2", "text": "Keep model identity stable."})
        self.bundle["tasks"].append({"id": "second", "title": "Apply the second task", "requirements": ["R2"],
                                     "acceptance": ["The second task uses its assigned model."], "depends_on": [],
                                     "scope": ["src/", "tests/"], "test_paths": ["tests/"], "checks": ["unit"]})
        run = self.runtime.run_create(self.bundle, "Create a two-task model identity check.")
        self.runtime.agent_register(run["id"], "same-native-id", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign Luna to the first task.")
        self.runtime.task_claim(run["id"], "policy", "same-native-id", "Claim the first task.")
        self.runtime.task_escalate(run["id"], "second", "orchestrator", "Escalate the pending second task.")
        with self.assertRaisesRegex(WorkflowError, "model|agent ID"):
            self.runtime.agent_register(run["id"], "same-native-id", "gpt-6.1-sol", "implementation", "second",
                                        reason="Reject a changed model for the same native agent.")

    def test_review_agent_cannot_become_an_escalated_implementation_agent(self):
        self.bundle["requirements"].append({"id": "R2", "text": "Keep review roles independent."})
        self.bundle["tasks"].append({"id": "second", "title": "Apply the second task", "requirements": ["R2"],
                                     "acceptance": ["The second task uses its assigned model."], "depends_on": [],
                                     "scope": ["src/", "tests/"], "test_paths": ["tests/"], "checks": ["unit"]})
        run = self.runtime.run_create(self.bundle, "Create a two-task reviewer independence check.")
        attempt = {"id": "submitted-fixture", "run_id": run["id"], "task_id": "policy", "status": "submitted",
                   "agent_id": "prior-worker", "candidate_commit": "candidate-1", "candidate_fingerprint": "fingerprint-1"}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "attempt", attempt)
            task = self.runtime.store.get(db, "task", run["id"] + "/policy")
            task.update(status="submitted", attempts=[attempt["id"]])
            self.runtime.store.put(db, "task", task)
        self.runtime.agent_register(run["id"], "sol-reviewer", "gpt-6.1-sol", "task_review", attempt["id"],
                                    reason="Assign Sol as the independent task reviewer.")
        self.runtime.task_escalate(run["id"], "second", "orchestrator", "Escalate the second task to Sol.")
        with self.assertRaisesRegex(WorkflowError, "reviewer ID|review|model"):
            self.runtime.agent_register(run["id"], "sol-reviewer", "gpt-6.1-sol", "implementation", "second",
                                        reason="Reject review and coding role reuse.")

    def test_reviewer_id_cannot_be_reused_for_a_second_candidate(self):
        run = self.runtime.run_create(self.bundle, "Create a fresh reviewer identity check.")
        first = {"id": "submitted-one", "run_id": run["id"], "task_id": "policy", "status": "submitted",
                 "agent_id": "worker-one", "candidate_commit": "candidate-1", "candidate_fingerprint": "fingerprint-1"}
        second = {"id": "submitted-two", "run_id": run["id"], "task_id": "policy", "status": "submitted",
                  "agent_id": "worker-two", "candidate_commit": "candidate-2", "candidate_fingerprint": "fingerprint-2"}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "attempt", first)
            self.runtime.store.put(db, "attempt", second)
        self.runtime.agent_register(run["id"], "fresh-reviewer", "gpt-6.1-sol", "task_review", first["id"],
                                    reason="Assign a fresh reviewer to the first candidate.")
        with self.assertRaisesRegex(WorkflowError, "fresh native agent ID"):
            self.runtime.agent_register(run["id"], "fresh-reviewer", "gpt-6.1-sol", "task_review", second["id"],
                                        reason="Reject reuse of the first review agent.")

    def test_candidate_change_invalidates_registered_reviewer(self):
        run = self.runtime.run_create(self.bundle, "Create a stale candidate registration check.")
        attempt = {"id": "submitted-one", "run_id": run["id"], "task_id": "policy", "status": "submitted",
                   "agent_id": "worker-one", "candidate_commit": "candidate-1", "candidate_fingerprint": "fingerprint-1"}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "attempt", attempt)
        self.runtime.agent_register(run["id"], "fresh-reviewer", "gpt-6.1-sol", "task_review", attempt["id"],
                                    reason="Assign a reviewer to the current candidate.")
        with self.runtime.store.transaction() as db:
            changed = self.runtime.store.get(db, "attempt", attempt["id"])
            changed["candidate_commit"] = "candidate-2"
            changed["candidate_fingerprint"] = "fingerprint-2"
            self.runtime.store.put(db, "attempt", changed)
        with self.assertRaisesRegex(WorkflowError, "stale"):
            self.runtime.review_record(run["id"], attempt["id"], "fresh-reviewer", "accept", [],
                                       "Reject a review assignment for an old candidate.")

    def test_schema_target_change_invalidates_registered_reviewer(self):
        run = self.runtime.run_create(self.bundle, "Create a stale schema review check.")
        proposal = {"id": "schema-one", "run_id": run["id"], "status": "requested", "author": "schema-author",
                    "proposal": {"commit": "schema-commit-1"}, "schema_generation": 1, "prior_fingerprint": "schema-1"}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "schema_proposal", proposal)
        self.runtime.agent_register(run["id"], "schema-reviewer", "gpt-6.1-sol", "schema_review", proposal["id"],
                                    reason="Assign a reviewer to the schema proposal.")
        with self.runtime.store.transaction() as db:
            changed = self.runtime.store.get(db, "schema_proposal", proposal["id"])
            changed["schema_generation"] = 2
            self.runtime.store.put(db, "schema_proposal", changed)
        with self.assertRaisesRegex(WorkflowError, "stale"):
            self.runtime.schema_review(proposal["id"], "schema-reviewer", "Reject the changed schema target.")

    def test_schema_registration_rejects_foreign_or_reviewed_proposal(self):
        run = self.runtime.run_create(self.bundle, "Create a schema ownership check.")
        proposal = {"id": "schema-one", "run_id": "another-run", "status": "requested", "author": "schema-author",
                    "proposal": {"commit": "schema-commit-1"}}
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "schema_proposal", proposal)
        with self.assertRaisesRegex(WorkflowError, "not reviewable"):
            self.runtime.agent_register(run["id"], "schema-reviewer", "gpt-6.1-sol", "schema_review", proposal["id"],
                                        reason="Reject a proposal from another run.")
        with self.runtime.store.transaction() as db:
            changed = self.runtime.store.get(db, "schema_proposal", proposal["id"])
            changed.update(run_id=run["id"], status="reviewed")
            self.runtime.store.put(db, "schema_proposal", changed)
        with self.assertRaisesRegex(WorkflowError, "not reviewable"):
            self.runtime.agent_register(run["id"], "schema-reviewer", "gpt-6.1-sol", "schema_review", proposal["id"],
                                        reason="Reject a proposal that is no longer requested.")

    def test_final_target_change_invalidates_registered_reviewer(self):
        run = self.runtime.run_create(self.bundle, "Create a stale final review check.")
        with self.runtime.store.transaction() as db:
            task = self.runtime.store.get(db, "task", run["id"] + "/policy")
            task["status"] = "integrated"
            self.runtime.store.put(db, "task", task)
        self.runtime.agent_register(run["id"], "final-reviewer", "gpt-6.1-sol", "final_review", run["id"],
                                    reason="Assign a reviewer to the current integration revision.")
        (Path(run["worktree"]) / "changed-after-registration.txt").write_text("Changed target.\n")
        with self.assertRaisesRegex(WorkflowError, "stale"):
            self.runtime.run_review_record(run["id"], "final-reviewer", ["R1"], [],
                                            "Reject the changed integration target.")

    def test_escalation_requires_abandoned_attempt_and_new_agent(self):
        run = self.runtime.run_create(self.bundle, "Create a run with model defaults.")
        self.runtime.agent_register(run["id"], "luna-worker", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign the Luna worker.")
        attempt = self.runtime.task_claim(run["id"], "policy", "luna-worker", "Claim the implementation task.")
        with self.assertRaisesRegex(WorkflowError, "abandon|pending"):
            self.runtime.task_escalate(run["id"], "policy", "orchestrator", "Escalate after blocked progress.")
        self.runtime.task_abandon(run["id"], attempt["id"], "Stop and preserve the Luna worktree.")
        escalation = self.runtime.task_escalate(run["id"], "policy", "orchestrator", "Escalate after blocked progress.")
        self.assertEqual(escalation["model"], "gpt-6.1-sol")
        self.assertEqual(escalation["generation"], 1)
        requirements = self.runtime.agent_requirements(run["id"], "implementation", "policy")
        self.assertEqual(requirements["model"], "gpt-6.1-sol")
        with self.assertRaisesRegex(WorkflowError, "new native agent ID|replacement|model"):
            self.runtime.agent_register(run["id"], "luna-worker", "gpt-6.1-sol", "implementation", "policy",
                                        reason="Reject a reused worker identity.")
        self.runtime.agent_register(run["id"], "sol-worker", "gpt-6.1-sol", "implementation", "policy",
                                    reason="Assign the escalated Sol worker.")
        replacement = self.runtime.task_claim(run["id"], "policy", "sol-worker", "Claim with the escalated model.")
        self.assertNotEqual(replacement["worktree"], attempt["worktree"])
        self.assertTrue(Path(attempt["worktree"]).exists())
        with self.assertRaisesRegex(WorkflowError, "already escalated"):
            self.runtime.task_escalate(run["id"], "policy", "orchestrator", "Reject a second escalation.")

    def test_abandonment_invalidates_the_old_implementation_assignment(self):
        run = self.runtime.run_create(self.bundle, "Create a task generation check.")
        self.runtime.agent_register(run["id"], "first-worker", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign the first worker.")
        attempt = self.runtime.task_claim(run["id"], "policy", "first-worker", "Claim the first attempt.")
        self.runtime.task_abandon(run["id"], attempt["id"], "Stop and preserve the first attempt.")
        with self.assertRaisesRegex(WorkflowError, "stale"):
            self.runtime.task_claim(run["id"], "policy", "first-worker", "Reject the stale assignment.")

    def test_implementation_agent_cannot_register_for_review(self):
        run = self.runtime.run_create(self.bundle, "Create a coding and review separation check.")
        self.runtime.agent_register(run["id"], "coder", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign the coding agent.")
        attempt = self.runtime.task_claim(run["id"], "policy", "coder", "Claim the coding task.")
        with self.runtime.store.transaction() as db:
            stored = self.runtime.store.get(db, "attempt", attempt["id"])
            stored.update(status="submitted", candidate_commit="candidate-1", candidate_fingerprint="fingerprint-1")
            self.runtime.store.put(db, "attempt", stored)
            task = self.runtime.store.get(db, "task", run["id"] + "/policy")
            task["status"] = "submitted"
            self.runtime.store.put(db, "task", task)
        with self.assertRaisesRegex(WorkflowError, "implementation agent"):
            self.runtime.agent_register(run["id"], "coder", "gpt-6.1-sol", "task_review", attempt["id"],
                                        reason="Reject a coding agent as its own reviewer.")

    def test_task_attempt_limit_survives_escalation(self):
        self.bundle["max_attempts"] = 1
        run = self.runtime.run_create(self.bundle, "Create a fixed attempt limit check.")
        self.runtime.agent_register(run["id"], "luna", "gpt-6-luna", "implementation", "policy",
                                    reason="Assign the Luna worker.")
        attempt = self.runtime.task_claim(run["id"], "policy", "luna", "Claim the only allowed attempt.")
        self.runtime.task_abandon(run["id"], attempt["id"], "Stop and preserve the attempt.")
        self.runtime.task_escalate(run["id"], "policy", "orchestrator", "Escalate without changing the attempt limit.")
        self.runtime.agent_register(run["id"], "sol", "gpt-6.1-sol", "implementation", "policy",
                                    reason="Assign the escalated Sol worker.")
        with self.assertRaisesRegex(WorkflowError, "attempt limit"):
            self.runtime.task_claim(run["id"], "policy", "sol", "Reject a claim beyond the fixed attempt limit.")

    def test_legacy_run_remains_claimable_without_policy_registration(self):
        run = self.runtime.run_create(self.bundle, "Create a run for a legacy compatibility check.")
        with self.runtime.store.transaction() as db:
            stored = self.runtime.store.get(db, "run", run["id"])
            stored["bundle"].pop("model_policy")
            self.runtime.store.put(db, "run", stored)
        self.assertEqual(self.runtime.run_status(run["id"])["model_policy_mode"], "legacy")
        attempt = self.runtime.task_claim(run["id"], "policy", "legacy-worker", "Claim a legacy task.")
        self.assertIsNone(attempt["requested_model"])

    def test_registration_persists_requested_and_missing_reported_models(self):
        run = self.runtime.run_create(self.bundle, "Create a run with model defaults.")
        assignment = self.runtime.agent_register(run["id"], "persistent-worker", "gpt-6-luna", "implementation", "policy",
                                                 reason="Persist the requested model assignment.")
        self.runtime = Runtime(self.root / "state")
        attempt = self.runtime.task_claim(run["id"], "policy", "persistent-worker", "Claim after runtime restart.")
        self.assertEqual(attempt["model_assignment_id"], assignment["id"])
        self.assertEqual(attempt["requested_model"], "gpt-6-luna")
        self.assertIsNone(attempt["reported_model"])
        self.assertIsNone(attempt["model_source"])
        self.assertEqual(self.runtime.run_status(run["id"])["agent_assignments"][0]["requested_model"], "gpt-6-luna")

    def test_mcp_catalog_exposes_typed_policy_operations(self):
        from ub_execute.tools import catalog

        definitions = {tool["name"]: tool for tool in catalog()}
        self.assertIn("agent_register", definitions)
        self.assertIn("agent_requirements", definitions)
        self.assertIn("task_escalate", definitions)
        self.assertEqual(definitions["agent_register"]["inputSchema"]["properties"]["requested_model"]["type"], "string")
        self.assertEqual(definitions["agent_register"]["inputSchema"]["properties"]["reported_model"]["type"], ["string", "null"])


if __name__ == "__main__":
    unittest.main()
