import math, os
QUANT = os.environ.get("FP32") != "1"
from benchmarks.runbook_selection import local_runner as lr
from benchmarks.runbook_selection.local_runner import *  # noqa
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier
import daintree_assistant_server.runbooks.local.setwise as sw
cat = lr.load_catalog(lr.default_runbooks_dir())
cases = [c for c in lr.load_cases(cat) if c.expect_docs is not None]
clf = SetwiseClassifier(lr.ARTIFACT, quantized=QUANT); clf.sync(cat)
last = {}
orig = clf._head.run
def run(*a, **k):
    out = orig(*a, **k); last['l'] = float(out[1][0]); return out
clf._head = type('H', (), {'run': staticmethod(run)})()
for c in cases:
    eligible=[m for m in c.messages if m.role in ("user","assistant","tool")]
    if c.active and eligible and eligible[-1].role != "user": continue
    sel = clf.select(lr.query_document(c.messages, turn_context=c.turn_context), [s for s in c.active if s in cat.selectable_ids], catalog=cat, max_active=lr.MAX_ACTIVE, docs_query=lr.last_user_text(c.messages))
    p = 1/(1+math.exp(-last['l']))
    ok = (p >= 0.40) == bool(c.expect_docs)
    print(f"{p:.3f} {'ok ' if ok else 'MISS'} expect={c.expect_docs} {c.id}")
