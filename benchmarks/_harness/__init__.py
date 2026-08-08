"""Shared plumbing for the benchmark suites.

Every suite drives the REAL production classes (assembler, orchestrator, task
runner, selector, provider, validators) and treats only the prompt directory /
a fixture corpus as an injectable seam — a measured number is exactly what
production produces. This package holds the pieces they all reuse:

- ``env``      — settings + live OpenRouter provider bootstrap (reads ``.env``).
- ``counting`` — a transparent provider decorator that counts model calls and
  captures the FIRST response, so a repaired-then-parsed result is no longer
  byte-indistinguishable from a clean first-try one (the production classes sum
  usage across attempts and expose no ``repaired`` flag).
- ``wire``     — the EXACT httpx wire serialization of an assembled request, plus
  the cacheable-region reconstruction and longest-common-prefix maths
  the flagship cost/cache benchmark is built on.
- ``stats``    — percentiles / mean / stdev with no numpy dependency.
- ``results``  — the per-suite JSON results document writer/loader.
- ``cases``    — a generic ``cases/*.yaml`` loader.
- ``fixtures`` — the captured CLI tool projection + RespondRequest builders.

Benchmarks are dev tooling: NOT in the package, the test suite, mypy, or CI
(``benchmarks`` is outside ``testpaths``/``src``). Nothing here persists server
state — the backend stays stateless and Cloud Run-safe; these only read it.
"""

from __future__ import annotations

from .cases import CaseError, load_yaml_cases
from .counting import CountingProvider
from .env import EnvError, build_settings, make_upstream_provider
from .results import load_doc, results_dir, save_doc
from .stats import mean, percentile, pstdev
from .wire import (
    cacheable_region_bytes,
    common_prefix_len,
    layer_segments,
    wire_bytes,
)

__all__ = [
    "CaseError",
    "CountingProvider",
    "EnvError",
    "build_settings",
    "cacheable_region_bytes",
    "common_prefix_len",
    "layer_segments",
    "load_doc",
    "load_yaml_cases",
    "make_upstream_provider",
    "mean",
    "percentile",
    "pstdev",
    "results_dir",
    "save_doc",
    "wire_bytes",
]
