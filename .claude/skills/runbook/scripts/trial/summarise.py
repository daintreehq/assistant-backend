#!/usr/bin/env python3
"""Find and summarise the transcript(s) of one runbook trial.

    summarise.py <run>/trials/<trial-id> [--full] [--json]
    summarise.py --transcript <file.jsonl> [--full] [--json]   # any Claude/Codex session log

Locating the log (no guessing by "newest file"):
  * Claude Code: ~/.claude/projects/<cwd with every non-alphanumeric char as '-'>/<session>.jsonl.
    The trial's own session is the file named by the pane's agentSessionId when Daintree
    recorded one, else the file in the pane's cwd dir, written since the trial started,
    whose first user message is the trial prompt. Every other file written in the trial's
    cwds since it started is a child agent's session.
  * Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl; its first line (session_meta)
    records the cwd. Same rule: cwd + start time + first user message.

The report lists every search_runbooks / get_runbook call with what came back (selected
ids, neighbours, supporting runbooks), every Daintree action with its arguments and a
short result, the notices Daintree typed into the pane, other tools used, the final
answer and token usage.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "sessions"


def ts(value: str | None) -> float:
    if not value:
        return 0.0
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def claude_dir(cwd: str) -> Path:
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p if isinstance(p, str) else (p.get("text") or "")
            for p in content
            if isinstance(p, (str, dict))
        )
    return json.dumps(content) if content is not None else ""


def first_ts(path: Path) -> float:
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    stamp_ = json.loads(line).get("timestamp")
                except ValueError:
                    continue
                if stamp_:
                    return ts(stamp_)
    except OSError:
        pass
    return 0.0


def first_user_text_claude(path: Path) -> str:
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") == "user" and not e.get("isMeta"):
                    t = text_of(e.get("message", {}).get("content"))
                    if t and not t.startswith("<"):
                        return t
    except OSError:
        pass
    return ""


def codex_items(path: Path):
    """(timestamp, item) for each completed item of a Codex rollout (codex-cli 0.15x)."""
    for line in path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = e.get("payload") or {}
        if e.get("type") == "event_msg" and p.get("type") == "item_completed":
            yield e.get("timestamp"), p.get("item") or {}


def codex_user_texts(path: Path, limit: int = 8) -> list[str]:
    """The first user messages of a Codex rollout, from either record shape: the
    `item_completed` UserMessage items, or `response_item` messages with role user
    (which also carry injected context, so the prompt may not be the first)."""
    out: list[str] = []
    for line in path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = e.get("payload") or {}
        if e.get("type") == "event_msg" and p.get("type") == "item_completed":
            item = p.get("item") or {}
            if item.get("type") == "UserMessage":
                out.append(text_of(item.get("content")))
        elif e.get("type") == "response_item" and p.get("type") == "message":
            if p.get("role") == "user":
                out.append(text_of(p.get("content")))
        if len(out) >= limit:
            break
    return out


def codex_meta(path: Path) -> tuple[str, str]:
    """(cwd, the first user messages joined) of a Codex rollout."""
    cwd = ""
    try:
        with path.open() as fh:
            e = json.loads(fh.readline())
            if e.get("type") == "session_meta":
                cwd = (e.get("payload") or {}).get("cwd", "")
        return cwd, "\n".join(codex_user_texts(path))
    except (OSError, ValueError):
        pass
    return cwd, ""


def locate(done: dict) -> tuple[Path | None, list[Path], str]:
    start = ts(done["startedAt"]) - 5
    end = ts(done["endedAt"])
    prompt = (done.get("request") or {}).get("prompt", "")
    key = prompt.strip()[:80]
    agent = (done.get("request") or {}).get("agentId", "claude")
    terms = done.get("terminals") or []
    trial = next((t for t in terms if t["id"] == done.get("trialTerminalId")), None)
    cwds = {t["cwd"] for t in terms if t.get("cwd")}
    if agent == "codex":
        files = []
        for p in CODEX_SESSIONS.rglob("*.jsonl"):
            if p.stat().st_mtime >= start and start <= first_ts(p) <= end:
                cwd, first = codex_meta(p)
                if cwd in cwds or any(cwd.startswith(c) for c in cwds):
                    files.append((p, first))
        own = next((p for p, first in files if key and key in first), None)
        return own, [p for p, _ in files if p != own], "codex"
    files = []
    for cwd in cwds:
        d = claude_dir(cwd)
        if d.is_dir():
            # Born during the trial (first record's timestamp), not merely touched:
            # a closed pane's session file is rewritten on exit, after the next trial starts.
            files += [
                p
                for p in d.glob("*.jsonl")
                if p.stat().st_mtime >= start and start <= first_ts(p) <= end
            ]
    own = None
    sid = (trial or {}).get("agentSessionId")
    if sid:
        own = next((p for p in files if p.stem == sid), None)
    if own is None:
        own = next(
            (
                p
                for p in sorted(files, key=lambda p: p.stat().st_mtime)
                if key and key in first_user_text_claude(p)
            ),
            None,
        )
    return own, [p for p in files if p != own], "claude"


def short(value, n=240) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


def runbook_result(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"raw": short(text, 200)}
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return {"raw": short(text, 200)}
    out = {}
    if isinstance(data, dict) and "runbooks" in data:
        out["selected"] = [r["id"] for r in data["runbooks"] if r.get("selected")]
        out["neighbours"] = [r["id"] for r in data["runbooks"] if not r.get("selected")]
    if isinstance(data, dict) and data.get("id"):
        out["got"] = data["id"]
    if isinstance(data, dict) and data.get("supporting_runbooks") is not None:
        out["supporting"] = [
            r["id"] + ("" if r.get("instructions") else " (note only)")
            for r in data["supporting_runbooks"]
        ]
    return out or {"raw": short(text, 200)}


def parse_claude(path: Path) -> dict:
    calls, texts, notices, users = [], [], [], []
    by_id: dict[str, dict] = {}
    usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    seen_msgs: set[str] = set()
    models: set[str] = set()
    t0 = None
    session = None
    for line in path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        at = ts(e.get("timestamp"))
        if at and t0 is None:
            t0 = at
        rel = (at - t0) if (at and t0) else 0.0
        session = session or e.get("sessionId")
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "assistant":
            if msg.get("model"):
                models.add(msg["model"])
            u = msg.get("usage")
            mid = msg.get("id")
            if u and mid not in seen_msgs:
                seen_msgs.add(mid)
                usage["input"] += u.get("input_tokens", 0)
                usage["cache_read"] += u.get("cache_read_input_tokens", 0)
                usage["cache_write"] += u.get("cache_creation_input_tokens", 0)
                usage["output"] += u.get("output_tokens", 0)
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "tool_use":
                    c = {
                        "at": rel,
                        "name": part["name"],
                        "input": part.get("input", {}),
                        "output": "",
                        "error": False,
                    }
                    calls.append(c)
                    by_id[part["id"]] = c
                elif part.get("type") == "text" and part.get("text", "").strip():
                    texts.append({"at": rel, "text": part["text"]})
        elif e.get("type") == "user":
            parts = (
                [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            )
            for part in parts:
                if part.get("type") == "tool_result":
                    c = by_id.get(part.get("tool_use_id"))
                    if c is not None:
                        c["output"] = text_of(part.get("content"))
                        c["error"] = bool(part.get("is_error"))
                elif part.get("type") == "text" and not e.get("isMeta"):
                    t = part.get("text", "")
                    if re.search(r"(^|\n)Daintree: ", t):
                        notices.append({"at": rel, "text": t})
                    elif t and not t.startswith("<"):
                        users.append({"at": rel, "text": t})
    duration = 0.0
    return {
        "kind": "claude",
        "session": session,
        "models": sorted(models),
        "calls": calls,
        "texts": texts,
        "notices": notices,
        "users": users,
        "usage": usage,
        "duration": duration,
    }


def parse_codex(path: Path) -> dict:
    """Codex records every step as an item_completed event: UserMessage, AgentMessage,
    McpToolCall (server, tool, arguments, result), CommandExecution, ... Its model
    reaches MCP tools through a code-mode `exec` script, so the items, not the raw
    custom_tool_call records, are what say which tool ran with which arguments."""
    calls, texts, notices, users = [], [], [], []
    usage = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    models: set[str] = set()
    session = None
    t0 = None
    for line in path.open():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = e.get("payload") or {}
        if e.get("type") == "session_meta":
            session = p.get("id")
            t0 = ts(p.get("timestamp") or e.get("timestamp"))
        elif e.get("type") == "turn_context" and p.get("model"):
            models.add(p["model"])
        elif e.get("type") == "event_msg" and p.get("type") == "token_count":
            tot = (p.get("info") or {}).get("total_token_usage") or {}
            if tot:
                usage = {
                    "input": tot.get("input_tokens", 0) - tot.get("cached_input_tokens", 0),
                    "cache_read": tot.get("cached_input_tokens", 0),
                    "cache_write": tot.get("cache_write_input_tokens", 0),
                    "output": tot.get("output_tokens", 0),
                }
    for stamp_, item in codex_items(path):
        rel = ts(stamp_) - t0 if (t0 and stamp_) else 0.0
        kind = item.get("type")
        if kind == "UserMessage":
            t = text_of(item.get("content"))
            (notices if re.search(r"(^|\n)Daintree: ", t) else users).append({"at": rel, "text": t})
        elif kind == "AgentMessage":
            t = text_of(item.get("content"))
            if t.strip():
                texts.append({"at": rel, "text": t})
        elif kind == "McpToolCall":
            res = item.get("result") or {}
            out = (
                json.dumps(res["structuredContent"])
                if res.get("structuredContent") is not None
                else text_of(res.get("content"))
            )
            calls.append(
                {
                    "at": rel,
                    "name": f"mcp__{item.get('server')}__{item.get('tool')}",
                    "input": item.get("arguments", {}),
                    "output": out,
                    "error": bool(res.get("isError")) or item.get("status") != "completed",
                }
            )
        elif kind == "CommandExecution":
            cmd = item.get("command")
            calls.append(
                {
                    "at": rel,
                    "name": "shell",
                    "input": cmd[-1] if isinstance(cmd, list) else cmd,
                    "output": str(item.get("aggregated_output", ""))[:2000],
                    "error": False,
                }
            )
    if not calls and not texts:
        # A rollout shape this parser does not know (Codex moves its format between
        # releases). Say so rather than report an empty, clean-looking trial.
        texts.append(
            {
                "at": 0.0,
                "text": "(transcript shape not recognised by summarise.py; read it with "
                ".claude/skills/learn-from-transcript/scripts/transcript_digest.py)",
            }
        )
    return {
        "kind": "codex",
        "session": session,
        "models": sorted(models),
        "calls": calls,
        "texts": texts,
        "notices": notices,
        "users": users,
        "usage": usage,
        "duration": 0.0,
    }


def classify(name: str) -> tuple[str, str]:
    """(server, tool) for an MCP tool name, else ("", name)."""
    m = re.match(r"mcp__(.+?)__(.+)$", name)
    if m:
        return m.group(1), m.group(2)
    return "", name


def report(t: dict, full: bool) -> list[str]:
    out = []
    rb, dtc, other = [], [], {}
    for c in t["calls"]:
        server, tool = classify(c["name"])
        if tool in ("search_runbooks", "get_runbook"):
            rb.append((c, tool))
        elif server.startswith("daintree"):
            dtc.append((c, tool))
        else:
            other[c["name"]] = other.get(c["name"], 0) + 1
    out.append(f"session {t['session']}  models {', '.join(t['models']) or '?'}")
    for u in t["users"][:1]:
        out.append(f"prompt: {short(u['text'], 400)}")
    out.append("")
    out.append(f"runbook calls ({len(rb)}):")
    for c, tool in rb:
        res = runbook_result(c["output"])
        out.append(f"  +{c['at']:6.1f}s {tool} {short(c['input'], 300)}")
        out.append(f"           -> {short(res, 400)}")
    if not rb:
        out.append("  (none: the agent never consulted the runbook server)")
    out.append(f"daintree actions ({len(dtc)}):")
    for c, tool in dtc:
        flag = " ERROR" if c["error"] else ""
        out.append(f"  +{c['at']:6.1f}s {tool}{flag} {short(c['input'], 500 if full else 300)}")
        out.append(f"           -> {short(c['output'], 1200 if full else 300)}")
    out.append(f"notices typed into the pane ({len(t['notices'])}):")
    for n in t["notices"]:
        out.append(f"  +{n['at']:6.1f}s {short(n['text'], 300)}")
    if t["users"][1:]:
        out.append("other user turns:")
        for u in t["users"][1:]:
            out.append(f"  +{u['at']:6.1f}s {short(u['text'], 200)}")
    out.append(
        "other tools: " + (", ".join(f"{k} x{v}" for k, v in sorted(other.items())) or "none")
    )
    if full:
        out.append("timeline:")
        for c in t["calls"]:
            out.append(f"  +{c['at']:6.1f}s CALL {c['name']} {short(c['input'], 400)}")
            out.append(f"           OUT {short(c['output'], 400)}")
        for x in t["texts"]:
            out.append(f"  +{x['at']:6.1f}s SAY {short(x['text'], 400)}")
    u = t["usage"]
    out.append(
        f"tokens: input {u['input']} + cache read {u['cache_read']}"
        f" + cache write {u['cache_write']}, output {u['output']}"
    )
    last = t["texts"][-1]["text"] if t["texts"] else "(no assistant text)"
    out.append("")
    out.append("final answer:")
    out += ["  " + line for line in last.strip().splitlines()]
    return out


def main(argv: list[str]) -> int:
    full = "--full" in argv
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if "--transcript" in argv:
        path = Path(args[0])
        t = parse_codex(path) if "rollout-" in path.name else parse_claude(path)
        if as_json:
            print(json.dumps(t, indent=2, default=str))
        else:
            print(f"transcript: {path}")
            print("\n".join(report(t, full)))
        return 0
    trial_dir = Path(args[0])
    done = json.loads((trial_dir / "done.json").read_text())
    own, children, kind = locate(done)
    header = [
        f"trial {done['trialId']}: {done['outcome']} after {done['seconds']}s "
        f"({done['request'].get('agentId')} / {done['request'].get('model', 'default')})",
        f"trial pane: {done.get('trialTerminalId')}  panes opened: "
        + ", ".join(f"{t['agent']}:{t['id'][-8:]}" for t in done.get("terminals", [])),
        f"transcript: {own or 'NOT FOUND'}",
    ]
    for c in children:
        first = first_user_text_claude(c) if kind == "claude" else codex_meta(c)[1]
        header.append(f"child transcript: {c}  (first message: {short(first, 120)})")
    if own is None:
        print("\n".join(header))
        return 1
    t = parse_codex(own) if kind == "codex" else parse_claude(own)
    if as_json:
        print(
            json.dumps(
                {
                    "done": done,
                    "transcript": str(own),
                    "children": [str(c) for c in children],
                    "parsed": t,
                },
                indent=2,
                default=str,
            )
        )
        return 0
    events = trial_dir / "events.jsonl"
    prompts = []
    if events.exists():
        for line in events.read_text().splitlines():
            e = json.loads(line)
            if e["msg"].startswith(("permission prompt", "trust dialog", "approving")):
                prompts.append(f"  {e['at'][11:19]} {e['msg']}")
    print("\n".join(header))
    if prompts:
        print("dialogs the holder saw:")
        print("\n".join(prompts))
    print("\n".join(report(t, full)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
