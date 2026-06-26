"""Prompt assembler tests."""

from __future__ import annotations

from daintree_assistant_server.contracts.extensions import (
    McpInfo,
    Memories,
    RuntimeContext,
    TurnContext,
)
from daintree_assistant_server.contracts.openai import ChatCompletionRequest
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.load_events import SkillLoadExecution


def _req(**kwargs: object) -> ChatCompletionRequest:
    base: dict[str, object] = {"model": "daintree-assistant", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)
    return ChatCompletionRequest.model_validate(base)


def test_message_layer_order(assembler: PromptAssembler, catalog) -> None:
    skill = catalog.skills[0]
    ex = SkillLoadExecution.from_skill("call_srv_1", skill)
    req = _req(
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "clean", "tool_call_id": "c1"},
        ]
    )
    pr = assembler.build_main_request(
        public_request=req,
        runtime=RuntimeContext(permission_tier="operator"),
        turn=TurnContext(goal="g"),
        skills=[skill],
        synthetic_executions=[ex],
        upstream_model="deepseek-v4-flash",
    )
    roles = [m["role"] for m in pr.messages]
    # base, runtime, loaded_skills, user, assistant, tool, synthetic assistant, synthetic tool, footer
    assert roles[:3] == ["system", "system", "system"]
    assert roles[-1] == "system"  # footer
    # synthetic skill load present
    assert any(
        m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "skill__load"
        for m in pr.messages
    )


def test_no_skills_fallback(assembler: PromptAssembler) -> None:
    rendered = assembler.render_loaded_skills([])
    assert "No task-specific skills" in rendered


def test_loaded_skills_includes_body(assembler: PromptAssembler, catalog) -> None:
    skill = catalog.skills[0]
    rendered = assembler.render_loaded_skills([skill])
    assert skill.title in rendered
    assert skill.id in rendered


def test_runtime_context_strips_control_chars(assembler: PromptAssembler) -> None:
    rc = RuntimeContext(permission_tier="operator", project_instructions="safe\x07text")
    rendered = assembler.render_runtime_context(rc)
    assert "\x07" not in rendered
    assert "safe" in rendered


def test_runtime_context_mcp_status(assembler: PromptAssembler) -> None:
    rc = RuntimeContext(mcp=McpInfo(connected=True, transport="stdio", tool_count=42))
    rendered = assembler.render_runtime_context(rc)
    assert "42 tools" in rendered


def test_footer_renders_memories_and_worktree(assembler: PromptAssembler) -> None:
    turn = TurnContext(goal="ship it", memories=Memories(pinned=["p1"], relevant=["r1"]))
    rendered = assembler.render_turn_footer(turn, active_worktree="feature/x")
    assert "ship it" in rendered
    assert "p1" in rendered
    assert "feature/x" in rendered


def test_tool_projection(assembler: PromptAssembler) -> None:
    req = _req(tools=[{"type": "function", "function": {"name": "git__status", "description": "d"}}])
    pr = assembler.build_main_request(
        public_request=req, runtime=None, turn=None, skills=[], synthetic_executions=[],
        upstream_model="deepseek-v4-flash",
    )
    assert pr.tools is not None
    assert pr.tools[0]["function"]["name"] == "git__status"
    assert pr.tools[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_main_request_params(assembler: PromptAssembler) -> None:
    req = _req(stream=True, stream_options={"include_usage": True}, temperature=0.5)
    pr = assembler.build_main_request(
        public_request=req, runtime=None, turn=None, skills=[], synthetic_executions=[],
        upstream_model="deepseek-v4-flash",
    )
    assert pr.thinking_disabled is True
    assert pr.prompt_cache_key == "daintree-main"
    assert pr.stream is True
    assert pr.include_usage is True
    assert pr.temperature == 0.5
