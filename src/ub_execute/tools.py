"""The public MCP and CLI operation catalog."""

import inspect

from .runtime import Runtime
from .store import WorkflowError

DESCRIPTIONS = {
    "action_record": "Record an agent action and its reason. This audit entry does not change acceptance state.",
    "plan_validate": "Check an execution bundle for missing requirements, invalid paths, and dependency cycles.",
    "run_create": "Snapshot a ready execution bundle and create an isolated integration worktree.",
    "run_status": "Read task progress, job status, and requirement references for one run.",
    "run_events": "Read ordered actions after a sequence number. Results include recorded reasons.",
    "run_control": "Pause, resume, or cancel a run. Pausing cancels active checks and preserves worktrees.",
    "task_claim": "Claim a ready task for one native agent and create its isolated worktree.",
    "task_context": "Read the task packet, spec snapshot, check definitions, and instruction paths.",
    "check_start": "Start a detached configured check. Return a job ID for later status requests.",
    "job_status": "Read a check result and bounded log tails. Detect an interrupted check worker.",
    "job_cancel": "Cancel a detached check. Preserve its logs and source worktree.",
    "red_confirm": "Record the worker's explanation of an observed failing assertion. A reviewer must assess this explanation.",
    "task_submit": "Submit a clean committed candidate with current checks and a valid TDD cycle or recorded exception.",
    "review_record": "Record a fresh-context review against the submitted candidate. Agent IDs are audit labels, not authentication.",
    "task_accept": "Accept a submitted candidate only when its checks and latest independent review remain current.",
    "task_integrate": "Apply accepted commits to the run's integration branch. Preserve conflicts for inspection.",
    "task_abandon": "Retire an unfinished attempt and preserve its worktree. Allow a new isolated attempt.",
    "run_recover": "Inspect unfinished attempts, check workers, and integration state without discarding work.",
    "run_complete": "Complete a run only after every task integrates and all final checks pass on the combined revision.",
    "integration_reconcile": "Reconcile a recorded integration intent after a completed or aborted cherry-pick. Preserve unresolved conflicts.",
    "run_review_record": "Record a fresh final review of all requirements against the combined integration revision.",
}
READ_ONLY = {"plan_validate", "run_status", "run_events", "task_context"}
INTEGER = {"after", "limit", "tail_bytes"}
OBJECT = {"bundle", "details"}
ARRAY = {"findings", "verified_requirements"}


def catalog():
    return [_definition(name) for name in DESCRIPTIONS]


def _parameter_schema(key, parameter):
    kinds = {"integer": INTEGER, "object": OBJECT, "array": ARRAY}
    kind = next((label for label, keys in kinds.items() if key in keys), "string")
    schema = {"type": [kind, "null"] if parameter.default is None else kind}
    if kind == "array":
        schema["items"] = {"type": "string" if key == "verified_requirements" else "object"}
    return schema


def _definition(name):
    parameters = {key: value for key, value in inspect.signature(getattr(Runtime, name)).parameters.items() if key != "self"}
    properties = {key: _parameter_schema(key, value) for key, value in parameters.items()}
    required = [key for key, value in parameters.items() if value.default is inspect.Parameter.empty]
    return {"name": name, "description": DESCRIPTIONS[name],
            "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            "annotations": {"readOnlyHint": name in READ_ONLY, "destructiveHint": name in {"run_control", "task_integrate", "job_cancel", "task_abandon"},
                            "openWorldHint": name in {"run_create", "check_start"}}}


def call(runtime, name, arguments):
    if name not in DESCRIPTIONS:
        raise WorkflowError("Unknown execution tool.")
    if not isinstance(arguments, dict):
        raise WorkflowError("Tool arguments must be an object.")
    definition = _definition(name)["inputSchema"]
    if set(arguments) - set(definition["properties"]) or set(definition["required"]) - set(arguments):
        raise WorkflowError("Tool arguments contain missing or unknown fields.")
    for key, value in arguments.items():
        _validate_value(key, value, definition["properties"][key]["type"])
    return getattr(runtime, name)(**arguments)


def _validate_value(key, value, kind):
    types = {"string": str, "integer": int, "object": dict, "array": list, "null": type(None)}
    allowed = kind if isinstance(kind, list) else [kind]
    valid = any(isinstance(value, types[item]) for item in allowed)
    if "integer" in allowed and isinstance(value, bool):
        valid = False
    if not valid:
        raise WorkflowError(f"Invalid argument type: {key}")
