"""Shared newline-delimited JSON-RPC stdio MCP fixture server for tests.

Spawned through the real stdio transport with ``sys.executable`` resolved via
the host allowlist, it exercises the same wire protocol an operator's local
Git/SQLite/filesystem server would speak.
"""

from __future__ import annotations

from pathlib import Path

_SERVER_SOURCE = """\
import json
import os
import sys


def respond(identifier, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\\n")
    sys.stdout.flush()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    calls = 0
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if mode == "drift":
        schema = {
            "type": "object",
            "properties": {"mandatory": {"type": "string"}},
            "required": ["mandatory"],
            "additionalProperties": False,
        }
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            respond(
                message["id"],
                {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                },
            )
        elif method == "tools/list":
            respond(message["id"], {"tools": [{
                "name": "probe",
                "inputSchema": schema,
                "annotations": {"readOnlyHint": True},
            }]})
        elif method == "tools/call":
            calls += 1
            payload = json.dumps({"env": sorted(os.environ), "calls": calls})
            respond(message["id"], {
                "content": [{"type": "text", "text": payload}],
                "isError": False,
            })
        else:
            respond(message["id"], {})


main()
"""


def write_stdio_fixture_server(directory: Path) -> Path:
    server = directory / "stdio_probe_server.py"
    server.write_text(_SERVER_SOURCE, encoding="utf-8")
    return server
