"""Model assignments, target snapshots, and explicit task escalation."""

import time

from .contracts import DEFAULT_MODEL_POLICY, need, text
from .store import WorkflowError, identifier
from .workspace import fingerprint


class AgentPolicy:
    @staticmethod
    def _model_roles():
        return {"implementation": "implementation", "task_review": "task_review",
                "schema_review": "schema_review", "final_review": "final_review"}

    def _target_snapshot(self, db, run, role, target_id):
        if role == "implementation":
            task = self._task(db, run["id"], target_id)
            return {"task_id": target_id, "generation": task.get("generation", 0), "status": task["status"],
                    "base_commit": run["integration_commit"]}
        if role == "task_review":
            attempt = self._attempt(db, run["id"], target_id)
            need(attempt["status"] == "submitted", "Submit the candidate before assigning a reviewer.")
            return {"attempt_id": target_id, "candidate_commit": attempt["candidate_commit"],
                    "fingerprint": attempt["candidate_fingerprint"]}
        if role == "schema_review":
            record = self.store.get(db, "schema_proposal", target_id)
            need(record["run_id"] == run["id"] and record["status"] == "requested", "The schema proposal is not reviewable.")
            return {"proposal_id": target_id, "commit": record["proposal"]["commit"],
                    "schema_generation": record.get("schema_generation"), "prior_fingerprint": record.get("prior_fingerprint")}
        if role == "final_review":
            need(target_id == run["id"], "A final reviewer must target the run.")
            need(all(task["status"] == "integrated" for task in self.store.list(db, "task", run["id"])),
                 "Integrate every task before assigning a final reviewer.")
            return {"run_id": run["id"], "commit": run["integration_commit"], "fingerprint": fingerprint(run["worktree"])}
        raise WorkflowError("Unknown model role.")

    def _expected_model(self, run, role, target_id):
        policy = run.get("bundle", {}).get("model_policy")
        if policy is None:
            return None
        if role == "implementation":
            task = run.get("task_escalations", {}).get(target_id)
            return task.get("model", policy[role]) if task else policy[role]
        return policy[role]

    def agent_requirements(self, run_id, role, target_id):
        need(role in self._model_roles(), "Unknown model role.")
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            snapshot = self._target_snapshot(db, run, role, target_id)
            model = self._expected_model(run, role, target_id)
            spawn = {"fork_turns": "none"}
            if model:
                spawn["model"] = model
            return {"run_id": run_id, "role": role, "target_id": target_id,
                    "policy_mode": "legacy" if model is None else "enforced", "model": model,
                    "target": snapshot, "spawn": spawn}

    def agent_register(self, run_id, agent_id, requested_model, role, target_id, reason,
                       reported_model=None, model_source=None, model_reference=None):
        text(agent_id, "a native agent ID")
        text(reason, "an action reason")
        self._validate_registration(role, requested_model, reported_model, model_source, model_reference)
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            snapshot = self._target_snapshot(db, run, role, target_id)
            expected = self._expected_model(run, role, target_id)
            self._validate_expected(expected, requested_model, reported_model)
            assignments = self.store.list(db, "agent_assignment", run_id)
            record_data = {"agent_id": agent_id, "role": role, "target_id": target_id,
                           "requested_model": requested_model, "reported_model": reported_model,
                           "model_source": model_source, "model_reference": model_reference,
                           "target": snapshot}
            prior = self._prior_assignment(db, run_id, agent_id, role, target_id,
                                           requested_model, record_data, assignments)
            if prior:
                return prior
            assignment = {"id": identifier("assignment"), "run_id": run_id, **record_data,
                          "registered_at": time.time()}
            self.store.put(db, "agent_assignment", assignment)
            self.store.event(db, run_id, "agent_registered", reason, assignment)
            return assignment

    @staticmethod
    def _validate_registration(role, requested_model, reported_model, model_source, model_reference):
        need(role in AgentPolicy._model_roles(), "Unknown model role.")
        need(requested_model in {None, *DEFAULT_MODEL_POLICY.values()}, "Unknown requested model.")
        if reported_model is None:
            need(model_source is None and model_reference is None,
                 "Model source and reference need reported model metadata.")
            return
        need(reported_model in DEFAULT_MODEL_POLICY.values(), "Unknown reported model.")
        need(model_source in {"host_metadata", "agent_report", "orchestrator"}, "Provide the reported model source.")
        if model_reference is not None:
            text(model_reference, "a model metadata reference")
        if model_source == "host_metadata":
            text(model_reference, "a host model metadata reference")

    @staticmethod
    def _validate_expected(expected, requested_model, reported_model):
        if expected is None:
            need(requested_model is None, "Legacy runs do not verify a model assignment.")
        else:
            need(requested_model == expected, "The requested model does not match the model policy.")
        if reported_model is not None:
            need(reported_model == requested_model, "The reported model does not match the requested model.")

    def _prior_assignment(self, db, run_id, agent_id, role, target_id,
                          requested_model, record_data, assignments):
        same = [item for item in assignments if item["agent_id"] == agent_id]
        for item in same:
            if all(item.get(key) == value for key, value in record_data.items()):
                return item
        need(not any(item["role"] == role and item["target_id"] == target_id for item in same),
             "Use a new native agent ID for a changed target assignment.")
        self._check_role_reuse(db, run_id, agent_id, role, same, assignments)
        need(all(item.get("requested_model") == requested_model for item in same),
             "A native agent ID cannot change its requested model within a run.")
        if role != "implementation":
            need(not same, "Use a fresh native agent ID for each review assignment.")
        return None

    def _check_role_reuse(self, db, run_id, agent_id, role, same, assignments):
        if role == "implementation":
            need(not any(item["role"] != "implementation" for item in same),
                 "A reviewer ID cannot become an implementation agent.")
            return
        implementation_ids = {item["agent_id"] for item in self.store.list(db, "attempt", run_id)}
        implementation_ids.update(item["agent_id"] for item in assignments if item["role"] == "implementation")
        need(agent_id not in implementation_ids, "An implementation agent cannot register for review.")

    def _assignment(self, db, run, agent_id, role, target_id):
        if run.get("bundle", {}).get("model_policy") is None:
            return None
        expected = self._expected_model(run, role, target_id)
        need(expected is not None, "A model assignment is required.")
        assignments = self.store.list(db, "agent_assignment", run["id"])
        assignment = next((item for item in assignments if self._matches_assignment(item, agent_id, role, target_id)), None)
        need(assignment is not None, "Register the agent model assignment first.")
        need(assignment["requested_model"] == expected, "The registered model does not match the model policy.")
        need(assignment["target"] == self._target_snapshot(db, run, role, target_id), "The registered target is stale.")
        return assignment

    @staticmethod
    def _matches_assignment(item, agent_id, role, target_id):
        return (item["agent_id"] == agent_id and item["role"] == role
                and item["target_id"] == target_id)

    def _claim_model_assignment(self, db, run, task_id, agent_id):
        return self._assignment(db, run, agent_id, "implementation", task_id)

    def _implementation_context(self, run, task):
        model = self._expected_model(run, "implementation", task["task_id"])
        spawn = {"fork_turns": "none"}
        if model:
            spawn["model"] = model
        mode = "legacy" if model is None else "enforced"
        target = {"task_id": task["task_id"], "generation": task.get("generation", 0),
                  "status": task["status"], "base_commit": run["integration_commit"]}
        return {"policy_mode": mode, "model": model, "spawn": spawn, "target": target}

    def task_escalate(self, run_id, task_id, orchestrator_id, reason):
        text(orchestrator_id, "an orchestrator agent ID")
        text(reason, "an escalation reason")
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            need(run.get("bundle", {}).get("model_policy") is not None, "Legacy runs do not support model escalation.")
            task = self._task(db, run_id, task_id)
            escalations = run.setdefault("task_escalations", {})
            need(task_id not in escalations, "This task was already escalated.")
            need(task["status"] == "pending", "Stop and abandon the active attempt before escalation.")
            attempts = [self.store.get(db, "attempt", item) for item in task["attempts"]]
            need(all(item["status"] == "abandoned" for item in attempts), "Stop and abandon every prior attempt before escalation.")
            record = {"task_id": task_id, "from_model": DEFAULT_MODEL_POLICY["implementation"],
                      "model": "gpt-6.1-sol", "orchestrator_id": orchestrator_id, "reason": reason,
                      "attempt_ids": list(task["attempts"]), "generation": task["generation"]}
            escalations[task_id] = record
            self.store.put(db, "run", run)
            self.store.event(db, run_id, "task_escalated", reason, record)
            return record

