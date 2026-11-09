# Native agent model policy

New runs use GPT-6 Luna for implementation and GPT-6.1 Sol for task, schema, and final reviews.
The orchestrator keeps the model selected for the main chat.
Codex starts the native agents. The MCP stores assignments and checks them at workflow gates.

| Role | Required model | Assignment target |
|---|---|---|
| Implementation | `gpt-6-luna` | Task ID |
| Task review | `gpt-6.1-sol` | Attempt ID and candidate revision |
| Schema review | `gpt-6.1-sol` | Proposal ID and schema revision |
| Final review | `gpt-6.1-sol` | Run ID and integration revision |

## Start an assigned agent

Call `agent_requirements(run_id, role, target_id)` before native dispatch.
Start a native agent with the exact model and a fresh context.
Supply the task packet, specification references, state directory, and relevant evidence.
Call `agent_register` with the returned native agent ID, requested model, role, target, and reason.
The worker can then claim its task. The reviewer can record its findings after inspection.

A registration does not create a native agent.
Native dispatch must happen through the host's sub-agent tools.
Codex supports explicit per-agent models. See the [official sub-agent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents).
If the host cannot select or access the required model, report the blocker.
Do not substitute another model or use the main context as an independent reviewer.

## Escalate coding to Sol

Escalation changes one pending coding task from Luna to Sol.
The orchestrator must record its agent ID and a specific reason.
If a worker already owns the task, stop that native worker before abandoning its attempt.
Check that its check jobs and owned services stopped. Preserve its worktree.
Call `task_escalate(run_id, task_id, orchestrator_id, reason)`.
Read the new requirements. Start and register a new Sol coding agent.
The replacement claims a new attempt and receives a new worktree.
Recover useful changes through deliberate inspection. Run current checks and obtain independent review again.

Escalation does not reset the attempt limit.
It does not change the model of an existing agent.
Reviews remain on Sol. No further model fallback is implicit.

## Model provenance

Assignments store `requested_model`, optional `reported_model`, `model_source`, and `model_reference`.
A supplied report uses source `host_metadata`, `agent_report`, or `orchestrator`.
Host metadata requires a reference. Source and reference fields require a reported model.
All supplied reports remain claims with stated sources. None establishes independent authentication.
Record host metadata only when the host supplies it.
If the host exposes only the request, leave the actual model unknown.
An agent's own statement does not establish host verification.
The runtime rejects reported model mismatches.

Native agent IDs and supplied metadata are cooperative audit records.
The MCP does not independently authenticate agent identity, model selection, or model execution.
Shell access can bypass the recorded procedure.

## Tool sequence

```text
agent_requirements(run_id, "implementation", task_id)
<host native spawn with the returned model and fresh context>
agent_register(run_id, native_agent_id, requested_model, "implementation", task_id, reason)
task_claim(run_id, task_id, native_agent_id, reason)
```

Before task review, use role `task_review` and the submitted attempt ID.
Before schema review, use role `schema_review` and the proposal ID.
Before final review, use role `final_review` and the run ID.
Changed targets require a fresh review agent and assignment.
A native agent ID keeps one requested model throughout a run.
Coding and review assignments use separate native agent IDs, in either assignment order.
Read assignment records through run status and recovery after an interruption.

## Existing runs

Stored runs created before this policy retain their original behavior.
They report legacy policy status and do not receive invented model assignments.
When `agent_requirements` reports `legacy`, skip registration and escalation. Preserve the original dispatch instructions.
Create a new run to require the default model policy.
New bundles use the fixed role defaults. Unsupported policy fields or model substitutions fail validation.
