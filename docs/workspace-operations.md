# Prepared parent workspaces

Use this mode after your repository workflow prepares a feature worktree, dependency installation, and local database.
The prepared parent becomes the integration target. UB Execute borrows it and never removes it through run cleanup.

## Attach

Install PostgreSQL support when you use the built-in role probe:

```sh
.venv/bin/pip install -e '.[postgres]'
```

Call `workspace_attach` with the prepared parent path, a configuration object, and a reason.
Store credential values in private files. Pass only file references through MCP.

```json
{
  "parent_path": "/absolute/feature-worktree",
  "config": {
    "adapter": "generic",
    "dependency_dir": "node_modules",
    "schema_paths": ["prisma/"],
    "database": {
      "admin_env_file": "/absolute/feature-worktree/.env",
      "expected_identity": {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "feature_database"
      },
      "schema_apply_argv": ["npm", "run", "db:push"]
    },
    "app": {
      "port": 43000,
      "port_count": 100,
      "origin": "http://127.0.0.1:43000"
    }
  },
  "reason": "Use the prepared feature environment."
}
```

Credential files must have mode `600`. They can contain dotenv entries or a JSON object of string values.
The built-in adapter provisions a restricted worker role when `worker_env_file` is absent.
Alternatively, supply an existing private worker file through `database.worker_env_file`.
The probe checks the actual database and effective worker privileges.

The default worker role permits reads. Set `database.data_write` to `true` only when the test policy requires data writes.
Workers cannot create permanent schema objects or inherit owner privileges through the supplied credential.
Every effective connection key must target the recorded local database.

For Backkr, use `adapter: "backkr"` and the prepared feature worktree's private environment file.
The coordinator invokes `npm run db:push` from that parent.
The existing Backkr helper retains its baseline, snapshot, and migration rules.
Do not invoke `backkr-agents prepare` for a child. That operation creates a staging-based database.

Add these fields to the execution bundle:

```json
{
  "workspace_mode": "attached_parent",
  "workspace_id": "workspace-ID"
}
```

Set `repository` to the prepared parent path. Each new child starts from the current parent integration commit.
Without these fields, standalone execution retains its separate integration worktree.

## Child environment

Children link `node_modules` to the parent's resolved installation.
The runtime records dependency manifests and checks each dependency generation.
For Node dependencies, it records the actual executable, version, engine declaration, `.nvmrc`, and `.node-version`.
A runtime identity change invalidates dependency evidence and requires parent attachment and child refresh.
Repository preparation must select a compatible Node version. The runtime records declared constraints without evaluating semver ranges.
It rejects linked workspace packages that can resolve application code from the parent.
Such repositories require a specific source adapter.

Children receive private `.ub-parent/worker.env.json` files.
Checks clear inherited secrets and conflicting database settings before they read that file.
The generated environment assigns child paths for supported build outputs and caches.
Configure repository tools to honor those paths.
Tools that write inside shared `node_modules` require an adapter before worker execution.
Child cron jobs remain disabled by default.

The optional app configuration allocates distinct child ports.
Start and stop apps through your repository's service workflow.
Register the actual child process with `service_register` before dependent checks.
The registration retains environment leases until `service_release` checks that the process ended.
A registration is an ownership attestation. It does not prove that a process serves the expected source.
Check the child's behavior and source revision during review.

For Backkr, reserve and release child services through `backkr-agents`.
Run them with the generated worker environment. Do not copy the parent's full environment into a child.
Use `check_start` for bounded commands that need the generated environment and recorded leases.

Declare `environment_dependencies` on checks:

| Value | Evidence dependency |
| --- | --- |
| `source` | Current parent source commit |
| `dependencies` | Dependency target, manifests, and generation |
| `database` | Database identity, actual schema fingerprint, and generation |
| `data` | Shared mutable data generation and an exclusive writer lease |
| `http` | Registered child server and child commit |

Attached checks default to dependencies and the configured database.
Use `[]` for pure unit checks that do not consume the shared environment.
Workers check exact lease ownership before dispatch, after execution, and before result recording.
Completed evidence retains the verification receipt after normal lease release.
A lost data writer advances the data epoch without releasing another owner.
Data writers advance the shared data generation, including failed and interrupted writers.
An earlier data result cannot satisfy acceptance after another writer changes that generation.

## Schema changes

1. The worker commits a schema-only proposal within the configured schema paths.
2. The worker calls `schema_change_request` with that commit and the expected schema fingerprint.
3. A native independent reviewer checks the proposal and records `schema_review`.
4. The parent stops affected services and waits for database readers and writers to release their leases.
5. The parent invokes the coordinator CLI.
6. The coordinator journals intent, integrates the schema commit, applies it, and probes the actual result.
7. Children reconcile their clean branches with the current parent commit, then call `workspace_refresh`.
8. Workers rerun affected checks before task submission and acceptance.

Obtain the expected fingerprint from a coordinator-controlled test database with the intended schema.
Do not infer it from an exit message or an incremented counter.

```sh
.venv/bin/python -m ub_execute --state /absolute/state coordinator apply \
  --proposal-id schema-ID \
  --capability-file /absolute/state/parents/workspace-ID/coordinator.capability \
  --reason "Apply the independently reviewed schema proposal."
```

Schema application is absent from the shared MCP catalog.
The CLI checks a private coordinator capability file.
Native agents share operating system access. They can read files available to that user.
The reported capability level is `cooperative`. Hard isolation requires a separate sandbox or credential broker.

The runtime records provisional schema commits. Final task integration does not apply those original commits twice.
Resolve Git conflicts through the normal repository workflow. The runtime preserves them.

After an interrupted application, inspect the actual schema through the coordinator:

```sh
.venv/bin/python -m ub_execute --state /absolute/state coordinator recover \
  --workspace-id workspace-ID --reason "Inspect interrupted schema application."
```

A matching expected fingerprint completes the recorded intent.
A partial result retains the exclusive lease and requires reconciliation.
Use `--resolution retry` only after inspection checks that the database still matches the prior fingerprint.
The coordinator also checks its Git staging journal before repeating work.

Dependency installation remains a parent operation.
Stop dependency readers before installation. Reattach the updated parent to advance its generation.
Refresh clean children before their next dependency check.


One state store coordinates normal attached runs and updates their shared parent revision.
Local state stores share database leases and mutable data generations on the same machine.
A schema update from another store invalidates stale evidence and blocks stale application before dispatch.
It does not automatically adopt the other store's schema journal. Coordinate explicit reconciliation before continuing that workspace.
