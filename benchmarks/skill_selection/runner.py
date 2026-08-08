"""Drives the REAL production selector against live DeepSeek.

Everything the production request path uses is used here unchanged — the
``Selector`` class, the selector prompt template, ``sanitize_for_selector``, the
catalog, the DeepSeek provider adapter, and the retry policy — so a measured
improvement here is exactly what ``/respond`` will see. The only injectable seam
is the prompts directory, which is how prompt variants are benchmarked before
being promoted into ``src``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from daintree_assistant_server.config import OPENROUTER_MODEL, Settings
from daintree_assistant_server.prompts.loader import TEMPLATE_SELECTOR_SYSTEM, PromptLoader
from daintree_assistant_server.providers.openrouter import OpenRouterProvider
from daintree_assistant_server.skills.catalog import (
    CatalogManager,
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.selector import Selector

from .cases import BenchCase, load_cases
from .scoring import TrialResult, score_trial, summarize

ROOT = Path(__file__).resolve().parents[2]
VARIANTS_DIR = Path(__file__).resolve().parent / "prompts"
BASELINE = "baseline"


class BenchError(Exception):
    """A benchmark configuration problem (bad variant, missing key, ...)."""


@dataclass
class BenchConfig:
    variant: str = BASELINE
    trials: int = 3
    concurrency: int = 6
    id_filter: str | None = None
    env_file: str | None = None
    settings_overrides: dict[str, Any] = field(default_factory=dict)


def available_variants() -> list[str]:
    if not VARIANTS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in VARIANTS_DIR.iterdir()
        if (p / TEMPLATE_SELECTOR_SYSTEM).is_file()
    )


def resolve_prompt_dir(variant: str) -> Path | None:
    """``None`` means the packaged (production) prompts directory."""
    if variant == BASELINE:
        return None
    candidate = VARIANTS_DIR / variant
    if not (candidate / TEMPLATE_SELECTOR_SYSTEM).is_file():
        raise BenchError(
            f"unknown prompt variant {variant!r} — expected "
            f"{candidate / TEMPLATE_SELECTOR_SYSTEM}. "
            f"Available: {', '.join(available_variants()) or '(none)'}"
        )
    return candidate


def _build_settings(config: BenchConfig) -> Settings:
    env_file = config.env_file
    if env_file is None:
        default = ROOT / ".env"
        env_file = str(default) if default.is_file() else None
    settings = Settings(_env_file=env_file, **config.settings_overrides)
    if not settings.openrouter_api_key.get_secret_value():
        raise BenchError(
            "OPENROUTER_API_KEY is not set — export it or point --env-file at a .env that has it"
        )
    return settings


async def _run_trial(
    selector: Selector,
    case: BenchCase,
    trial: int,
    semaphore: asyncio.Semaphore,
    api_key: SecretStr,
) -> TrialResult:
    async with semaphore:
        started = time.perf_counter()
        decision = await selector.select(
            case.messages,
            list(case.active),
            api_key=api_key,
            turn_context=case.turn_context,
            available_tools=case.available_tools or None,
        )
        latency = time.perf_counter() - started
    usage = decision.usage
    return score_trial(
        case,
        trial,
        decision.skill_ids,
        degraded=decision.degraded,
        latency_s=round(latency, 3),
        confidence=decision.confidence,
        task_type=decision.task_type,
        reason=decision.reason,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
    )


async def run_bench(config: BenchConfig) -> dict[str, Any]:
    """Run the full suite for one prompt variant; returns the results document."""
    settings = _build_settings(config)
    prompt_dir = resolve_prompt_dir(config.variant)
    loader = PromptLoader(prompt_dir)  # None -> packaged prompts
    prompt_path = (prompt_dir or Path(loader.dir)) / TEMPLATE_SELECTOR_SYSTEM
    prompt_source = prompt_path.read_text(encoding="utf-8")

    catalog = load_catalog()
    manager = CatalogManager(catalog, primary_dir=default_skills_dir())
    cases = load_cases(catalog, id_filter=config.id_filter)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.selector_timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    # No prompt_cache_key: the OpenRouter migration dropped that setting (no endpoint
    # for this model advertises the parameter, and sending an unadvertised one can
    # empty the eligible-endpoint pool under require_parameters). This suite predates
    # that move and still passed the removed `deepseek_send_prompt_cache_key`.
    provider = OpenRouterProvider(
        client,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_timeout=settings.selector_timeout_seconds,
    )
    selector = Selector(provider, loader, manager, settings)

    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(config.concurrency)
    jobs = [
        _run_trial(selector, case, trial, semaphore, settings.openrouter_api_key)
        for trial in range(config.trials)
        for case in cases
    ]
    try:
        results = list(await asyncio.gather(*jobs))
    finally:
        await provider.aclose()
    finished_at = datetime.now(UTC)

    by_case: dict[str, list[TrialResult]] = {c.id: [] for c in cases}
    for r in results:
        by_case[r.case_id].append(r)

    return {
        "meta": {
            "benchmark": "skill_selection",
            "variant": config.variant,
            "model": OPENROUTER_MODEL,
            "trials": config.trials,
            "concurrency": config.concurrency,
            "filter": config.id_filter,
            "catalog_revision": catalog.revision,
            "prompt_path": str(prompt_path),
            "prompt_sha256": hashlib.sha256(prompt_source.encode("utf-8")).hexdigest(),
            "max_active_skills": settings.max_active_skills,
            "selector_timeout_seconds": settings.selector_timeout_seconds,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
        },
        "summary": summarize(cases, results),
        "cases": [
            {
                "id": case.id,
                "category": case.category,
                "note": case.note,
                "active": case.active,
                "expect": {"required": case.required, "optional": case.optional},
                "pass_count": sum(t.passed for t in by_case[case.id]),
                "trials": [
                    {
                        "picked": t.picked,
                        "missing": t.missing,
                        "extra": t.extra,
                        "passed": t.passed,
                        "degraded": t.degraded,
                        "latency_s": t.latency_s,
                        "confidence": t.confidence,
                        "task_type": t.task_type,
                        "reason": t.reason,
                        "prompt_tokens": t.prompt_tokens,
                        "completion_tokens": t.completion_tokens,
                    }
                    for t in sorted(by_case[case.id], key=lambda t: t.trial)
                ],
            }
            for case in cases
        ],
    }
