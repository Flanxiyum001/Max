#!/usr/bin/env python3
"""Mock MCP server (stdio, newline-delimited JSON) used by MAX tests.

Exposes two tools:
  * echo  — returns the given text,
  * add   — returns a + b.

Speak only what MAX needs: initialize, tools/list, tools/call.
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the provided text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method == "initialize":
            reply(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "max-mock-mcp", "version": "1.0.0"},
                    },
                }
            )
        elif method == "tools/list":
            reply({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                text = str(args.get("text", ""))
                result = {"content": [{"type": "text", "text": f"echo: {text}"}], "isError": False}
            elif name == "add":
                try:
                    total = float(args.get("a", 0)) + float(args.get("b", 0))
                    if total == int(total):
                        total = int(total)
                    result = {"content": [{"type": "text", "text": str(total)}], "isError": False}
                except (TypeError, ValueError):
                    result = {"content": [{"type": "text", "text": "bad numbers"}], "isError": True}
            else:
                reply(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"unknown tool {name}"},
                    }
                )
                continue
            reply({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif msg_id is not None:
            reply(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"method {method} not found"},
                }
            )


if __name__ == "__main__":
    main()
