"""Structured adapters for the pinned UBMode analyzers."""

from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from .contracts import need
from .workspace import inside

PROJECT = Path(__file__).resolve().parents[2]


def load_tools():
    for name in ("crapper", "dryer", "mutator"):
        source = PROJECT / "vendor" / name / "src"
        if not source.is_dir():
            raise RuntimeError(f"The pinned source is missing: {source}")
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))
    from tree_sitter_language_pack import configure, PackConfig
    configure(PackConfig(cache_dir=str(PROJECT / ".parser-cache")))


def setup_parsers(languages):
    from tree_sitter_language_pack import init, PackConfig
    init(PackConfig(cache_dir=str(PROJECT / ".parser-cache"), languages=languages))
    return {"languages": languages, "cache": str(PROJECT / ".parser-cache")}


def evaluate(tool, data):
    policies = {"crapper": _crap_policy, "dryer": _duplication_policy, "mutator": _mutation_policy}
    if tool not in policies:
        raise ValueError("Unknown quality tool.")
    return {"version": 1, "tool": tool, "status": policies[tool](data), **data}


def _crap_policy(data):
    status = "passed"
    entries = data.get("entries", [])
    if not entries:
        status = "unsupported"
    elif any(entry.get("coverage") is None or entry.get("crap") is None for entry in entries):
        status = "error"
    elif any(entry["crap"] >= 10 for entry in entries):
        status = "failed"
    return status


def _duplication_policy(data):
    if data.get("warnings"):
        return "error"
    if data.get("entries_count", 1) == 0:
        return "unsupported"
    return "failed" if any(entry["score"] >= 0.8 for entry in data.get("candidates", [])) else "passed"


def _mutation_policy(data):
    outcomes = data.get("outcomes", [])
    if not outcomes:
        return "unsupported"
    if any(entry["status"] in {"invalid", "timeout", "error"} for entry in outcomes):
        return "error"
    return "failed" if any(entry["status"] != "killed" for entry in outcomes) else "passed"


def _run_commands(commands, root, timeout):
    for command in commands:
        need(isinstance(command, list) and command and all(isinstance(arg, str) for arg in command), "Use an argument vector for each coverage command.")
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        need(result.returncode == 0, "The coverage command failed.")


def _fresh_coverage(root, commands, files, timeout):
    need(commands, "Configure coverage commands. Existing reports cannot certify the current source.")
    started = time.time_ns()
    _run_commands(commands, root, timeout)
    for name in files:
        path = inside(root, name)
        need(path.is_file() and path.stat().st_mtime_ns >= started, f"A fresh coverage report is missing: {name}")
    # Only explicitly selected fresh reports enter the coverage bundle.
    from crapper.coverage import load_bundle
    directory = tempfile.TemporaryDirectory()
    try:
        for name in files:
            destination = Path(directory.name) / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inside(root, name), destination)
        return load_bundle(Path(directory.name))
    finally:
        directory.cleanup()


def _source_files(root, paths):
    from crapper.discover import iter_source_files
    files = iter_source_files([inside(root, name) for name in paths])
    for path in files:
        need(path.resolve().is_relative_to(root), "A source path escapes the repository.")
    return files


def _crapper(root, files, bundle):
    from crapper.analyze import analyze_files
    from crapper.discover import language_of
    from crapper.languages import functions_in_file
    bundle.bind_sources([path.relative_to(root).as_posix() for path in files])
    missing = []
    for path in files:
        for function in functions_in_file(language_of(path), path.read_text(), path.relative_to(root).as_posix(), str(root)):
            if bundle.percent_for(function) is None:
                missing.append({"file": function.path, "function": function.name})
    if missing:
        return {"version": 1, "tool": "crapper", "status": "error", "missing_coverage": missing}
    return evaluate("crapper", {"entries": [asdict(entry) for entry in analyze_files(files, root, bundle)]})


def _dryer(root, files):
    from dryer.scan import scan_files, find_duplicates
    entries, warnings = scan_files(files, root, min_lines=4, min_nodes=20)
    return evaluate("dryer", {"entries_count": len(entries), "warnings": warnings,
                              "candidates": [asdict(entry) for entry in find_duplicates(entries, threshold=0.8)]})


def _mutator(root, files, bundle, command, timeout, workers):
    from crapper.discover import language_of
    from mutator.coverage import _lookup, _covered_from_pairs, _go_lines
    from mutator.engine import mutate_file
    from mutator.runner import CommandRunner
    need(command and isinstance(command, list), "Configure an explicit mutation test argument vector.")
    _run_commands([command], root, timeout)
    runner = CommandRunner(inherit_process_group=True)
    outcomes = []
    for path in files:
        language = language_of(path)
        if language == "java":
            return {"version": 1, "tool": "mutator", "status": "unsupported", "error": "The adapter needs a Java line-coverage mapping."}
        lines = _go_lines(bundle.go_profile, path.as_posix()) if language == "go" else _covered_from_pairs(_lookup(bundle.lcov, path.as_posix()))
        with redirect_stdout(sys.stderr):
            result = mutate_file(path, root, runner=runner, covered_lines=lines, ignore_coverage=False,
                                 mutate_all=True, lines=None, test_command=command, timeout_factor=10,
                                 mutation_warning=100, baselines={}, max_workers=workers)
        need(not result.baseline_failed and not result.skipped, result.baseline_message or result.skipped or "Mutation baseline failed.")
        outcomes.extend({"site": asdict(site), "status": result.statuses.get(site.mutation_id, "error")} for site in result.sites)
        print(f"Measured {path.relative_to(root)}", file=sys.stderr, flush=True)
    return evaluate("mutator", {"outcomes": outcomes, "cache_policy": "All selected mutants execute for this source and test revision."})


def analyze(tool, root, paths, coverage_commands=None, coverage_files=None, test_command=None, timeout=300, workers=2):
    try:
        root = Path(root).resolve()
        if tool in {"crapper", "mutator"}:
            need(coverage_commands, "Configure coverage commands. Existing reports cannot certify the current source.")
        load_tools()
        files = _source_files(root, paths)
        if not files:
            return {"version": 1, "tool": tool, "status": "unsupported", "error": "No supported source files were selected."}
        if tool == "dryer":
            return _dryer(root, files)
        bundle = _fresh_coverage(root, coverage_commands, coverage_files or ["coverage/lcov.info"], timeout)
        if tool == "crapper":
            return _crapper(root, files, bundle)
        if tool == "mutator":
            return _mutator(root, files, bundle, test_command, timeout, workers)
        raise ValueError("Unknown quality tool.")
    except Exception as exc:
        return {"version": 1, "tool": tool, "status": "error", "error": str(exc)}
