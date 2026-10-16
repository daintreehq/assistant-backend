"""Export the two-stage selector to ONNX, with a decision-parity gate.

Serving runs onnxruntime, not torch — the production image takes numpy +
onnxruntime + tokenizers and nothing else. So the graphs, not the checkpoints,
are the artifact, and this is the only thing that produces them.

Three graphs and a table come out of here:

    retriever/model.onnx           stage 1 encoder, mean-pooled and L2-normalised (served fp32)
    ranking/model{,_int8}.onnx     stage 2 encoder, mean-pooled (served int8)
    head.onnx                      the set-wise head (fp32; ~2.4M params)
    vectors.npz                    both encoders' vectors for the build catalog

``vectors.npz`` is a CACHE, not a contract. Serving re-derives it from the live
catalog on load and re-embeds any runbook whose document digest has moved, so a
catalog that has drifted since the build costs a few milliseconds of embedding
rather than the degrade-open window the old fixed head required. Shipping it
means the common case — catalog unchanged since build — starts instantly.

The parity gate compares DECISIONS over the whole benchmark, not logits over two
sentences: an export that silently drops the head or mis-slices an output still
loads, still runs, and still returns arrays of the right shape.
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

from training.setwise_model import (  # noqa: E402
    RetrieverGraph,
    SetwiseGraph,
    SetwiseHead,
)

MODELS = ROOT / "training" / "models"
ARTIFACTS = (
    ROOT / "src" / "daintree_assistant_server" / "runbooks" / "local" / "artifacts"
)


def _export_encoder(
    graph: torch.nn.Module,
    tok,  # type: ignore[no-untyped-def]
    out: Path,
    *,
    max_length: int,
    quantize: bool,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    sample = tok(
        ["rename my terminals", "thanks, that's great"],
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=max_length,
    )
    fp32 = out / "model.onnx"
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
    from onnxruntime.quantization import QuantType, quantize_dynamic

    # PER-CHANNEL weights. A single scale for a whole weight matrix has to span
    # every output channel's dynamic range at once, so one wide channel costs
    # every narrow one its precision. Measured here: per-tensor int8 across the
    # two encoders moved the CORE decision on 14 of 169 benchmark cases and cost
    # 5 recall-pass; per-channel is the same size and the same speed.
    #
    # REDUCE_RANGE is not optional, and the reason is that this gate cannot see
    # it. 7-bit weights cost a little precision on paper; what they buy is the
    # x86 accumulation path. ONNX Runtime's AVX2 int8 kernels (no VNNI) saturate
    # on full-range s8 weights, and arm64's do not — so an export validated on an
    # Apple-silicon dev machine can be badly wrong on the linux/amd64 that CI and
    # Cloud Run actually run. Measured: the ranking encoder, fed IDENTICAL token
    # ids, returned a materially different embedding on x86 than on arm64
    # (component 3 of the pooled vector: 0.066 vs 0.251), which collapsed the
    # head's calibration and drove the cardinality argmax to 0 — the selector
    # returned NO runbooks for every request, while /health and /readyz stayed
    # green. The parity gate below compares int8 against fp32 on whichever
    # machine exports, so it is structurally blind to this. Keep the flag.
    #
    # Only the ranking encoder is quantised: serving runs the retriever in fp32
    # (see SetwiseClassifier.__init__), so an int8 retriever would be dead weight.
    if quantize:
        quantize_dynamic(
            model_input=str(fp32),
            model_output=str(out / "model_int8.onnx"),
            weight_type=QuantType.QInt8,
            per_channel=True,
            reduce_range=True,
            extra_options={"MatMulConstBOnly": True},
        )
    tok.save_pretrained(out)

    import onnxruntime as ort

    with torch.no_grad():
        ref = graph(sample["input_ids"], sample["attention_mask"]).numpy()
    got = ort.InferenceSession(str(fp32), providers=["CPUExecutionProvider"]).run(
        None,
        {
            "input_ids": sample["input_ids"].numpy().astype(np.int64),
            "attention_mask": sample["attention_mask"].numpy().astype(np.int64),
        },
    )[0]
    delta = float(np.abs(got - ref).max())
    assert delta < 0.01, f"{out.name} fp32 export diverged from torch: {delta}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setwise", default=str(MODELS / "setwise"))
    ap.add_argument("--out", default=str(ARTIFACTS / "selector"))
    args = ap.parse_args()

    from transformers import AutoConfig, AutoModel, AutoTokenizer

    src = Path(args.setwise)
    meta = json.loads((src / "meta.json").read_text())
    labels = json.loads((src / "labels.json").read_text())
    retriever_dir = Path(meta["retriever"])
    r_meta = json.loads((retriever_dir / "meta.json").read_text())

    final = Path(args.out)
    # Stage beside the target and swap only once the gate passes — the previous
    # good artifact must survive a failed export.
    out = final.parent / (final.name + ".staging")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"stage 1: {r_meta['base_model']}  stage 2: {meta['base_model']}")
    # Serving applies ONE document cap to both encoders (they embed the same
    # runbook document), so the two stages must have been trained under the same
    # one. Trained at 384 and served at 192, the runbook vectors would silently
    # lose the whenToUse exclusion clauses — which are the routing signal.
    assert r_meta["doc_max_length"] == meta["doc_max_length"], (
        f"stage 1 trained with doc_max_length={r_meta['doc_max_length']} but "
        f"stage 2 with {meta['doc_max_length']}; serving caps both at one value"
    )

    r_backbone = AutoModel.from_pretrained(retriever_dir)
    r_tok = AutoTokenizer.from_pretrained(retriever_dir)
    _export_encoder(
        RetrieverGraph(r_backbone).eval(),
        r_tok,
        out / "retriever",
        max_length=r_meta["max_length"],
        quantize=False,
    )

    state = torch.load(src / "model.pt", map_location="cpu")
    # ModernBERT-family backbones must be traced with eager attention: their
    # default path dispatches on runtime kernels the legacy exporter cannot
    # capture. BERT keeps its default so the shipped graph does not change.
    kind = AutoConfig.from_pretrained(meta["base_model"]).model_type
    attn = {} if kind == "bert" else {"attn_implementation": "eager"}
    backbone = AutoModel.from_pretrained(meta["base_model"], **attn)
    backbone.load_state_dict(state["backbone"])
    tok = AutoTokenizer.from_pretrained(src)
    _export_encoder(
        SetwiseGraph(backbone).eval(),
        tok,
        out / "ranking",
        max_length=meta["max_length"],
        quantize=True,
    )

    head = SetwiseHead(
        meta["hidden_size"],
        d_model=meta["d_model"],
        layers=meta["layers"],
        rank_positions=meta["rank_positions"],
    )
    head.load_state_dict(state["head"])
    head.eval()

    k = int(meta["candidates"])
    hidden = int(meta["hidden_size"])
    sample = (
        torch.randn(1, hidden),
        torch.randn(1, k, hidden),
        torch.randn(1, k, 3),
        torch.ones(1, k),
    )
    # FULLY STATIC — batch 1, exactly k candidate slots.
    #
    # Not a shortcut. ``nn.MultiheadAttention`` traces to reshapes that hardcode
    # the sequence length, so declaring the candidate axis dynamic produces a
    # graph that loads fine and then throws on any K but the traced one (caught
    # here by running the k=12 case against a k=32 export). Serving therefore
    # always feeds k slots, padding with zeros and mask 0 when the catalog is
    # smaller, and a static graph is what onnxruntime optimises best anyway.
    torch.onnx.export(
        head,
        sample,
        str(out / "head.onnx"),
        input_names=["query", "candidates", "features", "mask"],
        output_names=["runbook_logits", "docs_logit", "cardinality_logits"],
        opset_version=17,
        do_constant_folding=True,
        dynamo=False,
    )

    import onnxruntime as ort

    with torch.no_grad():
        ref_runbook, ref_docs, ref_card = head(*sample)
    got = ort.InferenceSession(
        str(out / "head.onnx"), providers=["CPUExecutionProvider"]
    ).run(
        None,
        {
            "query": sample[0].numpy(),
            "candidates": sample[1].numpy(),
            "features": sample[2].numpy(),
            "mask": sample[3].numpy(),
        },
    )
    head_delta = max(
        float(np.abs(got[0] - ref_runbook.numpy()).max()),
        float(np.abs(got[1] - ref_docs.numpy()).max()),
        float(np.abs(got[2] - ref_card.numpy()).max()),
    )
    assert head_delta < 0.01, f"head export diverged from torch: {head_delta}"

    (out / "labels.json").write_text(json.dumps(labels, indent=2))
    (out / "meta.json").write_text(
        json.dumps(
            {
                **{k2: v for k2, v in meta.items() if k2 != "retriever"},
                "retriever_base_model": r_meta["base_model"],
                "retriever_max_length": r_meta["max_length"],
                "retriever_hidden_size": r_meta["hidden_size"],
            },
            indent=2,
        )
    )

    # ---- the vector table, built through the SHIPPED code path -------------
    from daintree_assistant_server.runbooks.catalog import (
        default_runbooks_dir,
        load_catalog,
    )
    from daintree_assistant_server.runbooks.local.setwise import (
        SetwiseClassifier,
        save_table,
    )

    catalog = load_catalog(default_runbooks_dir())
    # Built by ``sync`` rather than by a private loop here, so the vectors that
    # ship are produced by exactly the function that will refresh them at
    # runtime. A separate build path is how a build-time index and a
    # request-time query end up in subtly different spaces.
    clf = SetwiseClassifier(out, quantized=True)
    table = clf.sync(catalog)
    save_table(out / "vectors.npz", table)
    print(f"  embedded {len(table.ids)} runbooks into vectors.npz")

    # ---- parity: DECISIONS over the whole benchmark ------------------------
    from benchmarks.runbook_selection.cases import load_cases
    from daintree_assistant_server.runbooks.local.documents import (
        last_user_text,
        query_document,
    )

    cases = load_cases(catalog)
    selectable = catalog.selectable_ids

    def decisions(quantized: bool) -> list[tuple[tuple[str, ...], bool]]:
        clf_local = SetwiseClassifier(out, quantized=quantized)
        clf_local.sync(catalog)
        result = []
        for case in cases:
            q = query_document(case.messages, turn_context=case.turn_context)
            active = [sid for sid in case.active if sid in selectable]
            # docs_query, because this gate scores `docs_wanted`. Serving decides
            # that on the bare last user message via a second pass; omitting it
            # here would compare two artifacts on a code path neither ships.
            sel = clf_local.select(
                q, active, docs_query=last_user_text(case.messages), exclude=case.exclude
            )
            result.append((tuple(sel.runbook_ids), sel.docs_wanted))
        return result

    d_fp32 = decisions(False)
    d_int8 = decisions(True)

    def recall_pass(dec: list[tuple[tuple[str, ...], bool]]) -> int:
        n = 0
        for case, (ids, _docs) in zip(cases, dec, strict=True):
            got_ids = set(ids)
            miss = set(case.required) - got_ids
            extra = got_ids - set(case.required) - set(case.optional)
            n += not miss and (bool(case.required) or not extra)
        return n

    # Two tiers, matching the old export's posture. CORE decisions — the
    # top-ranked runbook, whether the answer is empty, the docs verdict — must be
    # near-identical: a quantisation that changes those changed the model.
    # Bonus-margin extras flipping around BONUS_FLOOR are expected int8 noise,
    # so the secondary gate is outcome parity.
    core_diff = sum(
        1
        for (a_ids, a_docs), (b_ids, b_docs) in zip(d_fp32, d_int8, strict=True)
        if (a_ids[:1], bool(a_ids), a_docs) != (b_ids[:1], bool(b_ids), b_docs)
    )
    full_diff = sum(1 for x, y in zip(d_fp32, d_int8, strict=True) if x != y)
    rp_fp32, rp_int8 = recall_pass(d_fp32), recall_pass(d_int8)
    print(
        f"  int8 vs fp32: core-decision diff {core_diff}/{len(cases)}  "
        f"full-tuple diff {full_diff}/{len(cases)}  "
        f"recall-pass {rp_fp32} -> {rp_int8}"
    )
    assert core_diff <= 10, (
        f"int8 changes the CORE decision on {core_diff} of {len(cases)} cases "
        "(limit 10) — quantisation is not safe for this checkpoint"
    )
    assert abs(rp_fp32 - rp_int8) <= 3, (
        f"int8 moves recall-pass by {abs(rp_fp32 - rp_int8)} cases (limit 3)"
    )

    if final.exists():
        shutil.rmtree(final)
    out.rename(final)
    sizes = {
        str(p.relative_to(final)): round(p.stat().st_size / 1e6, 1)
        for p in sorted(final.rglob("*.onnx"))
    }
    print(f"\nexported to {final}")
    print(f"  sizes (MB): {sizes}")
    print(f"  shortlist k={k}  ordering={meta['ordering']}  runbooks={len(labels)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
