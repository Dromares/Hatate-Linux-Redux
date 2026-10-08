#!/usr/bin/env python3
"""Bridges a stdio-only MCP client (Claude Desktop and similar) to the
app's embedded MCP server, which only speaks streamable-HTTP over
loopback (see core/mcp_server.py for why: it is hosted inside the
already-running GUI, driving the user's live queue and Hydrus session -
there is nothing for a stdio transport to launch, since the server it
would need to attach to is the one already running).

DAN-707. Deliberately dependency-free beyond `requests` (already a core
requirement): this relays raw JSON-RPC messages rather than using the
`mcp` package's own client classes, so a stdio-only client can be
bridged in without installing the SDK a second time just for this
script, and without this script breaking every time that SDK's client
internals change shape.

Framing: one JSON-RPC message per line on stdin/stdout, which is the
same framing MCP's own stdio transport uses - so a client that expects
the ordinary MCP stdio transport does not need to know this is a bridge
at all.

Usage (direct):
    python3 tools/mcp_stdio_bridge.py --port 8787 --token <your-token>

Usage (env, for a client config that can't pass flags):
    HATATE_MCP_PORT=8787 HATATE_MCP_TOKEN=<your-token> \\
        python3 tools/mcp_stdio_bridge.py

Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "hatate-linux": {
          "command": "/path/to/Hatate-Linux-Redux/venv/bin/python3",
          "args": ["/path/to/Hatate-Linux-Redux/tools/mcp_stdio_bridge.py"],
          "env": {"HATATE_MCP_PORT": "8787", "HATATE_MCP_TOKEN": "<your-token>"}
        }
      }
    }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import requests

# Loopback only - matches core/mcp_server.HOST, which is a constant for
# the same reason this script never takes a --host flag: the server
# this bridges to never listens anywhere else.
HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# The streamable-HTTP transport can hold a request open while a tool
# call runs (a Hydrus upload, a re-search) - generous rather than tight,
# since a timeout here reads to the AI as the tool call having failed
# when it may simply still be running.
REQUEST_TIMEOUT_SECONDS = 120.0


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("HATATE_MCP_PORT", DEFAULT_PORT)),
        help=f"Port the embedded server is listening on (default {DEFAULT_PORT}, "
             "or $HATATE_MCP_PORT)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("HATATE_MCP_TOKEN", ""),
        help="Bearer token from Settings > MCP (or $HATATE_MCP_TOKEN)",
    )
    return parser.parse_args(argv)


def run(port: int, token: str) -> int:
    if not token:
        print(
            "mcp_stdio_bridge: no token given (--token or $HATATE_MCP_TOKEN) - "
            "the server will refuse every request", file=sys.stderr,
        )
        return 2

    url = f"http://{HOST}:{port}/mcp"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    session_id: Optional[str] = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_headers = dict(headers)
        if session_id:
            request_headers["Mcp-Session-Id"] = session_id

        try:
            response = requests.post(
                url, data=line, headers=request_headers, timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            _write_error(line, f"Could not reach the app's MCP server at {url}: {exc}")
            continue

        session_id = response.headers.get("Mcp-Session-Id", session_id)
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            for event_line in response.text.splitlines():
                if event_line.startswith("data:"):
                    _write_line(event_line[len("data:"):].strip())
        elif response.content:
            _write_line(response.text.strip())
    return 0


def _write_line(text: str) -> None:
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def _write_error(original_request: str, message: str) -> None:
    """Hands the client a JSON-RPC error rather than dropping the request
    silently - a stdio client waiting on a response to a specific id
    should see that id fail, not nothing at all."""
    request_id = None
    try:
        request_id = json.loads(original_request).get("id")
    except (json.JSONDecodeError, AttributeError):
        pass
    _write_line(json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32000, "message": message},
    }))


def main() -> int:
    args = parse_args()
    return run(args.port, args.token)


if __name__ == "__main__":
    sys.exit(main())
