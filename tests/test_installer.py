import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installer", Path(__file__).resolve().parents[1] / "scripts/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTest(unittest.TestCase):
    def test_installation_is_idempotent_and_preserves_existing_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            (root / ".venv/bin").mkdir(parents=True)
            (root / ".venv/bin/python").touch()
            (root / "skills/ub-execute").mkdir(parents=True)
            plan = installer.installation_plan(root, Path(temporary) / "codex")
            existing = {"transport": {"command": plan["mcp_command"][5], "args": plan["mcp_command"][6:]}}
            with patch.object(installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(existing), "")) as run:
                self.assertEqual(installer.install(plan)["status"], "installed")
                self.assertEqual(installer.install(plan)["status"], "installed")
                self.assertEqual(run.call_count, 2)
            self.assertTrue(Path(plan["skill_target"]).is_symlink())
            conflict = dict(plan, skill_source=str(root / "other"))
            with self.assertRaisesRegex(RuntimeError, "existing skill"):
                installer.install(conflict)


if __name__ == "__main__":
    unittest.main()
