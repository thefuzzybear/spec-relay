# Completed run cleanup

Cleanup removes only recorded, completed, owned worktrees.
Execution completion and cleanup completion remain separate states.
A blocked cleanup does not reverse the completed result.

## Enable cleanup

Authorize automatic cleanup in the execution bundle:

```json
{
  "cleanup_policy": {"authorized": true, "on_complete": true}
}
```

The orchestrator must still release actual native workers, reviewers, and owned services.
Call `writer_release` for each exact resource after those actors finish.
Use an attempt ID for a child. Use `<run-id>/integration` for a standalone integration resource.
Releasing a status record does not stop a native agent or process.

`run_complete` records the completed result first. It then enqueues authorized cleanup.
Inspect its `cleanup` result and use `cleanup_status` until the job finishes or reports a blocked resource.
The supervisor retries authorized cleanup after it releases its controller lock. Other blocked resources require a new inspection and plan.

Without a run policy, inspect a concrete plan through `cleanup_plan`.
Obtain the required user authorization for its exact paths and digest.
Record that authorization with `cleanup_authorize`, then invoke `cleanup_apply`.
An authorization must refer to the reviewed plan digest.

## Preservation and removal

For each eligible worktree, cleanup performs these checks:

1. Check recorded path, Git identity, branch, commit, writer release, and process locks.
2. Inventory every file and directory, including ignored files, without following symlinks.
3. Create a private filesystem archive and a self-contained Git bundle.
4. Restore both artifacts in a separate temporary directory and compare their recorded contents.
5. Repeat identity, writer, ref, and filesystem checks.
6. Remove the checked worktree through ordinary `git worktree remove`.
7. Reconcile the absent directory and registration with the archive record.

Every removal and restore checks the archive against its retained record and exact cleanup plan.
An internally valid archive from another worktree cannot satisfy that check.
Malformed recovery data records a blocked condition and preserves available source work.

Cleanup retains branches, archives, run state, and logs without automatic expiry.
Archive contents can include credentials. Archives stay local and private.
Reported archive bytes remain allocated. Reclaimed worktree bytes do not represent net disk savings.

Use `artifact_register` for exact generated files after a recorded producing operation.
Registration records their current hashes. Unknown untracked files block cleanup.
Changed registered files also block cleanup. Filename patterns alone do not authorize removal.

Dirty, staged, conflicted, abandoned, cancelled, active, foreign, and legacy worktrees remain protected.
Git index flags that can hide tracked edits block cleanup.
Unsupported ACLs, file flags, special files, nested repositories, submodules, and external Git storage also block cleanup.
Supported extended attributes, executable modes, timestamps, empty directories, binary data, and symlink targets survive restoration.

Cleanup never follows a child's borrowed dependency link.
It never removes the prepared parent, database, database snapshots, global dependency cache, or shared state store.
The parent PR workflow owns parent retention and release.

## Restore and recover

Call `cleanup_restore` with an archive ID and a new destination.
The operation refuses to overwrite an existing destination.
It reconstructs Git metadata from the bundle and restores recorded user files.
It does not restore the old worktree's `.git` pointer.

Inspect removed resources through `run_status`, `task_context`, and `run_recover`.
Those operations return retained archive references without recreating the worktree.
`run_recover` resumes unfinished authorized cleanup jobs.
A blocked job requires a new inspection and plan after its blocking condition changes.

The removal journal supports interruption before archival, after restoration checks, and after ordinary Git removal.
It also records checked intermediate states while removing exact registered outputs.
An interruption before an intermediate checkpoint can require manual inspection.
Uncertain state blocks further removal and preserves the available archive.

Historical worktrees lack the required creation manifests.
This release does not infer ownership from directory names or branch prefixes.
Inspect them separately before any future adoption or removal.
