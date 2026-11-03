import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

from ub_execute.quality import analyze, PROJECT

AVAILABLE = importlib.util.find_spec("tree_sitter_language_pack") is not None and (PROJECT / ".parser-cache").exists()


@unittest.skipUnless(AVAILABLE, "Install quality dependencies and parsers for integration checks.")
class QualityIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_calc.py").write_text("import unittest\nfrom calc import add\nclass Calc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        self.command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        self.coverage = [[sys.executable, "-m", "coverage", "run", "--source=calc", "-m", "unittest", "discover", "-s", "tests"],
                         [sys.executable, "-m", "coverage", "lcov", "-o", "coverage/lcov.info"]]

    def test_real_crapper_and_mutator_use_fresh_coverage(self):
        result = analyze("crapper", self.root, ["calc.py"], self.coverage)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["entries"][0]["coverage"], 100)
        result = analyze("mutator", self.root, ["calc.py"], self.coverage, test_command=self.command)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["outcomes"])
        self.assertEqual({item["status"] for item in result["outcomes"]}, {"killed"})

    def test_real_dryer_reports_duplicate_javascript(self):
        code = "function a(xs) {\n let n = xs.length;\n for(let i=0;i<n;i++){ console.log(xs[i]); }\n return n;\n}\n"
        (self.root / "a.js").write_text(code)
        (self.root / "b.js").write_text(code.replace("function a", "function b"))
        result = analyze("dryer", self.root, ["a.js", "b.js"])
        self.assertEqual(result["status"], "failed", result)
        self.assertGreaterEqual(result["candidates"][0]["score"], 0.8)


if __name__ == "__main__":
    unittest.main()
