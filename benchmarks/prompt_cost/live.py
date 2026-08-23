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

from daintree_assistant_server.config import EXECUTOR_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest

from .._harness.env import caller_api_key, make_upstream_provider
from .._harness.fixtures import build_respond_request
from .._harness.stats import latency_summary


async def _prompt_request(
    settings: Settings,
    *,
    messages: list[dict[str, Any]],
    stream: bool,
    json_mode: bool = False,
    runbooks: list[Any] | None = None,
) -> Any:
    """Assemble a real provider request, then clamp completion to bound cost — the
    prompt (what we measure) is untouched.

    ``runbooks`` selects the active set, which is what the topic-shift sequence varies: the
    runbook block is the first non-system message, so shifting it is what breaks the
    reusable prefix. Passing it through the assembler rather than editing the
    assembled array afterwards keeps the row identification the assembler's job —
    surgery on "the first non-system message" would silently target the
    integrations or startup row if the set were ever empty.
    """
    from daintree_assistant_server.prompts.loader import PromptLoader
    from daintree_assistant_server.runbooks.catalog import load_catalog
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler

    assembler = PromptAssembler(PromptLoader(), settings)
    if runbooks is None:
        runbooks = list(load_catalog().runbooks[:2])
    if not runbooks:
        raise ValueError("an empty runbook set would move the first non-system row")
    req = build_respond_request(messages=messages, with_tools=not json_mode, stream=stream)
    if json_mode:
        gen = req.generation.model_copy(update={"response_format": "json_object"})
        req = req.model_copy(update={"generation": gen})
    pr = assembler.build_main_request(request=req, runbooks=runbooks, upstream_model=EXECUTOR_MODEL)
    pr.max_tokens = 8  # bound completion cost; prompt tokens are the measurement
    pr.stream = stream
    pr.include_usage = stream
    return pr


def _cache_ratio(usage: Any) -> dict[str, Any]:
    """Reuse for one call, with ``None`` wherever the host reported nothing.

    The `or 0` this used to do turned "the host did not report cached tokens"
    into a ratio of 0.0000 and printed it as a measurement — a cold cache that
    was never observed to be cold. That is the same fabricated zero the usage
    parser goes out of its way to avoid, and it is worse here: this benchmark
    exists to decide whether the prefix layout pays off, so an invented zero
    argues directly against the thing it is measuring.
    """
    pt = usage.prompt_tokens
    ct = usage.cached_tokens
    ratio = round(ct / pt, 4) if pt and ct is not None else None
    return {
        "prompt_tokens": pt,
        "cached_tokens": ct,
        "cache_ratio": ratio,
        "cached_tokens_source": usage.cached_tokens_source,
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


#: Runbooks per turn, and turns per sequence. Six consecutive runbooks, walked two at
#: a time, so each turn presents a topic shift to the one before it.
_SHIFT_RUNBOOKS_PER_TURN = 2
_SHIFT_TURNS = 3
_SHIFT_SPAN = _SHIFT_RUNBOOKS_PER_TURN * _SHIFT_TURNS


async def _topic_shift_sequence(
    provider: Any, settings: Settings, *, offset: int
) -> list[dict[str, Any]]:
    """One session's worth of turns, SHIFTING the active runbook set between them.

    That shift is the whole experiment. The runbook block is the first non-system
    message, so changing it invalidates everything below while the invariant head
    above it — base prompt, response contract, tool schemas — stays perfectly
    reusable. What the sequence measures is how much of a request survives a
    topic shift under the host's automatic prefix cache.

    ``offset`` picks this run's slice of the catalog, so consecutive runs do not
    simply re-read each other's warm prefixes.
    """
    from daintree_assistant_server.runbooks.catalog import load_catalog

    catalog = load_catalog()
    windows = [
        list(catalog.runbooks[i : i + _SHIFT_RUNBOOKS_PER_TURN])
        for i in range(offset, offset + _SHIFT_SPAN, _SHIFT_RUNBOOKS_PER_TURN)
    ]
    calls: list[dict[str, Any]] = []
    for turn, runbooks in enumerate(windows):
        pr = await _prompt_request(
            settings,
            messages=[{"role": "user", "content": f"Prepare this branch for review ({turn})."}],
            stream=False,
            runbooks=runbooks,
        )
        result = await provider.complete(pr)
        calls.append({
            "turn": turn,
            "endpoint": result.endpoint,
            **_cache_ratio(result.usage),
        })
    return calls


def _summarize_topic_shift(calls: list[dict[str, Any]]) -> dict[str, Any]:
    endpoints = [c["endpoint"] for c in calls]
    pairs = list(zip(endpoints, endpoints[1:], strict=False))
    # Only KNOWN→KNOWN transitions count. `endpoint` is None when the provider
    # reported none, and this repo treats that as "not reported", never as a host:
    # counting [A, None, A] as two switches would invent two cache misses that may
    # not have happened, and counting it as zero would claim knowledge we lack. A
    # single-host provider reports nothing, so expect this to be entirely unknown.
    known = [(a, b) for a, b in pairs if a is not None and b is not None]
    # Switches, not distinct count: A→B→A is two switches and two cold prefixes,
    # which a distinct count of 2 would understate.
    switches = sum(1 for a, b in known if a != b)
    # Turn 0 is excluded: it has no preceding turn to have shifted away from, so
    # its ratio measures whatever the org-wide cache already held, not reuse
    # across a shift.
    #
    # Unreported calls are excluded from the mean rather than averaged in as
    # zero, and `measured_calls` says how many went into it — an average over
    # one observation and an average over three are different claims, and the
    # number alone cannot tell them apart. `None` when nothing was measured.
    later = [c["cache_ratio"] for c in calls[1:] if c["cache_ratio"] is not None]
    return {
        "endpoints": endpoints,
        "endpoint_switches": switches if len(known) == len(pairs) else None,
        "comparable_transitions": len(known),
        "unknown_transitions": len(pairs) - len(known),
        "mean_cache_ratio_after_first": round(sum(later) / len(later), 4) if later else None,
        "measured_calls": len(later),
        "unmeasured_calls": len(calls) - 1 - len(later),
        "calls": calls,
    }


async def _topic_shift_reuse(settings: Settings) -> dict[str, Any]:
    """How much of a request survives a runbook topic shift, live.

    **This replaces the issue #42 cache-affinity A/B, which measured nothing.**
    That experiment ran the sequence twice — once setting ``cache_session_id``,
    once leaving it ``None`` — to ask whether naming a sticky key kept a session
    on one endpoint. Against a ROUTER that was a real question. Against this host
    it is not one: ``_build_body`` DROPS the key under both vendors' spellings
    (docs/PROVIDER_POLICY.md section 3a), so the two arms sent byte-identical
    bodies and any delta between them was run order. Keeping it would have burned
    paid calls re-proving what a unit test already asserts, and — worse — printed
    a delta a reader would have taken for an affinity effect.

    What is left is the half that was always doing the work: one sequential
    session at concurrency one, well inside the upstream's prefix-cache TTL
    (Together guarantees 5 minutes, often longer), shifting runbooks every turn.

    The limit worth stating rather than papering over: Together matches prefixes
    ORG-WIDE, so any traffic from this account — another bench, a dev server, a
    real turn — can have warmed the same base prompt and runbook blocks already.
    The only freshness available is the offset, and the catalog affords a couple
    of dozen positions, so a rerun is not guaranteed cold. Space runs beyond the
    cache TTL if independence matters.

    What remains shared in every case is the invariant head above the runbook block
    (base prompt, response contract, tool schemas). That is not a confound to
    remove: it is shared by every Daintree request in existence, so the sequence
    meets it warm, which is exactly the condition the served fleet is in.
    """
    from daintree_assistant_server.runbooks.catalog import load_catalog

    run = uuid.uuid4().hex[:12]
    # `positions` counts valid STARTS, so it is the slack plus one: a catalog of
    # exactly one span fits, at the single offset 0.
    positions = len(load_catalog().runbooks) - _SHIFT_SPAN + 1
    if positions < 1:
        raise ValueError("catalog too small for a topic-shift sequence")
    offset = int(run[:8], 16) % positions
    provider = make_upstream_provider(settings, timeout=settings.main_timeout_seconds)
    try:
        calls = await _topic_shift_sequence(provider, settings, offset=offset)
    finally:
        await provider.aclose()

    return {
        "run": run,
        "offset": offset,
        **_summarize_topic_shift(calls),
        "note": "one sequence — indicative, not significant. Re-run before drawing a "
                "conclusion; the org-wide cache means a 'cold' first turn may not be.",
    }


async def run_live(
    settings: Settings, *, trials: int = 3, topic_shift: bool = False
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
    shift = await _topic_shift_reuse(settings) if topic_shift else None

    # Unreported ratios are dropped, not counted as zero — see `_cache_ratio`.
    warm_ratios = [
        r for c in warm_cases if not c["json_mode"]
        if (r := c["warm_call"]["cache_ratio"]) is not None
    ]
    doc: dict[str, Any] = {
        "meta": {
            "benchmark": "prompt_cost",
            "mode": "live",
            "model": EXECUTOR_MODEL,
            "trials": trials,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "summary": {
            "warm_tool_region_cache_ratio": max(warm_ratios) if warm_ratios else None,
            "streaming_ttfb_p50_s": ttfb["p50_s"],
            "composed_first_byte_p50_s": composed["first_byte"]["p50_s"],
            "note": "shared server-side cache → first call is NOT cold; only the warm "
                    "(second identical) call is a clean signal",
        },
        "warm_cache": warm_cases,
        "streaming_ttfb": ttfb,
        "composed_first_byte": composed,
    }
    if shift is not None:
        doc["topic_shift_reuse"] = shift
    return doc
