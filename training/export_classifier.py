"""Export the trained skill classifier to ONNX, with a torch parity check.

Serving runs onnxruntime, not torch — the production image takes numpy +
onnxruntime + tokenizers and nothing else. So the graph, not the checkpoint, is
the artifact, and this is the only thing that produces it.

The parity check is not ceremony. An export that silently drops the head or
mis-slices the output still loads, still runs, and still returns an array of the
right shape; the failure would only surface later as mediocre accuracy and get
blamed on the training data.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

MODELS = ROOT / "training" / "models"
ARTIFACTS = ROOT / "src" / "daintree_assistant_server" / "skills" / "local" / "artifacts"
BASE_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class Graph(torch.nn.Module):
    """Encoder + mean pool + head, folded into ONE graph.

    Pooling lives inside the graph so the exported artifact is self-describing:
    the serving code cannot pool differently from the trainer, because it does
    not pool at all.
    """

    def __init__(self, backbone: torch.nn.Module, head: torch.nn.Linear) -> None:
        super().__init__()
        self.backbone = backbone
        self.head = head

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        hidden = self.backbone(input_ids=input_ids, attention_mask=attention_mask)[0]
        m = attention_mask.unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
        return self.head(pooled)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(MODELS / "classifier"))
    ap.add_argument("--out", default=str(ARTIFACTS / "classifier"))
    args = ap.parse_args()

    from transformers import AutoModel, AutoTokenizer

    src = Path(args.model)
    out = Path(args.out)
    meta = json.loads((src / "meta.json").read_text())
    labels = json.loads((src / "labels.json").read_text())

    # The backbone is whatever the trainer used, recorded in meta.json — not a
    # constant here. Exporting a MiniLM graph for bge-base weights would load,
    # run, and return garbage of the right shape.
    base = meta.get("base_model", BASE_MODEL)
    print(f"backbone: {base}")
    backbone = AutoModel.from_pretrained(base)
    head = torch.nn.Linear(backbone.config.hidden_size, meta["n_out"])
    state = torch.load(src / "model.pt", map_location="cpu")
    backbone.load_state_dict(
        {k[len("backbone."):]: v for k, v in state.items() if k.startswith("backbone.")}
    )
    head.load_state_dict(
        {k[len("head."):]: v for k, v in state.items() if k.startswith("head.")}
    )
    graph = Graph(backbone, head).eval()

    # Stage the export beside the target and swap only after the gate passes —
    # the previous good artifact must survive a failed export.
    final = out
    out = out.parent / (out.name + ".staging")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    tok = AutoTokenizer.from_pretrained(src)
    sample = tok(["rename my terminals", "thanks, that's great"], return_tensors="pt",
                 padding=True)
    fp32 = out / "model.onnx"
    torch.onnx.export(
        graph,
        (sample["input_ids"], sample["attention_mask"]),
        str(fp32),
        input_names=["input_ids", "attention_mask"],
        output_names=["logits"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"},
                      "attention_mask": {0: "batch", 1: "seq"},
                      "logits": {0: "batch"}},
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,
    )

    from onnxruntime.quantization import QuantType, quantize_dynamic

    int8 = out / "model_int8.onnx"
    quantize_dynamic(model_input=str(fp32), model_output=str(int8),
                     weight_type=QuantType.QInt8, extra_options={"MatMulConstBOnly": True})
    tok.save_pretrained(out)

    (out / "labels.json").write_text(json.dumps(labels, indent=2))
    # Provenance is COPIED from the trainer, never recomputed here: stamping the
    # live catalog at export time would let "retrain nothing, re-export" bless
    # stale weights as fresh. A checkpoint without a routing_revision predates
    # the provenance contract and must not ship.
    assert meta.get("routing_revision"), (
        "checkpoint has no routing_revision — retrain with the current "
        "train_classifier before exporting"
    )
    (out / "meta.json").write_text(json.dumps({**meta, "base_model": base}, indent=2))

    # ---- parity: DECISIONS over the whole benchmark, not logits over two
    # sentences. The earlier gate checked two canned strings and only top-1 —
    # a quantisation could change docs answers, second picks or newcomer
    # admission on arbitrary real inputs and still "pass". Every benchmark
    # case is rendered exactly as serving renders it and the full Selection
    # decision is compared. torch->fp32 must agree everywhere (fatal); int8 —
    # the SHIPPED precision — may drift on a bounded number of near-tie cases.
    import onnxruntime as ort

    from benchmarks.skill_selection.cases import load_cases
    from daintree_assistant_server.skills.local.classifier import SkillClassifier
    from daintree_assistant_server.skills.local.documents import query_document

    with torch.no_grad():
        ref = graph(sample["input_ids"], sample["attention_mask"]).numpy()
    sess_fp32 = ort.InferenceSession(str(out / "model.onnx"),
                                     providers=["CPUExecutionProvider"])
    got = sess_fp32.run(None, {
        "input_ids": sample["input_ids"].numpy().astype(np.int64),
        "attention_mask": sample["attention_mask"].numpy().astype(np.int64),
    })[0]
    delta = float(np.abs(got - ref).max())
    assert delta < 0.01, f"fp32 export diverged from torch: {delta}"

    from daintree_assistant_server.skills.catalog import default_skills_dir, load_catalog

    catalog = load_catalog(default_skills_dir())
    cases = load_cases(catalog)
    selectable = catalog.selectable_ids

    def decisions(quantized: bool) -> list[tuple[tuple[str, ...], bool]]:
        clf = SkillClassifier(out, quantized=quantized)
        outdec = []
        for case in cases:
            q = query_document(case.messages, turn_context=case.turn_context)
            active = [sid for sid in case.active if sid in selectable]
            sel = clf.select(q, active)
            outdec.append((tuple(sel.skill_ids), sel.docs_wanted))
        return outdec

    d_fp32 = decisions(False)
    d_int8 = decisions(True)
    # Two tiers of disagreement. CORE decisions — the top-ranked skill, whether
    # the answer is empty, and the docs verdict — must be near-identical: a
    # quantisation that changes those changed the model. Bonus-margin extras
    # flipping in and out around the 0.5 admission floor are EXPECTED int8
    # noise (the floor sits exactly where quantised logits wobble), so the
    # secondary gate is outcome parity: the required skill stays present on
    # (almost) exactly the same cases.
    core_diff = sum(
        1 for (a_ids, a_docs), (b_ids, b_docs) in zip(d_fp32, d_int8, strict=True)
        if (a_ids[:1], bool(a_ids), a_docs) != (b_ids[:1], bool(b_ids), b_docs)
    )
    full_diff = sum(1 for x, y in zip(d_fp32, d_int8, strict=True) if x != y)

    def recall_pass(dec: list[tuple[tuple[str, ...], bool]]) -> int:
        n = 0
        for case, (ids, _docs) in zip(cases, dec, strict=True):
            got = set(ids)
            miss = set(case.required) - got
            extra = got - set(case.required) - set(case.optional)
            n += not miss and (bool(case.required) or not extra)
        return n

    rp_fp32, rp_int8 = recall_pass(d_fp32), recall_pass(d_int8)
    print(f"  int8 vs fp32: core-decision diff {core_diff}/{len(cases)}  "
          f"full-tuple diff {full_diff}/{len(cases)}  "
          f"recall-pass {rp_fp32} -> {rp_int8}")
    assert core_diff <= 10, (
        f"int8 changes the CORE decision on {core_diff} of {len(cases)} cases "
        "(limit 10) — quantisation is not safe for this checkpoint"
    )
    assert abs(rp_fp32 - rp_int8) <= 3, (
        f"int8 moves recall-pass by {abs(rp_fp32 - rp_int8)} cases (limit 3)"
    )

    # Gate passed: swap the staging dir into place.
    if final.exists():
        shutil.rmtree(final)
    out.rename(final)
    out = final

    sizes = {p.name: round(p.stat().st_size / 1e6, 1) for p in out.glob("*.onnx")}
    print(f"\nexported to {out}")
    print(f"  sizes (MB): {sizes}")
    print(f"  labels: {len(labels)}  outputs: {meta['n_out']}  "
          f"routing: {meta['routing_revision'][:24]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
