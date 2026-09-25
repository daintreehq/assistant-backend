"""The inference pool and the Cloud Run instance are sized together.

Cloud Run admits up to ``--concurrency`` requests per instance; the pool refuses
a search outright once ``QUEUE_DEPTH`` are admitted. A queue shallower than the
platform's concurrency turns ordinary load into retryable errors, so the two are
pinned against each other rather than trusted to be edited together.
"""

from __future__ import annotations

import re
from pathlib import Path

from daintree_assistant_server.runbooks.local.runtime import QUEUE_DEPTH, WORKER_THREADS

CLOUDBUILD = Path(__file__).resolve().parents[2] / "cloudbuild.yaml"


def _flag(name: str) -> str:
    found = re.findall(rf"^\s*--{name}=(\S+)\s*$", CLOUDBUILD.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1, f"expected exactly one --{name} in cloudbuild.yaml, found {found}"
    return found[0]


def test_the_pool_admits_everything_cloud_run_lets_in() -> None:
    assert int(_flag("concurrency")) <= QUEUE_DEPTH


def test_one_inference_thread_per_vcpu() -> None:
    assert int(_flag("cpu")) == WORKER_THREADS
