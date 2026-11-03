"""One bounded continuation owner for a persisted Codex thread."""

from contextlib import contextmanager
import fcntl
from pathlib import Path
import time

from .appserver import AppServer
from .contracts import need
from .store import WorkflowError


@contextmanager
def controller_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkflowError("Another supervisor owns this run.") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _snapshot(runtime, run_id):
    with runtime.store.transaction() as db:
        run = runtime.store.get(db, "run", run_id)
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        return run, sequence


def _attention(runtime, run_id, reason):
    with runtime.store.transaction() as db:
        run = runtime.store.get(db, "run", run_id)
        if run["status"] == "active":
            run["status"] = "needs_attention"
        run["controller_result"] = reason
        runtime.store.put(db, "run", run)
        runtime.store.event(db, run_id, "controller_stopped", reason, {})
        return {"run_id": run_id, "status": run["status"], "reason": reason}


def _goal_available(client, thread_id):
    goal = client.request("thread/goal/get", {"threadId": thread_id}).get("goal")
    reserved = {"active", "paused", "blocked", "budget_limited", "budget-limited"}
    need(not goal or goal.get("status") not in reserved, "A non-terminal Codex Goal already owns continuation.")


def _resume(client, thread_id):
    resumed = client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    need(resumed.get("thread", {}).get("id") == thread_id, "Codex resumed another thread.")
    status = resumed["thread"].get("status", {})
    need(not isinstance(status, dict) or status.get("type") not in {"active", "busy", "running"}, "The Codex thread already has active work.")
    _goal_available(client, thread_id)


def _interrupt_turn(client, thread_id, turn_id):
    if turn_id:
        try:
            client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except (WorkflowError, OSError):
            pass


def _continue_turn(client, runtime, run_id, thread_id, deadline):
    prompt = (f"Use $ub-execute to continue run {run_id}. The state directory is {runtime.store.root}. "
              "Read run_recover and current native agent activity before dispatch. Preserve user instructions. "
              "Continue ready work through native sub-agents, review, integration, and final verification. "
              "Do not create a Goal. Stop for a material unresolved decision or required permission. "
              "Record completed execution operations through the runtime.")
    skill = Path(__file__).resolve().parents[2] / "skills" / "ub-execute" / "SKILL.md"
    turn_id = None
    try:
        response = client.request("turn/start", {"threadId": thread_id, "input": [
            {"type": "text", "text": prompt}, {"type": "skill", "name": "ub-execute", "path": str(skill)}]})
        turn_id = response["turn"]["id"]
        turn = client.wait_turn(turn_id, max(0.1, deadline - time.monotonic()))
        need(turn.get("status") == "completed", f"The Codex turn ended with status {turn.get('status')}.")
    except (WorkflowError, KeyError, OSError):
        _interrupt_turn(client, thread_id, turn_id)
        raise


def _step(client, runtime, run_id, thread_id, deadline):
    run, before = _snapshot(runtime, run_id)
    if run["status"] != "active":
        return {"run_id": run_id, "status": run["status"]}, True
    try:
        need(time.monotonic() < deadline, "The supervisor reached its time limit.")
        _goal_available(client, thread_id)
        _continue_turn(client, runtime, run_id, thread_id, deadline)
    except (WorkflowError, KeyError, OSError) as exc:
        return _attention(runtime, run_id, str(exc)), True
    run, after = _snapshot(runtime, run_id)
    return {"run_id": run_id, "status": run["status"], "progress": before != after}, run["status"] != "active"


def _drive(client, runtime, run_id, thread_id, max_turns, max_seconds):
    deadline = time.monotonic() + max_seconds
    no_progress = 0
    for _ in range(max_turns):
        result, terminal = _step(client, runtime, run_id, thread_id, deadline)
        if terminal:
            return result
        no_progress = 0 if result["progress"] else no_progress + 1
        if no_progress >= 2:
            return _attention(runtime, run_id, "Two continuation turns produced no execution evidence.")
    return _attention(runtime, run_id, "The supervisor reached its turn limit.")


def supervise(runtime, run_id, thread_id, max_turns=10, max_seconds=3600, client_factory=None):
    need(type(max_turns) is int and max_turns > 0 and max_seconds > 0, "Provide positive continuation limits.")
    run, _ = _snapshot(runtime, run_id)
    if run["status"] != "active":
        return {"run_id": run_id, "status": run["status"]}
    root = runtime.store.root / "runs" / run_id
    factory = client_factory or (lambda: AppServer(run["repository"], root / "app-server.log"))
    with controller_lock(root / "controller.lock"), factory() as client:
        _resume(client, thread_id)
        return _drive(client, runtime, run_id, thread_id, max_turns, max_seconds)
