#!/usr/bin/env python3
"""Call one tool on the ISOLATED Daintree's MCP with its API key (stdlib only).

    mcp_call.py <mcp-url> <api-key-file | help-session.json> tools
    mcp_call.py <mcp-url> <api-key-file> <action-id> ['<json-args>']

An API-key session gets the `external` surface (terminal.list, terminal.getOutput,
terminal.getStatus, worktree.list, agent.launch, ...). Use it to inspect the trial
app; never point it at the user's own instance (port 45454).
"""

from __future__ import annotations

import json
import sys
import urllib.request


def post(url: str, key: str, body: dict, session: str | None) -> tuple[dict | None, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key}",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        sid = resp.headers.get("Mcp-Session-Id") or session
        raw = resp.read().decode()
    if not raw.strip():
        return None, sid
    if raw.lstrip().startswith("{"):
        return json.loads(raw), sid
    last = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            last = json.loads(line[5:].strip())
    return last, sid


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    url, key_file, tool = sys.argv[1], sys.argv[2], sys.argv[3]
    args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    with open(key_file) as fh:
        key = fh.read().strip()
    # help-session.json: a help-session bearer the holder provisioned on the Shell.
    if key_file.endswith(".json"):
        key = json.loads(key)["token"]
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "dtrb", "version": "0"},
        },
    }
    _, sid = post(url, key, init, None)
    post(url, key, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    if tool == "tools":
        res, _ = post(url, key, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        for t in (res or {}).get("result", {}).get("tools", []):
            print(t["name"])
        return 0
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    res, _ = post(url, key, body, sid)
    result = (res or {}).get("result", res)
    if isinstance(result, dict) and result.get("structuredContent") is not None:
        print(json.dumps(result["structuredContent"], indent=2))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
