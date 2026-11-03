#!/usr/bin/env python3
"""Install the source skill and register its local MCP server."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def installation_plan(root, codex_home):
    root, codex_home = Path(root).resolve(), Path(codex_home).expanduser().resolve()
    source = root / "skills" / "ub-execute"
    target = codex_home / "skills" / "ub-execute"
    python = root / ".venv" / "bin" / "python"
    command = ["codex", "mcp", "add", "ub-execute", "--", str(python), str(source / "scripts" / "ub.py"), "serve"]
    return {"skill_source": str(source), "skill_target": str(target), "mcp_command": command,
            "interpreter": str(python), "state": str(root / ".state")}


def install(plan):
    source, target = Path(plan["skill_source"]), Path(plan["skill_target"])
    if not Path(plan["interpreter"]).is_file():
        raise RuntimeError("Create the project virtual environment before installation.")
    if target.exists() or target.is_symlink():
        if not target.is_symlink() or target.resolve() != source:
            raise RuntimeError(f"An existing skill occupies {target}. No files were replaced.")
    probe = subprocess.run(["codex", "mcp", "get", "ub-execute", "--json"], capture_output=True, text=True)
    expected = plan["mcp_command"][5:]
    if probe.returncode == 0:
        existing = json.loads(probe.stdout)
        transport = existing.get("transport", {})
        if transport.get("command") != expected[0] or transport.get("args") != expected[1:]:
            raise RuntimeError("An existing MCP server uses the name ub-execute. No configuration was replaced.")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_symlink():
        target.symlink_to(source, target_is_directory=True)
    if probe.returncode != 0:
        subprocess.run(plan["mcp_command"], check=True)
    receipt = Path(plan["state"]) / "installation.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({**plan, "status": "installed"}, indent=2) + "\n")
    return {**plan, "status": "installed", "receipt": str(receipt)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    codex_directory = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    plan = installation_plan(root, codex_directory)
    try:
        print(json.dumps(install(plan) if args.apply else plan, indent=2))
    except (RuntimeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
