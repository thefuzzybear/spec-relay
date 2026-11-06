# Cleanup lifecycle proposal

Status: accepted design, implemented in version 0.2.0. See the operator guide for supported behavior and recovery limits.
Date: 2026-11-06.

## Outcome

Reclaim completed run worktrees while preserving their results, history, and recovery evidence. Cleanup must explain each removal and each blocked candidate.

Use a Python cleanup coordinator for filesystem and Git operations. A native cleanup agent can inspect exceptions and report decisions. An agent's judgment alone cannot authorize removal.

Keep execution status separate from cleanup status. A run can be complete while cleanup remains blocked. Cleanup failure must preserve the completed result.

## Findings before version 0.2.0

- `Runtime.run_create` creates an integration worktree. `Runtime.task_claim` creates one worktree for each attempt.
- `Runtime.task_abandon` preserves the attempt directory. Completed runs retain both integration and attempt worktrees.
- `workspace.file_names` excludes some ignored and generated files. `workspace.require_clean` uses that list for submission checks.
- Those submission checks cannot certify removal. Cleanup needs a complete filesystem inventory, including ignored and untracked files.
- `Runtime.run_recover` expects the integration directory to exist. Recovery must support archived and removed worktrees before enabling automatic cleanup.
- The shared state directory also contains installation files, reports, and a public release checkout. A directory's location under `.state` does not establish cleanup ownership.

## Eligibility and retention

| Candidate | Automatic action |
| --- | --- |
| Completed run, released writers, unchanged task worktree, checked recovery copy | Remove the eligible task worktree |
| Completed run, unchanged runtime-created integration worktree, retained delivery branch, checked recovery copy | Remove the integration worktree |
| Borrowed prepared parent from the PR workflow | Preserve it for that workflow's delivery and release rules |
| Active, paused, or attention-required run | Preserve the worktree |
| Cancelled or abandoned attempt | Preserve it pending a recovery decision |
| Tracked edits, staged edits, conflicts, unexpected commits, or unknown untracked files | Preserve the worktree and report the difference |
| Missing ownership data, an active writer, or an unknown resource | Preserve the worktree |
| Failed archive creation, insufficient space, or failed restore check | Preserve the worktree |

Start with cleanup after whole-run completion. Early cleanup after task integration can follow later. It adds coordination with dependent tasks and reviewers.

Retain spec snapshots, check logs, reviews, action records, archive manifests, and recovery bundles. Retain archives without automatic expiry in the first release. Archive expiry needs a separate retention decision.

Preserve the integration branch when the requested delivery is a local branch. Worktree removal does not require a merge, push, or branch removal.

## Ownership and writer release

For runs attached to a prepared parent, apply the [parent workspace ownership contract](parent-workspace-design.md). Child resource leases protect borrowed dependencies and databases. Child removal must not remove or traverse a borrowed target.

Record a resource manifest when the runtime creates a worktree. Include the run ID, attempt ID, canonical path, Git common directory, expected branch, and base commit. Record the repository's primary worktree separately as protected.

Before planning removal, compare the manifest with Git's registered worktrees and the current filesystem. Reject path escapes, symlink substitutions, changed repository identities, and unexpected branches.

The native orchestrator must check that workers and reviewers finished or stopped. Record that release against the exact attempt and candidate revision. A terminal database status does not establish native agent liveness.

Check the detached worker locks, command locks, and supervisor controller lock. Repository adapters must also stop and release owned app servers and other services. An unavailable adapter or uncertain writer state blocks removal.

Use an exclusive cleanup lease for each candidate. All runtime operations that can dispatch work or change that candidate must honor the lease. Cleanup cannot rely on a lock used only by other cleanup calls.

Existing runs need an inventory and ownership-adoption pass. Directory names and branch prefixes alone cannot authorize removal of older worktrees.

## Preservation contract

Each eligible candidate needs two recovery artifacts outside every removal target:

1. A self-contained Git bundle with the candidate's recorded history and commit IDs.
2. A filesystem archive of the worktree contents, including ignored files and generated outputs.

Do not use the execution fingerprint as an archive manifest. Enumerate every directory and file without following symlinks. Record hashes, file types, executable bits, and symlink targets. Preserve empty directories and supported metadata.

Record the worktree's Git linkage as metadata. Do not restore its `.git` pointer into the recovery destination. Recreate Git metadata from the bundle before restoring user files. Compare the restored user files separately from that generated Git metadata.

Use private local archive permissions. Ignored files can contain credentials. Archives must never enter the public repository or upload automatically.

An ignored path is not evidence that its contents are disposable. Preserve its contents before Git removes the worktree. Avoid filename rules that automatically discard `.env`, `node_modules`, `.venv`, or `target`.

The first release must block unsupported special files, submodules, nested repositories, and external object stores. Add adapters only when they preserve the required data and pass restoration tests.

Write archives to temporary destinations. Complete and flush their contents before atomically publishing the archive manifest. A partial archive cannot authorize removal.

Check Git recovery in a temporary repository without access to the source repository. Restore the filesystem archive into a separate directory. Compare the restored inventory and hashes with the original inventory.

Git bundles preserve committed objects. They do not replace filesystem archives for uncommitted or ignored data. See the [Git bundle manual](https://git-scm.com/docs/git-bundle).

After checking, repeat the ownership, writer, Git, and filesystem checks. Any change invalidates the removal plan.

## Removal protocol

Use this durable progression for each worktree:

```text
discovered -> inspected -> archived -> restore_verified -> removal_authorized
           -> removal_started -> removed -> reconciled
```

At each stage, a failed condition records `blocked` and its reason. Recovery resumes from the last checked stage. It must inspect the current filesystem before repeating an operation.

Automatic removal requires an execution policy that authorizes cleanup of checked managed resources. Honor repository rules that require confirmation before removal. For older runs without that authorization, present a concrete plan for approval.

Recheck the plan digest immediately before removal. Bind it to the exact paths, Git identities, branch tips, complete inventory, and archive checksums.

Use `git worktree remove` without force. Git provides additional protection against tracked edits, untracked files, and locked worktrees. Respect explicit user locks. See the [Git worktree manual](https://git-scm.com/docs/git-worktree).

For generated untracked files, first check their recorded ownership and archive copies. Remove only those exact archived entries before ordinary Git removal. Unknown untracked files block the candidate. Do not use a broad `git clean` command.

The first release preserves all branch refs. A later branch-retirement operation can use archived refs and compare-and-swap checks. Cherry-picked commits require special handling because their original commit IDs can differ from the integration history.

After removal, check that the directory and its Git worktree registration are absent. Record the archive ID and the retained result branch. Update recovery paths to reference the archive.

If the process stops after Git removal but before the database update, reconciliation must record the observed removal. It must not repeat a filesystem removal.

## Runtime tools

| Tool | Contract |
| --- | --- |
| `cleanup_plan(run_id)` | Inspect recorded resources and return eligible candidates, blocked candidates, reasons, and a plan digest |
| `cleanup_apply(plan_id, reason)` | Check authorization, repeat eligibility checks, preserve and restore-test candidates, then remove checked resources |
| `cleanup_status(cleanup_id)` | Return per-resource progress, archive references, and unresolved conditions |
| `cleanup_restore(archive_id, destination)` | Restore into a new destination without overwriting existing work |

Planning performs no removal. Applying a plan starts a durable job and returns its ID. Repeated requests use the same cleanup ID and cannot create duplicate removals.

Keep actor-release records separate from these tools. They attest to native agent and repository service release. Tools must not invent those attestations from elapsed time.

## Completion hook and recovery

After `run_complete` commits the completed execution result, enqueue cleanup under the run's authorized policy. The native orchestrator reports both outcomes. For example: `Execution complete. Two worktrees removed. One worktree preserved because an untracked file needs a decision.`

Cleanup must also run during normal recovery when an authorized cleanup job remains unfinished. No recurring automation is necessary for this lifecycle. Starting a service after machine shutdown remains a separate operational choice.

Extend `run_status`, `task_context`, and `run_recover` before enabling the completion hook. They must handle missing worktree directories when a checked archive exists. Completed results must remain inspectable without recreating their worktrees.

Register created services and generated artifacts when their owning operations run. Stop only resources with matching ownership records. The initial cleanup implementation must not remove databases, snapshots, global caches, the shared state store, or unrelated repositories.

## Implementation boundaries

- Add cleanup state and artifact manifests through the existing Store. Keep cleanup state separate from execution state.
- Add complete inventory, preservation, restore checking, and removal helpers in a focused cleanup module. Preserve the existing submission fingerprint behavior.
- Add the four public operations to Runtime and the tool catalog. Mark mutation capabilities accurately.
- Add writer-release and service-release checks to the skill workflow. Require actual native completion before acknowledging release.
- Update recovery and status methods to use retained refs and archive metadata after removal.
- Enable the completion hook only after the preservation and restore tests pass.

## Acceptance tests

1. Remove a completed eligible task worktree and restore its exact recorded contents in a new destination.
2. Preserve a completed integration result after removing its worktree. Inspect the result through the retained branch and archive.
3. Preserve dirty, staged, conflicted, abandoned, and cancelled worktrees. Explain the condition that blocks each candidate.
4. Preserve ignored credentials and generated data byte-for-byte in a private archive before removing an eligible worktree.
5. Block unknown untracked files and unexpected commits, including commits added after execution completed.
6. Block active native writers, detached checks, command children, app servers, and unavailable resource adapters.
7. Reject changed files, changed refs, substituted paths, and stale plans between inspection and removal.
8. Preserve source work when archive storage fills, archive checks fail, or restoration produces a mismatch.
9. Recover from interruptions before archival, after checking, after Git removal, and before database reconciliation.
10. Reject concurrent cleanup claims and runtime writes against a cleanup lease.
11. Preserve unrelated repositories, primary worktrees, and the public release checkout under the shared state directory.
12. Exercise filenames with spaces, newlines, binary contents, executable bits, empty directories, and symlinks without path traversal.

Measure reclaimed bytes separately from archived bytes. Report the retained recovery data so cleanup does not imply that all disk usage disappeared.
