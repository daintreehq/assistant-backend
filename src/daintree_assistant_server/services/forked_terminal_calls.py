"""Names, schema and serialization for the forked terminal-spawn call.

One module owns every literal this feature puts on a wire — the internal
function's name, the target it expands into, the reserved message names, the
branch count, the output ceiling — so that the orchestrator, the expansion
service, the rewriter and the validator cannot drift into two spellings of the
same string. A leaked internal name is a client 400 and a spoofed one is a
security hole; neither is a thing to spell twice.

**The internal function is server-owned and provider-facing.** It is injected
into ``ProviderChatRequest.tools`` after the client's inventory has been
validated and projected, never into ``RespondRequest.input.tools`` — that object
is the client's real callable surface and is replayed back to us next round. The
``daintree_internal__`` prefix is already refused on every inbound path
(``services/validation.py``), which is what makes a name in this namespace
unspoofable rather than merely private.
"""

from __future__ import annotations

import json
from typing import Any, Final

from ..contracts.forked_terminals import (
    FORK_BRANCH_COUNT,
    MAX_AGENT_ID_CHARS,
    MAX_WORKTREE_ID_CHARS,
)

#: The internal function the primary model calls. Under the OpenAI 64-character
#: function-name limit, and inside the namespace the request boundary already
#: refuses from clients.
FORK_TOOL_WIRE_NAME: Final = "daintree_internal__fork_spawn_terminals"

#: The one tool it expands into. Both spellings, because skills declare tools in
#: the dotted form and the wire carries the double-underscore one.
SPAWN_TOOL_DOTTED_NAME: Final = "agentTask.spawnForEdits"
SPAWN_TOOL_WIRE_NAME: Final = "agentTask__spawnForEdits"

#: Reserved ``name`` values on the server-authored messages an expansion request
#: appends. Refused from client history (``services/validation.py``) so a user
#: typing the same words cannot address the expander, and so a transcript cannot
#: replay one back at us.
FORK_MANIFEST_MESSAGE_NAME: Final = "daintree_internal_fork_manifest"
FORK_BRANCH_MESSAGE_NAME: Final = "daintree_internal_fork_branch"
FORK_REPAIR_MESSAGE_NAME: Final = "daintree_internal_fork_repair"

RESERVED_MESSAGE_NAMES: Final = frozenset(
    {
        FORK_MANIFEST_MESSAGE_NAME,
        FORK_BRANCH_MESSAGE_NAME,
        FORK_REPAIR_MESSAGE_NAME,
    }
)

#: The whole namespace is reserved, not just the three names in use — the same
#: shape as ``RESERVED_TOOL_PREFIX``. A fourth server-authored message added
#: later must not need a second edit at the request boundary to become
#: unspoofable, and the reader of these names is a language model rather than a
#: protocol parser, so a near-miss is worth refusing too.
RESERVED_MESSAGE_PREFIX: Final = "daintree_internal_"


def is_reserved_message_name(name: str | None) -> bool:
    """Whether a client-supplied message ``name`` addresses server machinery.

    Case-insensitive and applied to the STRIPPED value, then refused rather than
    normalized. ``" Daintree_Internal_Fork_Branch "`` is not the reserved string
    to a byte comparison and is indistinguishable from it to the model that
    reads it, so accepting it after tidying it up would defeat the check it
    passed.
    """
    if name is None:
        return False
    return name.strip().casefold().startswith(RESERVED_MESSAGE_PREFIX)


#: Output ceiling for one branch expansion. It has to fit a 300–600 token
#: ``taskPrompt`` plus the surrounding spawn arguments and tool-call overhead;
#: measure truncation before lowering it.
FORK_EXPANSION_MAX_TOKENS: Final = 900

#: The tags the manifest and repair payloads are framed with. They exist so the
#: expander reads relayed, user-derived text as DATA — the same proximity
#: framing every other untrusted carrier in this server uses.
FORK_MANIFEST_TAG: Final = "fork_manifest"
FORK_REJECTED_TAG: Final = "rejected_call"


def canonical_json(value: object) -> str:
    """Compact JSON, unsorted, unescaped — the encoding cache diagnostics measure.

    Three choices, all load-bearing and all matching ``benchmarks/_harness/wire.py``:

    - compact separators, because the bytes are what the prefix cache matches;
    - ``ensure_ascii=False``, because escaping one CJK character to six inflates
      a measurement by 5x and, here, would inflate the manifest the four branch
      requests share;
    - **no key sorting**, because sorting is the obvious thing to do to a
      canonical form and the wrong thing to do to this one. Two manifests with
      the same keys in a different insertion order are a cache MISS; a sorted
      encoding would report them identical, which is a false negative on the one
      property the four requests exist to hold.
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


# The internal function's schema, written out rather than generated from
# ``ForkSpawnPlan``. Pydantic's JSON schema emits ``$defs`` and ``$ref`` for the
# nested models, and a provider-facing function schema wants to be flat, stable
# and readable — these bytes sit in every enabled request's tool region, so what
# goes upstream should be what a reader sees here. ``contracts/forked_terminals``
# stays the authority on what is ACCEPTED; this is what is ADVERTISED, and
# ``test_forked_terminal_contracts`` pins the two together.
_BRANCH_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "worktreeId", "brief"],
    "properties": {
        "id": {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_-]{0,47}$",
            "description": "Short slug naming this branch, e.g. \"backend\". Unique in the plan.",
        },
        "worktreeId": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_WORKTREE_ID_CHARS,
            "description": (
                "The ALREADY-RESOLVED worktree this branch's agent runs in. "
                "Never invent one; resolve or create worktrees in an earlier turn."
            ),
        },
        "brief": {
            "type": "string",
            "minLength": 8,
            "maxLength": 2_000,
            "description": (
                "Compact: outcome, the scope this branch owns, what it must NOT "
                "touch, and what done looks like. Two or three sentences. Do NOT "
                "write the full worker prompt here."
            ),
        },
        "agentId": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_AGENT_ID_CHARS,
            "description": "Overrides defaults.agentId.",
        },
        "mode": {
            "type": "string",
            "enum": ["edit", "explore"],
            "description": "Overrides defaults.mode.",
        },
        "watch": {"type": "boolean", "description": "Overrides defaults.watch."},
        "titleHint": {
            "type": "string",
            "maxLength": 80,
            "description": "Optional 2-4 word tab label for this branch.",
        },
        "contextFilePaths": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 4_096},
            "description": "Files this branch's agent should be handed up front.",
        },
        "acceptanceHint": {
            "type": "string",
            "maxLength": 2_000,
            "description": "Optional checkable definition of done to expand from.",
        },
        "watchGoalHint": {
            "type": "string",
            "maxLength": 1_000,
            "description": "Optional steer for the supervisor watcher's goal.",
        },
    },
}

_FORK_TOOL_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sharedGoal", "branches"],
    "properties": {
        "sharedGoal": {
            "type": "string",
            "minLength": 8,
            "maxLength": 4_000,
            "description": "The one goal all four branches serve.",
        },
        "sharedConstraints": {
            "type": "array",
            "maxItems": 16,
            "items": {"type": "string"},
            "description": "Rules every branch must follow. State them once here.",
        },
        "defaults": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "agentId": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_AGENT_ID_CHARS,
                },
                "mode": {"type": "string", "enum": ["edit", "explore"]},
                "watch": {"type": "boolean"},
            },
            "description": "Values shared by every branch; a branch's own field wins.",
        },
        "branches": {
            "type": "array",
            "minItems": FORK_BRANCH_COUNT,
            "maxItems": FORK_BRANCH_COUNT,
            "items": _BRANCH_SCHEMA,
            "description": f"Exactly {FORK_BRANCH_COUNT} branches, in the order to spawn them.",
        },
    },
}

_FORK_TOOL_DESCRIPTION: Final = (
    "Use ONLY when this reply must spawn exactly four fresh agent terminals. "
    "Provide one shared goal and four compact branch briefs plus the exact "
    "agentId, worktreeId, mode and watch policy for each branch (per branch or "
    "in defaults; all four must resolve, watch included). Do NOT write the four "
    "full task prompts; the backend expands them concurrently and replaces this "
    "call with four normal agentTask.spawnForEdits calls. Every branch needs its "
    "OWN already-resolved worktree id — never a branch name, never one worktree "
    "twice. Call this function ALONE: no other tool call in the same reply. It "
    "cannot fan out any other tool."
)

#: The wire definition, built once at import. A module-level constant rather
#: than a builder call per turn: these bytes sit in the tool region of every
#: enabled request, and the prefix cache matches them literally, so they must be
#: the same object with the same key order every time.
FORK_TOOL_DEFINITION: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": FORK_TOOL_WIRE_NAME,
        "description": _FORK_TOOL_DESCRIPTION,
        "parameters": _FORK_TOOL_SCHEMA,
    },
}


def find_tool(tools: list[dict[str, Any]] | None, wire_name: str) -> dict[str, Any] | None:
    """The named entry in a normalized provider tools array, or ``None``.

    Used for both directions of the feature's one dependency: the fork tool is
    not offered unless the spawn tool is present, and the expansion result is
    validated against the spawn tool's REAL schema rather than a second copy
    maintained here — the CLI owns that schema and this repo only sees whatever
    the client sent this turn.
    """
    for tool in tools or ():
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name") == wire_name:
            return tool
    return None
