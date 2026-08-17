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

    # ---- parity ---------------------------------------------------------
    import onnxruntime as ort

    with torch.no_grad():
        ref = graph(sample["input_ids"], sample["attention_mask"]).numpy()
    for variant in ("model.onnx", "model_int8.onnx"):
        sess = ort.InferenceSession(str(out / variant), providers=["CPUExecutionProvider"])
        got = sess.run(None, {
            "input_ids": sample["input_ids"].numpy().astype(np.int64),
            "attention_mask": sample["attention_mask"].numpy().astype(np.int64),
        })[0]
        delta = float(np.abs(got - ref).max())
        # What matters is not the logits but the DECISIONS they produce, so the
        # argmax of the cardinality head and the skill ordering are compared too.
        same_card = bool((got[:, meta["labels"] + 1:].argmax(1)
                          == ref[:, meta["labels"] + 1:].argmax(1)).all())
        same_top = bool((got[:, :meta["labels"]].argmax(1)
                         == ref[:, :meta["labels"]].argmax(1)).all())
        print(f"  {variant}: max|logit delta| {delta:.5f}  "
              f"top-skill agrees {same_top}  cardinality agrees {same_card}")
        if variant == "model.onnx":
            assert delta < 0.01, f"fp32 export diverged from torch: {delta}"
            assert same_top and same_card, "fp32 export changes the DECISION"
        elif not (same_top and same_card):
            # Reported, not fatal — and the reason fp32 is what SHIPS.
            #
            # int8 changes which skill ranks first on this model. For the
            # reranker that trade was worth taking, because its forward pass was
            # 300ms and quantisation bought real time. Here the whole selector is
            # ~2ms, so quantisation buys nothing that matters and costs decision
            # stability. 90MB of fp32 on Git LFS is the cheaper side of that
            # trade by a wide margin.
            print("    ^ int8 changes decisions; fp32 is the shipped precision")

    sizes = {p.name: round(p.stat().st_size / 1e6, 1) for p in out.glob("*.onnx")}
    print(f"\nexported to {out}")
    print(f"  sizes (MB): {sizes}")
    print(f"  labels: {len(labels)}  outputs: {meta['n_out']}  "
          f"routing: {meta['routing_revision'][:24]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
