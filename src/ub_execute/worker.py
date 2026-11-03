"""Detached check worker. Command output stays outside the source tree."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .store import Store, WorkflowError
from .workspace import fingerprint
from .processes import held_lock
from .provenance import runner_fingerprint


def _interrupt(signum, frame):
    raise InterruptedError("The check was cancelled.")


def _command_result(job, store):
    directory = Path(job["directory"])
    substitutions = {"{python}": sys.executable, "{worktree}": job["worktree"], "{artifacts}": str(directory)}
    argv = [substitutions.get(arg, arg) for arg in job["check"]["argv"]]
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["UB_EXECUTE_ARTIFACTS"] = str(directory)
    process = None
    with held_lock(directory / "command.lock") as command_lock, (directory / "stdout.log").open("wb") as output, (directory / "stderr.log").open("wb") as error:
        try:
            process = subprocess.Popen(argv, cwd=job["worktree"], stdout=output, stderr=error, env=environment,
                                       start_new_session=True, pass_fds=(command_lock.fileno(),))
            with store.transaction() as db:
                current = store.get(db, "job", job["id"])
                current.update(command_pid=process.pid)
                job.update(command_pid=process.pid)
                store.put(db, "job", current)
            code = process.wait(timeout=job["check"]["timeout_seconds"])
            status = "passed" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status, code = "timeout", None
        except InterruptedError:
            status, code = "cancelled", None
        except OSError as exc:
            status, code = "error", None
            job["error"] = str(exc)
        finally:
            _stop_process(process)
    job.update(status=status, exit_code=code)
    return job


def _stop_process(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def execute(job, store):
    job = _command_result(job, store)
    if job["check"].get("kind", "test") in {"crapper", "dryer", "mutator"}:
        _quality_result(job)
    return job


def _quality_result(job):
    if job["status"] not in {"passed", "failed"}:
        return
    try:
        report = json.loads((Path(job["directory"]) / "stdout.log").read_text())
        if report.get("version") != 1 or report.get("tool") != job["check"]["kind"]:
            raise WorkflowError("The quality report has an invalid schema.")
        job["report"] = report
        job["status"] = report["status"] if report["status"] in {"passed", "failed", "error", "unsupported"} else "error"
        if job["exit_code"] != 0 and job["status"] == "passed":
            job["status"] = "error"
    except (OSError, ValueError, KeyError) as exc:
        job.update(status="error", error=str(exc))


def run_job(state, job_id):
    directory = Path(state) / "jobs" / job_id
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with held_lock(directory / "worker.lock"):
            _run_job_locked(state, job_id)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run_job_locked(state, job_id):
    store = Store(state)
    signal.signal(signal.SIGTERM, _interrupt)
    with store.transaction() as db:
        job = store.get(db, "job", job_id)
        run = store.get(db, "run", job["run_id"])
        if job["status"] != "queued":
            return
        if run["status"] != "active":
            job["status"] = "cancelled"
            store.put(db, "job", job)
            return
        job.update(status="running", pid=os.getpid(), started_at=time.time())
        store.put(db, "job", job)
    try:
        if runner_fingerprint() != job.get("runner_fingerprint"):
            job.update(status="invalidated", error="The runtime changed before the check started.")
        elif fingerprint(job["worktree"]) != job["fingerprint"]:
            job.update(status="invalidated", error="The source changed before the check started.")
        else:
            job = execute(job, store)
            if fingerprint(job["worktree"]) != job["fingerprint"]:
                job.update(status="invalidated", error="The source changed during the check.")
            if runner_fingerprint() != job.get("runner_fingerprint"):
                job.update(status="invalidated", error="The runtime changed during the check.")
    except (Exception, InterruptedError) as exc:
        job.update(status="error", error=str(exc))
    job["finished_at"] = time.time()
    with store.transaction() as db:
        current = store.get(db, "job", job_id)
        if current["status"] == "cancelled":
            job["status"] = "cancelled"
        store.put(db, "job", job)
        store.event(db, job["run_id"], "check_finished", "The check worker recorded its result.",
                    {"job_id": job_id, "status": job["status"]})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    run_job(args.state, args.job)


if __name__ == "__main__":
    main()
