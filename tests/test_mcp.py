import io
import json
import tempfile
import unittest

from ub_execute.mcp import serve
from ub_execute.runtime import Runtime


class MCPTest(unittest.TestCase):
    def exchange(self, requests):
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            serve(Runtime(temporary), io.StringIO("\n".join(json.dumps(request) for request in requests) + "\n"), output)
            return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_initialize_discover_and_reject_invalid_tool_arguments(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_status", "arguments": {"run_id": "missing"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "run_events", "arguments": {"run_id": 5}}},
        ])
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-11-25")
        names = {tool["name"] for tool in responses[1]["result"]["tools"]}
        self.assertIn("task_claim", names)
        self.assertIn("check_start", names)
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertTrue(responses[3]["result"]["isError"])

    def test_requires_initialization(self):
        response = self.exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])[0]
        self.assertIn("error", response)


if __name__ == "__main__":
    unittest.main()
