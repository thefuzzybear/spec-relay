---
name: ub-execute
description: Execute a reviewed specification through native Codex sub-agents, isolated worktrees, UBMode checks, and durable evidence. Use for implementation from an existing spec, including EGM output.
---

# Execute a reviewed specification

Use this skill to implement an existing specification. Use EGM separately when the user needs to create or revise the specification.

Read [UBMode principles](references/ub-principles.md). Apply Make it Work, Make it Right, and Practice to each task.

## Establish the run

1. Read the specification, repository instructions, and saved project preferences.
2. Inspect the repository before selecting commands or implementation boundaries.
3. Read [the execution contract](references/execution-contract.md).
4. Build an execution bundle from the reviewed spec and recorded decisions.
5. Call `plan_validate`, then `run_create`.

Use the connected `ub-execute` MCP tools. If they are unavailable, use `scripts/ub.py` through the installed interpreter. The CLI exposes identical operations:

```text
<python> <skill>/scripts/ub.py --state <state-directory> call <tool> --args-file <arguments.json>
```

Use `tools` to inspect argument schemas. The runtime source and interpreter paths appear in the installation receipt.

The bundle must preserve requirements, constraints, decisions, unresolved findings, and the authorized delivery outcome. Resolve material ambiguity before implementing dependent work. Continue independent work when possible. Do not invent product decisions or mark an incomplete EGM review as ready.

## Use a prepared parent

When execution starts in a prepared feature worktree, read [parent workspace operations](../../docs/workspace-operations.md).
Attach that worktree with `workspace_attach`. Select `attached_parent` in the bundle and use the returned workspace ID.

Children borrow the parent's dependencies and restricted database credential. Run checks through `check_start` to use the generated environment and leases.
Configure each tool's outputs and caches inside its child. Require an adapter before checks can write into borrowed dependencies.
Keep schema pushes and dependency installation with the parent coordinator. Do not run the repository's environment preparation command for each child.

For schema-only proposals, use role `schema_review` and the proposal ID with the same native dispatch and registration procedure.
Apply reviewed proposals through the coordinator CLI before affected database checks.
Reconcile clean child branches with the updated parent, then call `workspace_refresh`. Preserve dirty work for reconciliation.

Register owned child services before dependent checks. Stop them through the repository workflow before `service_release`.
Check that child HTTP checks exercise child code. Restart a registered server after its source changes.

## Orchestrate native sub-agents

Act as the orchestrator. Explicitly spawn native sub-agents for bounded implementation tasks and independent reviews. Use the host's available spawn, message, wait, and interruption tools. Do not simulate independent agents in the main context.

Keep the main context focused on requirements, decisions, task dependencies, and results. Store detailed evidence through the runtime. Record shell actions and their reasons with `action_record`. The other runtime tools record their own completed operations.

Read [native model policy](../../docs/model-policy.md) before dispatch.
New runs require `gpt-6-luna` for coding and `gpt-6.1-sol` for task, schema, and final reviews.
Keep the orchestrator's selected model.
For stored legacy runs, preserve their original dispatch instructions. Skip the new registration and escalation operations.

For each ready task:

1. Call `agent_requirements` with role `implementation` and the task ID.
2. Spawn a native worker with the returned model and `fork_turns: "none"` when supported.
3. Supply its run ID, task ID, state directory, specification references, and this skill path.
4. Call `agent_register` with the actual returned agent ID, requested model, role, target, and reason.
5. Tell the worker to claim the task using that native agent ID.
6. Require the worker to read `task_context` and work only in its assigned worktree.
7. Wait for actual completion before assigning review or integration.

Report a blocker when the host cannot select or access the required model. Do not silently substitute another model.
Record optional reported model information only from its stated source. Do not infer actual execution from a model request.

For explicit coding escalation, stop the native worker and its owned services before `task_abandon`.
Call `task_escalate` for the pending task with the orchestrator ID and a specific reason.
Read the new requirements. Spawn and register a new Sol worker, then claim a new attempt.
Preserve prior worktrees. Require current checks and fresh review. Escalation does not reset the attempt limit.

Workers must not spawn additional implementation workers. Limit concurrent workers to the host capacity and run policy. Assign dependent tasks after their dependencies integrate. Assign independent tasks in parallel when their contracts permit it.

Follow the target repository's instructions for staging, app servers, ports, and databases. The runtime creates Git worktrees. Use the repository's tools to start and stop its services.

## Worker procedure

1. Claim the task with `task_claim`.
2. Read `task_context`, the spec snapshot, and applicable `AGENTS.md` files.
3. Add or change a behavior test within the assigned test paths.
4. Call `check_start` with phase `red` and the configured test check ID.
5. Inspect the completed job through `job_status`.
6. If the assertion fails for the intended reason, record that reason with `red_confirm`.
7. Implement the smallest sufficient change.
8. Run the same test check with phase `green`.
9. Refactor and resolve configured quality checks using phase `quality`.
10. Stage only assigned source and test changes. Commit them in small ordered commits.
11. Call `task_submit`.

Repeat the red-to-green cycle when tests change. Existing tests and smoke tests remain required where relevant. Do not change tests to manufacture a passing result.

Use the bundle's recorded TDD exception only when an honest automated test is impractical. Preserve the reason and require independent assessment of the alternative evidence.

Checks return job IDs. Inspect bounded log tails while a job runs. Missing dependencies, timeouts, invalid reports, and unsupported checks do not count as passing evidence.

## Review and integrate

Call `agent_requirements` with role `task_review` and the submitted attempt ID.
Spawn a `gpt-6.1-sol` reviewer with a fresh context. Use `fork_turns: "none"` when supported.
Register its actual native ID and returned requirements through `agent_register` before `review_record`.
Supply the spec references, task packet, candidate revision, and evidence.
Do not supply the worker's conversation or private reasoning. Do not reuse review agents for changed targets.
Keep each native ID on one model. Use separate IDs for coding and review roles.

Require the reviewer to inspect behavior, test quality, TDD explanations or exceptions, scope, and current evidence. A reviewer must return concrete findings or an acceptance reason. Record the result with `review_record` using its actual native agent ID.

Return findings to the worker. After any candidate change, require new checks, submission, and review.

Call `task_accept` only after the latest review accepts the candidate. Integrate accepted tasks with `task_integrate`. Preserve conflicts and consult the repository's integration workflow. Use `run_recover` and `integration_reconcile` after an interrupted integration.

Run configured final checks against the combined integration worktree. Use role `final_review` and the run ID to dispatch and register a fresh Sol reviewer against the spec.
Record its findings with `run_review_record`. Call `run_complete` only after every requirement has current evidence.

Completion produces an integration branch. Perform a push, PR, merge, or deployment only within the user's authorization and repository workflow. Upstream UBMode's main-line instruction does not override those boundaries.

## Continue and recover

Continue through all available tasks during an active execution request. If the user explicitly requests a Codex Goal, use the host's Goal tools. Define success as runtime completion plus the authorized delivery outcome. Do not infer permission to create a Goal from ordinary work.

For automatic continuation through a separate controller, read [supervisor operation](references/supervisor.md). Use one continuation owner per run. A supervisor and an active Goal must not dispatch concurrent turns.

After an interruption, call `run_recover`. Inspect native agents and preserved worktrees before resuming. A task claim does not prove that its agent remains alive. Stop a previous worker before abandoning its attempt. A replacement receives a new worktree.

After two approaches fail at the same gate, record their shared premise and reconsider it. Use the attempt limit to prevent repeated replacement without diagnosis. Preserve unresolved decisions and request only the input needed to continue.

Honor user pauses and cancellations. Report completion, a failure that needs action, or a material decision. Avoid repeated updates when the state does not change.

## Release completed worktrees

Read [cleanup operations](../../docs/cleanup-operations.md) before enabling cleanup.
Include an authorized `cleanup_policy` only when the user's instructions permit completed managed worktree removal.
Otherwise, present the exact cleanup plan and obtain the required authorization before `cleanup_authorize`.

Check actual native worker and reviewer completion. Stop owned child services and release their leases.
Call `writer_release` for each exact resource revision after those actors finish.
Record generated outputs through `artifact_register` after the producing operation. Do not adopt unknown files by filename pattern.

After `run_complete`, inspect the separate cleanup result. Use `cleanup_status` or `run_recover` to continue unfinished cleanup.
Report removed resources, blocked resources, retained branches, and private archive references.
The runtime never removes a borrowed prepared parent or its database. The parent PR workflow owns their retention.

## Evidence limits

The runtime checks recorded state and source fingerprints. Agent IDs are audit labels. They do not authenticate native agent identities. Agents with shell access can bypass tool procedures or edit state. Enforce acceptance through these tools and independent review. Do not describe the runtime as a security sandbox.

Fingerprinting includes tracked files and non-ignored source files. It excludes common untracked build artifacts. Runtime acceptance does not replace browser, service, database, or performance checks required by the spec.
