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
from typing import Any

from daintree_assistant_server.config import OPENROUTER_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest

from .._harness.env import make_upstream_provider
from .._harness.fixtures import build_respond_request
from .._harness.stats import latency_summary


async def _prompt_request(
    settings: Settings, *, messages: list[dict[str, Any]], stream: bool, json_mode: bool = False
) -> Any:
    """Assemble a real provider request, then clamp completion to bound cost — the
    prompt (what we measure) is untouched."""
    from daintree_assistant_server.prompts.loader import PromptLoader
    from daintree_assistant_server.services.prompt_assembler import PromptAssembler
    from daintree_assistant_server.skills.catalog import load_catalog

    assembler = PromptAssembler(PromptLoader(), settings)
    skills = list(load_catalog().skills[:2])
    req = build_respond_request(messages=messages, with_tools=not json_mode, stream=stream)
    if json_mode:
        gen = req.generation.model_copy(update={"response_format": "json_object"})
        req = req.model_copy(update={"generation": gen})
    pr = assembler.build_main_request(request=req, skills=skills, upstream_model=OPENROUTER_MODEL)
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
            outcome = await container.orchestrator.complete(req, upstream_model=OPENROUTER_MODEL)
            assert outcome.stream is not None
            async for _chunk in outcome.stream:
                first_byte.append(time.perf_counter() - t0)
                break
            # drain remainder so the upstream connection closes cleanly
            async for _ in outcome.stream:
                pass
        await container.orchestrator.drain_background_tasks(timeout=5.0)
    finally:
        await provider.aclose()
    return {"trials": trials, "first_byte": latency_summary(first_byte)}


async def run_live(settings: Settings, *, trials: int = 3) -> dict[str, Any]:
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

    warm_ratios = [c["warm_call"]["cache_ratio"] for c in warm_cases if not c["json_mode"]]
    return {
        "meta": {
            "benchmark": "prompt_cost",
            "mode": "live",
            "model": OPENROUTER_MODEL,
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
