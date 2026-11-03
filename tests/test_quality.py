import tempfile
from pathlib import Path
import unittest

from ub_execute.quality import evaluate, analyze


class QualityTest(unittest.TestCase):
    def test_crap_ten_fails_and_missing_coverage_is_error(self):
        self.assertEqual(evaluate("crapper", {"entries": [{"crap": 10.0, "coverage": 100}]})["status"], "failed")
        self.assertEqual(evaluate("crapper", {"entries": [{"crap": 9.99, "coverage": 100}]})["status"], "passed")
        self.assertEqual(evaluate("crapper", {"entries": [{"crap": None, "coverage": None}]})["status"], "error")

    def test_duplicate_at_point_eight_fails(self):
        self.assertEqual(evaluate("dryer", {"candidates": [{"score": 0.8}]})["status"], "failed")

    def test_uncovered_and_invalid_mutants_cannot_pass(self):
        for status in ("uncovered", "survived", "timeout", "invalid", "error"):
            report = evaluate("mutator", {"outcomes": [{"status": status}]})
            self.assertNotEqual(report["status"], "passed", status)
        self.assertEqual(evaluate("mutator", {"outcomes": [{"status": "killed"}]})["status"], "passed")

    def test_empty_reports_do_not_claim_success(self):
        for tool, data in (("crapper", {"entries": []}), ("dryer", {"entries_count": 0, "candidates": []}), ("mutator", {"outcomes": []})):
            self.assertNotEqual(evaluate(tool, data)["status"], "passed")

    def test_crapper_does_not_accept_stale_coverage_without_a_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "calc.py").write_text("def add(a,b):\n    return a+b\n")
            result = analyze("crapper", root, ["calc.py"])
            self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
