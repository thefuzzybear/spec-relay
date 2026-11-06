# Verification record

Previous release verification: 2026-11-03.

## Version 0.2.0 lifecycle verification

Date: 2026-11-06. Platform: macOS, Python 3.14, PostgreSQL 16.

All 145 runtime tests passed with the PostgreSQL test configuration enabled.
Two integration cases directly used a disposable PostgreSQL server.
The PostgreSQL-gated class contains ten cases, including inherited parent checks.
No feature or staging database supplied test data.
The offline suite skips those ten tests when `UB_PARENT_TEST_ADMIN_DSN` is absent.

Seventeen combined lifecycle tests exercise the public Runtime operations.
They cover parent integration, child source resolution, private environments, service leases, schema-only tasks, and stale evidence.
They also check schema staging recovery, exact artifact registration, cleanup leases, private restoration, and parent retention.
A supervisor regression checks cleanup dispatch after controller release.
Queued checks reject lost or changed lease ownership. Completed evidence requires a saved dispatch and result verification receipt.

Cleanup tests preserve ignored secrets, binary data, symlink targets, extended attributes, empty directories, executable modes, and timestamps.
They reject hidden index flags, unknown files, stale plans, unsupported metadata, and foreign resources.
Process-death tests check interrupted removal and exact generated-file recovery.
Archive tests reject valid foreign archives, retained-record mismatches, malformed manifests, and truncated tar data.

The PostgreSQL tests check worker role restrictions and inherited owner hazards.
They also check schema fingerprint changes for row security, policies, views, triggers, and sequences.
Separate tests reject conflicting coordinator URLs and a stale baseline before privileged dispatch.
Shared data generations invalidate evidence across local state stores.
Node tests bind the actual executable, version, and repository declarations to dependency evidence and child refresh.

Crapper measured 363 runtime functions below CRAP 10.
Dryer found no duplicate candidates at similarity 0.8 or higher.
The behavior tests killed every selected mutant in the shared-lease and process-lock scopes.
Mutation checks use selected source files. This release does not claim a full-runtime mutation score.

Native independent reviews found defects in cleanup recovery, archive binding, lease ownership, connection settings, and schema fingerprints.
Each defect received a focused regression and a checked fix before acceptance.

The skill validator and documentation linter passed.
The parent capability remains cooperative because native agents share operating system access.
A separate sandbox or credential broker must enforce hard isolation.
Cross-store schema adoption remains an explicit reconciliation step.
Archives and branches have no automatic expiry.

## Independent native-agent exercise

The skill-creator forward test used a disposable pricing repository. One native worker implemented two dependent tasks. Fresh native reviewers assessed each candidate and the combined result.

The run completed on an integration branch. The original fixture branch retained its initial commit.

- Seven behavior tests passed.
- Crapper reported 100% coverage and CRAP 1 for both functions.
- Dryer reported no duplicate candidates in the configured combined scope.
- Mutator killed all three selected pricing mutants.
- Fresh CLI processes retained usable detached check jobs.
- Runtime gates rejected a dependency claim, acceptance without review, completion before integration, and completion without final review.

The exercise exposed a generated-artifact cleanliness defect and a missing optional-field default. Both defects received fixes and regression tests. A later process-recovery change required `ps`, which the sandbox denied. Kernel file locks replaced that dependency. Successful retries preserved the earlier failed jobs.


## Runtime and adapters

All 49 runtime regression tests passed. They check concurrency, TDD evidence, stale source, changed tests, runtime provenance, review binding, detached processes, and recovery. They also check MCP messages, App Server transport, installer idempotency, and continuation limits.

Crapper measured 121 runtime functions below CRAP 10. Dryer found no runtime duplicate candidates at similarity 0.8 or higher. A separate mutation test killed all four selected process-lock mutants.

The vendored compatibility suite passed 301 tests. One test requires the absent Babashka executable and did not run. That test checks EDN interoperability. The JSON adapters do not depend on Babashka.

The skill validator passed. The new instructions and documentation passed the STE linter without violations. The pinned upstream principles retain their original wording.

## Continuation boundary

The installed Codex App Server passed a real initialization handshake. The supervisor transport and continuation policy passed offline tests. The installed server's exported schema includes the required thread, turn, and Goal methods.

An unattended desktop resumption exercise did not run. Native Goal continuation remains the first execution path. The optional supervisor requires an existing thread ID and an active controller process.
