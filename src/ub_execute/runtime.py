"""Execution operations shared by the CLI and MCP server."""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading

from .contracts import need, text, validate_bundle
from .store import Store, WorkflowError, encode, identifier
from .workspace import add_worktree, changed_paths, file_names, fingerprint, git, require_clean, selected
from .processes import is_locked
from .provenance import runner_fingerprint
from .lifecycle import Lifecycle
from .agent_policy import AgentPolicy
from .cleanup import resource_manifest
from .parent import ParentManager


class Runtime(AgentPolicy, Lifecycle):
    def __init__(self, state):
        self.store = Store(state)

    def plan_validate(self, bundle):
        bundle = validate_bundle(bundle)
        return {"valid": True, "tasks": len(bundle["tasks"]), "requirements": len(bundle["requirements"]),
                "model_policy": bundle["model_policy"]}

    def run_create(self, bundle, reason):
        bundle = validate_bundle(bundle)
        text(reason, "an action reason")
        repo = bundle["repository"]
        need(git(repo, "rev-parse", "--show-toplevel") == repo, "Use the repository root.")
        need(not git(repo, "diff", "HEAD", "--name-only"), "Commit or preserve tracked changes before execution.")
        spec = Path(bundle["spec"]).read_text()
        run_id = identifier("run")
        base = git(repo, "rev-parse", "HEAD")
        directory = self.store.root / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "spec.md").write_text(spec)
        (directory / "bundle.json").write_text(encode(bundle) + "\n")
        artifacts = []
        for index, original in enumerate(bundle.get("review_artifacts", [])):
            data = Path(original).expanduser().resolve().read_bytes()
            need(len(data) <= 10 * 1024 * 1024, "The review artifact exceeds 10 MiB.")
            destination = directory / f"review-{index}.md"
            destination.write_bytes(data)
            artifacts.append({"original": str(Path(original).resolve()), "snapshot": str(destination), "digest": hashlib.sha256(data).hexdigest()})
        workspace_id = bundle.get('workspace_id')
        if workspace_id:
            workspace = self.workspace_context(workspace_id)
            need(workspace['parent_path'] == repo, 'Use the attached parent as the bundle repository.')
            need(workspace['status'] == 'ready', 'Reconcile the parent environment before execution.')
            tree, branch = repo, workspace['branch']
        else:
            branch = f"ub/{run_id}/integration"
            tree = add_worktree(repo, directory / "integration", branch, base)
        run = {"id": run_id, "status": "active", "repository": repo, "base_commit": base,
               "integration_commit": base, "integration_branch": branch, "worktree": tree,
               "spec_digest": hashlib.sha256(spec.encode()).hexdigest(), "bundle": bundle,
               "review_artifacts": artifacts,
               "created_at": time.time(), "workspace_id": workspace_id, "integration_owned": not bool(workspace_id)}
        with self.store.transaction() as db:
            self.store.put(db, "run", run)
            self.store.put(db, "cleanup_resource", resource_manifest(run))
            for definition in bundle["tasks"]:
                task = {**definition, "id": run_id + "/" + definition["id"], "task_id": definition["id"],
                        "run_id": run_id, "status": "pending", "attempts": [], "generation": 0}
                self.store.put(db, "task", task)
            self.store.event(db, run_id, "run_created", reason, {"spec_digest": run["spec_digest"], "base": base})
        return self._summary(run)

    def _run(self, db, run_id, active=False):
        run = self.store.get(db, "run", run_id)
        if active:
            self._mutable(db, run_id)
            need(not run.get("schema_staging"), "Reconcile the pending schema staging intent first.")
            if run.get("workspace_id"):
                workspace = self.store.get(db, "parent_workspace", run["workspace_id"])
                need(workspace["status"] == "ready", "Reconcile the parent environment before runtime changes.")
            need(run["status"] == "active", f"The run is {run['status']}.")
        return run

    def _task(self, db, run_id, task_id):
        return self.store.get(db, "task", run_id + "/" + task_id)

    def _attempt(self, db, run_id, attempt_id, editable=False):
        attempt = self.store.get(db, "attempt", attempt_id)
        need(attempt["run_id"] == run_id, "The attempt belongs to another run.")
        if editable:
            need(attempt["status"] in {"running", "submitted"}, "The attempt is no longer editable.")
        return attempt

    @staticmethod
    def _summary(run):
        return {key: value for key, value in run.items() if key != "bundle"}

    def run_status(self, run_id):
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            tasks = self.store.list(db, "task", run_id)
            jobs = self.store.list(db, "job", run_id)
            return {**self._summary(run), "model_policy": run.get("bundle", {}).get("model_policy"),
                    "model_policy_mode": "legacy" if "model_policy" not in run.get("bundle", {}) else "enforced", "tasks": tasks,
                    "agent_assignments": self.store.list(db, "agent_assignment", run_id),
                    "jobs": [{key: job[key] for key in ("id", "check_id", "status", "attempt_id", "phase")} for job in jobs],
                    "requirements": run["bundle"]["requirements"],
                    "resources": self.store.list(db, "cleanup_resource", run_id),
                    "cleanup": self.store.list(db, "cleanup", run_id)}

    def run_events(self, run_id, after=0, limit=100):
        with self.store.transaction() as db:
            self._run(db, run_id)
            rows = db.execute("SELECT sequence,time,action,reason,data FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                              (run_id, after, max(1, min(limit, 500)))).fetchall()
        return {"events": [{"sequence": seq, "time": stamp, "action": action, "reason": reason, "data": json.loads(data)}
                           for seq, stamp, action, reason, data in rows]}

    def action_record(self, run_id, agent_id, action, reason, details):
        text(agent_id, "an agent ID")
        text(action, "an action description")
        need(isinstance(details, dict), "Provide action details as an object.")
        with self.store.transaction() as db:
            self._run(db, run_id)
            self.store.event(db, run_id, "agent_action", reason, {"agent_id": agent_id, "action": action, "details": details})
            sequence = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"run_id": run_id, "sequence": sequence}

    def run_control(self, run_id, action, reason):
        need(action in {"pause", "resume", "cancel"}, "Use pause, resume, or cancel.")
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            self._mutable(db, run_id)
            need(run["status"] not in {"complete", "cancelled"}, "The run is terminal.")
            run["status"] = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            self.store.put(db, "run", run)
            self.store.event(db, run_id, "run_" + action, reason, {})
            jobs = self.store.list(db, "job", run_id)
        if action in {"pause", "cancel"}:
            for job in jobs:
                if job["status"] in {"queued", "running"}:
                    self.job_cancel(job_id=job["id"], reason=reason)
        return self._summary(run)

    def task_claim(self, run_id, task_id, agent_id, reason):
        text(agent_id, "a worker agent ID")
        text(reason, "an action reason")
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            task = self._task(db, run_id, task_id)
            need(task["status"] == "pending", "The task already has an owner or result.")
            assignment = self._claim_model_assignment(db, run, task_id, agent_id)
            active = [entry for entry in self.store.list(db, "task", run_id) if entry["status"] in {"running", "submitted"}]
            need(len(active) < run["bundle"]["max_parallel_tasks"], "The run reached its concurrent task limit.")
            need(len(task["attempts"]) < run["bundle"].get("max_attempts", 3), "The task reached its attempt limit. Review the failed premise.")
            for dependency in task["depends_on"]:
                need(self._task(db, run_id, dependency)["status"] == "integrated", "A task dependency is not integrated.")
            attempt_id = identifier("attempt")
            branch = f"ub/{run_id}/tasks/{task_id}/{attempt_id}"
            tree = add_worktree(run["repository"], self.store.root / "runs" / run_id / attempt_id, branch, run["integration_commit"])
            attempt = {"id": attempt_id, "run_id": run_id, "task_id": task_id, "agent_id": agent_id,
                       "worktree": tree, "branch": branch, "base_commit": run["integration_commit"], "status": "running",
                       "model_assignment_id": assignment["id"] if assignment else None,
                       "requested_model": assignment["requested_model"] if assignment else None,
                       "reported_model": assignment["reported_model"] if assignment else None,
                       "model_source": assignment["model_source"] if assignment else None,
                       "model_reference": assignment["model_reference"] if assignment else None}
            ParentManager(self).prepare_child(db, run, attempt)
            self.store.put(db, "cleanup_resource", resource_manifest(run, attempt))
            task["attempts"].append(attempt_id)
            task["status"] = "running"
            self.store.put(db, "attempt", attempt)
            self.store.put(db, "task", task)
            self.store.event(db, run_id, "task_claimed", reason, {"task_id": task_id, "attempt_id": attempt_id, "agent_id": agent_id})
            return attempt

    def task_context(self, run_id, task_id, attempt_id=None):
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            task = self._task(db, run_id, task_id)
            attempt = self._attempt(db, run_id, attempt_id) if attempt_id else None
            if attempt:
                need(attempt["task_id"] == task_id, "The attempt belongs to another task.")
            tree = Path(attempt["worktree"] if attempt else run["worktree"])
            instructions = self._instruction_paths(run, tree)
            resource_id = attempt["id"] if attempt else run_id + "/integration"
            resource = next((item for item in self.store.list(db, "cleanup_resource", run_id) if item["id"] == resource_id), None)
            return {"run_id": run_id, "task": task, "attempt": attempt,
                    "spec_snapshot": str(self.store.root / "runs" / run_id / "spec.md"),
                    "requirements": [entry for entry in run["bundle"]["requirements"] if entry["id"] in task["requirements"]],
                    "checks": {key: run["bundle"]["checks"][key] for key in task["checks"]},
                    "review_artifacts": run.get("review_artifacts", []),
                    "model_policy": run.get("bundle", {}).get("model_policy"),
                    "model_policy_mode": "legacy" if "model_policy" not in run.get("bundle", {}) else "enforced",
                    "implementation_agent": self._implementation_context(run, task),
                    "agent_assignments": self.store.list(db, "agent_assignment", run_id),
                    "instruction_paths": sorted(set(instructions)), "decisions": run["bundle"].get("decisions", []),
                    "constraints": run["bundle"].get("constraints", []),
                    "resource": resource, "parent_environment": attempt.get("parent_environment") if attempt else None}

    @staticmethod
    def _instruction_paths(run, tree):
        root = Path(run['repository'])
        paths = [str(parent / 'AGENTS.md') for parent in reversed([root, *root.parents])
                 if (parent / 'AGENTS.md').is_file()]
        if tree.is_dir():
            paths.extend(str(tree / name) for name in file_names(tree) if Path(name).name == 'AGENTS.md')
        return paths

    def check_start(self, run_id, check_id, phase, reason, attempt_id=None):
        text(reason, "an action reason")
        need(phase in {"red", "green", "quality", "final"}, "Unknown check phase.")
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            need(check_id in run["bundle"]["checks"], "Unknown check ID.")
            check = run["bundle"]["checks"][check_id]
            tree, task = self._check_target(db, run, check_id, phase, attempt_id)
            for existing in self.store.list(db, "job", run_id):
                busy = existing["status"] in {"queued", "running"} or (existing["status"] == "cancelled" and self._worker_alive(existing))
                need(existing["worktree"] != tree or not busy, "A check already owns this worktree.")
            job_id = identifier("job")
            directory = self.store.root / "jobs" / job_id
            directory.mkdir(parents=True)
            job = {"id": job_id, "run_id": run_id, "attempt_id": attempt_id, "check_id": check_id,
                   "phase": phase, "status": "queued", "worktree": tree, "check": check,
                   "directory": str(directory), "created_at": time.time(), "pid": None,
                   "fingerprint": fingerprint(tree),
                   "runner_fingerprint": runner_fingerprint(),
                   "test_fingerprint": fingerprint(tree, task["test_paths"]) if task else None}
            attempt = self._attempt(db, run_id, attempt_id) if attempt_id else None
            ParentManager(self).acquire_check(db, run, job, attempt)
            self.store.put(db, "job", job)
            self.store.event(db, run_id, "check_started", reason, {"job_id": job_id, "check_id": check_id, "phase": phase})
        return self._launch_job(job)

    def _check_target(self, db, run, check_id, phase, attempt_id):
        if not attempt_id:
            need(phase == "final" and check_id in run["bundle"]["final_checks"], "Use a configured final check.")
            return run["worktree"], None
        need(phase != "final", "Final checks use the integration worktree.")
        attempt = self._attempt(db, run["id"], attempt_id, editable=True)
        task = self._task(db, run["id"], attempt["task_id"])
        need(check_id in task["checks"], "This check is not assigned to the task.")
        if phase in {"red", "green"}:
            need(run["bundle"]["checks"][check_id].get("kind", "test") == "test", "TDD evidence requires a test check.")
        return attempt["worktree"], task

    def _launch_job(self, job):
        job_id, directory = job["id"], Path(job["directory"])
        environment = ParentManager(self).worker_environment(job)
        source = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        try:
            with (directory / "worker.log").open("ab") as log:
                process = subprocess.Popen([sys.executable, "-m", "ub_execute.worker", "--state", str(self.store.root), "--job", job_id],
                                           env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            with self.store.transaction() as db:
                job = self.store.get(db, "job", job_id)
                job["pid"] = process.pid
                self.store.put(db, "job", job)
            threading.Thread(target=process.wait, daemon=True).start()
        except OSError as exc:
            with self.store.transaction() as db:
                job = self.store.get(db, "job", job_id)
                job.update(status="error", error=str(exc))
                ParentManager(self).release_check(db, job)
                self.store.put(db, "job", job)
        return job

    @staticmethod
    def _worker_alive(job):
        return is_locked(Path(job["directory"]) / "worker.lock")

    def job_status(self, job_id, tail_bytes=4096):
        with self.store.transaction() as db:
            job = self.store.get(db, "job", job_id)
            if job["status"] in {"queued", "running"} and time.time() - job["created_at"] > 5 and not self._worker_alive(job):
                self._stop_orphan_command(job)
                job["status"] = "interrupted"
                ParentManager(self).release_check(db, job)
                self.store.put(db, "job", job)
                self.store.event(db, job["run_id"], "check_interrupted", "The check worker is no longer running.", {"job_id": job_id})
        with self.store.transaction() as db:
            job = self.store.get(db, 'job', job_id)
            if job['status'] not in {'queued','running'} and not self._worker_alive(job) and not is_locked(Path(job['directory']) / 'command.lock'):
                ParentManager(self).release_check(db, job)
                self.store.put(db, 'job', job)
        tails = {}
        for name in ("stdout.log", "stderr.log", "worker.log"):
            path = Path(job["directory"]) / name
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - max(0, min(tail_bytes, 65536))))
                    tails[name] = stream.read(65536).decode(errors="replace")
        return {**job, "log_tails": tails}

    def job_cancel(self, job_id, reason):
        with self.store.transaction() as db:
            job = self.store.get(db, "job", job_id)
            if job["status"] not in {"queued", "running"}:
                return job
            job["status"] = "cancelled"
            self.store.put(db, "job", job)
            self.store.event(db, job["run_id"], "check_cancelled", reason, {"job_id": job_id})
        if self._worker_alive(job):
            try:
                os.killpg(job["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._stop_orphan_command(job)
        # Release environment access only after both the worker and command locks end.
        with self.store.transaction() as db:
            current = self.store.get(db, 'job', job_id)
            if not self._worker_alive(current) and not is_locked(Path(current['directory']) / 'command.lock'):
                ParentManager(self).release_check(db, current)
                self.store.put(db, 'job', current)
        return job

    @staticmethod
    def _stop_orphan_command(job):
        pid = job.get("command_pid")
        if not pid:
            return
        if is_locked(Path(job["directory"]) / "command.lock"):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def red_confirm(self, job_id, explanation, agent_id):
        text(explanation, "the observed assertion failure")
        with self.store.transaction() as db:
            job = self.store.get(db, "job", job_id)
            self._run(db, job["run_id"], active=True)
            need(job["phase"] == "red" and job["status"] == "failed", "Confirm only an ordinary failed red test.")
            attempt = self._attempt(db, job["run_id"], job["attempt_id"], editable=True)
            need(agent_id == attempt["agent_id"], "The worker must explain its red result.")
            task = self._task(db, job["run_id"], attempt["task_id"])
            need(fingerprint(attempt["worktree"], task["test_paths"]) == job["test_fingerprint"], "The red test changed.")
            changed = changed_paths(attempt["worktree"], attempt["base_commit"])
            untracked_tests = [path for path in file_names(attempt["worktree"])
                               if selected(path, task["test_paths"])]
            # Git diff omits untracked tests. Compare each test to the base tree as well.
            baseline_tests = set(git(attempt["worktree"], "ls-tree", "-r", "--name-only", attempt["base_commit"]).splitlines())
            need(any(selected(path, task["test_paths"]) for path in changed) or any(path not in baseline_tests for path in untracked_tests),
                 "The red cycle must add or change a behavior test.")
            job.update(red_confirmed=True, red_explanation=explanation)
            self.store.put(db, "job", job)
            self.store.event(db, job["run_id"], "red_confirmed", explanation, {"job_id": job_id, "agent_id": agent_id})
            return job

    def _task_evidence(self, db, run, attempt, task):
        tree = attempt["worktree"]
        digest = fingerprint(tree)
        runner = runner_fingerprint()
        jobs = [job for job in self.store.list(db, "job", run["id"]) if job["attempt_id"] == attempt["id"]]
        for check_id in task["checks"]:
            good = [job for job in jobs if self._current_task_check(job, check_id, digest, runner)
                    and ParentManager(self).accepts_check(db, run, job)]
            need(bool(good), f"Current passing evidence is missing for check {check_id}.")
        if not task.get("tdd_exception"):
            self._require_red_cycle(jobs, fingerprint(tree, task["test_paths"]), digest)
        return digest

    @staticmethod
    def _current_task_check(job, check_id, digest, runner):
        return (job["check_id"] == check_id and job["status"] == "passed" and job["fingerprint"] == digest
                and job["phase"] in {"green", "quality"} and job.get("runner_fingerprint") == runner)

    @staticmethod
    def _valid_green(red, green, digest):
        return (green["created_at"] > red.get("finished_at", red["created_at"]) and green["fingerprint"] == digest
                and green["status"] == "passed" and green["phase"] == "green" and green["check_id"] == red["check_id"]
                and red["fingerprint"] != digest)

    def _require_red_cycle(self, jobs, current_tests, digest):
        reds = [job for job in jobs if job.get("red_confirmed") and job["status"] == "failed" and job["test_fingerprint"] == current_tests]
        valid = any(self._valid_green(red, green, digest) for red in reds for green in jobs)
        need(valid, "A current red-to-green behavior test cycle is missing.")

    def task_submit(self, run_id, attempt_id, reason):
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            attempt = self._attempt(db, run_id, attempt_id, editable=True)
            task = self._task(db, run_id, attempt["task_id"])
            require_clean(attempt["worktree"])
            git(attempt["worktree"], "merge-base", "--is-ancestor", attempt["base_commit"], "HEAD")
            need(not git(attempt["worktree"], "rev-list", "--merges", attempt["base_commit"] + "..HEAD"), "Submit ordered commits without merge commits.")
            changed = changed_paths(attempt["worktree"], attempt["base_commit"])
            need(changed or self._provisional_task(db, run, attempt, task), "The candidate contains no changes.")
            need(all(selected(path, task["scope"]) for path in changed), "The candidate changes files outside its task scope.")
            digest = self._task_evidence(db, run, attempt, task)
            attempt.update(status="submitted", candidate_commit=git(attempt["worktree"], "rev-parse", "HEAD"), candidate_fingerprint=digest)
            task["status"] = "submitted"
            self.store.put(db, "attempt", attempt)
            self.store.put(db, "task", task)
            self.store.event(db, run_id, "task_submitted", reason, {"attempt_id": attempt_id, "commit": attempt["candidate_commit"]})
            return attempt

    def review_record(self, run_id, attempt_id, reviewer_id, decision, findings, reason):
        need(decision in {"accept", "changes_required"}, "Unknown review decision.")
        need(isinstance(findings, list), "Provide a findings list.")
        text(reviewer_id, "a reviewer agent ID")
        with self.store.transaction() as db:
            self._run(db, run_id, active=True)
            attempt = self._attempt(db, run_id, attempt_id)
            need(attempt["status"] == "submitted", "Submit the candidate before review.")
            need(reviewer_id != attempt["agent_id"], "The worker cannot review its own candidate.")
            assignment = self._assignment(db, self._run(db, run_id), reviewer_id, "task_review", attempt_id)
            need(decision != "accept" or not findings, "Resolve findings before accepting the candidate.")
            need(git(attempt["worktree"], "rev-parse", "HEAD") == attempt["candidate_commit"], "The reviewed candidate changed.")
            need(fingerprint(attempt["worktree"]) == attempt["candidate_fingerprint"], "The reviewed worktree changed.")
            review = {"id": identifier("review"), "run_id": run_id, "attempt_id": attempt_id,
                      "reviewer_id": reviewer_id, "decision": decision, "findings": findings,
                      "candidate_commit": attempt["candidate_commit"], "fingerprint": attempt["candidate_fingerprint"],
                      "model_assignment_id": assignment["id"] if assignment else None,
                      "requested_model": assignment["requested_model"] if assignment else None,
                      "reported_model": assignment["reported_model"] if assignment else None,
                      "model_source": assignment["model_source"] if assignment else None,
                      "model_reference": assignment["model_reference"] if assignment else None}
            self.store.put(db, "review", review)
            self.store.event(db, run_id, "review_recorded", reason, review)
            return review

    def task_accept(self, run_id, attempt_id, reason):
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            attempt = self._attempt(db, run_id, attempt_id)
            need(attempt["status"] == "submitted", "Submit the candidate before acceptance.")
            task = self._task(db, run_id, attempt["task_id"])
            require_clean(attempt["worktree"])
            need(git(attempt["worktree"], "rev-parse", "HEAD") == attempt["candidate_commit"], "The candidate changed after submission.")
            self._task_evidence(db, run, attempt, task)
            reviews = [review for review in self.store.list(db, "review", run_id) if review["attempt_id"] == attempt_id and review["candidate_commit"] == attempt["candidate_commit"]]
            need(reviews and reviews[-1]["decision"] == "accept", "A current independent review is missing.")
            attempt["status"] = task["status"] = "accepted"
            self.store.put(db, "attempt", attempt)
            self.store.put(db, "task", task)
            self.store.event(db, run_id, "task_accepted", reason, {"attempt_id": attempt_id})
            return attempt

    def _task_integrate(self, run_id, task_id, reason):
        text(reason, "an action reason")
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            task = self._task(db, run_id, task_id)
            if task["status"] == "integrated":
                return task
            need(task["status"] == "accepted", "Accept the task before integration.")
            attempt = self._attempt(db, run_id, task["attempts"][-1])
            for dependency in task["depends_on"]:
                need(self._task(db, run_id, dependency)["status"] == "integrated", "A dependency is not integrated.")
            tree = run["worktree"]
            require_clean(tree)
            need(git(tree, "rev-parse", "HEAD") == run["integration_commit"], "Reconcile the interrupted integration before continuing.")
            need(not any(job["status"] in {"queued", "running"} and job["worktree"] == tree for job in self.store.list(db, "job", run_id)), "A final check owns the integration worktree.")
            commits = self._remaining_commits(db, run, attempt, task)
            if not commits:
                task.update(status='integrated', integrated_commit=run['integration_commit'])
                self.store.put(db, 'task', task)
                self.store.event(db, run_id, 'task_integrated', reason, {'task_id': task_id, 'provisional_only': True})
                return task
            need(not run.get("integrating_task"), "Another integration needs reconciliation.")
            run["integrating_task"] = task_id
            task["status"] = "integrating"
            task["integration_intent"] = {"before": run["integration_commit"], "commits": commits}
            self.store.put(db, "run", run)
            self.store.put(db, "task", task)
            self.store.event(db, run_id, "integration_started", reason, {"task_id": task_id, "commits": commits})
        try:
            git(tree, "cherry-pick", "-x", *commits)
        except WorkflowError as exc:
            with self.store.transaction() as db:
                run = self._run(db, run_id)
                run["status"] = "needs_attention"
                self.store.put(db, "run", run)
                self.store.event(db, run_id, "integration_conflict", reason, {"task_id": task_id, "error": str(exc)})
            return {"status": "needs_attention", "worktree": tree, "error": str(exc)}
        return self._integration_reconcile(run_id=run_id, reason=reason)

    def _integration_reconcile(self, run_id, reason):
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            self._mutable(db, run_id)
            need(run["status"] not in {"complete", "cancelled"}, "The run is terminal.")
            task_id = run.get("integrating_task")
            need(task_id, "The run has no pending integration intent.")
            task = self._task(db, run_id, task_id)
            tree = run["worktree"]
            require_clean(tree)
            marker = Path(git(tree, "rev-parse", "--git-path", "CHERRY_PICK_HEAD"))
            if not marker.is_absolute():
                marker = Path(tree) / marker
            need(not marker.exists(), "Finish or abort the active cherry-pick before reconciliation.")
            intent = task["integration_intent"]
            head = git(tree, "rev-parse", "HEAD")
            if head == intent["before"]:
                task["status"] = "accepted"
            else:
                messages = git(tree, "log", "--format=%B", intent["before"] + "..HEAD")
                need(all(commit in messages for commit in intent["commits"]), "The integration history does not contain every accepted commit marker.")
                need(all(selected(path, task["scope"]) for path in changed_paths(tree, intent["before"])), "The integration changes files outside the task scope.")
                task.update(status="integrated", integrated_commit=head)
                self._advance_parent(db, run, intent["before"], head)
            run.pop("integrating_task")
            if run["status"] == "needs_attention":
                run["status"] = "active"
            self.store.put(db, "run", run)
            self.store.put(db, "task", task)
            self.store.event(db, run_id, "integration_reconciled", reason, {"task_id": task_id, "commit": head, "status": task["status"]})
            return task

    def task_abandon(self, run_id, attempt_id, reason):
        with self.store.transaction() as db:
            self._run(db, run_id, active=True)
            attempt = self._attempt(db, run_id, attempt_id, editable=True)
            task = self._task(db, run_id, attempt["task_id"])
            attempt["status"] = "abandoned"
            task["status"] = "pending"
            task["generation"] = task.get("generation", 0) + 1
            self.store.put(db, "attempt", attempt)
            self.store.put(db, "task", task)
            jobs = self.store.list(db, "job", run_id)
            self.store.event(db, run_id, "task_abandoned", reason, {"attempt_id": attempt_id, "preserved_worktree": attempt["worktree"]})
        for job in jobs:
            if job["attempt_id"] == attempt_id and job["status"] in {"queued", "running"}:
                self.job_cancel(job["id"], reason)
        return attempt

    def run_recover(self, run_id):
        with self.store.transaction() as db:
            run = self._run(db, run_id)
            attempts = self.store.list(db, "attempt", run_id)
            job_ids = [job["id"] for job in self.store.list(db, "job", run_id)]
        jobs = [self.job_status(key) for key in job_ids]
        return {"run": {**self._summary(run), "model_policy": run.get("bundle", {}).get("model_policy"),
                         "model_policy_mode": "legacy" if "model_policy" not in run.get("bundle", {}) else "enforced",
                         "agent_assignments": self.run_status(run_id)["agent_assignments"]},
                "attempts": [{**attempt, "worktree_exists": Path(attempt["worktree"]).exists(),
                  "dirty": bool(git(attempt["worktree"], "status", "--porcelain")) if Path(attempt["worktree"]).exists() else None} for attempt in attempts],
                "jobs": [{"id": job["id"], "status": job["status"]} for job in jobs],
                "integration_head": git(run["worktree"], "rev-parse", "HEAD") if Path(run["worktree"]).is_dir() else None,
                "resources": self.run_status(run_id)["resources"],
                "cleanup": self._recover_cleanup(run_id),
                "next_action": "Inspect preserved work. Resume only after reconciling live workers and integration state."}

    def run_complete(self, run_id, reason):
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            need(all(task["status"] == "integrated" for task in self.store.list(db, "task", run_id)), "Some tasks are not integrated.")
            require_clean(run["worktree"])
            need(git(run["worktree"], "rev-parse", "HEAD") == run["integration_commit"], "The integration revision changed.")
            digest = fingerprint(run["worktree"])
            jobs = self.store.list(db, "job", run_id)
            runner = runner_fingerprint()
            for check_id in run["bundle"]["final_checks"]:
                need(any(self._current_final_check(job, check_id, digest, runner)
                         and ParentManager(self).accepts_check(db, run, job) for job in jobs),
                     f"Current final evidence is missing for check {check_id}.")
            reviews = self.store.list(db, "final_review", run_id)
            need(reviews and reviews[-1]["fingerprint"] == digest and reviews[-1]["commit"] == run["integration_commit"] and not reviews[-1]["findings"],
                 "A current final specification review is missing.")
            run["status"] = "complete"
            self.store.put(db, "run", run)
            self.store.event(db, run_id, "run_completed", reason, {"commit": run["integration_commit"]})
        cleanup = self._auto_cleanup(run)
        return {**self._summary(run), **({"cleanup": cleanup} if cleanup else {})}

    @staticmethod
    def _current_final_check(job, check_id, digest, runner):
        return (job["phase"] == "final" and job["check_id"] == check_id and job["status"] == "passed"
                and job["fingerprint"] == digest and job.get("runner_fingerprint") == runner)

    def run_review_record(self, run_id, reviewer_id, verified_requirements, findings, reason):
        text(reviewer_id, "a reviewer agent ID")
        need(isinstance(findings, list) and isinstance(verified_requirements, list), "Provide review findings and requirement IDs.")
        with self.store.transaction() as db:
            run = self._run(db, run_id, active=True)
            assignment = self._assignment(db, run, reviewer_id, "final_review", run_id)
            need(all(task["status"] == "integrated" for task in self.store.list(db, "task", run_id)), "Integrate every task before final review.")
            need(reviewer_id not in {attempt["agent_id"] for attempt in self.store.list(db, "attempt", run_id)}, "An implementation worker cannot perform final review.")
            requirement_ids = {entry["id"] for entry in run["bundle"]["requirements"]}
            need(set(verified_requirements) == requirement_ids, "Final review must address every requirement.")
            require_clean(run["worktree"])
            need(git(run["worktree"], "rev-parse", "HEAD") == run["integration_commit"], "Reconcile the integration revision before review.")
            review = {"id": identifier("final-review"), "run_id": run_id, "reviewer_id": reviewer_id,
                      "verified_requirements": verified_requirements, "findings": findings,
                      "commit": run["integration_commit"], "fingerprint": fingerprint(run["worktree"]),
                      "model_assignment_id": assignment["id"] if assignment else None,
                      "requested_model": assignment["requested_model"] if assignment else None,
                      "reported_model": assignment["reported_model"] if assignment else None,
                      "model_source": assignment["model_source"] if assignment else None,
                      "model_reference": assignment["model_reference"] if assignment else None}
            self.store.put(db, "final_review", review)
            self.store.event(db, run_id, "final_review_recorded", reason, review)
            return review
