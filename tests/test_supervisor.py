import tempfile
from pathlib import Path
import unittest

from ub_execute.store import WorkflowError
from ub_execute.supervisor import supervise
from ub_execute.runtime import Runtime


class FakeClient:
    def __init__(self, runtime, run_id, progress=True, goal=False):
        self.runtime, self.run_id = runtime, run_id
        self.progress, self.goal = progress, goal
        self.turns = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, params, timeout=30):
        if method == "thread/resume":
            return {"thread": {"id": "thread-test", "status": {"type": "idle"}}}
        if method == "thread/goal/get":
            return {"goal": {"status": "active"} if self.goal else None}
        if method == "turn/start":
            self.turns += 1
            if self.progress:
                with self.runtime.store.transaction() as db:
                    run = self.runtime.store.get(db, "run", self.run_id)
                    run["status"] = "complete"
                    self.runtime.store.put(db, "run", run)
                    self.runtime.store.event(db, self.run_id, "run_completed", "Simulate a completed execution turn.", {})
            return {"turn": {"id": f"turn-{self.turns}"}}
        return {}

    def wait_turn(self, turn_id, timeout):
        return {"id": turn_id, "status": "completed"}


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Runtime(Path(self.temp.name))
        with self.runtime.store.transaction() as db:
            self.runtime.store.put(db, "run", {"id": "run-test", "status": "active", "repository": self.temp.name, "bundle": {}})

    def test_stops_after_runtime_completion(self):
        client = FakeClient(self.runtime, "run-test")
        result = supervise(self.runtime, "run-test", "thread-test", client_factory=lambda: client)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(client.turns, 1)

    def test_two_no_progress_turns_require_attention(self):
        client = FakeClient(self.runtime, "run-test", progress=False)
        result = supervise(self.runtime, "run-test", "thread-test", client_factory=lambda: client)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(client.turns, 2)

    def test_active_goal_prevents_a_second_continuation_owner(self):
        client = FakeClient(self.runtime, "run-test", goal=True)
        with self.assertRaisesRegex(WorkflowError, "Goal"):
            supervise(self.runtime, "run-test", "thread-test", client_factory=lambda: client)
        self.assertEqual(client.turns, 0)


if __name__ == "__main__":
    unittest.main()
