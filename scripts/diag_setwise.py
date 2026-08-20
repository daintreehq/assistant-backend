"""Throwaway: isolate where the two platforms diverge."""
from __future__ import annotations

import platform
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

sys.path.insert(0, "src")
from daintree_assistant_server.skills.local import setwise as S  # noqa: E402

ART = Path("src/daintree_assistant_server/skills/local/artifacts/selector")
Q = "File a new issue called Login crash and label it bug."

print("platform :", platform.machine(), platform.system(), "| ort", ort.__version__)

# --- 1. tokenization: is it identical? ---
tok = S._tokenizer(ART / "ranking", 256)
enc = tok.encode_batch([Q])[0]
print("token ids:", enc.ids)

# --- 2. FIXED input, no tokenizer. Any difference here is pure kernel. ---
fixed = np.array([[101, 2054, 2003, 2023, 1029, 102]], dtype=np.int64)
fmask = np.ones_like(fixed)
for name in ("retriever", "ranking"):
    for graph in ("model_int8.onnx", "model.onnx"):
        p = ART / name / graph
        if not p.exists():
            print(f"{name:9} {graph:16} ABSENT")
            continue
        sess = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
        out = sess.run(None, {"input_ids": fixed, "attention_mask": fmask})[0]
        print(
            f"{name:9} {graph:16} norm={float(np.linalg.norm(out)):.6f} "
            f"head5={np.round(out[0][:5], 5).tolist()}"
        )
