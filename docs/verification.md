# Verification record

Date: 2026-11-03.

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
