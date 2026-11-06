# Parent workspace and shared environment proposal

Status: accepted design, implemented in version 0.2.0. See the operator guide for supported behavior and recovery limits.
Date: 2026-11-06.

## Outcome

Start UB Execute from a prepared feature worktree. Use that worktree as the integration target and environment owner. Give task worktrees controlled access to its dependencies and local database.

The parent coordinator owns schema changes, dependency installation, and the feature PR. Task workers propose code changes and run checks under the environment policy.

```text
Prepared feature worktree and main agent
  |-- owns feature branch, dependency generation, and local Docker database
  |-- task A: code worktree, dependency link, worker environment
  |-- task B: code worktree, dependency link, worker environment
  `-- task C: code worktree, dependency link, worker environment
```

The hierarchy describes ownership. It does not require nested Git repositories on disk. Managed child directories can remain in the runtime state area.

## Original behavior and workflow

The staging PR workflow already prepares a feature branch and local database. Its database helper records a private schema baseline for the feature worktree. UB Execute should attach to that existing environment.

The current runtime creates a separate integration worktree. Add an explicit `attached_parent` mode that uses the prepared parent as its integration worktree. Preserve standalone mode for ordinary execution.

In attached mode, the parent branch is the delivery branch. The coordinator remains its only integration writer. Child branches start from the current parent integration commit. They must not restart from staging after the feature diverges.

Before attachment, check the parent Git identity, branch, tracked changes, dependency installation, local database identity, and environment settings. Preserve existing private files and service registrations.

## Ownership contract

| Resource or operation | Owner | Child permission |
| --- | --- | --- |
| Feature branch and integrated schema code | Parent coordinator | Propose changes through task branches |
| Installed dependencies | Parent or recorded upstream owner | Borrow the dependency generation |
| Local feature database | Parent environment | Connect through a restricted worker role |
| Schema push and schema baseline state | Parent coordinator | Request a schema change |
| Dependency installation | Parent coordinator | Request a dependency change |
| Child source and test files | Assigned task worker | Edit within task scope |
| Child build output and test caches | Assigned task attempt | Use private output paths |
| Child app server and port | Child resource record | Start and stop through the repository adapter |

Record ownership separately from resource access. A child that can use the database or dependencies does not own those resources.

## Dependency links

Create each child's `node_modules` link to the parent's installed dependency directory. Record the link, its resolved target, the lockfile digest, the package manifest digest, and the supported Node version.

If the parent's directory is itself a symlink, record its actual owner. Do not assume that the parent owns a shared installation from another checkout.

Workers must not run install, update, prune, rebuild, or code generation commands that change shared dependencies. A symlink alone does not make its target read-only.

Use child-local paths for `.next`, coverage, generated application code, and tool caches. Configure tools that normally write caches inside `node_modules`. Block unsupported shared writes until the adapter supplies a private destination.

Check linked local packages as well as external dependencies. A workspace package can resolve to parent source through a dependency symlink. Tests must prove that application code resolves from the child's checkout.

A task can propose package or lockfile changes. The parent reviews them, stops dependency readers, updates the shared installation, and advances its dependency generation. Children refresh against that generation before resuming affected checks.

The initial implementation can pause every dependency reader during an update. Immutable dependency generations can follow later if update frequency justifies them.

## Child environments and database access

Create a private environment file for each child. Keep it ignored and mode `600`. Do not symlink the parent's complete environment file.

The generated environment contains the parent's exact local database identity and a restricted worker credential. It also contains the child's own app URL and port when that child runs a server.

Do not give workers migration or owner credentials through environment files or inherited shell variables. Check all effective database URL keys. Clear conflicting inherited values before starting a worker check or app server.

Record a non-secret database identifier in task packets and logs. Resolve credentials only through the private environment adapter.

Worker roles must not own database objects, create permanent schema objects, inherit schema-owner rights, or assume a privileged role. Audit effective grants, role membership, and callable privileged functions. PostgreSQL associates schema-changing rights with object ownership and other privileges. See the [PostgreSQL privilege documentation](https://www.postgresql.org/docs/current/ddl-priv.html).

Default workers to read access. Grant the data-write access needed by specific integration tests through the test policy. Data writes and schema changes require different permissions.

This design needs an explicit enforcement boundary. Current native agents can share filesystem access and MCP credentials. Instructions and an agent ID field cannot establish a private coordinator capability.

The supported runtime must reject worker schema requests outside the coordinator pipeline. A hard prohibition also requires privileged credentials and execution capabilities that workers cannot access. A worker sandbox or separate credential broker must provide that isolation. Do not claim this guarantee when workers can read the parent's owner credentials or access an administrative Docker socket.

## Schema proposal and parent application

Workers can edit schema code and prepare a proposal. They must not run `db:push`, direct schema SQL, or migration commands. The parent applies reviewed local schema changes through the existing repository workflow.

Use a separate schema proposal phase before full task acceptance:

1. The worker records the schema patch, intended change, prerequisite schema generation, and checks that can run before application.
2. An independent reviewer checks the proposal and any data backfill or compatibility requirements.
3. The parent acquires an exclusive lease for the actual database identity.
4. The parent pauses database checks, dependent app writers, and cron jobs. It waits for their release.
5. The parent integrates the reviewed schema proposal into its feature branch and records the application intent.
6. The parent applies the change from the prepared parent worktree through the existing guarded database command.
7. The parent checks the resulting database schema and private schema state. It records the new schema generation.
8. Affected children refresh their code and environment context. They acknowledge the new generation before running database checks.
9. The worker runs its remaining checks and proceeds through normal task review and acceptance.

A proposal is not a completed task. This phase prevents a dependency cycle where task acceptance needs database tests but schema application waits for task acceptance.

For the Backkr adapter, the parent executes `npm run db:push`. It uses the prepared feature database and existing private schema baseline. Do not run `backkr-agents prepare` for each borrowed child environment. That command creates a new staging-based database.

Keep the staging integration rules intact. Feature work records schema code and PR notes. The staging integrator still generates migrations and checks them against current staging.

Serialize schema application across every run attached to the same database. A per-task or per-run lock is insufficient when several runs share an environment.

## Propagation and check evidence

Changes to the shared database become visible to connected children. Their Git source, generated code, and test evidence still need an explicit refresh.

Record environment generations in each task packet and relevant check result:

- parent workspace and integrated commit.
- dependency generation and lockfile digest.
- database identity and schema generation.
- test data generation when the check depends on mutable data.
- child app server identity and source revision when the check uses HTTP.

Mark checks with their actual environment dependencies. A schema change invalidates affected database evidence. Pure unit evidence can remain usable when its declared dependencies remain unchanged.

A child with uncommitted work must pause for reconciliation before adopting a new parent commit. Do not reset its files or switch its branch automatically. Preserve the work and reconcile through the normal task workflow.

The first implementation should stop affected checks during environment changes. Discard results from checks that overlap an environment transition. Later optimization can narrow invalidation with documented compatibility rules.

The runtime must inspect the actual schema during attachment and recovery. A generation counter alone cannot detect an out-of-band database push.

## Shared test data and app servers

Git worktrees isolate code. They do not isolate data in a shared database.

Allow compatible read checks in parallel. Give tests that change shared records an exclusive data-test lease. Require fixture cleanup and a known data baseline before the next writer.

Transaction rollback is sufficient only when every tested write uses the same controlled transaction. Browser and background-job tests often require stronger fixture isolation. If isolation cannot be established, serialize those tests or request a coordinator-created test clone.

Any test clone must start from the parent's current environment generation. A fresh staging clone can omit the feature schema already applied by the parent. Schema ownership still belongs to the coordinator.

A child server must serve its own checkout. Give it a private port and origin. A test against the parent's running app cannot prove behavior in an unintegrated child branch.

Keep child cron jobs disabled unless the task requires them. The repository adapter records and stops each child server and cron runner through its existing ownership workflow.

## Failure and recovery

Record schema application intent before invoking the repository command. Include the proposal commit, prior schema fingerprint, target database identity, and expected result.

After a failure or interruption, compare the actual database schema with that intent. A database transaction can finish before runtime bookkeeping records success. Do not apply the same SQL blindly or advance a generation based on an exit message alone.

Keep affected checks paused until reconciliation succeeds. Preserve the source proposal and private database evidence. Any corrective Git commit or database recovery must follow the parent workflow. Do not reset child work or restore shared database data automatically.

## Cleanup interaction

Extend the [cleanup lifecycle](cleanup-design.md) with owned and borrowed resource records.

For a child, cleanup stops its owned services and releases its environment lease. It archives its own files and the dependency link metadata. It unlinks the child link without traversing or removing the target.

Child cleanup must never remove the parent dependency directory, feature database, parent environment file, or parent service. Archive a child's private environment securely. Reissue credentials when restoring a runnable child environment.

The parent remains protected while any child holds a resource lease. A cleanup agent cannot infer resource release from its own run status alone.

UB Execute borrows the prepared parent from the PR workflow. Automatic run cleanup removes only runtime-created child worktrees. Even after child release, parent worktree removal still belongs to the PR workflow and its delivery conditions.

Removing a child directory does not authorize database removal. Parent database retention remains governed by the PR workflow and explicit ownership decisions.

## Runtime interface

| Operation | Purpose |
| --- | --- |
| `workspace_attach(parent_path, config, reason)` | Adopt a prepared parent after checking repository, environment, and coordinator ownership |
| `workspace_context(workspace_id)` | Return resource references, environment generations, and current leases without credentials |
| `schema_change_request(attempt_id, proposal, reason)` | Queue a worker's schema proposal for parent review and application |
| `workspace_refresh(run_id, attempt_id, reason)` | Reconcile a child with current parent code and environment generations |

Keep schema application behind the coordinator capability and repository adapter. Do not expose an unrestricted apply tool to every agent sharing the same MCP server.

Use internal lease operations for dependency readers, database checks, data writers, schema application, and cleanup. Check those leases again when jobs start. Agent instructions alone cannot serialize already-queued work.

## Implementation sequence and acceptance

1. Add parent workspace identity, attached integration mode, and resource manifests. Preserve standalone behavior.
2. Add dependency links, child environment creation, and source-resolution checks.
3. Add restricted database roles and the repository adapter's coordinator path.
4. Add the schema proposal phase, exclusive application lease, and recovery journal.
5. Add environment evidence, child refresh, and stale-check rejection.
6. Connect resource release to the cleanup lifecycle.

Tests must prove these outcomes:

- Child work starts from the prepared feature commit and borrows the expected dependency generation.
- Child checks execute child source and keep build output private.
- Worker schema commands fail through the supported runtime and through the worker's database credential.
- Privileged credentials and capabilities stay outside the worker's enforced access boundary when the runtime claims that guarantee.
- Parent application updates the feature database once and refreshes every affected child.
- Dirty children preserve their work while waiting for reconciliation.
- Concurrent schema requests serialize across runs, and stale database evidence cannot satisfy acceptance.
- Recovery distinguishes an unapplied change from a committed database change with incomplete bookkeeping.
- Shared data writers cannot contaminate another task's accepted test result.
- Child cleanup releases borrowed resources without removing parent data, dependencies, or services.

The initial design uses one parent environment, serialized schema application, dependency links, and isolated child outputs. Additional database clones and immutable dependency installations remain optional extensions.
