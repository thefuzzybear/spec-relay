# Long-running execution

Use native Codex Goals for continuation when the user explicitly requests a Goal. The runtime stores execution state separately from the conversation.

Use the optional supervisor when one controller must drive bounded continuation through the Codex App Server. It resumes a supplied Codex thread. It does not create a new sidebar chat.

```text
<python> <skill>/scripts/ub.py --state <state> supervise --run-id <run> --thread-id <thread> --max-turns 10 --max-seconds 3600
```

The supervisor uses the installed `codex app-server` process. It retains a local controller lock. It rejects a non-terminal native Goal to prevent duplicate continuation. It stops on user input or approval requests, runtime pause, terminal state, a time limit, or repeated lack of progress.

The supervisor never approves commands or supplies a missing user decision. Inspect its result and the preserved run before resuming. Actual desktop attachment depends on the installed Codex version. Test attachment on a disposable run before relying on unattended recovery.

The supervisor must remain running. Machine shutdown stops it. Restart the same command to recover from persisted run state. The installer does not create a service, daemon, or recurring automation.
