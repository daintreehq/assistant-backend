"""Drives the REAL production selector against live DeepSeek.

Everything the production request path uses is used here unchanged — the
``Selector`` class, the selector prompt template, ``split_selector_evidence``, the
catalog, the DeepSeek provider adapter, and the retry policy — so a measured
improvement here is exactly what ``/respond`` will see. The only injectable seam
is the prompts directory, which is how prompt variants are benchmarked before
being promoted into ``src``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from daintree_assistant_server.config import CEREBRAS_MODEL, Settings
from daintree_assistant_server.prompts.loader import TEMPLATE_SELECTOR_SYSTEM, PromptLoader
from daintree_assistant_server.providers.base import (
    ChatProvider,
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
)
from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.skills.catalog import (
    CatalogManager,
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.selector import Selector

from .._harness.quality import attach_verdict
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
    #: Route this run at a DIFFERENT model than the pinned one. ``None`` uses
    #: ``CEREBRAS_MODEL``, which is what a normal run must always do.
    #:
    #: The pinned id is hardcoded in `config.py` on purpose, and trying a
    #: candidate must not mean editing it: an experiment that starts by changing
    #: production is one that has to be remembered to change back. The override
    #: lives here, in the harness that produces the evidence, so a candidate can
    #: be scored against the same 146 cases before anything is pinned. Section 9
    #: of docs/PROVIDER_POLICY.md lists what else a real swap needs.
    model: str | None = None


class _ModelOverride:
    """A provider that rewrites the model id on the way out, and nothing else.

    The selector hardcodes ``CEREBRAS_MODEL`` inside ``_build_request``, which
    is correct — the pinned model is a product decision, not a parameter. So to
    score a CANDIDATE model the substitution has to happen below the selector,
    and this is the narrowest place it can: one field, on the request, in the
    harness.

    Deliberately a wrapper rather than a patch of `config.CEREBRAS_MODEL`. A
    module-level monkeypatch would also change the id the results document
    records and every other read in the process, so a run could report the model
    it *meant* to use rather than the one it sent. Here the two cannot disagree:
    `run_bench` records `config.model or CEREBRAS_MODEL`, and this class is the
    only thing that puts a model id on the wire.
    """

    def __init__(self, inner: ChatProvider, model: str) -> None:
        self._inner = inner
        self._model = model

    def _rewrite(self, request: ProviderChatRequest) -> ProviderChatRequest:
        return replace(request, model=self._model)

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        return await self._inner.complete(self._rewrite(request))

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        return await self._inner.open_stream(self._rewrite(request))

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        return await self._inner.structured_tool_call(
            self._rewrite(request),
            function_name=function_name,
            function_schema=function_schema,
        )

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        return await self._inner.verify_key(api_key)

    async def aclose(self) -> None:
        await self._inner.aclose()


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
    if not settings.cerebras_api_key.get_secret_value():
        raise BenchError(
            "CEREBRAS_API_KEY is not set — export it or point --env-file at a .env that has it"
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
        docs_query=decision.docs_query,
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
    # The unfiltered corpus, read BEFORE the paid run rather than after it. Read
    # afterwards it would be a time-of-check/time-of-use window: an edit made
    # while 438 calls were in flight would be discovered only once the money was
    # spent. Same catalog object, so grading cannot shift under the run either.
    whole_corpus = cases if not config.id_filter else load_cases(catalog)

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.selector_timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    # Affinity is the provider's business now: Cerebras caches prefixes
    # automatically and org-wide, and the adapter attaches `prompt_cache_key`
    # itself when a caller names one. Nothing to configure here.
    provider: ChatProvider = CerebrasProvider(
        client,
        api_key=settings.cerebras_api_key,
        base_url=settings.cerebras_base_url,
        default_timeout=settings.selector_timeout_seconds,
    )
    if config.model:
        provider = _ModelOverride(provider, config.model)
    selector = Selector(provider, loader, manager, settings)

    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(config.concurrency)
    jobs = [
        _run_trial(selector, case, trial, semaphore, settings.cerebras_api_key)
        for trial in range(config.trials)
        for case in cases
    ]
    try:
        results = list(await asyncio.gather(*jobs))
    finally:
        await provider.aclose()
    finished_at = datetime.now(UTC)

    return build_results_doc(
        config=config,
        cases=cases,
        whole_corpus=whole_corpus,
        results=results,
        catalog=catalog,
        settings=settings,
        prompt_path=prompt_path,
        prompt_source=prompt_source,
        started_at=started_at,
        finished_at=finished_at,
    )


def build_results_doc(
    *,
    config: BenchConfig,
    cases: list[BenchCase],
    whole_corpus: list[BenchCase],
    results: list[TrialResult],
    catalog: Any,
    settings: Any,
    prompt_path: Any,
    prompt_source: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    """Assemble the results document, verdict included.

    A separate function so the VERDICT WIRING is testable without a paid run.
    It is not hypothetical prudence: this wiring was written, silently removed by
    an unused-import cleanup, and every offline test stayed green — because the
    tests built the metadata themselves rather than asking the runner for it. The
    only thing that would have caught it was a live run, which is the one thing
    nobody does casually.
    """
    by_case: dict[str, list[TrialResult]] = {c.id: [] for c in cases}
    for r in results:
        by_case[r.case_id].append(r)

    return attach_verdict({
        "meta": {
            "benchmark": "skill_selection",
            # What a COMPLETE run looks like, from the UNFILTERED corpus. Derived
            # from `cases` it would be self-certifying: a --filter'd run would
            # declare its own subset to be the corpus and pass for covering it.
            "cases_expected": len(whole_corpus),
            "trials_expected": len(whole_corpus) * config.trials,
            "docs_trials_expected": sum(
                1 for c in whole_corpus if c.expect_docs is not None
            ) * config.trials,
            "categories_expected": sorted({c.category for c in whole_corpus}),
            "variant": config.variant,
            "model": config.model or CEREBRAS_MODEL,
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
                "expect": {
                    "required": case.required,
                    "optional": case.optional,
                    "docs": case.expect_docs,
                },
                "pass_count": sum(t.passed for t in by_case[case.id]),
                "docs_pass_count": sum(bool(t.docs_ok) for t in by_case[case.id]),
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
                        # Serialized so a docs miss is DIAGNOSABLE, not just
                        # counted: the summary can say five trials classified
                        # wrongly, and only the query it actually returned says
                        # which way and why.
                        "docs_query": t.docs_query,
                        "docs_ok": t.docs_ok,
                    }
                    for t in sorted(by_case[case.id], key=lambda t: t.trial)
                ],
            }
            for case in cases
        ],
    }, "skill_selection")
