"""Deterministic startup + concurrency + CPU-throughput measurements.

The pure-CPU / boot numbers that gate Cloud Run cold start and the per-request
CPU floor — no API key, no network:

1. **Cold start** — ``import daintree_assistant_server`` (plus the app module,
   which pulls FastAPI and the whole service graph) in a FRESH subprocess so
   import cost is real, then ``build_container`` in-process (validates every
   prompt and loads the skill catalog during construction).
2. **Catalog build** — ``load_catalog()`` latency plus a determinism check on
   the ``revision`` hash (prompt-cache identity and state validation ride it).
3. **State-codec throughput** — ``StateCodec.issue``/``verify`` ops/sec (every
   ``/respond`` verifies the inbound token and issues a fresh one).
4. **Concurrency under load** — the REAL ``ChatOrchestrator.complete`` over a
   fake 0-latency provider, sequentially and at K-way ``asyncio.gather``. With
   an instant provider all measured latency is the server's own per-request CPU
   work (selector prompt render, the unmemoized ``base_prompt()`` re-read, the
   ~21.5k-token tool-schema normalization walk, response assembly). Concurrent
   latency is measured from SUBMISSION to completion, so it includes event-loop
   queueing behind other requests' CPU — ``p99_inflation_ratio`` is the
   stall/contention proxy.
5. **Readiness (light)** — ``/healthz`` and ``/readyz`` over the real app via
   FastAPI's TestClient, with the fake provider injected.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.modes import STANDARD_MODEL
from daintree_assistant_server.observability import configure_logging
from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.state.codec import StateCodec

from .._harness.env import BENCH_CALLER_KEY, build_settings
from .._harness.fixtures import build_respond_request
from .._harness.stats import latency_summary, percentile

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"


class PerfError(Exception):
    """A benchmark configuration or measurement problem."""


@dataclass
class BenchConfig:
    env_file: str | None = None
    concurrency: int = 32
    iters: int = 20_000
    cold_repeats: int = 5
    build_repeats: int = 5
    catalog_repeats: int = 10
    waves: int = 3
    sequential_calls: int = 24


# ---- fake provider ---------------------------------------------------------


class FakeStream(ProviderStream):
    """A trivial one-chunk stream (protocol completeness; the probe is non-streaming)."""

    def __init__(self) -> None:
        self.usage = ProviderUsage()
        self._chunks: list[dict[str, Any]] = [
            {
                "id": "x",
                "created": 0,
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ]

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        return self._chunks.pop(0) if self._chunks else None

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        return None


class FakeChatProvider:
    """A 0-latency, in-process ``ChatProvider``: every call answers instantly, so
    any latency measured through the orchestrator is the server's own CPU work.

    A ``json_object`` request (the selector) gets a valid selection over
    ``selector_skill_ids`` so the real selector path succeeds without a repair
    round; everything else gets a plain final answer (no tool calls, so the turn
    is a completed round and never a continuation).
    """

    def __init__(self, selector_skill_ids: list[str] | None = None) -> None:
        self.selector_skill_ids = list(selector_skill_ids or [])
        self.calls = 0
        self.selector_calls = 0

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        self.calls += 1
        if (request.response_format or {}).get("type") == "json_object":
            self.selector_calls += 1
            content = json.dumps(
                {
                    "task_type": "general",
                    "reason": "deterministic benchmark selection",
                    "skill_ids": self.selector_skill_ids,
                    "confidence": 0.9,
                }
            )
        else:
            content = "ok"
        return ProviderChatResult(
            id="x",
            created=0,
            model="fake",
            choices=[
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            usage=ProviderUsage(),
            finish_reason="stop",
        )

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:  # noqa: ARG002
        self.calls += 1
        return FakeStream()

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,  # noqa: ARG002
        *,
        function_name: str,
        function_schema: dict[str, Any],  # noqa: ARG002
    ) -> ProviderToolCallResult:
        self.calls += 1
        return ProviderToolCallResult(
            name=function_name, arguments_json="{}", usage=ProviderUsage()
        )

    async def verify_key(self, api_key: Any = None) -> dict[str, Any]:
        # Part of ``ChatProvider``, so it exists; microperf measures the turn path
        # only and never signs in.
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


# ---- 1. cold start ---------------------------------------------------------

_COLD_START_CODE = """\
import time
t0 = time.perf_counter()
import daintree_assistant_server
t1 = time.perf_counter()
import daintree_assistant_server.main
t2 = time.perf_counter()
print("COLD", (t1 - t0) * 1000.0, (t2 - t1) * 1000.0)
"""


def _ms_stats(runs_ms: list[float]) -> dict[str, Any]:
    return {
        "min_ms": round(min(runs_ms), 2),
        "median_ms": round(percentile(runs_ms, 50), 2),
        "runs_ms": [round(v, 2) for v in runs_ms],
    }


def measure_cold_start(repeats: int = 5) -> dict[str, Any]:
    """Import cost in a FRESH interpreter per run (nothing cached in-process)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR)
    package_ms: list[float] = []
    app_ms: list[float] = []
    total_ms: list[float] = []
    for _ in range(repeats):
        proc = subprocess.run(
            [sys.executable, "-c", _COLD_START_CODE],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
            check=False,
            timeout=120,
        )
        if proc.returncode != 0:
            raise PerfError(f"cold-start subprocess failed:\n{proc.stderr.strip()}")
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("COLD ")), None)
        if line is None:
            raise PerfError(f"cold-start subprocess printed no timing line:\n{proc.stdout}")
        _, pkg, appm = line.split()
        package_ms.append(float(pkg))
        app_ms.append(float(appm))
        total_ms.append(float(pkg) + float(appm))
    return {
        "repeats": repeats,
        "import_package_ms": _ms_stats(package_ms),
        "import_app_ms": _ms_stats(app_ms),
        "import_ms": _ms_stats(total_ms),
        "note": (
            "fresh subprocess per run; the bare package __init__ is tiny, so import_ms "
            "(package + app module — FastAPI, providers, services, module-level app) is "
            "the load-bearing cold-start number"
        ),
    }


async def measure_build_container(settings: Settings, repeats: int = 5) -> dict[str, Any]:
    """In-process ``build_container``: prompt validation + catalog load + wiring."""
    from daintree_assistant_server.main import build_container

    runs_ms: list[float] = []
    catalog_skills = 0
    for _ in range(repeats):
        t0 = time.perf_counter()
        container = build_container(settings)
        runs_ms.append((time.perf_counter() - t0) * 1000.0)
        catalog_skills = len(container.catalog)
        await container.aclose()
    return {
        "repeats": repeats,
        "catalog_skills": catalog_skills,
        "build_container_ms": _ms_stats(runs_ms),
        "note": "validates every prompt and loads the skill catalog during construction",
    }


# ---- 2. catalog build + determinism ----------------------------------------


def measure_catalog(repeats: int = 10) -> dict[str, Any]:
    runs_ms: list[float] = []
    catalog = load_catalog()
    for _ in range(repeats):
        t0 = time.perf_counter()
        catalog = load_catalog()
        runs_ms.append((time.perf_counter() - t0) * 1000.0)
    again = load_catalog()
    return {
        "repeats": repeats,
        "catalog_build_ms": _ms_stats(runs_ms),
        "revision_deterministic": catalog.revision == again.revision,
        "revision": catalog.revision,
        "skills": len(catalog),
    }


# ---- 3. state-codec throughput ----------------------------------------------


def measure_state_codec(iters: int = 20_000) -> dict[str, Any]:
    codec = StateCodec(
        current_key=b"microperf-key-not-a-secret",
        current_kid="bench",
        salt=b"microperf-salt",
        ttl_seconds=3600,
    )
    active = ["daintree.skill.alpha", "daintree.skill.beta", "daintree.skill.gamma"]
    revision = "sha256:" + "0" * 64

    token = ""
    t0 = time.perf_counter()
    for _ in range(iters):
        token = codec.issue(
            session_id="bench-session",
            active_skills=active,
            catalog_revision=revision,
            turn_id="turn-1",
            instruction_revision=0,
        )
    issue_s = time.perf_counter() - t0

    valid = 0
    t0 = time.perf_counter()
    for _ in range(iters):
        valid += int(codec.verify(token, session_id="bench-session").valid)
    verify_s = time.perf_counter() - t0

    return {
        "iters": iters,
        "active_skills": len(active),
        "token_bytes": len(token.encode("utf-8")),
        "issue_ops_per_sec": round(iters / issue_s),
        "verify_ops_per_sec": round(iters / verify_s),
        "all_verified": valid == iters,
    }


# ---- 4. concurrency under load ----------------------------------------------


def _probe_requests(tag: str, n: int) -> list[RespondRequest]:
    """Valid contract requests with the captured CLI tool projection; distinct
    session/turn ids per call so every round runs the full selector path."""
    return [
        build_respond_request(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Summarize the failing test output and propose a fix "
                        f"(probe {tag}-{i})."
                    ),
                }
            ],
            session_id=f"bench-{tag}-{i}",
            turn_id=f"turn-{tag}-{i}",
        )
        for i in range(n)
    ]


async def measure_concurrency(
    settings: Settings,
    *,
    concurrency: int = 32,
    waves: int = 3,
    sequential_calls: int = 24,
) -> dict[str, Any]:
    """Drive the REAL orchestrator (built via ``build_container``) over the fake
    provider: a sequential baseline, then K-way ``asyncio.gather`` at a few levels."""
    from daintree_assistant_server.main import build_container

    catalog = load_catalog()
    selector_ids = [s.id for s in catalog.skills[:2]]
    provider = FakeChatProvider(selector_skill_ids=selector_ids)
    container = build_container(settings, provider=provider)
    orchestrator = container.orchestrator

    warning_count = 0
    # Warnings above the production concurrency cap are DESIGNED behaviour, not
    # skipped work: the local selector's admission pool matches Cloud Run's
    # --concurrency=8, so a 32-way burst on one instance refuses the overflow
    # and degrades those turns open — exactly what production would do, on a
    # load shape Cloud Run itself prevents. Only warnings at or below the cap
    # indicate the server failing to do its job.
    warning_count_at_cap = 0
    production_cap = 8

    async def one(
        request: RespondRequest, t_submit: float, *, level: int = 1
    ) -> float:
        nonlocal warning_count, warning_count_at_cap
        outcome = await orchestrator.complete(
            request, api_key=BENCH_CALLER_KEY
        )
        elapsed = time.perf_counter() - t_submit
        n_warn = len((outcome.body or {}).get("warnings") or [])
        warning_count += n_warn
        if level <= production_cap:
            warning_count_at_cap += n_warn
        return elapsed

    try:
        sequential: list[float] = []
        for request in _probe_requests("seq", sequential_calls):
            sequential.append(await one(request, time.perf_counter()))
        single_p50_s = percentile(sequential, 50)

        levels = sorted({8, concurrency, concurrency * 2})
        level_docs: list[dict[str, Any]] = []
        for k in levels:
            latencies: list[float] = []
            for wave in range(waves):
                batch = _probe_requests(f"c{k}w{wave}", k)
                # Submission-to-completion latency: t_submit is evaluated at task
                # creation, before any request runs, so a request's latency includes
                # queueing behind the other K-1 requests' CPU on the event loop.
                tasks = [
                    asyncio.create_task(one(request, time.perf_counter(), level=k))
                    for request in batch
                ]
                latencies.extend(await asyncio.gather(*tasks))
            p99_s = percentile(latencies, 99)
            level_docs.append(
                {
                    "concurrency": k,
                    "waves": waves,
                    **latency_summary(latencies),
                    "p99_inflation_ratio": (
                        round(p99_s / single_p50_s, 2) if single_p50_s else None
                    ),
                }
            )
    finally:
        await orchestrator.drain_background_tasks(timeout=5.0)
        await container.aclose()

    total_requests = sequential_calls + sum(d["count"] for d in level_docs)
    return {
        "sequential": {"calls": sequential_calls, **latency_summary(sequential)},
        "levels": level_docs,
        "requested_concurrency": concurrency,
        "active_skill_ids": selector_ids,
        "total_requests": total_requests,
        "provider_calls": provider.calls,
        "provider_calls_per_request": (
            round(provider.calls / total_requests, 2) if total_requests else None
        ),
        "selector_calls": provider.selector_calls,
        "warning_count": warning_count,
        "warning_count_at_production_cap": warning_count_at_cap,
        "note": (
            "stateless-server concurrency probe: the real ChatOrchestrator.complete "
            "(non-streaming) over an instant provider, so all latency is per-request CPU "
            "(the LOCAL two-stage skill selector's ONNX forward passes, ~21.5k-token tool-schema "
            "normalization, response assembly). Selection shares a bounded worker pool "
            "sized to Cloud Run's --concurrency=8; above that, refusals degrade open by "
            "design and are excluded from the warning gate."
        ),
    }


# ---- 5. readiness (light) ----------------------------------------------------


def measure_readiness(settings: Settings) -> dict[str, Any]:
    """GET /healthz and /readyz over the real app (fake provider injected).

    Best-effort: an environment problem is recorded, never raised — this section
    must not sink the suite.
    """
    try:
        from fastapi.testclient import TestClient

        from daintree_assistant_server.main import create_app

        app = create_app(settings=settings, provider=FakeChatProvider())
        with TestClient(app) as client:
            healthz = client.get("/healthz")
            readyz = client.get("/readyz")
        doc: dict[str, Any] = {
            "healthz_status": healthz.status_code,
            "readyz_status": readyz.status_code,
        }
        try:
            body = readyz.json()
            if readyz.status_code == 200:
                doc["catalog_revision"] = body.get("catalog_revision")
                doc["skills"] = body.get("skills")
            else:
                doc["readyz_error"] = body.get("error")
        except ValueError:
            pass
        return doc
    except Exception as exc:  # readiness is best-effort, never sinks the suite
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---- orchestration -----------------------------------------------------------


def run_bench(config: BenchConfig) -> dict[str, Any]:
    if config.concurrency < 1:
        raise PerfError(f"--concurrency must be >= 1 (got {config.concurrency})")
    if config.iters < 1:
        raise PerfError(f"--iters must be >= 1 (got {config.iters})")

    # Quiet + deterministic: keep per-request INFO log rendering off the measured
    # path, and make sure the local .env can never fire skill-learning background
    # passes into the probe.
    configure_logging("WARNING")
    settings = build_settings(config.env_file, SKILL_LEARNING_MODE="off")

    cold = measure_cold_start(repeats=config.cold_repeats)
    catalog = measure_catalog(repeats=config.catalog_repeats)
    codec = measure_state_codec(iters=config.iters)
    build = asyncio.run(measure_build_container(settings, repeats=config.build_repeats))
    concurrency = asyncio.run(
        measure_concurrency(
            settings,
            concurrency=config.concurrency,
            waves=config.waves,
            sequential_calls=config.sequential_calls,
        )
    )
    readiness = measure_readiness(settings)

    primary = next(
        (lvl for lvl in concurrency["levels"] if lvl["concurrency"] == config.concurrency),
        concurrency["levels"][-1],
    )
    summary = {
        "import_ms_median": cold["import_ms"]["median_ms"],
        "build_container_ms_median": build["build_container_ms"]["median_ms"],
        "catalog_build_ms_median": catalog["catalog_build_ms"]["median_ms"],
        "revision_deterministic": catalog["revision_deterministic"],
        "catalog_skills": catalog["skills"],
        "codec_issue_ops_per_sec": codec["issue_ops_per_sec"],
        "codec_verify_ops_per_sec": codec["verify_ops_per_sec"],
        "single_call_p50_ms": round(concurrency["sequential"]["p50_s"] * 1000, 2),
        "concurrent_p50_ms": round(primary["p50_s"] * 1000, 2),
        "concurrent_p99_ms": round(primary["p99_s"] * 1000, 2),
        "p99_inflation_ratio": primary["p99_inflation_ratio"],
        "healthz_status": readiness.get("healthz_status"),
        "readyz_status": readiness.get("readyz_status"),
        "provider_calls_per_request": concurrency.get("provider_calls_per_request"),
        "provider_calls_total": concurrency.get("provider_calls"),
        "requests_total": concurrency.get("total_requests"),
        "selector_provider_calls": concurrency.get("selector_calls"),
        "concurrency_warnings": concurrency.get("warning_count"),
        "concurrency_warnings_at_cap": concurrency.get("warning_count_at_production_cap"),
    }
    # Semantic pass: a "faster" run is only meaningful if the server actually did the
    # production work. Guard against a regression that skips the selector/main call,
    # emits warnings, breaks catalog determinism, or fails readiness — any of which
    # would otherwise show a misleadingly fast, "green" p99.
    summary["passed"] = bool(
        summary["revision_deterministic"]
        and summary["healthz_status"] == 200
        and summary["readyz_status"] == 200
        and summary["codec_verify_ops_per_sec"] > 0
        # Exactly ONE provider call per request — the main completion — gated
        # on EXACT integers, not a rounded ratio (one extra call across 336
        # requests rounds to 1.00). Skill selection never touches the provider:
        # selector_calls must be exactly zero, or the upstream selector came
        # back without anyone deciding that.
        and summary["provider_calls_total"] == summary["requests_total"]
        and summary["selector_provider_calls"] == 0
        and (summary["concurrency_warnings_at_cap"] or 0) == 0
    )
    return {
        "meta": {
            "benchmark": "microperf",
            "mode": "deterministic",
            "model": STANDARD_MODEL,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "concurrency": config.concurrency,
            "codec_iters": config.iters,
            "skill_learning_forced_off": True,
            "note": (
                "fake 0-latency provider; no network, no API key — every number is the "
                "server's own CPU/boot cost"
            ),
        },
        "summary": summary,
        "cold_start": cold,
        "build_container": build,
        "catalog": catalog,
        "state_codec": codec,
        "concurrency": concurrency,
        "readiness": readiness,
    }
