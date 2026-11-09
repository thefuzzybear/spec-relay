# Execution bundle version 1

The agent derives the bundle from a reviewed specification. Python checks the structure and dependency graph. Python does not infer requirements from prose.

Use absolute paths for `spec` and `repository`. Use repository-relative paths for task scope and test paths. Each requirement needs an ID and observable text. Each task needs requirement references, acceptance criteria, dependencies, scope, test paths, and check IDs.

```json
{
  "version": 1,
  "spec": "/absolute/project/spec.md",
  "repository": "/absolute/project",
  "readiness": {"status": "ready", "open_findings": []},
  "requirements": [{"id": "REQ-1", "text": "Add two integers."}],
  "decisions": ["Use the existing integer representation."],
  "constraints": ["Preserve existing public interfaces."],
  "checks": {
    "unit": {
      "kind": "test",
      "argv": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
      "timeout_seconds": 300
    }
  },
  "tasks": [{
    "id": "addition",
    "title": "Add two integers",
    "requirements": ["REQ-1"],
    "acceptance": ["Adding 2 and 3 returns 5."],
    "depends_on": [],
    "scope": ["src/calc.py", "tests/"],
    "test_paths": ["tests/"],
    "checks": ["unit"]
  }],
  "final_checks": ["unit"]
}
```

The example uses only a unit check. Configure quality checks and end-to-end checks required by the target repository. Available check kinds are `test`, `command`, `crapper`, `dryer`, and `mutator`.

The runtime executes argument vectors without a shell. `{python}`, `{worktree}`, and `{artifacts}` expand only when they occupy a complete argument. Use the target project's interpreter when its dependencies differ from the runtime interpreter.

For a quality check, execute the installed launcher with `quality`. Supply `--tool`, `--root`, and `--paths`. Crapper and Mutator require `--coverage-json`, a JSON array of command argument vectors. Those commands must regenerate every report named in `--coverage-files`. Mutator also requires `--test-json`, one test argument vector. Its default worker limit is two.

Example arguments for a Crapper adapter:

```json
["/absolute/ub-execute/.venv/bin/python", "/absolute/ub-execute/skills/ub-execute/scripts/ub.py", "quality", "--tool", "crapper", "--root", "{worktree}", "--paths", "src/", "--coverage-json", "[[\"/absolute/project-python\",\"-m\",\"coverage\",\"run\",\"-m\",\"unittest\",\"discover\",\"-s\",\"tests\"],[\"/absolute/project-python\",\"-m\",\"coverage\",\"lcov\",\"-o\",\"coverage/lcov.info\"]]"]
```

Configure meaningful scopes. Dryer compares the supplied source tree, so include relevant existing code when checking new duplication. All reported duplicate candidates at or above 0.8 fail the default policy. Crapper requires a score below 10. Mutator requires executed kills for every selected site. Unsupported scopes and unresolved equivalent mutants require a policy decision. Workers must not silently omit required checks.

An optional task `tdd_exception` records why an honest automated behavior test is impractical. Such a task still needs passing configured checks and independent review. Use this exception only when the spec or task plan justifies it.

`max_attempts` defaults to three. `max_parallel_tasks` defaults to two. Configure these values during intake. Changes to a run's requirements or policy require a new bundle and run. Existing worktrees remain available for deliberate reuse.

Standalone execution preserves the original branch and integrates into a runtime branch. Attached execution integrates into the prepared parent branch. Each final check must pass against the current combined source. A final review must cover every requirement before completion.

The append-only event record contains completed operations and their reasons. It is an operational audit record. It is not a tamper-proof record of every shell action.


## Optional workspace and cleanup policy

Use `workspace_mode: "attached_parent"` with the `workspace_id` returned by `workspace_attach`.
Set `repository` to the prepared parent root. Standalone mode remains the default.
See [parent operations](../../../docs/workspace-operations.md) for private configuration, schema proposals, services, and environment generations.

Each check can declare `environment_dependencies`: `source`, `dependencies`, `database`, `data`, or `http`.
Use an empty list only for checks that do not depend on the shared environment.
Attached checks otherwise depend on the prepared dependencies and configured database.

Use `cleanup_policy: {"authorized": true, "on_complete": true}` when the user authorizes automatic completed worktree cleanup.
The orchestrator must also release actual native writers, reviewers, and owned services for each exact resource revision.
See [cleanup operations](../../../docs/cleanup-operations.md) for preservation, authorization, and restoration.
Existing runs without creation manifests remain protected.


## Native model policy

New bundles receive this fixed default policy when `model_policy` is absent:

```json
{
  "implementation": "gpt-6-luna",
  "task_review": "gpt-6.1-sol",
  "schema_review": "gpt-6.1-sol",
  "final_review": "gpt-6.1-sol"
}
```

An explicit `model_policy` must preserve these role defaults. Unknown fields and model substitutions fail validation.
The orchestrator keeps its selected model. The MCP does not select that model.

Call `agent_requirements` before native dispatch. Register the returned native agent ID and exact requested model with `agent_register`.
Claims and reviews require an assignment for their current target. Changed review targets require a fresh native reviewer.
A registration records a request. It does not authenticate native identity or establish the actual model.
Optional model reports retain their source and reference. Missing host information stays unknown.

`task_escalate` changes one pending coding task from Luna to Sol with an explicit reason.
Stop and abandon any active attempt before escalation. The replacement receives a new worktree and must produce current evidence.
Escalation preserves prior attempts and the attempt limit. Reviews remain on Sol.

Stored runs without `model_policy` retain legacy behavior. Create a new run to require model assignments.
Read [native model operations](../../../docs/model-policy.md) for dispatch, provenance, and unavailable-model handling.
