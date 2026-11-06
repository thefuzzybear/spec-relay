"""Bounded JSON-RPC client for the local Codex App Server."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time

from .store import WorkflowError
from . import __version__


class NeedsInput(WorkflowError):
    pass


class AppServer:
    def __init__(self, cwd, log_path, command=None):
        self.cwd, self.log_path = cwd, Path(log_path)
        self.command = command or ["codex", "app-server"]
        self.messages = queue.Queue()
        self.notifications = []
        self.sequence = 0

    def __enter__(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("a")
        self.process = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.log, text=True, bufsize=1, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {"name": "ub-execute", "title": "UB Execute", "version": __version__}})
            self._send({"method": "initialized", "params": {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
        self.process.stdout.close()
        self.log.close()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except Exception as exc:
            self.messages.put({"transport_error": str(exc)})
        finally:
            self.messages.put({"transport_error": "The Codex App Server connection closed."})

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkflowError("The Codex App Server operation timed out.")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty:
            raise WorkflowError("The Codex App Server operation timed out.") from None
        if "transport_error" in message:
            raise WorkflowError(message["transport_error"])
        if "method" in message and "id" in message:
            self._send({"id": message["id"], "error": {"code": -32000, "message": "UB Execute requires user input for this server request."}})
            raise NeedsInput(f"Codex requires user input: {message['method']}")
        return message

    def request(self, method, params, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id:
                if "error" in message:
                    raise WorkflowError(str(message["error"]))
                return message.get("result", {})
            if "method" in message:
                self.notifications.append(message)

    def wait_turn(self, turn_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            message = self.notifications.pop(0) if self.notifications else self._receive(deadline)
            method, params = message.get("method"), message.get("params", {})
            if method == "turn/started" and params.get("turn", {}).get("id") != turn_id:
                raise NeedsInput("Another controller started a turn in this Codex thread.")
            if method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                return params["turn"]
