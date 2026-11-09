# First run

Install Spec Relay from the [README](../README.md). Start a fresh Codex session with native sub-agents enabled.

Use a disposable repository for your first run. Run these commands from a directory where you can create a project:

```sh
mkdir spec-relay-demo
cd spec-relay-demo
git init
printf 'def add(a, b):\n    return 0\n' > calc.py
cat > SPEC.md <<'EOF'
# Addition

REQ-1: Implement add(a, b) in calc.py. It must return the sum of two integers.

Acceptance: add(2, 3) returns 5. add(-4, 1) returns -3.
Use Python unittest for behavior tests. Keep the public function signature.
Deliver a local integration branch.
EOF
git add calc.py SPEC.md
git commit -m "Create the addition demo"
```

Open the demo repository in Codex and enter:

```text
Use $ub-execute to implement SPEC.md in this repository as a Codex Goal.
Treat this demo spec as approved for implementation.
Use the local integration branch as the delivery outcome.
```

The orchestrator creates a run and gives the task to a native worker. The worker records a failing test, implements the behavior, and submits its commits for review. After integration, the orchestrator runs the final checks and reports the integration branch.

Record the run ID from the result. To continue after an interruption, give Codex that ID:

```text
Use $ub-execute to recover and continue run <run-id>.
```

The state directory lives in the Spec Relay checkout by default. Keep it to retain the run records and worktrees. For check configuration, see the [execution contract](../skills/ub-execute/references/execution-contract.md).

New runs request Luna for coding and Sol 6.1 for reviews.
Read [native model policy](model-policy.md) if the host cannot select either model or a coding task needs escalation.
