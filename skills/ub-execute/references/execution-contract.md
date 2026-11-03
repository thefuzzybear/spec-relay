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

The runtime preserves the original branch. It integrates accepted task commits into its own branch. Each final check must pass against the current combined source. A final review must cover every requirement before completion.

The append-only event record contains completed operations and their reasons. It is an operational audit record. It is not a tamper-proof record of every shell action.
