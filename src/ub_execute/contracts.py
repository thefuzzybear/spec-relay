"""Validate execution bundles at the input boundary."""

import json
import math
import re
from pathlib import Path

from .store import WorkflowError
from .workspace import relative_path


DEFAULT_MODEL_POLICY = {
    "implementation": "gpt-6-luna",
    "task_review": "gpt-6.1-sol",
    "schema_review": "gpt-6.1-sol",
    "final_review": "gpt-6.1-sol",
}


def need(condition, message):
    if not condition:
        raise WorkflowError(message)


def text(value, label):
    need(isinstance(value, str) and bool(value.strip()), f"Provide {label}.")


def names(value, label, empty=False):
    need(isinstance(value, list) and (empty or bool(value)), f"Provide a list of {label}.")
    need(all(isinstance(item, str) and item.strip() for item in value), f"Invalid {label}.")
    need(len(set(value)) == len(value), f"Duplicate {label}.")


def validate_bundle(raw):
    need(isinstance(raw, dict), "The execution bundle must be an object.")
    bundle = json.loads(json.dumps(raw))
    bundle["model_policy"] = _model_policy(bundle.get("model_policy"))
    need(type(bundle.get("version")) is int and bundle["version"] == 1, "Use execution bundle version 1.")
    _locations_and_limits(bundle)
    _lifecycle(bundle)
    _readiness(bundle.get("readiness", {}))
    requirement_ids = _requirements(bundle.get("requirements"))
    checks = _checks(bundle.get("checks"))
    tasks = bundle.get("tasks")
    need(isinstance(tasks, list) and bool(tasks), "Provide implementation tasks.")
    for task in tasks:
        _task_definition(task, requirement_ids, checks)
    ids = [task["id"] for task in tasks]
    names(ids, "task IDs")
    covered = {key for task in tasks for key in task["requirements"]}
    need(covered == set(requirement_ids), "Every requirement needs an implementation task.")
    _dependencies(tasks, ids)
    names(bundle.get("final_checks"), "final check IDs")
    need(set(bundle["final_checks"]) <= set(checks), "Unknown final check.")
    return bundle


def _model_policy(policy):
    if policy is None:
        return dict(DEFAULT_MODEL_POLICY)
    need(isinstance(policy, dict), "The model policy must be an object.")
    need(set(policy) == set(DEFAULT_MODEL_POLICY), "The model policy has unknown or missing roles.")
    need(policy == DEFAULT_MODEL_POLICY, "Use the supported default model policy.")
    return dict(DEFAULT_MODEL_POLICY)


def _locations_and_limits(bundle):
    for name, default in (("max_attempts", 3), ("max_parallel_tasks", 2)):
        value = bundle.setdefault(name, default)
        need(type(value) is int and 1 <= value <= 64, f"Invalid {name}.")
    for field in ("spec", "repository"):
        text(bundle.get(field), field)
        bundle[field] = str(Path(bundle[field]).expanduser().resolve())
    names(bundle.get("review_artifacts", []), "review artifact paths", empty=True)


def _readiness(readiness):
    need(isinstance(readiness, dict), "Provide readiness information.")
    need(readiness.get("status") == "ready", "The specification is not ready for execution.")
    need(isinstance(readiness.get("open_findings", []), list), "Invalid open findings.")
    for finding in readiness.get("open_findings", []):
        need(isinstance(finding, dict), "Invalid review finding.")
        severity = finding.get("severity", "")
        need(isinstance(severity, str), "Invalid finding severity.")
        need(severity.lower() not in {"blocker", "major", "critical"}, "Resolve the open review finding before execution.")


def _requirements(requirements):
    need(isinstance(requirements, list) and bool(requirements), "Provide requirements.")
    for requirement in requirements:
        need(isinstance(requirement, dict), "Invalid requirement.")
        text(requirement.get("id"), "a requirement ID")
        text(requirement.get("text"), "requirement text")
    requirement_ids = [entry["id"] for entry in requirements]
    names(requirement_ids, "requirement IDs")
    return requirement_ids


def _checks(checks):
    need(isinstance(checks, dict) and bool(checks), "Provide check definitions.")
    for name, check in checks.items():
        text(name, "a check ID")
        need(isinstance(check, dict), "Invalid check definition.")
        _command(check.get("argv"))
        need(check.get("kind", "test") in {"test", "command", "crapper", "dryer", "mutator"}, "Unknown check kind.")
        seconds = check.get("timeout_seconds", 300)
        need(isinstance(seconds, (float, int)) and math.isfinite(seconds) and 0 < seconds <= 86400, "Invalid check timeout.")
        check["timeout_seconds"] = seconds
        if 'environment_dependencies' in check:
            names(check['environment_dependencies'], 'environment dependencies', empty=True)
            need(set(check['environment_dependencies']) <= {'source','dependencies','database','data','http'}, 'Unknown environment dependency.')
    return checks


def _task_definition(task, requirement_ids, checks):
    need(isinstance(task, dict), "Invalid task.")
    task_id = task.get("id", "")
    need(isinstance(task_id, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", task_id), "Invalid task ID.")
    text(task.get("title"), "a task title")
    names(task.get("requirements"), "task requirement IDs")
    need(set(task["requirements"]) <= set(requirement_ids), "A task refers to an unknown requirement.")
    for field in ("acceptance", "scope", "checks"):
        names(task.get(field), field)
    names(task.setdefault("depends_on", []), "task dependencies", empty=True)
    need(set(task["checks"]) <= set(checks), "A task refers to an unknown check.")
    for path in task["scope"]:
        relative_path(path)
    _tdd_contract(task, checks)


def _tdd_contract(task, checks):
    if task.get("tdd_exception"):
        text(task["tdd_exception"], "the TDD exception reason")
    else:
        names(task.get("test_paths"), "test paths")
        for path in task["test_paths"]:
            relative_path(path)
        need(any(checks[key].get("kind", "test") == "test" for key in task["checks"]), "A task needs a behavior test.")
    task.setdefault("test_paths", [])


def _dependencies(tasks, ids):
    graph = {task["id"]: task["depends_on"] for task in tasks}
    for dependencies in graph.values():
        need(set(dependencies) <= set(ids), "Unknown task dependency.")
    done = set()
    while len(done) < len(graph):
        ready = {key for key, dependencies in graph.items() if set(dependencies) <= done} - done
        need(bool(ready), "The task dependency graph contains a cycle.")
        done.update(ready)


def _command(argv):
    need(isinstance(argv, list) and argv and all(isinstance(arg, str) and arg for arg in argv), "Provide command arguments.")


def _lifecycle(bundle):
    mode = bundle.setdefault('workspace_mode', 'standalone')
    need(mode in {'standalone','attached_parent'}, 'Use standalone or attached_parent workspace mode.')
    if mode == 'attached_parent':
        text(bundle.get('workspace_id'), 'the attached parent workspace ID')
    else:
        need(not bundle.get('workspace_id'), 'Use attached_parent mode with a workspace ID.')
    policy = bundle.get('cleanup_policy', {})
    need(isinstance(policy, dict) and set(policy) <= {'authorized','on_complete'}, 'Invalid cleanup policy.')
    need(all(type(value) is bool for value in policy.values()), 'Cleanup policy values must be boolean.')
    need(not policy.get('on_complete') or policy.get('authorized'), 'Automatic cleanup requires explicit authorization.')
