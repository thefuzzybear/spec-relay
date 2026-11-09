# Spec Relay

Turn a reviewed specification into tested commits with native Codex sub-agents and a local Python MCP runtime.

Spec Relay builds on a fork of [Robert C. Martin’s UBMode](https://github.com/unclebob/ub-mode). It adds a Codex skill, durable execution state, and structured adapters for Crapper, Dryer, and Mutator. The skill and MCP server use the name `ub-execute`.

The global skill directs implementation and fresh-context review. The runtime preserves task ownership, worktrees, check jobs, source fingerprints, and an ordered event record.

## Install

Use Python 3.11 or later on macOS or Linux. Keep this checkout at its installed location.

```sh
git clone https://github.com/thefuzzybear/spec-relay.git
cd spec-relay
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/install.py
.venv/bin/python scripts/install.py --apply
```

The installer first prints its plan. `--apply` creates a global skill symlink and registers the STDIO MCP server through `codex mcp add`. It preserves existing unrelated configuration. It refuses to replace an unrelated skill or server with the same name.

Restart Codex or start a fresh session to discover the skill and tools. Invoke:

```text
Use $ub-execute to implement /absolute/path/to/reviewed-spec.md in this repository.
```

For native Goal continuation, explicitly request a Goal in the invocation. For bounded App Server continuation, read the skill's [supervisor instructions](skills/ub-execute/references/supervisor.md).

Try the [first run guide](docs/quickstart.md) on a disposable repository.

## Execution

The agent reads the spec and repository instructions. It builds a version 1 execution bundle, checks its structure, and creates a run. Each requirement maps to tasks and acceptance criteria.

New runs assign `gpt-6-luna` to coding and `gpt-6.1-sol` to task, schema, and final reviews.
The orchestrator keeps its selected model. Explicit coding escalation can assign a replacement Sol worker with a recorded reason.
Read [native model policy](docs/model-policy.md) for assignment, provenance, and unavailable-model handling.

Each worker owns a separate worktree. It records a failing behavior test, implements the behavior, refactors, runs configured checks, and commits the candidate. A fresh reviewer evaluates the candidate and evidence. The runtime accepts current evidence and integrates accepted tasks in dependency order.

Final checks run against the combined branch. A final reviewer must address every requirement. Standalone completion returns the integration branch. Attached execution integrates into the prepared parent feature branch. Repository instructions govern later pushes, pull requests, merges, and deployments.

Read the [execution contract](skills/ub-execute/references/execution-contract.md) for bundle fields and check configuration. The agent constructs this bundle. The runtime does not infer requirements from prose.

```sh
.venv/bin/python -m ub_execute tools
.venv/bin/python -m ub_execute --state /absolute/state call plan_validate --args-file arguments.json
.venv/bin/python -m ub_execute --state /absolute/state serve
```

The default state directory is `.state` in this checkout. All clients must use the same state directory for a run. Check jobs run in detached processes and retain full logs outside the source tree. Runtime and MCP restarts do not discard their state.

Intake snapshots the spec and optional `review_artifacts`. Check evidence records the repository fingerprint and the runtime source fingerprint. Runtime updates require new passing checks. Workers record shell actions and reasons with `action_record`.

## Quality tools

Pinned upstream sources and licenses live in `vendor/`. Revisions appear in `upstream.lock.json`. Local changes appear in [the fork notes](docs/fork-notes.md).

The runtime has no third-party Python dependencies. The optional quality tools use Tree-sitter. Parser installation is explicit and uses this checkout's `.parser-cache`. Analysis does not install target-project dependencies or download parsers.

```sh
.venv/bin/pip install -e '.[quality]'
.venv/bin/python -m ub_execute quality-setup python
```

Add the parsers your repository needs. The available parser names include `typescript`, `tsx`, `javascript`, `java`, `go`, `rust`, and `clojure`.

Adapters produce versioned JSON. Crapper requires fresh coverage and `CRAP < 10`. Dryer reports structural candidates at similarity `>= 0.8`. Mutator separates uncovered sites, survivors, invalid mutations, timeouts, and errors. The adapter reruns all selected mutants, so old results cannot hide changed tests.

Commands, scope, and required checks come from the intake bundle. Missing or unsupported checks cannot count as passing evidence. Analyzer limitations can require a documented scope decision. Do not enlarge code to satisfy a metric.

## Recovery and limits

Use `run_recover` to inspect unfinished attempts, check workers, integration state, and cleanup progress. Abandoned attempts keep their files.

Version 0.2 adds [completed run cleanup](docs/cleanup-operations.md). Authorized cleanup archives every eligible worktree and tests restoration before ordinary Git removal. Dirty work, unknown files, active writers, and historical worktrees without ownership manifests remain protected. Branches and private recovery archives remain available.

[Prepared parent workspaces](docs/workspace-operations.md) reuse a feature worktree's dependencies and local PostgreSQL database. Children borrow dependency links and receive restricted private environments. The parent applies independently reviewed schema proposals through a coordinator CLI. Child cleanup never removes the prepared parent or its database.

The PostgreSQL adapter uses optional `.[postgres]` dependencies. Native agents share operating system access, so parent environments report cooperative capability. Restricted database roles enforce the supplied worker credential's permissions. Hard isolation requires a separate sandbox or broker.

Integration records its intent before changing Git state. `integration_reconcile` checks completed or aborted cherry-picks after interruption. Unresolved conflicts require inspection. The runtime never discards them automatically.

The optional supervisor resumes a supplied Codex thread through `codex app-server`. Its transport and continuation policy have offline tests. Desktop attachment still requires testing against the user's installed Codex session. The supervisor does not install a daemon or start automatically after machine shutdown.

Agents with shell access can bypass procedures and edit state. Agent IDs are audit labels. Fresh-context review depends on native agent dispatch. This runtime is an execution aid, not a security sandbox or a tamper-proof audit service.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover execution, parent environments, schema application, cleanup restoration, concurrent claims, stale evidence, recovery, MCP messages, and continuation policy.
Node source-resolution fixtures require an available Node.js runtime. PostgreSQL tests require a disposable test server. Quality integration tests require the optional dependencies and selected parsers.

See the [verification record](docs/verification.md) for test results and limits. Spec Relay uses the [MIT license](LICENSE). The pinned upstream copies retain their own [licenses and attribution](docs/fork-notes.md).
