"""Local operator commands and MCP transport entry point."""

import argparse
import json
import os
from pathlib import Path
import sys

from .runtime import Runtime
from .tools import call, catalog


def build_parser():
    parser = argparse.ArgumentParser(description="Execute reviewed specs with native Codex agents.")
    parser.add_argument("--state", default=os.environ.get("UB_EXECUTE_STATE", str(Path(__file__).resolve().parents[2] / ".state")))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("tools")
    invoke = sub.add_parser("call")
    invoke.add_argument("name")
    group = invoke.add_mutually_exclusive_group(required=True)
    group.add_argument("--args")
    group.add_argument("--args-file")
    quality = sub.add_parser("quality")
    quality.add_argument("--tool", required=True, choices=["crapper", "dryer", "mutator"])
    quality.add_argument("--root", default=".")
    quality.add_argument("--paths", nargs="+", required=True)
    quality.add_argument("--coverage-json", default="[]")
    quality.add_argument("--coverage-files", nargs="+", default=["coverage/lcov.info"])
    quality.add_argument("--test-json", default="null")
    quality.add_argument("--timeout", type=float, default=300)
    quality.add_argument("--workers", type=int, default=2)
    setup = sub.add_parser("quality-setup")
    setup.add_argument("languages", nargs="+", default=["python"])
    supervisor = sub.add_parser("supervise")
    supervisor.add_argument("--run-id", required=True)
    supervisor.add_argument("--thread-id", required=True)
    supervisor.add_argument("--max-turns", type=int, default=10)
    supervisor.add_argument("--max-seconds", type=float, default=3600)
    return parser


def execute(args):
    if args.command == "tools":
        return {"tools": catalog()}
    if args.command == "call":
        raw = args.args if args.args is not None else Path(args.args_file).read_text()
        return call(Runtime(args.state), args.name, json.loads(raw))
    if args.command == "quality":
        from .quality import analyze
        return analyze(args.tool, args.root, args.paths, json.loads(args.coverage_json), args.coverage_files,
                       json.loads(args.test_json), args.timeout, args.workers)
    if args.command == "quality-setup":
        from .quality import setup_parsers
        return setup_parsers(args.languages)
    from .supervisor import supervise
    return supervise(Runtime(args.state), args.run_id, args.thread_id, args.max_turns, args.max_seconds)


def main():
    args = build_parser().parse_args()
    try:
        if args.command == "serve":
            from .mcp import serve
            serve(Runtime(args.state))
            return
        result = execute(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "quality" and result["status"] != "passed":
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
