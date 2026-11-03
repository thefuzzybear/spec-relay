#!/usr/bin/env python3
"""Run the source checkout through a global skill symlink."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from ub_execute.cli import main

main()
