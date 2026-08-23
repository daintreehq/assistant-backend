"""How small can stage 1 be? Train each candidate backbone and measure.

Stage 1 only has to put the right runbook somewhere in the top ``k``. That is a
much easier job than stage 2's, and paying bge-base-class capacity for it would
be waste sitting on the first-byte path of every turn.

## Reading the numbers, and why recall@32 is NOT the metric here

With 38 selectable runbooks, ``k=32`` is 84% of the catalog: essentially anything
scores 1.000 and the number discriminates nothing. The metric that predicts
behaviour at the catalog size this architecture is FOR is recall at a small
FRACTION of the catalog — ``r@8`` here is 21% of 38, the same fraction ``k=32``
would be of a 150-runbook catalog and ``k=210`` of a 1,000-runbook one.

So the rule this sweep applies: ``r@32`` must be 1.000 (a shortlist that drops
the answer caps the whole selector below where it already is), and among the
backbones that clear it, pick on ``r@8``/``r@3`` and cost.

Latency is measured through the SHIPPED path — exported to ONNX int8, single
intra-op thread, matching the 1-vCPU container — not as torch on MPS, which
would flatter every candidate equally and rank them wrongly.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

MODELS = ROOT / "training" / "models"

#: Candidates, smallest first. Parameter counts are dominated by the token
#: embedding table (vocab 30522), so a 3-layer 384-d model is far cheaper to RUN
#: than its parameter count suggests — the layers are what a forward pass pays
#: for, and they are what these differ in.
CANDIDATES = [
    "prajjwal1/bert-tiny",  # 2 layers, 128-d
    "sentence-transformers/paraphrase-MiniLM-L3-v2",  # 3 layers, 384-d
    "sentence-transformers/all-MiniLM-L6-v2",  # 6 layers, 384-d
    "BAAI/bge-small-en-v1.5",  # 12 layers, 384-d — the incumbent
]


def measure(model_dir: Path, base: str) -> dict[str, float]:
    """Export to ONNX int8 and time it the way production runs it."""
    import numpy as np
    import onnxruntime as ort
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModel, AutoTokenizer

    from training.setwise_model import RetrieverGraph

    tmp = model_dir / "_onnx"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    tok = AutoTokenizer.from_pretrained(model_dir)
    graph = RetrieverGraph(AutoModel.from_pretrained(model_dir)).eval()
    params = sum(p.numel() for p in graph.parameters())
    sample = tok(["rename my terminals"], return_tensors="pt", padding=True)
    fp32 = tmp / "model.onnx"
    torch.onnx.export(
        graph,
        (sample["input_ids"], sample["attention_mask"]),
        str(fp32),
        input_names=["input_ids", "attention_mask"],
        output_names=["embedding"],
        dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"},
            "attention_mask": {0: "batch", 1: "seq"},
            "embedding": {0: "batch"},
        },
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,
    )
    int8 = tmp / "model_int8.onnx"
    quantize_dynamic(
        model_input=str(fp32),
        model_output=str(int8),
        weight_type=QuantType.QInt8,
        extra_options={"MatMulConstBOnly": True},
    )
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(int8), opts, providers=["CPUExecutionProvider"])
    # A realistic conversation query, not a three-word one: cost is linear-ish in
    # sequence length and a short probe would rank the deep models too kindly.
    probe = tok(
        [
            "the four agents are all in the same checkout and it is getting messy, "
            "can you split them into their own worktrees and tell each one where it moved"
        ],
        return_tensors="pt",
        padding=True,
    )
    feed = {
        "input_ids": probe["input_ids"].numpy().astype(np.int64),
        "attention_mask": probe["attention_mask"].numpy().astype(np.int64),
    }
    for _ in range(10):
        sess.run(None, feed)
    times = []
    for _ in range(100):
        started = time.perf_counter()
        sess.run(None, feed)
        times.append((time.perf_counter() - started) * 1000)
    times.sort()
    size_mb = int8.stat().st_size / 1e6
    shutil.rmtree(tmp)
    return {
        "params_m": round(params / 1e6, 1),
        "int8_mb": round(size_mb, 1),
        "p50_ms": round(times[len(times) // 2], 2),
        "mean_ms": round(statistics.mean(times), 2),
        "tokens": int(probe["input_ids"].shape[1]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=5)
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    ap.add_argument("--out", default=str(ROOT / "training" / "results" / "retriever_sweep.json"))
    args = ap.parse_args()

    rows = []
    for base in args.models:
        slug = base.split("/")[-1]
        out_dir = MODELS / f"sweep_{slug}"
        print(f"\n=== {base} ===", flush=True)
        proc = subprocess.run(
            [
                sys.executable, "-u", "-m", "training.train_retriever",
                "--base-model", base,
                "--epochs", str(args.epochs),
                "--doc-max-length", "384",
                "--out", str(out_dir),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"  FAILED: {proc.stderr.strip().splitlines()[-3:]}")
            continue
        final = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("epoch")][-1]
        print(" ", final.strip())
        recalls = {
            part.split()[0]: float(part.split()[1])
            for part in _recall_parts(final)
        }
        row = {"base_model": base, **recalls, **measure(out_dir, base)}
        rows.append(row)
        print(f"  {row}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2))

    print(f"\n{'backbone':46} {'params':>7} {'int8':>7} {'p50':>7} "
          f"{'r@3':>6} {'r@8':>6} {'r@16':>6} {'r@32':>6}")
    for row in rows:
        print(
            f"{row['base_model']:46} {row['params_m']:>6.1f}M {row['int8_mb']:>6.1f}M "
            f"{row['p50_ms']:>6.2f}ms {row.get('r@3', 0):>6.3f} {row.get('r@8', 0):>6.3f} "
            f"{row.get('r@16', 0):>6.3f} {row.get('r@32', 0):>6.3f}"
        )
    return 0


def _recall_parts(line: str) -> list[str]:
    """Pull the 'r@1 0.850 r@3 0.971 …' tail out of a trainer epoch line."""
    tokens = line.split()
    parts = []
    for i, tok in enumerate(tokens):
        if tok.startswith("r@") and i + 1 < len(tokens):
            parts.append(f"{tok} {tokens[i + 1]}")
    return parts


if __name__ == "__main__":
    raise SystemExit(main())
