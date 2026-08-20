"""Throwaway: dump the selector's intermediate values so two platforms can be compared."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort

sys.path.insert(0, "src")
from daintree_assistant_server.skills.catalog import default_skills_dir, load_catalog  # noqa: E402
from daintree_assistant_server.skills.local import setwise as S  # noqa: E402

ART = Path("src/daintree_assistant_server/skills/local/artifacts/selector")
QUERIES = [
    ("rename my terminals so i can tell the agents apart", "daintree.terminals.rename"),
    ("File a new issue called Login crash and label it bug.", "daintree.forge.manage-issue"),
    ("Squash and merge pull request 42 after checking every gate.", "daintree.forge.land-pull-request"),
]

print("== env ==")
print("platform :", platform.machine(), platform.system())
print("ort      :", ort.__version__, "providers:", ort.get_available_providers())
print("numpy    :", np.__version__)

cat = load_catalog(default_skills_dir())
clf = S.SetwiseClassifier(ART)
tbl = clf.sync(cat)
print("table    : ids=%d retrieval%s ranking%s" % (len(tbl.ids), tbl.retrieval.shape, tbl.ranking.shape))
print("table checksum: retrieval=%.6f ranking=%.6f" % (float(tbl.retrieval.sum()), float(tbl.ranking.sum())))

for q, want in QUERIES:
    print("\n== %s ==" % q[:60])
    q_ret = S._run_encoder(clf._retriever, clf._retriever_tok, [q], normalize=True)[0]
    q_rank = S._run_encoder(clf._ranker, clf._ranker_tok, [q], normalize=False)[0]
    print("  q_ret  : norm=%.6f  head5=%s" % (float(np.linalg.norm(q_ret)), np.round(q_ret[:5], 5).tolist()))
    print("  q_rank : norm=%.6f  head5=%s" % (float(np.linalg.norm(q_rank)), np.round(q_rank[:5], 5).tolist()))
    sims = tbl.retrieval @ q_ret
    order = np.argsort(-sims)[:3]
    print("  top sims:", [(tbl.ids[int(i)].replace("daintree.", ""), round(float(sims[int(i)]), 5)) for i in order])
    sel = clf.select(q, [])
    print("  card=%d picked=%s" % (sel.cardinality, [s.replace('daintree.', '') for s in sel.skill_ids]))
    print("  want=%s score=%.6f" % (want.replace("daintree.", ""), sel.scores.get(want, float("nan"))))
