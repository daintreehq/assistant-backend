#!/usr/bin/env python3
"""Render a Claude Code or Codex transcript as a redacted, readable digest.

    .venv/bin/python .claude/skills/learn-from-transcript/scripts/transcript_digest.py \\
        <transcript.jsonl> [--out digest.md] [--max-output N] [--max-input N]
        [--include-injected]

Reads the two shapes a standard assistant leaves behind:

  * a Claude Code session   ~/.claude/projects/<slug>/<session-id>.jsonl
  * a Codex rollout         ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

A raw transcript is hundreds of kilobytes of JSON in which tool output is JSON
inside JSON strings. This prints the conversation in order with timestamps,
truncates every tool payload, attaches each result to its own call, and pulls
out two things worth a closer look:

  * **Runbook searches** — every ``search_runbooks`` call: the query as sent,
    the ranking that came back (id, score, selected), and which of the selected
    runbooks' tools the agent went on to call.
  * **Signals** — errors, timeouts, oversized outputs, repeated calls, short
    sends, interrupts. Leads, not findings.

Redaction is pattern-based: every string is scrubbed WHOLE before it is clipped,
because a key cut in half is too short for any pattern to recognise and the half
that survives is still a leak. It cannot know which project names, paths or
pasted content are private, so read before quoting anything anywhere.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

HOME = os.path.expanduser("~")

# Field names whose VALUE is a secret wherever they appear, matched through any
# depth of JSON escaping: an MCP result inside a Codex exec cell is escaped two
# or three times, and `\\\"token\\\":` must redact the same as `"token":`.
_SECRET_KEYS = (
    r"api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|"
    r"token|secret|client[_-]?secret|password|passwd|pwd|authorization|private[_-]?key|"
    r"access[_-]?key(?:[_-]?id)?|secret[_-]?access[_-]?key|session[_-]?key|bearer"
)

# Order matters: whole blocks and specific shapes before the generic rules, so a
# PEM block or a GitHub token is named rather than chewed into fragments.
_REDACTIONS = [
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.S,
        ),
        "<redacted:private-key>",
    ),
    (re.compile(r"(?i)\b(Bearer|Basic)(\s+|%20)[A-Za-z0-9._~+/=-]{6,}"), r"\1 <redacted>"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        "<redacted:jwt>",
    ),
    (
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
        "<redacted:github-token>",
    ),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}"), "<redacted:api-key>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted:slack-token>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted:aws-key>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<redacted:google-key>"),
    # scheme://user:password@host: keep the scheme and host, drop the userinfo.
    (
        re.compile(r"\b([a-z][a-z0-9+.-]{1,20}://)[^\s/:@\"'\\]{1,256}:[^\s/@\"'\\]{1,256}@"),
        r"\1<redacted>@",
    ),
    # "key": "value" — the WHOLE quoted value, spaces and commas included.
    (
        re.compile(
            r'(?i)("(?:[A-Za-z0-9_-]*[_-])?(?:' + _SECRET_KEYS + r')"\s*:\s*")'
            r'((?:\\.|[^"\\])*)(")'
        ),
        r"\1<redacted>\3",
    ),
    # The same, one or more levels of JSON escaping down: \"key\": \"value\". The
    # value ends at a quote escaped exactly as deep as its opening one; a quote
    # inside it is escaped deeper, so it cannot end the match early.
    (
        re.compile(
            r'(?i)((\\+)"(?:[A-Za-z0-9_-]*[_-])?(?:' + _SECRET_KEYS + r')\2"\s*:\s*\2")'
            r'(.*?)((?<!\\)\2")'
        ),
        r"\1<redacted>\4",
    ),
    # Unquoted key = value / key: value (shell, env dumps, YAML).
    (
        re.compile(
            r"(?i)((?:\b|_)(?:" + _SECRET_KEYS + r"))"
            r"(\\*[\"']?\s*[:=]\s*\\*[\"']?)([^\s\"'\\,;}&]+)"
        ),
        r"\1\2<redacted>",
    ),
]
# Bounded components keep this linear: an unbounded prefix retried at every
# position of a long unbroken string made it quadratic.
_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b"
)
# Long opaque runs. No `/` and no `-` in the class, so file paths, terminal ids
# and URLs survive; a run must mix letters and digits, so a long word or a hex
# SHA inside a path segment is not mistaken for one.
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+_=])[A-Za-z0-9+_=]{48,}(?![A-Za-z0-9+_=])")


def _long_token(match: re.Match[str]) -> str:
    text = match.group(0)
    if re.search(r"[A-Za-z]", text) and re.search(r"[0-9]", text):
        return "<redacted:long-token>"
    return text


def redact(text: Any) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if HOME and HOME != "/":
        text = text.replace(HOME, "~")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if "@" in text:
        text = _EMAIL.sub("<email>", text)
    return _LONG_TOKEN.sub(_long_token, text)


def clip(text: str, limit: int) -> str:
    """Clip an ALREADY-REDACTED string, never through a redaction marker."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = limit
    opening = text.rfind("<", max(0, limit - 32), limit)
    if (
        opening != -1
        and text.find(">", opening) >= limit
        and text.startswith(("<redacted", "<email"), opening)
    ):
        cut = opening
    return f"{text[:cut]} … [+{len(text) - cut} chars]"


def safe(text: Any, limit: int = 0) -> str:
    """The only way text reaches the output: redact the whole, then clip."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return clip(redact(text), limit)


def unescape(text: str) -> str:
    """Collapse the nested JSON escaping MCP results arrive wrapped in."""
    for _ in range(3):
        text = (
            text.replace("\\\\n", "\n")
            .replace("\\n", "\n")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return text


def flatten_content(content: Any) -> str:
    """Text out of a content field that may be a string or a block list."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
                elif "content" in block:
                    parts.append(flatten_content(block.get("content")))
        return "\n".join(parts)
    if isinstance(content, dict):
        return flatten_content(content.get("content") or content.get("text"))
    return str(content)


# Harness-generated wrappers. A message that is ONLY these is injected; one that
# carries a real request after them keeps the request.
_INJECTED_BLOCK = re.compile(
    r"<(system-reminder|command-message|command-name|command-args|local-command-stdout|"
    r"local-command-stderr|local-command-caveat|task-notification|user_instructions|"
    r"environment_context|permissions instructions|skills_instructions)>.*?</\1>",
    re.S,
)
_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>")


def split_injected(text: str) -> tuple[str, str | None]:
    """(genuine user text, injected summary or None)."""
    stripped = text.strip()
    for prefix in _INJECTED_PREFIXES:
        if stripped.startswith(prefix):
            first = stripped.splitlines()[0]
            return "", f"[{first[:80]} … {len(text)} chars]"
    names = [m.group(1) for m in _INJECTED_BLOCK.finditer(text)]
    if not names:
        return text, None
    rest = _INJECTED_BLOCK.sub("", text).strip()
    return rest, f"[injected: {', '.join(sorted(set(names)))}]"


@dataclass
class Event:
    kind: str  # user | assistant | injected | harness | call | result
    text: str
    ts: str | None = None
    name: str | None = None
    call_id: str | None = None
    raw: str = ""  # what signals and search parsing read; defaults to text
    is_error: bool = False
    step: int | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.raw:
            self.raw = self.text


def load_jsonl(path: str) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    bad = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                bad += 1
    return rows, bad


def detect(rows: list[dict[str, Any]]) -> str:
    for row in rows[:50]:
        if row.get("type") in ("session_meta", "response_item", "turn_context"):
            return "codex"
        if row.get("type") in ("user", "assistant") and isinstance(row.get("message"), dict):
            return "claude"
    return "unknown"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    """A field that should be text, whatever a malformed record put there."""
    if isinstance(value, str):
        return value
    return "" if value is None else json.dumps(value, ensure_ascii=False)


Meta = dict[str, Any]


def parse_codex(rows: list[dict[str, Any]]) -> tuple[Meta, list[Event]]:
    meta: Meta = {}
    contexts: list[dict[str, Any]] = []
    events: list[Event] = []
    usage = None
    for row in rows:
        kind = row.get("type")
        payload = _dict(row.get("payload"))
        ts = row.get("timestamp")
        if kind == "session_meta":
            for key in ("id", "cwd", "originator", "cli_version", "source", "model_provider"):
                if payload.get(key) is not None:
                    meta[key] = payload[key]
            git = payload.get("git")
            if isinstance(git, dict):
                meta["git"] = f"{git.get('branch')} @ {str(git.get('commit_hash') or '')[:12]}"
        elif kind == "turn_context":
            keys = ("model", "effort", "approval_policy", "sandbox_policy", "cwd")
            contexts.append({k: payload[k] for k in keys if payload.get(k) is not None})
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = _dict(payload.get("info"))
            usage = info.get("total_token_usage") or usage
        elif kind == "response_item":
            _codex_item(payload, ts, events)
    if contexts:
        meta["turn_contexts"] = [c for i, c in enumerate(contexts) if c not in contexts[:i]]
    if usage:
        meta["token_usage"] = usage
    return meta, events


def _codex_item(payload: dict[str, Any], ts: str | None, events: list[Event]) -> None:
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        text = flatten_content(payload.get("content"))
        if role in ("developer", "system"):
            summary = f"[{role} instructions: {len(text)} chars]"
            events.append(Event("injected", summary, ts, raw=text))
        elif role == "user":
            genuine, injected = split_injected(text)
            if injected:
                events.append(Event("injected", injected, ts, raw=text))
            if genuine.strip():
                events.append(Event("user", genuine, ts))
        else:
            events.append(Event("assistant", text, ts))
    elif ptype in ("custom_tool_call", "function_call", "local_shell_call"):
        raw = payload.get("input")
        if raw is None:
            raw = payload.get("arguments")
        if raw is None:
            raw = payload.get("action")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        name = _text(payload.get("name")) or str(ptype)
        events.append(Event("call", text, ts, name, _text(payload.get("call_id"))))
    elif ptype in ("custom_tool_call_output", "function_call_output", "local_shell_call_output"):
        out = payload.get("output")
        raw = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        text = out if isinstance(out, str) else flatten_content(out)
        is_error = False
        # A JSON document in a string: show its output, but keep the whole
        # document as `raw` so an exit code or timedOut in a sibling field
        # still reaches the signals.
        if isinstance(out, str) and out.lstrip().startswith("{"):
            try:
                doc = json.loads(out)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                text = flatten_content(doc.get("output")) or text
                code = doc.get("exit_code", _dict(doc.get("metadata")).get("exit_code"))
                is_error = (isinstance(code, int) and code != 0) or doc.get("isError") is True
        call_id = _text(payload.get("call_id"))
        events.append(Event("result", text, ts, None, call_id, raw=raw, is_error=is_error))


# System subtypes that carry no information about the session's course.
HARNESS_NOISE = frozenset(["turn_duration"])


def parse_claude(rows: list[dict[str, Any]]) -> tuple[Meta, list[Event]]:
    meta: Meta = {}
    events: list[Event] = []
    models: list[str] = []
    usage_by_message: dict[Any, dict[str, Any]] = {}
    for row in rows:
        kind = row.get("type")
        msg = _dict(row.get("message"))
        ts = row.get("timestamp")
        for key in ("sessionId", "cwd", "version", "gitBranch"):
            if key not in meta and row.get(key):
                meta[key] = row[key]
        if kind == "assistant":
            model = msg.get("model")
            if model and model not in models:
                models.append(model)
            # One API message is written as several records, each repeating the
            # same usage. Keep the last per id, or totals count a reply twice.
            use = msg.get("usage")
            if isinstance(use, dict):
                key = msg.get("id")
                usage_by_message[key if isinstance(key, str) and key else id(row)] = use
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and _text(block.get("text")).strip():
                    events.append(Event("assistant", _text(block.get("text")), ts))
                elif block.get("type") == "tool_use":
                    arguments = json.dumps(block.get("input"), ensure_ascii=False)
                    name, call_id = _text(block.get("name")), _text(block.get("id"))
                    events.append(Event("call", arguments, ts, name, call_id))
        elif kind == "user":
            content = msg.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            for block in blocks or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    events.append(
                        Event(
                            "result",
                            flatten_content(block.get("content")),
                            ts,
                            None,
                            _text(block.get("tool_use_id")),
                            is_error=bool(block.get("is_error")),
                        )
                    )
                elif block.get("type") == "text":
                    text = _text(block.get("text"))
                    genuine, injected = split_injected(text)
                    if injected:
                        # Kept whole: `--include-injected` is the only way to see
                        # what a reminder offered (deferred tools, instructions).
                        events.append(Event("injected", injected, ts, raw=text))
                    if genuine.strip():
                        events.append(Event("user", genuine, ts))
        elif kind == "system":
            # The harness's own records (/loop ticks, Stop-hook verdicts, away
            # summaries): the only trace of WHY a turn started.
            subtype = row.get("subtype") or "system"
            if subtype in HARNESS_NOISE:
                continue
            text = row.get("content")
            if not isinstance(text, str) or not text.strip():
                hooks = row.get("hookInfos") or []
                first = hooks[0] if hooks and isinstance(hooks[0], dict) else {}
                text = first.get("command") or ""
            label = f"{subtype}: {text.strip()}" if text else subtype
            events.append(Event("harness", label, ts, name=subtype))
    if models:
        meta["models"] = models
    totals: Counter[str] = Counter()
    for use in usage_by_message.values():
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        ):
            if isinstance(use.get(key), int):
                totals[key] += use[key]
    if totals:
        meta["token_usage"] = dict(totals)
    return meta, events


def number_steps(events: list[Event]) -> None:
    """A step per call; each result attached to its call by id, not position.

    Parallel calls return out of order, so "the most recent call" is the wrong
    owner for a result and mislabels exactly the steps a finding cites.
    """
    by_call_id: dict[str, int] = {}
    step = 0
    for event in events:
        if event.kind == "call":
            step += 1
            event.step = step
            if event.call_id:
                by_call_id[event.call_id] = step
    for event in events:
        if event.kind == "result" and event.call_id:
            event.step = by_call_id.get(event.call_id)


# ── tools and runbook searches ─────────────────────────────────────────────

# A Codex exec cell runs tools as JavaScript: `tools.mcp__daintree__terminal_list({})`.
INNER_TOOL = re.compile(r"tools\.([A-Za-z0-9_]+)\s*\(")
SEARCH_TOOL = "search_runbooks"
_JS_SEARCH = re.compile(r"search_runbooks\s*\(\s*\{(.*?)\}\s*\)", re.S)
# The property name may be bare or quoted: `{query: "…"}` and `{"query": "…"}`.
_JS_QUERY = re.compile(r"""(?:["']query["']|\bquery)\s*:\s*(["'`])((?:\\.|(?!\1).)*)\1""", re.S)
_JS_MAX = re.compile(r"""(?:["']max_results["']|\bmax_results)\s*:\s*(\d+)""")


def tool_names(event: Event) -> list[str]:
    """The tool(s) a call used. A Codex exec cell hides MCP calls inside JS."""
    return INNER_TOOL.findall(event.text or "") or [event.name or "?"]


def runbook_tool(name: str) -> str:
    """A tool name as the agent saw it, in the dotted form runbooks write.

    `mcp__daintree__terminal_sendCommand` -> `terminal.sendCommand`; the CLI's
    wire form `terminal__rename` -> `terminal.rename`. Anything else (Bash,
    Read) comes back unchanged and matches no runbook tool, which is right.
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        name = parts[2] if len(parts) == 3 else name
    return name.replace("__", ".").replace("_", ".")


@dataclass
class Search:
    step: int | None
    ts: str | None
    query: str | None
    max_results: int | None
    results: list[dict[str, Any]]
    error: str | None = None
    #: Anything the reader must know before trusting the pairing above.
    note: str | None = None


def _search_docs(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Every search result document in a tool output, in order, however wrapped.

    Claude Code hands back the text block, which is the JSON itself. Codex may
    hand back a CallToolResult, or an exec cell's printout, and any layer can be
    a JSON string inside JSON. Each layer is decoded as JSON in its own right: a
    blanket unescape corrupts the escaped newlines and quotes in `instructions`,
    and the document is lost.
    """
    if depth > 8:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("runbooks"), list) and "catalog_revision" in value:
            return [value]
        if value.get("structuredContent") is not None:
            # A CallToolResult: `content` carries the same document again as text.
            found = _search_docs(value["structuredContent"], depth + 1)
            if found:
                return found
        return [doc for item in value.values() for doc in _search_docs(item, depth + 1)]
    if isinstance(value, list):
        return [doc for item in value for doc in _search_docs(item, depth + 1)]
    if not isinstance(value, str) or "catalog_revision" not in value:
        return []
    try:
        return _search_docs(json.loads(value), depth + 1)
    except ValueError:
        pass
    # Printed text with documents embedded in it: decode each object in turn.
    found: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    index = value.find("{")
    while index != -1:
        try:
            obj, end = decoder.raw_decode(value, index)
        except ValueError:
            index = value.find("{", index + 1)
            continue
        found.extend(_search_docs(obj, depth + 1))
        index = value.find("{", end)
    return found


def _js_string(quote: str, body: str) -> str | None:
    """A JS string literal's value, or None when only the running cell knew it."""
    if quote == "`" and "${" in body:
        return None
    if quote != '"':
        body = re.sub(r'(?<!\\)"', r'\\"', body.replace("\\" + quote, quote))
    body = re.sub(r"(?<!\\)\\x([0-9A-Fa-f]{2})", r"\\u00\1", body)
    try:
        value = json.JSONDecoder(strict=False).decode(f'"{body}"')
    except ValueError:
        return body
    return value if isinstance(value, str) else body


def _direct_search(event: Event, result: Event | None) -> Search:
    try:
        args = _dict(json.loads(event.text or "{}"))
    except ValueError:
        args = {}
    query, limit = args.get("query"), args.get("max_results")
    docs = _search_docs(result.raw) if result else []
    error = None
    if result is None:
        error = "no result recorded for this call"
    elif result.is_error or not docs:
        error = result.text or "no result document"
    return Search(
        event.step,
        event.ts,
        query if isinstance(query, str) else None,
        limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        docs,
        error,
    )


def _cell_searches(event: Event, result: Event | None) -> list[Search]:
    """Searches made inside one Codex exec cell.

    The cell's printout is all there is, so a result is paired with a call only
    when that is unambiguous. Pairing by print order when a cell searched more
    than once is flagged; a count mismatch pairs nothing rather than guessing.
    """
    bodies = _JS_SEARCH.findall(event.text or "") or [""]
    docs = _search_docs(result.raw) if result else []
    searches = []
    for index, body in enumerate(bodies):
        match = _JS_QUERY.search(body)
        query = _js_string(match.group(1), match.group(2)) if match else None
        limit_match = _JS_MAX.search(body)
        search = Search(
            event.step, event.ts, query, int(limit_match.group(1)) if limit_match else None, []
        )
        if match and query is None:
            search.note = "the query is a runtime expression; read the cell in raw.jsonl"
        if len(bodies) == 1:
            search.results = docs
            if len(docs) > 1:
                search.note = f"the cell printed {len(docs)} results for one call site"
        elif len(docs) == len(bodies):
            search.results = [docs[index]]
            search.note = "paired by print order within the cell; confirm in raw.jsonl"
        else:
            search.note = (
                f"the cell searched {len(bodies)} times and printed {len(docs)} results; "
                "which answers which is unknown"
            )
        if result is None:
            search.error = "no result recorded for this cell"
        elif not search.results and not search.note:
            search.error = "result not found in the cell output"
        searches.append(search)
    return searches


def find_searches(events: list[Event]) -> list[Search]:
    results = {e.call_id: e for e in events if e.kind == "result" and e.call_id}
    searches: list[Search] = []
    for event in events:
        if event.kind != "call":
            continue
        result = results.get(event.call_id) if event.call_id else None
        if (event.name or "").endswith(SEARCH_TOOL):
            searches.append(_direct_search(event, result))
        elif any(name.endswith(SEARCH_TOOL) for name in tool_names(event)):
            searches.extend(_cell_searches(event, result))
    return searches


def tools_called_after(events: list[Event], step: int | None) -> set[str]:
    return {
        runbook_tool(name)
        for event in events
        if event.kind == "call" and (step is None or (event.step or 0) > step)
        for name in tool_names(event)
    }


# ── signals ────────────────────────────────────────────────────────────────

SEND_IN_JS = re.compile(
    r"sendCommand\w*\s*\(\s*\{[^}]*?[\"']?command[\"']?\s*:\s*[\"']([^\"']{0,3})[\"']", re.S
)
ERROR_MARKERS = [
    # Error PAYLOADS, not error vocabulary: tool descriptions name error codes
    # in prose, and a bare-word match fired on every discovery call.
    ("TIER_NOT_PERMITTED", re.compile(r"\"(?:code|errorCode)\"\s*:\s*\"TIER_NOT_PERMITTED\"")),
    ("MCP isError", re.compile(r"\"isError\"\s*:\s*true")),
    ("tool error field", re.compile(r"\"(?:error|errorCode)\"\s*:\s*[\"{]")),
    ("ok:false", re.compile(r"\"ok\"\s*:\s*false")),
    ("non-zero exit", re.compile(r"\"exit_code\"\s*:\s*[1-9]|Process exited with code [1-9]")),
    ("not found", re.compile(r"(?i)terminal not found|\bNOT_FOUND\b|ENOENT")),
]
_STOP_VERBS = ("interrupt", "kill", "close", "stop", "cancel")


def is_stop_tool(name: str) -> bool:
    # By name COMPONENT, so `Skill` is not "kill" and `terminal_interruptOwned` is.
    parts = re.split(r"[._]+|(?<=[a-z])(?=[A-Z])", name or "")
    return any(part.lower().startswith(_STOP_VERBS) for part in parts)


def short_sends(event: Event) -> list[str]:
    found = list(SEND_IN_JS.findall(event.text or ""))
    if "sendcommand" in (event.name or "").lower():
        try:
            args = json.loads(event.text or "")
        except ValueError:
            args = None
        command = args.get("command") if isinstance(args, dict) else None
        if isinstance(command, str) and len(command) <= 3:
            found.append(command)
    return found


def signals(events: list[Event]) -> dict[str, list[str]]:
    calls = [e for e in events if e.kind == "call"]
    out: dict[str, list[str]] = {}

    def where(event: Event) -> str:
        return f"step {event.step}" if event.step else "unmatched result"

    counts = Counter(name for event in calls for name in tool_names(event))
    out["tool use"] = [f"{n:4d}  {name}" for name, n in counts.most_common(25)]

    shorts = [
        f"{where(e)}: sendCommand with {value!r}, a prompt answered for the user?"
        for e in calls
        for value in short_sends(e)
    ]
    stops = [
        f"{where(e)}: {', '.join(dict.fromkeys(n for n in tool_names(e) if is_stop_tool(n)))}"
        for e in calls
        if any(is_stop_tool(n) for n in tool_names(e))
    ]
    if shorts:
        out["short sends"] = shorts
    if stops:
        out["interrupts, kills, closes"] = stops

    timeouts: list[str] = []
    errors: list[str] = []
    big: list[str] = []
    for event in (e for e in events if e.kind == "result"):
        text = unescape(event.raw or "")
        if re.search(r"\"timedOut\"\s*:\s*true", text):
            timeouts.append(where(event))
        hits = [label for label, pattern in ERROR_MARKERS if pattern.search(text)]
        if event.is_error:
            hits.insert(0, "tool reported an error")
        if hits:
            errors.append(f"{where(event)}: {', '.join(hits)}")
        match = re.search(r"original token count:\s*(\d+)", text)
        if match:
            big.append(f"{where(event)}: output truncated (original {match.group(1)} tokens)")
        elif len(event.raw or "") > 20000:
            big.append(f"{where(event)}: {len(event.raw)} chars of output")
    if timeouts:
        out["waits that timed out"] = timeouts
    if errors:
        out["error markers"] = errors
    if big:
        out["oversized outputs"] = big

    harness = Counter(e.name for e in events if e.kind == "harness")
    if harness:
        out["harness events"] = [f"{n:4d}  {name}" for name, n in harness.most_common()]

    repeats = Counter((e.text or "").strip() for e in calls)
    dup = [n for text, n in repeats.items() if n > 1 and text]
    if dup:
        out["identical repeated calls"] = [
            f"{len(dup)} call bodies were sent more than once (max {max(dup)} times)"
        ]
    return out


# ── rendering ──────────────────────────────────────────────────────────────


def stamp(ts: str | None) -> str:
    if not isinstance(ts, str):
        return ""
    match = re.search(r"T(\d{2}:\d{2}:\d{2})", ts)
    if not match:
        return ""
    return f" · {match.group(1)}{'Z' if ts.endswith('Z') else ''}"


def _fence(text: str) -> list[str]:
    return ["```", text.replace("```", "ʼʼʼ"), "```"]


def render_searches(events: list[Event]) -> list[str]:
    searches = find_searches(events)
    lines = ["## Runbook searches", ""]
    if not searches:
        lines += [
            "No `search_runbooks` call in this session. Every task in it is still a routing",
            "question: route the user's own words with `bench search`.",
            "",
        ]
        return lines
    revisions = sorted(
        {r for s in searches for d in s.results if isinstance(r := d.get("catalog_revision"), str)}
    )
    lines += [
        f"{len(searches)} search(es). Served from catalog_revision: "
        f"{', '.join(f'`{safe(r)}`' for r in revisions) or 'unknown'}. `bench search` prints the",
        "working tree's; if they differ, the catalog has moved since this session.",
        "",
    ]
    for search in searches:
        limit = f" · max_results {search.max_results}" if search.max_results else ""
        lines += [f"### step {search.step}{stamp(search.ts)}{limit}", ""]
        lines += _fence(safe(search.query) if search.query else "<query not recovered>") + [""]
        if search.error:
            lines += [f"**error:** {safe(search.error, 400)}", ""]
        if search.note:
            lines += [f"_note: {safe(search.note)}_", ""]
        for doc in search.results:
            hits = [h for h in doc.get("runbooks") or [] if isinstance(h, dict)]
            if not any(h.get("selected") for h in hits):
                lines += ["_nothing selected: the search said no runbook really fits_", ""]
            lines += ["| # | runbook | score | selected |", "|---|---|---|---|"]
            for rank, hit in enumerate(hits, 1):
                score = hit.get("score")
                shown = f"{score:.4f}" if isinstance(score, (int, float)) else "?"
                mark = "**yes**" if hit.get("selected") else ""
                lines.append(f"| {rank} | `{safe(hit.get('id'))}` | {shown} | {mark} |")
            lines.append("")
            called = tools_called_after(events, search.step)
            for hit in hits:
                tools = hit.get("required_tools")
                tools = tools if isinstance(tools, list) else []
                needed = [safe(t) for t in tools if isinstance(t, str)]
                if not hit.get("selected") or not needed:
                    continue
                used = [t for t in needed if t in called]
                unused = [t for t in needed if t not in called]
                lines.append(
                    f"- `{safe(hit.get('id'))}` names {len(needed)} tool(s); called after this "
                    f"search: {', '.join(used) or 'none'}"
                    + (f"; never called: {', '.join(unused)}" if unused else "")
                )
            lines.append("")
    return lines


def render(
    path: str,
    fmt: str,
    meta: Meta,
    events: list[Event],
    bad_lines: int,
    max_output: int,
    max_input: int,
    include_injected: bool,
) -> str:
    lines = ["# Transcript digest", "", f"- source: `{safe(os.path.abspath(path))}`"]
    lines.append(f"- format: {fmt}")
    if bad_lines:
        lines.append(f"- records skipped (unparseable or not objects): {bad_lines}")
    for key, value in meta.items():
        lines.append(f"- {key}: {safe(value, 600)}")
    users = sum(1 for e in events if e.kind == "user")
    calls = sum(1 for e in events if e.kind == "call")
    unmatched = sum(1 for e in events if e.kind == "result" and not e.step)
    orphan = f", results with no matching call: {unmatched}" if unmatched else ""
    lines += [f"- user turns: {users} · tool calls: {calls}{orphan}", ""]

    lines += render_searches(events)

    lines += ["## Signals", "", "Leads to read, not findings. Each points at a step below.", ""]
    for heading, items in signals(events).items():
        lines += [f"**{heading}**", ""] + [f"- {safe(item)}" for item in items] + [""]

    lines += ["## Timeline", ""]
    for event in events:
        when = stamp(event.ts)
        if event.kind == "injected":
            lines.append(f"_{safe(event.text)}{when}_")
            if include_injected and event.raw != event.text:
                lines += [""] + _fence(safe(event.raw))
            lines.append("")
        elif event.kind == "harness":
            lines += [f"_[harness] {safe(event.text, 300)}{when}_", ""]
        elif event.kind in ("user", "assistant"):
            if (event.text or "").strip():
                lines += [f"### {event.kind.upper()}{when}", "", safe(event.text.strip()), ""]
        elif event.kind == "call":
            names = ", ".join(dict.fromkeys(tool_names(event)))
            lines += [f"**step {event.step} → {safe(names)}**{when}", ""]
            lines += _fence(safe(event.text or "", max_input)) + [""]
        elif event.kind == "result":
            label = f"step {event.step} result" if event.step else "result (no matching call)"
            if event.is_error:
                label += " · error"
            lines += [f"<details><summary>{label}{when}</summary>", ""]
            lines += _fence(clip(redact(unescape(event.text or "")), max_output))
            lines += ["</details>", ""]
    return "\n".join(lines) + "\n"


def digest(path: str, *, max_output: int, max_input: int, include_injected: bool) -> str:
    """The digest as a string. Raises ValueError when the file is not a transcript."""
    rows, bad = load_jsonl(path)
    if not rows:
        raise ValueError(f"no JSON records in {path}")
    fmt = detect(rows)
    if fmt == "codex":
        meta, events = parse_codex(rows)
    elif fmt == "claude":
        meta, events = parse_claude(rows)
    else:
        raise ValueError("neither a Codex rollout nor a Claude Code session; read it raw")
    if not any(e.kind in ("user", "assistant", "call") for e in events):
        raise ValueError(f"parsed as {fmt} but found no conversation in it")
    number_steps(events)
    return render(path, fmt, meta, events, bad, max_output, max_input, include_injected)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("transcript")
    parser.add_argument("--out", help="write the digest here instead of stdout")
    parser.add_argument(
        "--max-output", type=int, default=1500, help="chars kept per tool result (0 = all)"
    )
    parser.add_argument(
        "--max-input", type=int, default=1200, help="chars kept per tool call (0 = all)"
    )
    parser.add_argument(
        "--include-injected",
        action="store_true",
        help="print injected instructions (AGENTS.md, reminders) in full, redacted",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not os.path.isfile(args.transcript):
        sys.stderr.write(f"transcript_digest: no such file: {redact(args.transcript)}\n")
        return 2
    try:
        text = digest(
            args.transcript,
            max_output=args.max_output,
            max_input=args.max_input,
            include_injected=args.include_injected,
        )
    except ValueError as exc:
        sys.stderr.write(f"transcript_digest: {redact(str(exc))}\n")
        return 3
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        sys.stderr.write(f"transcript_digest: wrote {redact(args.out)}\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
