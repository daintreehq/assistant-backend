"""Live cross-check for the deterministic flagship — proves the byte-proxy converts
to real token-level cache reuse, and measures the latencies the user actually feels.

Makes REAL, paid DeepSeek calls. Three things the deterministic suite cannot see:

- **Warm-turn cache reuse.** DeepSeek's prefix cache is a SHARED, server-side,
  TTL-bounded cache, so a first call is already partly warm (the base+tool region
  is byte-identical across every run) — there is NO clean "cold baseline". The
  honest, measurable signal is: send an IDENTICAL request twice back-to-back and
  read ``cached_tokens/prompt_tokens`` on the SECOND (warm) call. High reuse there
  is the proof the stable-prefix layout pays off.
- **json_object cache behavior.** The selector runs ``response_format=json_object``,
  which suppresses ``prompt_cache_key`` — recorded here as an observation (DeepSeek
  may still auto-cache), not asserted.
- **First-byte latency + throughput.** Streaming TTFB (``prefetch_first_event``) and
  the COMPOSED first byte through the real orchestrator (selector → assembly →
  main stream) — the single number a selector-firing turn actually costs.

Non-determinism: DeepSeek is not deterministic even at temperature 0, and the
shared cache is load-dependent, so these numbers carry run-to-run variance; treat
them as a tracked baseline with trials, not an exact constant.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.modes import STANDARD_MODEL

from .._harness.env import caller_api_key, make_upstream_provider
from .._harness.fixtures import build_respond_request
from .._harness.stats import latency_summary


async def _prompt_request(
    settings: Settings,
    *,
    messages: list[dict[str, Any]],
    stream: bool,
    json_mode: bool = False,
    skills: list[Any] | None = None,
) -> Any:
    """Assemble a real provider request, then clamp completion to bound cost — the
    prompt (what we measure) is untouched.

    ``skills`` selects the active set, which is what the affinity A/B varies: the
    skill block is the first non-system message, so shifting it is what breaks the
    reusable prefix. Passing it through the assembler rather than editing the
    assembled array afterwards keeps the row identification the assembler's job —
    surgery on "the first non-system message" would silently target the
    integrations or startup row if the set were ever empty.
    """
    from daintree_assistant_server.prompts.loader import PromptLoader
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler
    from daintree_assistant_server.skills.catalog import load_catalog

    assembler = PromptAssembler(PromptLoader(), settings)
    if skills is None:
        skills = list(load_catalog().skills[:2])
    if not skills:
        raise ValueError("an empty skill set would move the first non-system row")
    req = build_respond_request(messages=messages, with_tools=not json_mode, stream=stream)
    if json_mode:
        gen = req.generation.model_copy(update={"response_format": "json_object"})
        req = req.model_copy(update={"generation": gen})
    pr = assembler.build_main_request(request=req, skills=skills, upstream_model=STANDARD_MODEL)
    pr.max_tokens = 8  # bound completion cost; prompt tokens are the measurement
    pr.stream = stream
    pr.include_usage = stream
    return pr


def _cache_ratio(usage: Any) -> dict[str, Any]:
    pt = usage.prompt_tokens or 0
    ct = usage.cached_tokens or 0
    return {
        "prompt_tokens": pt,
        "cached_tokens": ct,
        "cache_ratio": round(ct / pt, 4) if pt else 0.0,
    }


async def _warm_pair(provider: Any, settings: Settings, label: str,
                     messages: list[dict[str, Any]], json_mode: bool = False) -> dict[str, Any]:
    pr = await _prompt_request(settings, messages=messages, stream=False, json_mode=json_mode)
    cold = await provider.complete(pr)
    warm = await provider.complete(pr)  # identical body → second call is warm
    return {
        "case": label,
        "json_mode": json_mode,
        "first_call": _cache_ratio(cold.usage),
        "warm_call": _cache_ratio(warm.usage),
    }


async def _ttfb(provider: Any, settings: Settings, trials: int) -> dict[str, Any]:
    latencies: list[float] = []
    for i in range(trials):
        pr = await _prompt_request(
            settings, messages=[{"role": "user", "content": f"Say ok ({i})."}], stream=True
        )
        t0 = time.perf_counter()
        stream = await provider.open_stream(pr)
        await stream.prefetch_first_event()
        latencies.append(time.perf_counter() - t0)
        async for _ in stream:
            pass
        await stream.aclose()
    return {"trials": trials, **latency_summary(latencies)}


async def _composed_first_byte(settings: Settings, trials: int) -> dict[str, Any]:
    """Real selector → assembly → main stream: the user-perceived first byte, with
    the selector cost attributed separately."""
    from daintree_assistant_server.main import build_container

    provider = make_upstream_provider(settings, timeout=settings.main_timeout_seconds)
    container = build_container(settings, provider=provider)
    first_byte: list[float] = []
    try:
        for i in range(trials):
            req: RespondRequest = build_respond_request(
                messages=[{"role": "user",
                           "content": f"Rename my terminals to match their agents ({i})."}],
                stream=True,
                session_id=f"composed-{i}",
                turn_id=f"turn-{i}",
            )
            t0 = time.perf_counter()
            outcome = await container.orchestrator.complete(
                req, api_key=caller_api_key(settings)
            )
            assert outcome.stream is not None
            async for _chunk in outcome.stream:
                first_byte.append(time.perf_counter() - t0)
                break
            # drain remainder so the upstream connection closes cleanly
            async for _ in outcome.stream:
                pass
        await container.orchestrator.drain_background_tasks(timeout=5.0)
    finally:
        # The container's own teardown, not just the provider's: it owns a second
        # connection pool (the documentation MCP client) that closing the
        # provider alone would leave open.
        await container.aclose()
    return {"trials": trials, "first_byte": latency_summary(first_byte)}


#: Skills per turn, and turns per arm. Six consecutive skills per arm, so two arms
#: can take disjoint slices of the catalog (see `_affinity_ab`).
_AFFINITY_SKILLS_PER_TURN = 2
_AFFINITY_TURNS = 3
_AFFINITY_ARM_SPAN = _AFFINITY_SKILLS_PER_TURN * _AFFINITY_TURNS


async def _affinity_sequence(
    provider: Any, settings: Settings, *, cache_session_id: str | None, offset: int
) -> list[dict[str, Any]]:
    """One session's worth of turns, run with or without an affinity key.

    The sequence deliberately SHIFTS the active skill set between turns. That is
    the whole experiment: the skill block is the first non-system message, so a
    shift invalidates everything below it while the invariant head above it (base
    prompt, response contract, tool schemas) is still perfectly reusable. An arm
    that sent the same request twice would measure nothing.

    **What this A/B can still tell you has narrowed**, and it is worth saying so
    rather than reading the old number off a new provider. Against a ROUTER the
    key decided which of many endpoints answered, so the arms could differ by a
    whole cold prefill. Cerebras hosts one endpoint and caches prefixes
    automatically, org-scoped, with no sticky record to steer — so
    ``prompt_cache_key`` is a hint it may ignore and the honest expectation is
    that the two arms agree. A DIFFERENCE here is now the finding; parity is the
    null result, not a regression.

    ``cache_session_id=None`` is the NO-KEY baseline.

    ``offset`` picks this arm's slice of the catalog, and it is the ONLY lever that
    moves the default sticky key: that key hashes the first system message and the
    first non-system message, which here are the invariant base prompt and the
    skill block. Nothing further down — not the user message, not the tail — takes
    part in it. So a per-arm, per-run offset is what stops the no-key arm from
    inheriting the sticky record and warm prefix of the previous arm or the
    previous run, which would read as an affinity effect that is really run order.
    """
    from daintree_assistant_server.skills.catalog import load_catalog

    catalog = load_catalog()
    windows = [
        list(catalog.skills[i : i + _AFFINITY_SKILLS_PER_TURN])
        for i in range(offset, offset + _AFFINITY_ARM_SPAN, _AFFINITY_SKILLS_PER_TURN)
    ]
    calls: list[dict[str, Any]] = []
    for turn, skills in enumerate(windows):
        pr = await _prompt_request(
            settings,
            messages=[{"role": "user", "content": f"Prepare this branch for review ({turn})."}],
            stream=False,
            skills=skills,
        )
        pr.cache_session_id = cache_session_id
        result = await provider.complete(pr)
        calls.append({
            "turn": turn,
            "endpoint": result.endpoint,
            **_cache_ratio(result.usage),
        })
    return calls


def _summarize_affinity(calls: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints = [c["endpoint"] for c in calls]
    pairs = list(zip(endpoints, endpoints[1:], strict=False))
    # Only KNOWN→KNOWN transitions count. `endpoint` is None when the provider
    # reported none, and this repo treats that as "not reported", never as a host:
    # counting [A, None, A] as two switches would invent two cache misses that may
    # not have happened, and counting it as zero would claim knowledge we lack.
    known = [(a, b) for a, b in pairs if a is not None and b is not None]
    # Switches, not distinct count: A→B→A is two switches and two cold prefixes,
    # which a distinct count of 2 would understate.
    switches = sum(1 for a, b in known if a != b)
    # Turn 0 is excluded: it elects an endpoint rather than returning to one, so
    # neither arm can differ there and averaging it in only dilutes the signal.
    later = [c["cache_ratio"] for c in calls[1:]]
    return {
        "endpoints": endpoints,
        "endpoint_switches": switches if len(known) == len(pairs) else None,
        "comparable_transitions": len(known),
        "unknown_transitions": len(pairs) - len(known),
        "mean_cache_ratio_after_first": round(sum(later) / len(later), 4) if later else 0.0,
        "calls": calls,
    }


async def _affinity_ab(settings: Settings) -> dict[str, Any]:
    """The issue #42 A/B: does naming the sticky key keep a session on one endpoint?

    Both arms are one sequential session at concurrency one, well inside the
    upstream's prefix-cache TTL (Cerebras guarantees 5 minutes, often longer).

    The two arms take DISJOINT, run-randomized slices of the catalog. That is what
    makes the comparison WITHIN a run fair rather than ordered: the arms share no
    skill block, so neither can warm the other's prefix or leave behind a sticky
    record the other inherits. Their skill CONTENT differs as a result, which is
    noise — the metric is how much of each arm's own prefix survived its own topic
    shifts — where sharing the slices would have been directional bias in the
    second arm's favour.

    ACROSS runs it is weaker, and the limit is worth stating rather than papering
    over. Cerebras matches prefixes ORG-WIDE, so any traffic from this account —
    another bench, a dev server, a real turn — can have warmed the same base
    prompt and skill blocks already. The only freshness available is the offset,
    and the catalog affords a couple of dozen positions, so two runs can draw
    overlapping slices. A rerun is therefore not guaranteed cold. Space runs
    beyond the cache TTL if independence matters.

    What remains shared in every case is the invariant head above the skill block
    (base prompt, response contract, tool schemas). That is not a confound to
    remove: it is shared by every Daintree request in existence, so both arms meet
    it warm, which is exactly the condition the served fleet is in.
    """
    from daintree_assistant_server.skills.catalog import load_catalog

    run = uuid.uuid4().hex[:12]
    # Two disjoint arms have to fit inside the catalog. `positions` counts valid
    # STARTS, so it is the slack plus one: a catalog of exactly 2 * span fits, at
    # the single offset 0.
    positions = len(load_catalog().skills) - 2 * _AFFINITY_ARM_SPAN + 1
    if positions < 1:
        raise ValueError("catalog too small for two disjoint affinity arms")
    offset = int(run[:8], 16) % positions
    provider = make_upstream_provider(settings, timeout=settings.main_timeout_seconds)
    try:
        control = await _affinity_sequence(
            provider, settings, cache_session_id=None, offset=offset
        )
        treatment = await _affinity_sequence(
            provider,
            settings,
            cache_session_id=f"daintree-main-bench-{run}",
            offset=offset + _AFFINITY_ARM_SPAN,
        )
    finally:
        await provider.aclose()

    control_summary = _summarize_affinity(control)
    treatment_summary = _summarize_affinity(treatment)

    def delta(key: str) -> Any:
        lhs, rhs = treatment_summary[key], control_summary[key]
        return None if lhs is None or rhs is None else round(lhs - rhs, 4)

    return {
        "run": run,
        "control": control_summary,
        "treatment": treatment_summary,
        "delta": {
            "endpoint_switches": delta("endpoint_switches"),
            "mean_cache_ratio_after_first": delta("mean_cache_ratio_after_first"),
        },
        "note": "one sequence per arm — indicative, not significant. Re-run before "
                "drawing a conclusion; endpoint switch rate is diagnostic (an endpoint "
                "is many GPU workers, so the same name is not the same cache).",
    }


async def run_live(
    settings: Settings, *, trials: int = 3, affinity_ab: bool = False
) -> dict[str, Any]:
    from datetime import UTC, datetime

    from daintree_assistant_server.observability import configure_logging

    configure_logging("WARNING")  # keep the benchmark's stdout to the report only
    provider = make_upstream_provider(settings, timeout=settings.main_timeout_seconds)
    warm_cases: list[dict[str, Any]] = []
    try:
        warm_cases.append(await _warm_pair(
            provider, settings, "tool_region_warm",
            [{"role": "user", "content": "Prepare this branch for review."}]))
        warm_cases.append(await _warm_pair(
            provider, settings, "json_object_warm",
            [{"role": "user", "content": "Classify this."}], json_mode=True))
        ttfb = await _ttfb(provider, settings, trials)
    finally:
        await provider.aclose()

    composed = await _composed_first_byte(settings, trials)
    affinity = await _affinity_ab(settings) if affinity_ab else None

    warm_ratios = [c["warm_call"]["cache_ratio"] for c in warm_cases if not c["json_mode"]]
    doc: dict[str, Any] = {
        "meta": {
            "benchmark": "prompt_cost",
            "mode": "live",
            "model": STANDARD_MODEL,
            "trials": trials,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "summary": {
            "warm_tool_region_cache_ratio": max(warm_ratios) if warm_ratios else 0.0,
            "streaming_ttfb_p50_s": ttfb["p50_s"],
            "composed_first_byte_p50_s": composed["first_byte"]["p50_s"],
            "note": "shared server-side cache → first call is NOT cold; only the warm "
                    "(second identical) call is a clean signal",
        },
        "warm_cache": warm_cases,
        "streaming_ttfb": ttfb,
        "composed_first_byte": composed,
    }
    if affinity is not None:
        doc["affinity_ab"] = affinity
    return doc
