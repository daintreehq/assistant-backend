"""Deterministic cost/cache flagship — the free, CI-able scoreboard.

Measures, over the REAL ``PromptAssembler.build_main_request`` and the EXACT bytes
``OpenRouterProvider`` would POST, the numbers the whole cost/latency story turns on:

1. **Cacheable-prefix stability per transition type.** DeepSeek's server-side
   prefix cache reuses the leading, byte-identical portion of a request. The
   assembler's whole layout exists to keep the expensive ``[system messages] →
   [tools]`` region byte-stable across turns (the documented 36%→99% fix). For
   each transition (tool-continuation, runtime-context change, skill topic-shift,
   new session) we build the two consecutive requests and measure how much of the
   cacheable region survives byte-identical:
     - ``system_tools_stable`` — fraction of the [system+tools] region that is a
       byte prefix of the next turn's. **1.0 is the target** for continuation and
       runtime-context changes (the layout's core promise); a topic-shift is an
       EXPECTED drop (only base+integrations stay stable), not a failure.
     - ``full_prefix`` — fraction of the whole [system+tools+conversation] region
       reused (the append-only conversation is the largest cached segment).

2. **Per-layer byte / token budget** — where the request's size lives (base,
   integrations, skills, the ~18k-token tool schemas, conversation, the volatile
   tail). ``token_est`` is a bytes/4 PROXY (no tokenizer in the repo); the live
   cross-check (``live.py``) is what pins real magnitude.

3. **State-token size** vs active-skill-set size — the portable, stateless-safe
   token stays tiny (Cloud Run carries no server memory; correctness rides this).

All deterministic: no API key, no network, safe in CI.
"""

from __future__ import annotations

from typing import Any

from daintree_assistant_server.config import OPENROUTER_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.prompts.loader import (
    TEMPLATE_SELECTOR_SYSTEM,
    PromptLoader,
)
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.models import Skill
from daintree_assistant_server.state.codec import StateCodec
from daintree_assistant_server.text_utils import estimate_tokens

from .._harness.env import build_provider_body
from .._harness.fixtures import build_respond_request, load_tool_inventory
from .._harness.wire import (
    cacheable_region_bytes,
    common_prefix_len,
    dumps,
    layer_segments,
    wire_bytes,
)


def _system_tools_bytes(body: dict[str, Any]) -> bytes:
    """The [system messages] + [tools] portion only — the expensive prefix the
    layout is engineered to keep byte-stable (excludes the conversation)."""
    messages = body.get("messages") or []
    units = [dumps(m) for m in messages if m.get("role") == "system"]
    if body.get("tools"):
        units.append(dumps(body["tools"]))
    return b"\x1e".join(units)


def _body_for(
    assembler: PromptAssembler,
    settings: Settings,
    request: RespondRequest,
    skills: list[Skill],
) -> dict[str, Any]:
    pr = assembler.build_main_request(
        request=request, skills=skills, upstream_model=OPENROUTER_MODEL
    )
    return build_provider_body(pr, settings)


def _stability(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    st_b, st_a = _system_tools_bytes(before), _system_tools_bytes(after)
    full_b, full_a = cacheable_region_bytes(before), cacheable_region_bytes(after)
    return {
        "system_tools_stable": round(common_prefix_len(st_b, st_a) / len(st_a), 6),
        "system_tools_bytes": len(st_a),
        "full_prefix": round(common_prefix_len(full_b, full_a) / len(full_a), 6),
        "full_region_bytes": len(full_a),
    }


# ---- conversation building blocks -----------------------------------------

_USER = {"role": "user", "content": "Prepare this branch for review and summarize what changed."}
_ASSIST_TOOL = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "fs__read", "arguments": '{"path":"CHANGELOG.md"}'},
        }
    ],
}
_TOOL_RESULT = {
    "role": "tool",
    "tool_call_id": "call_1",
    "content": "## Unreleased\n- refactored checkout\n- fixed a race in the watcher poll loop",
}


def _transitions(
    assembler: PromptAssembler, settings: Settings, skills: list[Skill]
) -> list[dict[str, Any]]:
    inv_present = bool(load_tool_inventory())
    a, b = (skills[:1], skills[1:2]) if len(skills) >= 2 else (skills[:1], [])

    def req(messages: list[dict[str, Any]], **kw: Any) -> RespondRequest:
        return build_respond_request(messages=messages, **kw)

    out: list[dict[str, Any]] = []

    # 1. tool-continuation: same skills/runtime; a tool call + result are appended.
    before = _body_for(assembler, settings, req([_USER]), skills)
    after = _body_for(assembler, settings, req([_USER, _ASSIST_TOOL, _TOOL_RESULT]), skills)
    out.append({
        "transition": "tool_continuation",
        "expect": "system_tools_stable == 1.0 (only appended messages + tail differ)",
        "floor_system_tools_stable": 1.0,
        **_stability(before, after),
    })

    # 2. runtime-context change: same conversation/skills; the volatile runtime rides
    #    the trailing user-role block, so system+tools must stay byte-identical.
    rt1 = {"permission_tier": "operator",
           "worktree": {"current": {"id": "wt-1", "branch": "feature/checkout"}},
           "open_terminals": [{"id": "t1", "agent_state": "working"}]}
    rt2 = {"permission_tier": "operator",
           "worktree": {"current": {"id": "wt-1", "branch": "feature/checkout-v2"}},
           "open_terminals": [{"id": "t1", "agent_state": "idle", "exit_code": 0}]}
    before = _body_for(assembler, settings, req([_USER], runtime=rt1), skills)
    after = _body_for(assembler, settings, req([_USER], runtime=rt2), skills)
    out.append({
        "transition": "runtime_context_change",
        "expect": "system_tools_stable == 1.0 (runtime change is tail-only)",
        "floor_system_tools_stable": 1.0,
        **_stability(before, after),
    })

    # 3. startup-context change: project/roster changes intentionally invalidate the
    #    cache from the stable startup row onward, but never the expensive system +
    #    tool-schema region before it.
    startup1 = {
        "project": {"id": "acme-web", "name": "Acme Web", "path": "/repo/acme"},
        "agent_roster": {
            "agents": [{"id": "codex", "source": "built-in", "pinned": True}],
            "complete": True,
            "availability_complete": True,
            "total_count": 1,
        },
    }
    startup2 = {
        "project": {"id": "acme-api", "name": "Acme API", "path": "/repo/api"},
        "agent_roster": {
            "agents": [{"id": "claude", "source": "built-in", "pinned": True}],
            "complete": True,
            "availability_complete": True,
            "total_count": 1,
        },
    }
    before = _body_for(assembler, settings, req([_USER], startup=startup1), skills)
    after = _body_for(assembler, settings, req([_USER], startup=startup2), skills)
    out.append({
        "transition": "startup_context_change",
        "expect": "system_tools_stable == 1.0 (startup changes only its following prefix)",
        "floor_system_tools_stable": 1.0,
        **_stability(before, after),
    })

    # 4. skill topic-shift: the active skill set changes → the loaded-skills system
    #    message changes → the contract + tools that follow it re-encode. EXPECTED
    #    drop; only base+integrations stay stable (measured below as the floor).
    if b:
        before = _body_for(assembler, settings, req([_USER]), a)
        after = _body_for(assembler, settings, req([_USER]), b)
        stab = _stability(before, after)
        out.append({
            "transition": "skill_topic_shift",
            "expect": "EXPECTED cache drop; base+integrations remain stable, "
                      "loaded-skills+contract+tools re-encode",
            "note": "not a failure — a topic shift legitimately busts the tool region",
            **stab,
        })

    # 5. new session: different conversation, identical tools/skills/runtime — the
    #    static system prefix + the whole tool region are still reused cross-session.
    before = _body_for(assembler, settings, req(
        [{"role": "user", "content": "Which worktrees are ready?"}],
        session_id="sess-a", turn_id="turn-a"), skills)
    after = _body_for(assembler, settings, req(
        [{"role": "user", "content": "Spin up an agent to fix the flaky test."}],
        session_id="sess-b", turn_id="turn-b"), skills)
    out.append({
        "transition": "new_session",
        "expect": "system_tools_stable == 1.0 (tool inventory is session-stable)",
        "floor_system_tools_stable": 1.0 if inv_present else 0.0,
        **_stability(before, after),
    })
    return out


def _layer_budget(
    assembler: PromptAssembler, loader: PromptLoader, settings: Settings, skills: list[Skill]
) -> dict[str, Any]:
    """Per-layer byte/token budget of one representative full request."""
    request = build_respond_request(messages=[_USER, _ASSIST_TOOL, _TOOL_RESULT])
    tools = load_tool_inventory()

    base = loader.base_prompt()
    integrations = assembler.render_integrations(request.runtime)
    skills_block = assembler.render_loaded_skills(skills)
    contract = assembler.render_response_contract(request.runtime)
    startup_block = assembler.render_startup_context(request.startup)
    runtime_block = assembler.render_runtime_context(request.runtime)
    footer = assembler.render_turn_footer(request.turn)
    # Conversation bytes from the assembled wire body. EXCLUDE both server-injected
    # user-role blocks: stable startup is counted separately, as is the volatile
    # runtime/footer tail, keeping the layer budget disjoint.
    body = _body_for(assembler, settings, request, skills)
    conv_msgs = [m for m in body["messages"] if m.get("role") != "system"]
    if conv_msgs and str(conv_msgs[0].get("content", "")).startswith(
        "[Injected startup context"
    ):
        conv_msgs = conv_msgs[1:]
    if conv_msgs and str(conv_msgs[-1].get("content", "")).startswith("[Injected context"):
        conv_msgs = conv_msgs[:-1]
    convo = "".join(dumps(m).decode("utf-8") for m in conv_msgs)
    tools_str = dumps(tools).decode("utf-8")

    layers = layer_segments([
        ("base_prompt", base),
        ("integrations", integrations),
        ("loaded_skills", skills_block),
        ("response_contract", contract),
        ("tool_schemas", tools_str),
        ("startup_context (stable user block)", startup_block),
        ("conversation", convo),
        ("runtime_context (tail)", runtime_block),
        ("turn_footer (tail)", footer),
    ])
    total_bytes = sum(x["bytes"] for x in layers)
    total_tok = sum(x["token_est"] for x in layers)
    for x in layers:
        x["byte_pct"] = round(100 * x["bytes"] / total_bytes, 1) if total_bytes else 0.0
    return {
        "layers": layers,
        "total_bytes": total_bytes,
        "total_token_est": total_tok,
        "token_est_note": "chars/4 PROXY (under-counts JSON tool schemas); trend-only. "
        "Layers are disjoint: the injected runtime/footer tail is counted only once.",
    }


def _selector_prompt_cost(loader: PromptLoader) -> dict[str, Any]:
    catalog = load_catalog()
    rendered = loader.render(
        TEMPLATE_SELECTOR_SYSTEM,
        catalog=catalog.selector_metadata(),
        active_ids=[],
        max_active=3,
    )
    per_skill = [len(dumps(s.selector_view()).decode("utf-8")) for s in catalog.skills]
    return {
        "catalog_skills": len(catalog.skills),
        "selector_prompt_bytes": len(rendered.encode("utf-8")),
        "selector_prompt_token_est": estimate_tokens(rendered),
        "mean_selector_view_bytes_per_skill": round(sum(per_skill) / len(per_skill), 1)
        if per_skill else 0.0,
        "note": "token_est is a bytes/4 proxy; live selector prompt_tokens live in "
                "the selector suite",
    }


def _state_token_size(settings: Settings) -> dict[str, Any]:
    codec = StateCodec(
        current_key=b"benchmark-key-not-a-secret",
        current_kid="bench",
        salt=b"bench-salt",
        ttl_seconds=settings.state_token_ttl_seconds,
    )
    sizes = []
    for n in (0, 1, 3):
        token = codec.issue(
            session_id="s",
            active_skills=[f"daintree.skill.{i}" for i in range(n)],
            catalog_revision="rev-abc123",
            turn_id="turn-1",
            instruction_revision=0,
        )
        sizes.append({"active_skills": n, "token_bytes": len(token.encode("utf-8"))})
    return {"sizes": sizes, "cap_bytes": 16 * 1024}


def run_deterministic(settings: Settings | None = None) -> dict[str, Any]:
    from datetime import UTC, datetime

    settings = settings or __import__(
        "benchmarks._harness.env", fromlist=["build_settings"]
    ).build_settings()
    loader = PromptLoader()
    assembler = PromptAssembler(loader, settings)
    catalog = load_catalog()
    skills = list(catalog.skills[:2])

    transitions = _transitions(assembler, settings, skills)
    # A byte-proxy self-check: our compact encoder must equal httpx's wire bytes,
    # or the whole cache metric is measured over a body production never sends.
    probe = _body_for(assembler, settings,
                      build_respond_request(messages=[_USER]), skills)
    wire_matches = dumps(probe) == wire_bytes(probe)

    continuity = [
        t for t in transitions
        if t["transition"]
        in {
            "tool_continuation",
            "runtime_context_change",
            "startup_context_change",
            "new_session",
        }
    ]
    min_stable = min((t["system_tools_stable"] for t in continuity), default=0.0)
    # A continuity transition MUST keep the [system+tools] region byte-identical
    # (floor 1.0). If it drops, the cache-critical layout regressed — fail hard so
    # the dashboard / CI reddens rather than storing a broken number as "ok".
    floors_met = all(
        t["system_tools_stable"] >= t.get("floor_system_tools_stable", 1.0) for t in continuity
    )
    summary = {
        "wire_encoder_matches_httpx": wire_matches,
        "min_system_tools_stable_continuity": min_stable,
        "topic_shift_system_tools_stable": next(
            (t["system_tools_stable"] for t in transitions
             if t["transition"] == "skill_topic_shift"), None),
        "passed": bool(wire_matches and floors_met),
    }
    return {
        "meta": {
            "benchmark": "prompt_cost",
            "mode": "deterministic",
            "model": OPENROUTER_MODEL,
            "catalog_revision": catalog.revision,
            "tool_inventory_count": len(load_tool_inventory()),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "summary": summary,
        "transitions": transitions,
        "layer_budget": _layer_budget(assembler, loader, settings, skills),
        "selector_prompt_cost": _selector_prompt_cost(loader),
        "state_token_size": _state_token_size(settings),
    }
