"""The selector's model files are named by `selector.lock`, not stored in Git.

The lock is the artifact's identity: the build downloads exactly what it names, so a commit
whose lock disagrees with the model on disk ships the wrong model or none. These tests hold
that together, and exercise the store tool against a directory standing in for the bucket.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "src/daintree_assistant_server/runbooks/local/artifacts/selector"


def _load_store() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "selector_store", ROOT / "training" / "selector_store.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load_store()


def _lock() -> dict[str, Any]:
    return json.loads((ROOT / "selector.lock").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _tracked(*patterns: str) -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout.split()


# --- the lock against the repository ------------------------------------------


def test_the_lock_names_every_model_file_the_server_loads() -> None:
    files = set(_lock()["files"])
    assert files == {
        "head.onnx",
        "ranking/model_int8.onnx",
        "ranking/tokenizer.json",
        "retriever/model.onnx",
        "retriever/tokenizer.json",
        "vectors.npz",
    }
    for entry in _lock()["files"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        assert entry["size"] > 0


def test_no_model_file_is_tracked_by_git() -> None:
    """Weights in Git are what this replaced: each retrain added ~225 MB of history."""
    stored = [
        path
        for path in _tracked("src/daintree_assistant_server/runbooks/local/artifacts")
        if store.is_stored(path.split("artifacts/selector/", 1)[-1])
    ]
    assert stored == []
    assert not (ROOT / ".gitattributes").exists() or "filter=lfs" not in (
        ROOT / ".gitattributes"
    ).read_text(encoding="utf-8")


def test_the_bootstrap_bucket_is_the_bucket_the_lock_names() -> None:
    bootstrap = (ROOT / "infra/gcp/bootstrap.sh").read_text(encoding="utf-8")
    default = re.search(r'SELECTOR_BUCKET="\$\{SELECTOR_BUCKET:-([^}]+)\}"', bootstrap)
    assert default is not None
    assert _lock()["bucket"] == "gs://" + default.group(1)


def test_the_installed_model_is_the_one_the_lock_names() -> None:
    """A retrain that was installed but not published (or the reverse) fails here."""
    present = [rel for rel in _lock()["files"] if (ARTIFACT / rel).is_file()]
    if not present:
        pytest.skip("selector model not fetched (training/fetch_selector.sh)")
    assert store.check(_lock(), ARTIFACT) == []


# --- the tool, against a directory standing in for the bucket ------------------


def _artifact(tmp_path: Path, payload: bytes = b"weights") -> Path:
    directory = tmp_path / "artifact"
    (directory / "ranking").mkdir(parents=True)
    (directory / "head.onnx").write_bytes(payload)
    (directory / "ranking" / "tokenizer.json").write_bytes(b"{}")
    (directory / "ranking" / "model.onnx").write_bytes(b"fp32 graph: never stored")
    (directory / "meta.json").write_text("{}", encoding="utf-8")
    return directory


def test_only_the_served_binaries_are_stored() -> None:
    assert store.is_stored("head.onnx")
    assert store.is_stored("retriever/model.onnx")
    assert store.is_stored("ranking/tokenizer.json")
    assert store.is_stored("vectors.npz")
    assert not store.is_stored("ranking/model.onnx")  # the fp32 ranker is never shipped
    assert not store.is_stored("meta.json")
    assert not store.is_stored("ranking/tokenizer_config.json")


def test_publish_then_fetch_reproduces_the_artifact(tmp_path: Path) -> None:
    bucket, lock_path = tmp_path / "bucket", tmp_path / "selector.lock"
    source = _artifact(tmp_path)
    assert store.publish(str(bucket), source, lock_path) == 0

    lock = store.read_lock(lock_path)
    assert sorted(lock["files"]) == ["head.onnx", "ranking/tokenizer.json"]
    assert store.check(lock, source) == []

    fresh = tmp_path / "fresh"
    assert store.fetch(lock, fresh, use_cache=False) == 0
    assert (fresh / "head.onnx").read_bytes() == b"weights"
    assert not (fresh / "ranking" / "model.onnx").exists()


def test_fetch_refuses_an_object_that_is_not_what_the_lock_says(tmp_path: Path) -> None:
    bucket, lock_path = tmp_path / "bucket", tmp_path / "selector.lock"
    store.publish(str(bucket), _artifact(tmp_path), lock_path)
    lock = store.read_lock(lock_path)
    sha = lock["files"]["head.onnx"]["sha256"]
    (bucket / "blobs" / sha).write_bytes(b"substitut")  # same name, different bytes

    with pytest.raises(SystemExit, match="does not match the lock"):
        store.fetch(lock, tmp_path / "fresh", use_cache=False)
    assert not (tmp_path / "fresh" / "head.onnx").exists()
    assert not list((tmp_path / "fresh").rglob("*.partial"))


def test_check_reports_a_missing_resized_and_altered_file(tmp_path: Path) -> None:
    lock = {
        "bucket": "x",
        "files": {
            "a.onnx": {"sha256": store.sha256_of(_write(tmp_path / "a.onnx", b"one")), "size": 3},
            "b.onnx": {"sha256": "0" * 64, "size": 3},
            "c.onnx": {"sha256": "0" * 64, "size": 3},
            "d.onnx": {"sha256": "0" * 64, "size": 9},
        },
    }
    _write(tmp_path / "b.onnx", b"two")  # right size, wrong hash
    _write(tmp_path / "d.onnx", b"two")  # wrong size
    problems = store.check(lock, tmp_path)
    assert problems == [
        "content differs from the lock: b.onnx",
        "missing: c.onnx",
        "size 3, lock says 9: d.onnx",
    ]


def test_prune_keeps_what_a_lock_names_and_what_is_young(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_git_locks", lambda *_: [])
    bucket, lock_path = tmp_path / "bucket", tmp_path / "selector.lock"
    store.publish(str(bucket), _artifact(tmp_path), lock_path)
    referenced = store.lock_hashes(store.read_lock(lock_path))

    old, young = bucket / "blobs" / ("a" * 64), bucket / "blobs" / ("b" * 64)
    old.write_bytes(b"old")
    young.write_bytes(b"young")
    stale = time.time() - 3 * 86400
    os.utime(old, (stale, stale))

    store.prune(False, 3, 24, lock_path)
    assert old.exists(), "a dry run deletes nothing"
    store.prune(True, 3, 24, lock_path)
    assert not old.exists()
    assert young.exists(), "an object younger than the grace period may belong to an unpushed lock"
    assert all((bucket / "blobs" / sha).exists() for sha in referenced)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path
