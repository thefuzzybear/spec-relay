"""MCP STDIO transport using newline-delimited JSON-RPC."""

import json
import sys

from . import __version__
from .tools import call, catalog

PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


class RPCError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _initialize(params):
    version = params.get("protocolVersion")
    return {"protocolVersion": version if version in PROTOCOLS else "2025-11-25",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ub-execute", "version": __version__},
            "instructions": "Execute reviewed specs through durable task and evidence tools. Codex owns native sub-agents. Read task_context before edits. Use check_start and job_status for checks. Acceptance requires current evidence and independent review. MCP does not create a Goal or grant permissions."}


def _tool_call(runtime, params):
    try:
        value = call(runtime, params.get("name"), params.get("arguments", {}))
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value, "isError": False}
    except Exception as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}


def _validate(request):
    if not isinstance(request, dict):
        raise RPCError(-32600, "Invalid JSON-RPC request.")
    if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        raise RPCError(-32600, "Invalid JSON-RPC request.")
    if not isinstance(request.get("params", {}), dict):
        raise RPCError(-32602, "Invalid request parameters.")


def _dispatch(runtime, request, state):
    method, params = request["method"], request.get("params", {})
    if method == "initialize":
        state["initialized"] = True
        return _initialize(params)
    if method == "ping":
        return {}
    if not state["initialized"]:
        raise RPCError(-32000, "Initialize the MCP connection first.")
    if method == "tools/list":
        return {"tools": catalog()}
    if method == "tools/call":
        return _tool_call(runtime, params)
    raise RPCError(-32601, "Unknown MCP method.")


def _response(runtime, line, state):
    request = None
    try:
        request = json.loads(line)
        _validate(request)
        if "id" not in request:
            return None
        return {"jsonrpc": "2.0", "id": request["id"], "result": _dispatch(runtime, request, state)}
    except json.JSONDecodeError as exc:
        error = {"code": -32700, "message": str(exc)}
    except RPCError as exc:
        error = {"code": exc.code, "message": str(exc)}
    request_id = request.get("id") if isinstance(request, dict) else None
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def serve(runtime, source=None, sink=None):
    source, sink = source or sys.stdin, sink or sys.stdout
    state = {"initialized": False}
    for line in source:
        response = _response(runtime, line, state)
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
