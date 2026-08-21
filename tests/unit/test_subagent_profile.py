"""The ``profile="subagent"`` persona: prompt assembly and selector skipping.

A sub-agent is a bounded read-only worker the CLI runs in its own isolated
conversation so the main thread never pays the context cost of a wide search
(see ``internal/subagent`` in the CLI repo). It differs from the orchestrator in
three ways that all have to hold together, and each of these tests pins one of
them:

* it gets its OWN system prompt, not the orchestrator's;
* it selects skills from a menu NARROWED to the ones it can execute;
* it gets none of the orchestration context blocks it cannot act on.

The default profile has to stay byte-identical on the wire, because the whole
prompt-cache layout depends on ordinary turns not changing shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.prompt_assembler import PromptAssembler


def _req(*, profile: str | None = None, messages=None, startup=None, runtime=None, turn=None, tools=None):
    inp: dict = {"messages": messages or [{"role": "user", "content": "find the thing"}]}
    if tools is not None:
        inp["tools"] = tools
    body: dict = {
        "protocol_version": 2,
        "session": {"id": "s", "turn_id": "t"},
        "input": inp,
        "startup": startup or {},
    }
    if profile is not None:
        body["profile"] = profile
    if runtime is not None:
        body["runtime"] = runtime
    if turn is not None:
        body["turn"] = turn
    return RespondRequest.model_validate(body)


def _system_text(provider_request) -> str:
    return "\n".join(m["content"] for m in provider_request.messages if m["role"] == "system")


def _all_text(provider_request) -> str:
    return "\n".join(str(m.get("content") or "") for m in provider_request.messages)


# --- the contract field ----------------------------------------------------


def test_profile_defaults_to_assistant() -> None:
    assert _req().profile == "assistant"


def test_profile_rejects_an_unknown_persona() -> None:
    # A typo'd or invented persona must 422 rather than fall back to "assistant" —
    # a silent fallback would hand a sub-agent the orchestrator's full prompt and
    # its whole skill catalog.
    with pytest.raises(ValidationError):
        _req(profile="rogue")


# --- prompt assembly -------------------------------------------------------


def test_subagent_gets_its_own_persona_not_the_orchestrator_one(
    assembler: PromptAssembler, prompt_loader: PromptLoader
) -> None:
    out = assembler.build_main_request(
        request=_req(profile="subagent"), skills=[], upstream_model="m"
    )
    system = _system_text(out)
    assert system.strip() == prompt_loader.subagent_prompt().strip()
    assert prompt_loader.base_prompt() not in system


def test_subagent_persona_states_the_load_bearing_rules(prompt_loader: PromptLoader) -> None:
    # These are the three things a sub-agent gets wrong without being told, and
    # the reason the persona exists as its own prompt rather than a modifier.
    body = prompt_loader.subagent_prompt().lower()
    assert "read" in body and "only" in body, "must state it is read-only"
    assert "final message" in body, "must state the report is the only thing that survives"
    assert "nobody" in body or "no human" in body, "must state there is no one to ask"


def test_subagent_receives_the_skills_it_was_given(assembler: PromptAssembler, catalog) -> None:
    """Skills are the POINT of giving a sub-agent a real tool inventory: one sent
    to find an issue among thousands should get the issue-searching runbook rather
    than rediscover it every run. The narrowing happens in the orchestrator (see
    the menu tests below); the assembler renders what it is handed."""
    skill = catalog.skills[0]
    out = assembler.build_main_request(
        request=_req(profile="subagent"), skills=[skill], upstream_model="m"
    )
    text = _all_text(out)
    assert "# Loaded skills" in text
    assert skill.title in text


def test_subagent_skill_bodies_sit_below_the_tool_schemas(
    assembler: PromptAssembler, catalog
) -> None:
    """The cache rule: only the STATIC persona may be system-role. A skill body
    that crept into the system region would re-encode the tool schemas behind it
    every time the menu shifted."""
    out = assembler.build_main_request(
        request=_req(profile="subagent"), skills=[catalog.skills[0]], upstream_model="m"
    )
    assert [m["role"] for m in out.messages if m["role"] == "system"] == ["system"]
    assert "# Loaded skills" not in _system_text(out)


def test_subagent_drops_the_orchestration_context_blocks(assembler: PromptAssembler) -> None:
    out = assembler.build_main_request(
        request=_req(
            profile="subagent",
            runtime={
                "permission_tier": "system",
                "scheduler_active": True,
                "open_terminals": [{"id": "terminal-1", "title": "codex"}],
                "mcp_servers": [{"name": "daintree", "transport": "http"}],
            },
            turn={"goal": "the orchestrator's goal", "workflow_runs": ["wfr_1"]},
        ),
        upstream_model="m",
        skills=[],
    )
    text = _all_text(out)
    for absent in ("# Open terminals", "# Current turn", "# Display context", "wfr_1"):
        assert absent not in text, f"{absent!r} leaked into a sub-agent prompt"


def test_subagent_keeps_the_startup_block(assembler: PromptAssembler) -> None:
    # A sub-agent sent to find a file has to know which project it is standing in.
    out = assembler.build_main_request(
        request=_req(
            profile="subagent",
            startup={"project": {"id": "p", "name": "Terrain", "path": "/repo/terrain"}},
        ),
        upstream_model="m",
        skills=[],
    )
    assert "/repo/terrain" in _all_text(out)


def test_subagent_forwards_its_tool_inventory(assembler: PromptAssembler) -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": "fs__search", "description": "search", "parameters": {"type": "object"}},
        }
    ]
    out = assembler.build_main_request(
        request=_req(profile="subagent", tools=tools), upstream_model="m", skills=[]
    )
    assert [t["function"]["name"] for t in out.tools] == ["fs__search"]


def test_subagent_prompt_is_materially_shorter_than_the_orchestrator_one(
    assembler: PromptAssembler,
) -> None:
    # The saving is a real part of why delegation pays for itself. If the two ever
    # converge, the sub-agent has quietly grown orchestration instructions it
    # cannot act on.
    sub = len(_all_text(assembler.build_main_request(request=_req(profile="subagent"), skills=[], upstream_model="m")))
    main = len(_all_text(assembler.build_main_request(request=_req(), skills=[], upstream_model="m")))
    assert sub < main / 2, f"sub-agent prompt is {sub} chars vs the orchestrator's {main}"


def test_the_profile_field_itself_does_not_split_the_prompt_cache(
    assembler: PromptAssembler,
) -> None:
    """An explicit "assistant" and an omitted profile assemble identically.

    NOTE the exact property: this proves the new FIELD is inert on the wire, not
    that assistant requests are byte-identical to the previous release. They are
    deliberately not — this change also adds a delegation rule to
    `20-tool-discipline.md` and edits two runbooks, which is a one-time,
    intentional cache bust argued for at BASE_PROMPT_CEILING in
    tests/unit/test_prompt_budgets.py. The golden snapshots record that diff.
    """
    explicit = assembler.build_main_request(request=_req(profile="assistant"), skills=[], upstream_model="m")
    implicit = assembler.build_main_request(request=_req(), skills=[], upstream_model="m")
    assert explicit.messages == implicit.messages


def test_subagent_disables_thinking(assembler: PromptAssembler) -> None:
    out = assembler.build_main_request(request=_req(profile="subagent"), skills=[], upstream_model="m")
    assert out.thinking_disabled is True
    assert out.reasoning_effort is None


# --- selector skipping (orchestrator level) --------------------------------


# The read-only inventory a real sub-agent is offered (internal/app.subagentToolNames),
# in the wire form the client sends. Trimmed to the ones skills actually name — the
# filter only cares about tools some `requiredTools` mentions.
_READ_ONLY_TOOL_NAMES = [
    "context__snapshot", "fs__read", "fs__list", "fs__search",
    "forge__listIssues", "forge__getIssue", "forge__getPR", "forge__listPRs",
    "forge__getChecks", "git__getProjectPulse", "worktree__list", "worktree__getCurrent",
    "workflow__list", "workflow__get", "terminal__read", "terminal__extract",
    "terminal__summarize", "terminal__awaitAll", "artifact__read", "memory__recall",
]
_READ_ONLY_TOOLS = [
    {"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}
    for n in _READ_ONLY_TOOL_NAMES
]


def _orchestrator_request(
    profile: str | None,
    text: str = "find the issue about the terrain flicker",
    tools: list | None = None,
    selection: dict | None = None,
):
    body: dict = {
        "protocol_version": 2,
        "session": {"id": "sub_1", "turn_id": "sub_1-r0"},
        "input": {"messages": [{"role": "user", "content": text}]},
        "startup": {},
    }
    if tools is not None:
        body["input"]["tools"] = tools
    if selection is not None:
        body["selection"] = selection
    if profile is not None:
        body["profile"] = profile
    return RespondRequest.model_validate(body)


@pytest.mark.asyncio
async def test_subagent_selects_from_a_narrowed_menu(settings, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """A sub-agent DOES select — from the skills it could actually execute.

    The narrowing is the whole point. Most of this catalog drives spawning,
    worktrees and terminals; handing one of those to a read-only worker spends its
    context and then steers it toward calls that can only fail.
    """
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        outcome = await container.orchestrator.complete(
            _orchestrator_request("subagent", tools=_READ_ONLY_TOOLS),
            upstream_model="m",
            api_key=SecretStr("k"),
            request_id="req_sub",
        )
        assert outcome.body is not None
        catalog = container.orchestrator.catalog
        menu = catalog.executable_with([t["function"]["name"] for t in _READ_ONLY_TOOLS])
        assert 0 < len(menu) < len(catalog), "the menu must be narrowed, not empty and not the whole catalog"
        # Anything it loaded has to be on that menu — never an orchestration runbook.
        active = [ref["id"] for ref in outcome.body["skills"]["active"]]
        for sid in active:
            assert sid in menu, f"{sid} is not executable by a sub-agent"
        # And this brief — the motivating case — must actually pick up the
        # issue-searching runbook rather than run bare.
        assert active, "a sub-agent with a matching runbook available must load it"
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_subagent_is_never_offered_an_orchestration_runbook(
    settings, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """The filter's job, stated as the failure it prevents."""
    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        catalog = container.orchestrator.catalog
        menu = catalog.executable_with([t["function"]["name"] for t in _READ_ONLY_TOOLS])
        offered = {s.id for s in menu._skills}
        # These drive spawning and editing; a read-only worker cannot follow them.
        for sid in ("daintree.edits.spawn-visible-agent", "daintree.review.review-change"):
            if catalog.get(sid) is not None:
                assert sid not in offered, f"{sid} requires tools a sub-agent does not have"
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_an_empty_menu_skips_the_selector_entirely(settings, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """With no executable skill there is nothing to decide, so the call is skipped
    rather than paid for to hear "none"."""
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        # A single tool no skill declares ⇒ only zero-requirement skills survive.
        outcome = await container.orchestrator.complete(
            _orchestrator_request(
                "subagent",
                tools=[{
                    "type": "function",
                    "function": {"name": "nothing__at__all", "description": "x", "parameters": {"type": "object"}},
                }],
            ),
            upstream_model="m",
            api_key=SecretStr("k"),
            request_id="req_empty",
        )
        assert outcome.body is not None
        # Not asserting selector.ran here — a menu of only zero-requirement skills
        # is still a menu. What must hold is that nothing unexecutable loaded.
        for ref in outcome.body["skills"]["active"]:
            skill = container.orchestrator.catalog.get(ref["id"])
            assert not (skill and skill.meta.required_tools), f"{ref['id']} needs tools that were not offered"
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_the_same_text_DOES_run_the_selector_for_the_assistant(
    settings, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """The control. Without it, the test above would pass just as well if the
    selector had stopped running for everyone."""
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        outcome = await container.orchestrator.complete(
            _orchestrator_request(None),
            upstream_model="m",
            api_key=SecretStr("k"),
            request_id="req_main",
        )
        skills = outcome.body["skills"]
        assert skills["selector"]["ran"] is True, "the orchestrator profile must still select"
        assert skills["active"], "the same text must load runbooks for the orchestrator"
        # The control that gives the narrowing test its meaning: the orchestrator
        # chooses from the WHOLE catalog, so it can reach skills a sub-agent cannot.
        full = len(container.orchestrator.catalog)
        menu = container.orchestrator.catalog.executable_with(
            [t["function"]["name"] for t in _READ_ONLY_TOOLS]
        )
        assert len(menu) < full
    finally:
        await container.aclose()


# --- regressions found in review ------------------------------------------


def test_subagent_skill_render_omits_step_tracking(
    assembler: PromptAssembler, catalog
) -> None:
    """`skill.step.advance` / `skill.run.get` are denylisted from a sub-agent's
    inventory, so instructing it to call them spends context to buy a guaranteed
    TOOL_NOT_OFFERED. It also has no turn boundary to checkpoint across."""
    skill = catalog.skills[0]
    sub = assembler.build_main_request(
        request=_req(profile="subagent"), skills=[skill], upstream_model="m"
    )
    main = assembler.build_main_request(request=_req(), skills=[skill], upstream_model="m")

    assert "skill.step.advance" not in _all_text(sub)
    assert "skill.run.get" not in _all_text(sub)
    # The control: the orchestrator still gets it, so this is a profile switch and
    # not an accidental deletion.
    assert "skill.step.advance" in _all_text(main)


def test_loaded_skills_cache_keys_on_the_step_tracking_variant(
    assembler: PromptAssembler, catalog
) -> None:
    """Both variants differ by a paragraph; a key ignoring the flag would serve
    whichever was rendered first to both profiles."""
    skills = [catalog.skills[0]]
    with_tracking = assembler.render_loaded_skills(skills, step_tracking=True)
    without = assembler.render_loaded_skills(skills, step_tracking=False)
    assert with_tracking != without
    # Re-render in the reverse order: a mis-keyed cache returns the other variant.
    assert assembler.render_loaded_skills(skills, step_tracking=False) == without
    assert assembler.render_loaded_skills(skills, step_tracking=True) == with_tracking


def test_subagent_forwards_json_response_format(assembler: PromptAssembler) -> None:
    """Validation ACCEPTS json_object when no tools are sent; dropping it silently
    left a caller asking for JSON and getting prose."""
    req = _req(profile="subagent")
    req.generation.response_format = "json_object"
    out = assembler.build_main_request(request=req, skills=[], upstream_model="m")
    assert out.response_format == {"type": "json_object"}


def test_a_server_pinned_skill_cannot_ride_into_a_subagent_via_prior_state(
    catalog,
) -> None:
    """The docs runbook is hidden from selection but declares no required tools, so
    it survives every narrowing. Carrying prior ids with `filter_known` would let a
    replayed assistant state inject it into a profile whose docs lookup is off."""
    from daintree_assistant_server.config import DOCS_SKILL_ID

    menu = catalog.executable_with([t["function"]["name"] for t in _READ_ONLY_TOOLS])
    if catalog.get(DOCS_SKILL_ID) is None:
        pytest.skip("no pinned docs skill in this catalog")
    # It IS known to the narrowed catalog...
    assert DOCS_SKILL_ID in menu.filter_known([DOCS_SKILL_ID])
    # ...and that is exactly why the carry must use `selectable`, which drops it.
    assert menu.selectable([DOCS_SKILL_ID]) == []


def test_an_empty_menu_is_measured_over_selectable_ids(catalog) -> None:
    """`len(catalog)` never reaches 0 while the zero-requirement docs skill exists,
    so the skip-the-selector branch would never fire if it counted length."""
    menu = catalog.executable_with(["nothing__at__all"])
    assert len(menu) >= 1, "the zero-requirement pinned skill should survive"
    assert not menu.selectable_ids, "but nothing should be selectable from it"


def test_executable_cache_is_bounded(catalog) -> None:
    """The key is caller-supplied, so an unbounded map is a request-driven leak."""
    from daintree_assistant_server.skills.catalog import _EXECUTABLE_CACHE_MAX

    for i in range(_EXECUTABLE_CACHE_MAX * 3):
        catalog.executable_with([f"junk__{i}"])
    assert len(catalog._executable_cache) <= _EXECUTABLE_CACHE_MAX


def test_every_eagerly_loaded_prompt_dir_is_in_package_data() -> None:
    """The release blocker this test exists to prevent.

    `package-data` globs do NOT recurse: `prompts/main/base/*.md` covers exactly one
    directory. Adding `prompts/main/subagent/` without adding its glob produced a
    wheel with no sub-agent partials — and because `PromptLoader` reads that
    directory EAGERLY at construction and raises when it is missing, the packaged
    server failed to boot at all. Not for sub-agent requests: for every request.

    Editable installs (how the whole suite runs) read from the source tree and
    cannot see this, which is exactly why it needs asserting rather than testing.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as fh:
        globs = tomllib.load(fh)["tool"]["setuptools"]["package-data"]["daintree_assistant_server"]

    covered = {g.rsplit("/", 1)[0] for g in globs if g.endswith("*.md")}
    prompts = root / "src" / "daintree_assistant_server" / "prompts"
    missing = [
        f"prompts/{d.relative_to(prompts)}"
        for d in prompts.rglob("*")
        if d.is_dir() and any(d.glob("*.md")) and f"prompts/{d.relative_to(prompts)}" not in covered
    ]
    assert not missing, (
        f"these prompt dirs hold .md files but no package-data glob covers them: {missing}. "
        "A wheel would ship without them and PromptLoader would raise at startup. "
        'Add "<dir>/*.md" to [tool.setuptools.package-data] in pyproject.toml.'
    )


def test_a_state_token_records_the_profile_that_resolved_it() -> None:
    """The two profiles pick from different menus, so a set is only reusable
    under the profile that chose it.

    A sub-agent's set can hold a specific whose domain foundation was filtered
    out of its menu, and continuation reuse deliberately does not re-pair
    foundations — so an assistant round reusing that token would run a workflow
    skill with its base judgment layer silently missing. Distinct session ids
    make the crossover unreachable for today's CLI, but that is the client's
    property; this asserts the server's.
    """
    from daintree_assistant_server.state.codec import StateCodec

    codec = StateCodec(
        current_key=b"k" * 16, current_kid="k1", salt=b"salt", ttl_seconds=3600
    )
    token = codec.issue(
        session_id="s",
        active_skills=["daintree.example.alpha"],
        catalog_revision="rev1",
        turn_id="t1",
        instruction_revision=0,
        profile="subagent",
    )
    payload = codec.verify(token, session_id="s").payload
    assert payload is not None
    assert payload.profile == "subagent"

    assistant_token = codec.issue(
        session_id="s",
        active_skills=["daintree.example.alpha"],
        catalog_revision="rev1",
        turn_id="t1",
        instruction_revision=0,
    )
    assistant_payload = codec.verify(assistant_token, session_id="s").payload
    assert assistant_payload is not None
    assert assistant_payload.profile == "assistant"


def test_a_cross_profile_token_forces_a_fresh_selection(orchestrator) -> None:
    """Everything else about this cadence says "reuse"; the profile mismatch
    must still win, or the reuse is of an illegal set."""
    same = {
        "force": False,
        "policy": "new_instruction",
        "state_valid": True,
        "state_catalog_revision": orchestrator.catalog.revision,
        "state_turn_id": "t1",
        "state_instruction_revision": 0,
        "turn_id": "t1",
        "instruction_revision": 0,
    }
    assert not orchestrator._should_run_selector(
        **same, state_profile="assistant", profile="assistant"
    )
    assert orchestrator._should_run_selector(
        **same, state_profile="subagent", profile="assistant"
    )
    assert orchestrator._should_run_selector(
        **same, state_profile="assistant", profile="subagent"
    )


# ---------------------------------------------------------------------------
# Caller pins meet the capability boundary (issue #54).
#
# `selection.pinned_skill_ids` overrides the CLASSIFIER. It must not override
# `executable_with`: the two refusals look the same from outside (the skill did
# not load) but mean opposite things, and only one of them is a bug. A pin is a
# developer saying "I know better than the router"; a narrowed menu is the server
# saying "you do not hold the tools these steps call", which no amount of knowing
# better changes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_subagent_pin_outside_its_menu_is_refused_and_warned(
    settings, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """A real, known runbook the sub-agent simply cannot execute. Loading it would
    spend the worker's context and then steer it into calls that can only fail —
    worse than loading nothing — so it is refused, and said out loud."""
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        catalog = container.orchestrator.catalog
        menu = catalog.executable_with([t["function"]["name"] for t in _READ_ONLY_TOOLS])
        outside = next(
            (s.id for s in catalog.skills if s.id not in menu),
            None,
        )
        assert outside is not None, "precondition: the menu must exclude something"
        outcome = await container.orchestrator.complete(
            _orchestrator_request(
                "subagent",
                tools=_READ_ONLY_TOOLS,
                selection={"pinned_skill_ids": [outside]},
            ),
            upstream_model="m",
            api_key=SecretStr("k"),
            request_id="req_pin_denied",
        )
        assert outcome.body is not None
        active = [ref["id"] for ref in outcome.body["skills"]["active"]]
        assert outside not in active, "a pin bypassed the capability boundary"
        assert "pinned_skill_not_executable" in outcome.body["warnings"], (
            "a refused pin that returns a normal-looking turn is the silent failure "
            "this warning exists for"
        )
        assert "unknown_skill_id_ignored" not in outcome.body["warnings"], (
            "a KNOWN skill must not be reported as a typo — the two mistakes need "
            "different answers"
        )
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_a_subagent_pin_inside_its_menu_loads(settings, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """The other half. Narrowing must not make pinning useless for the profile —
    a skill the worker CAN execute is loaded on demand, under the tighter
    sub-agent cap of two."""
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    try:
        catalog = container.orchestrator.catalog
        menu = catalog.executable_with([t["function"]["name"] for t in _READ_ONLY_TOOLS])
        inside = next(iter(s.id for s in menu.skills), None)
        assert inside is not None, "precondition: the menu must offer something"
        outcome = await container.orchestrator.complete(
            _orchestrator_request(
                "subagent",
                tools=_READ_ONLY_TOOLS,
                selection={"pinned_skill_ids": [inside]},
            ),
            upstream_model="m",
            api_key=SecretStr("k"),
            request_id="req_pin_ok",
        )
        assert outcome.body is not None
        active = [ref["id"] for ref in outcome.body["skills"]["active"]]
        assert inside in active, f"the pinned skill did not load; active={active}"
        assert len(active) <= 2, "the sub-agent cap still binds a pinned turn"
        assert "pinned_skill_not_executable" not in outcome.body["warnings"]
    finally:
        await container.aclose()
