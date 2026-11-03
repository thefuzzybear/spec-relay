from contextlib import redirect_stdout, redirect_stderr
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from ub_execute.cli import main


class CLITest(unittest.TestCase):
    def invoke(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        code = 0
        with patch("sys.argv", ["ub-execute", *arguments]), redirect_stdout(output), redirect_stderr(error):
            try:
                main()
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), error.getvalue()

    def test_catalog_is_valid_json(self):
        code, output, _ = self.invoke(["tools"])
        self.assertEqual(code, 0)
        self.assertIn("run_create", {tool["name"] for tool in json.loads(output)["tools"]})

    def test_invalid_call_returns_an_error_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, _, error = self.invoke(["--state", temporary, "call", "run_status", "--args", '{"run_id":"missing"}'])
        self.assertEqual(code, 1)
        self.assertIn("Unknown run", json.loads(error)["error"])

    def test_missing_quality_configuration_fails(self):
        code, output, _ = self.invoke(["quality", "--tool", "crapper", "--paths", "src/"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["status"], "error")


if __name__ == "__main__":
    unittest.main()
