"""Drives the REAL production ``TaskRunner`` against live DeepSeek.

Everything the production ``/v1/daintree/tasks`` path uses is used here
unchanged — the ``TaskRunner`` class, the real per-task profiles (user-message
builders, parsers, output caps, the one repair retry), the packaged task
prompts, the DeepSeek adapter, and the retry policy — so a measured accuracy
change here is exactly what a client will see. The only injectable seam is an
optional prompts overlay: a variant directory that overrides individual
``tasks/<id>.md`` bodies (falling back to the packaged prompt for the rest), so
a candidate prompt can be benchmarked before it is promoted into ``src``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks._harness.env import make_upstream_provider
from daintree_assistant_server.config import SERVING_KEY_ENV, SERVING_MODEL, Settings
from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.task_runner import TaskRunner

from .cases import TaskCase, load_cases
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


class OverlayPromptLoader(PromptLoader):
    """Packaged prompt loader with a per-task overlay: ``tasks/<id>.md`` under
    ``overlay_dir`` wins when present, otherwise the packaged body is used. Only
    ``task_system_prompt`` is overridden — every other prompt stays packaged."""

    def __init__(self, overlay_dir: Path | None) -> None:
        super().__init__(None)  # packaged prompts
        self.overlay_dir = overlay_dir

    def task_system_prompt(self, task_id: str) -> str:
        if self.overlay_dir is not None:
            path = self.overlay_dir / "tasks" / f"{task_id}.md"
            if path.is_file():
                return path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
        return super().task_system_prompt(task_id)


def available_variants() -> list[str]:
    if not VARIANTS_DIR.is_dir():
        return []
    return sorted(p.name for p in VARIANTS_DIR.iterdir() if (p / "tasks").is_dir())


def resolve_overlay_dir(variant: str) -> Path | None:
    """``None`` means the packaged (production) prompts with no overlay."""
    if variant == BASELINE:
        return None
    candidate = VARIANTS_DIR / variant
    if not (candidate / "tasks").is_dir():
        raise BenchError(
            f"unknown prompt variant {variant!r} — expected a tasks/ dir at "
            f"{candidate / 'tasks'}. Available: {', '.join(available_variants()) or '(none)'}"
        )
    return candidate


def _build_settings(config: BenchConfig) -> Settings:
    env_file = config.env_file
    if env_file is None:
        default = ROOT / ".env"
        env_file = str(default) if default.is_file() else None
    settings = Settings(_env_file=env_file, **config.settings_overrides)
    # The SERVING credential, read the same way production reads it. This line
    # named a vendor while the provider two functions down was a different one,
    # which is how the wrong secret ended up in an Authorization header bound
    # for the right endpoint.
    if not settings.serving_api_key.get_secret_value():
        raise BenchError(
            f"{SERVING_KEY_ENV} is not set — export it or point --env-file at a "
            ".env that has it"
        )
    return settings


def _prompt_hashes(loader: PromptLoader, tasks: set[str]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for task in sorted(tasks):
        body = loader.task_system_prompt(task)
        hashes[task] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashes


async def _run_trial(
    runner: TaskRunner,
    case: TaskCase,
    trial: int,
    semaphore: asyncio.Semaphore,
) -> TrialResult:
    async with semaphore:
        started = time.perf_counter()
        error: str | None = None
        output: dict[str, Any] = {}
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        try:
            result = await runner.run(
                TaskRequest(task=case.task, input=case.input)
            )
            output = result.output
            prompt_tokens = result.usage.prompt_tokens
            completion_tokens = result.usage.completion_tokens
        except DaintreeError as exc:
            error = f"{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001 — a benchmark crash is a scored failure
            error = f"{type(exc).__name__}: {exc}"
        latency = time.perf_counter() - started
    return score_trial(
        case,
        trial,
        output,
        error=error,
        latency_s=round(latency, 3),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


async def run_bench(config: BenchConfig) -> dict[str, Any]:
    """Run the full suite for one prompt variant; returns the results document."""
    if config.trials < 1:
        raise BenchError(f"--trials must be >= 1 (got {config.trials})")
    if config.concurrency < 1:
        raise BenchError(f"--concurrency must be >= 1 (got {config.concurrency})")
    settings = _build_settings(config)
    overlay_dir = resolve_overlay_dir(config.variant)
    loader = OverlayPromptLoader(overlay_dir)

    cases = load_cases(id_filter=config.id_filter)
    tasks_in_run = {c.task for c in cases}

    # Built through the shared harness rather than hand-wired, which is also the
    # fix for a bug this line carried: it named `settings.deepseek_send_prompt_cache_key`,
    # a field removed with the DeepSeek-direct adapter, so this runner raised
    # AttributeError before its first call. Nothing caught it — a live benchmark
    # is not in the test suite, and the dashboard does not run the paid ones.
    provider = make_upstream_provider(settings, timeout=settings.utility_timeout_seconds)
    # No per-call `api_key`: the provider carries the server credential, exactly
    # as production does. A hand-picked per-call key OVERRIDES the provider's
    # own, which is how a secret meant for one vendor ended up in a header bound
    # for another — the second bug this line shipped.
    runner = TaskRunner(settings=settings, provider=provider, prompt_loader=loader)

    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(config.concurrency)
    jobs = [
        _run_trial(runner, case, trial, semaphore)
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
            "benchmark": "utility_tasks",
            "variant": config.variant,
            "model": SERVING_MODEL,
            "trials": config.trials,
            "concurrency": config.concurrency,
            "filter": config.id_filter,
            "overlay_dir": str(overlay_dir) if overlay_dir else None,
            "prompt_sha256": _prompt_hashes(loader, tasks_in_run),
            "utility_timeout_seconds": settings.utility_timeout_seconds,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
        },
        "summary": summarize(cases, results),
        "cases": [
            {
                "id": case.id,
                "task": case.task,
                "category": case.category,
                "note": case.note,
                "expect": case.expect,
                "pass_count": sum(t.passed for t in by_case[case.id]),
                "trials": [
                    {
                        "output": t.output,
                        "mismatch": t.mismatch,
                        "error": t.error,
                        "passed": t.passed,
                        "latency_s": t.latency_s,
                        "confidence": t.confidence,
                        "prompt_tokens": t.prompt_tokens,
                        "completion_tokens": t.completion_tokens,
                    }
                    for t in sorted(by_case[case.id], key=lambda t: t.trial)
                ],
            }
            for case in cases
        ],
    }
