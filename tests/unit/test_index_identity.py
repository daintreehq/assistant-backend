"""Re-embedding must never borrow vectors from different encoder weights."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from daintree_assistant_server.runbooks.local.setwise import (
    RunbookTable,
    _load_table,
    encoder_fingerprint,
    save_table,
)
from training.protocol import retired_inbox_reason


def test_encoder_identity_changes_with_weights_tokenizer_and_render_contract(
    tmp_path: Path,
) -> None:
    files = (
        "retriever/model.onnx",
        "ranking/model_int8.onnx",
        "ranking/tokenizer.json",
        "retriever/tokenizer.json",
    )
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"original")

    def identity(cap: int = 512, variant: str = "full") -> str:
        return encoder_fingerprint(tmp_path, quantized=True, doc_max=cap, variant=variant)

    original = identity()
    for name in files:
        path = tmp_path / name
        path.write_bytes(b"different")
        assert identity() != original
        path.write_bytes(b"original")
    assert identity() == original
    assert identity(256) != original and identity(variant="trigger") != original


def test_index_round_trip_keeps_encoder_identity(tmp_path: Path) -> None:
    table = RunbookTable(
        ("a",), np.ones((1, 2)), np.ones((1, 3)), {"a": "document"}, "int8-ranker", "encoder-A"
    )
    path = tmp_path / "vectors.npz"
    save_table(path, table)
    assert _load_table(path).encoder_fingerprint == "encoder-A"
    save_table(path, replace(table, encoder_fingerprint="encoder-B"))
    assert _load_table(path).encoder_fingerprint == "encoder-B"


def test_retired_protocol_guard_checks_history_and_spares_ordinary_lanes() -> None:
    assert retired_inbox_reason("check it", ["assistant: host.sendMessage accepted it"], [])
    assert retired_inbox_reason("read each lane's exchange", [], ["daintree.hosts.survey-hosts"])
    assert retired_inbox_reason("move e2e to a nightly lane", [], []) is None
    assert (
        retired_inbox_reason(
            "exchange the currency labels", [], ["daintree.edits.spawn-visible-agent"]
        )
        is None
    )
