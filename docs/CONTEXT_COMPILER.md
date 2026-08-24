# The Context Compiler — a future feature, assessed against what exists

**Status: future work. Nothing described here as "the compiler" is built.**

This file records an external design — *Daintree Assistant: Adaptive Runbook
Orchestration and Query-Aware Context Compilation System*, v1.1, 23 August 2026 —
and says which parts of it this codebase should adopt, which parts it has already
built under other names, and which parts contradict decisions that were made
deliberately and measured.

Like [`TODO.md`](../TODO.md), and unlike the other files in `docs/`, this document
is **not covered by `tests/unit/test_docs_drift.py`**. It is future tense by
construction, so nothing in it is pinned to the code. `PROVIDER_POLICY.md`,
`KEY_HANDLING.md`, `INTERNAL_BETA.md` and `DAINTREE_API.md` describe what the
service does today and are authoritative where they disagree with this file.
Check every claim here about present behaviour against the source before acting
on it.

---

## 1. The position in one paragraph

The spec's core abstraction is right for this service and worth adopting:

> A query-conditioned, textually lossless, selectively lossy working set compiled
> from an immutable structured event history. The loss is entirely in what is
> omitted.

Everything retained is either exact canonical source bytes or a clearly-marked
deterministic projection carrying source provenance. A model **scores relevance
and returns identifiers**; deterministic code turns identifiers into byte ranges
and reconstructs the text. That division — models advise, deterministic code
enforces — is the same one this server already applies to runbook selection, tool
projection and the state token, so it needs no new philosophy, only new machinery.

**There is exactly one architectural mismatch, and it is load-bearing.** The spec
assumes the server owns a durable history store and compiles context *out of it*.
This server owns no history at all: the CLI holds the conversation and resends it
every round, and nothing is persisted (see §5 below). Resolving that is not a
matter of building the store — it is realising that **the store is only needed for
training, never for serving**. The compiler at request time is a pure function
over the transcript the client already sends. Everything in the spec that reads as
"the server must remember" belongs to the offline half.

---

## 2. Already built, under a different name

The spec presents these as work to do. They exist. Adopting the spec must not mean
rebuilding them.

| Spec | Here today |
|---|---|
| §25 hierarchical selection — cheap retrieval, then a fine-grained learned selector | Exactly the shipped runbook selector: a bi-encoder shortlists `k`=32 by cosine, then a set-wise head reads the query and all candidates together (`runbooks/local/setwise.py`) |
| §24 student selector, ~100–150M query-aware encoder, per-unit keep probabilities | The same shape. Per-candidate features (cosine, `rank_norm`, `active`), a probability per candidate, plus a cardinality head making "keep nothing" a first-class answer |
| §32 selector on CPU, in-process ONNX, no GPU | Done. `runbooks/local/` ships an ONNX artifact and selects in ~10ms |
| §26 local training factory on a 64 GB M4 | `training/` — corpus assembly, retriever and set-wise training, evaluation, ONNX export. `training/EXPERIMENTS.md` is the log |
| §7 fast planner's visible preamble, which must not falsely claim work has happened | `services/fast_response.py`. `validate_preamble` refuses completed-work claims in four grammatical shapes, text addressed to the model, and control markers |
| §33 provider abstraction | `providers/base.py::ChatProvider`, with Together and Cerebras adapters behind it |
| §34 typed stable redactions, e.g. `<REDACTED:API_KEY:hash_8f24>` | `tracing/redaction.py` emits `<REDACTED:API_KEY:h_8f24>` — same idea, keyed HMAC digest |
| §34 explicit training eligibility | `tracing/eligibility.py`. Standing rule: **using the service is not consent** |
| §18 full-context internal testing with complete structured traces | `tracing/` — the plane exists. It has never been switched on (§6) |
| §17 Stage 1, protected content that may never be dropped | The principle is enforced for *tools*: `ALWAYS_AVAILABLE` is re-derived from the base prompt text by `tests/unit/test_prompt_hygiene_tools.py`, because withholding a tool the permanent prompt instructs is a contradiction the model cannot resolve. The same argument generalises to context units |
| §28 context manifest | The coarse ancestor exists: `PromptSegment` logs a digest and byte count per assembled *layer* on every turn (`prompt_segments`). The spec wants per-*unit* provenance |
| §17 Stage 8 budget allocation by priority | Already drafted, in `TODO.md` — "Shed context instead of only refusing it" carries a never-dropped / dropped-last / shrunk-first ordering that closely matches Stage 8 |
| §29 evaluation against a full-context baseline | Partially: `benchmarks/` has 12 suites and a deterministic dashboard. The missing half is behavioural, and `TODO.md` already owns it — "A behavioural benchmark for the main assistant" |

The practical consequence: the spec's §35 implementation order is wrong for us
because it assumes a greenfield. Steps 4, 5, 6, 7, 21, 25 and 32 are done or
substantially done.

## 3. Applies, and is genuinely new

Ordered by how much they are worth, not by the spec's numbering.

**Typed source units with byte provenance (§10, §12).** The single most valuable
idea in the document. Today the assembler's unit of thought is a *layer* — base
prompt, runbooks, startup, conversation, tail. The compiler needs a much finer
unit that varies by content type: a sentence for prose, a whole tool-call object,
a JSON subtree for a result, a line range for a log, a whole hunk for a diff, a
symbol range for code. Each carries `event_id`, `blob_id`, `start_byte`,
`end_byte` and a type. Nothing here does this.

**The extractive fidelity guarantee (§11).** `compiled_span == source[start:end]`,
checked, with text materialised only at the last moment before the request is
built. This is the property that makes selective loss safe, and it is the property
a generative summariser cannot offer at any temperature.

**Tool-call atomicity as a compiler rule (§13.3, §13.4, §5).** A call and its
result are a dependency pair; never orphan either. The *validator* already
enforces this shape on input (`services/validation.py` runs the transcript as a
state machine, and `_is_tool_closed` in `services/compaction.py` refuses a span
that would split a transaction). The compiler needs the same rule on the way out,
where it is a harder problem because selection is per-unit.

**JSON Pointer result projections (§14).** Keep `/status`, `/stderr`, `/exit_code`
and drop `/debug_metadata`; split a large string field into addressable line
ranges. This is where the volume actually is in an orchestration transcript, and
it is deterministic — no model needed for the mechanism, only for the choice.

**Structural reducers (§16).** Test output, compiler output, `git status`, search
results, file reads. Deterministic, testable, no model, and they run *before* any
learned selection. Cheapest real win in the document and independently useful —
they would reduce context today, with no compiler and no new model.

**The task ledger (§8) and the context query (§9).** Relevance must not be
conditioned on the literal last user message: "Okay, fix it." carries no signal.
The spec builds a 50–200 token query from objective, phase, state, constraints and
expected next actions.

We have hard-won evidence about exactly this, and it cuts both ways. `documents.fit_query`
already composes `[prior] [newest] [turn_context]` and shrinks it deliberately
under a 256-token cap rather than letting the encoder truncate from the right.
But the docs-retrieval decision deliberately uses a **different, barer string** —
the cleaned last user message alone — because the corpus contains no rows with
history or an active set, so any added context read to the head as "not a docs
turn" and drove recall to 5/120. Read that as the warning it is: **a richer query
is only better if the training corpus was built with the richer query.** Query
construction and corpus construction are one decision, not two.

**Confidence-graded fallback (§30).** Bands from "standard compiled context" down
to "full conversation", plus unconditional fallback on ambiguity, destructive
operations, invalid provenance and repeated task failure. This matches the
fail-open posture the selector already has, and the spec is right that false
negatives cost far more than retained slack.

**Teacher labeling: requirements → evidence → coverage (§19–§23).** The offline
half, and the part that matches the batch job we want. A local 14–32B teacher on
the Mac Studio reads a trace *with hindsight* — which files were later opened,
which tools later called, whether the task succeeded — and answers "which source
units were actually necessary?" by returning **IDs, never text**. Deterministic
code validates the IDs, expands the sparse KEEP set into a full KEEP/DROP mask,
and aligns to token labels via offset mapping. The teacher solves semantics only;
it never produces the compressed text. The two-pass structure — evidence first,
then a separate coverage pass asking "what is missing?" — deliberately optimises
recall, which is the right bias.

## 4. Does not apply

Each of these is a real conflict with a decision already made here, not a
difference of taste. Adopting the spec wholesale would undo measured work.

**§17 Stage 9's reassembly order is wrong for this server, and it is the most
dangerous item in the document.** The spec recommends: system prefix → policy →
task ledger → current user message → execution spec → selected runbooks → recent
conversation. That interleaves volatile content ahead of stable content. The
serving model's chat template serialises *all system messages* → *tools* →
*non-system conversation*, so anything volatile placed in the system region lands
ahead of ~21.5k tokens of tool schemas and destroys their cache — measured
2026-07-08 at 36% hit on byte-identical turns, restored to 99% by ordering rows by
how widely each can be shared, widest first. A compiler that re-derives its
context every turn makes this *more* acute, not less: the more of the prompt that
becomes query-dependent, the more of it re-encodes every round. **Cache economics
must be a first-class constraint on the compiler's output ordering**, and the spec
does not model them at all. Our own order stands; the compiler must slot units
into it.

**§31's history retrieval escape hatch (`history.search`, `history.fetch`,
`history.fetch_range`) cannot work as described.** Two independent reasons. The
CLI reports an unoffered tool as `callable: false` and dynamic invocation of a
withheld tool does not exist (CLI #368) — so a tool the compiler wants the
executor to reach for must be offered up front, on every turn, and counted in the
core. And the server has no history to serve: the CLI owns it. If this is wanted,
it is a **CLI-side** capability over the CLI's own store, surfaced through the
existing tool inventory — not a server route. The spec's good idea inside it
survives either way: **a history fetch is an implicit relevance signal and should
be recorded as a training label.**

**§6's runbook schema conflicts with settled policy.** It proposes `version: 4`
and `status: active` per runbook. This project is pre-release and deliberately
unversioned — runbooks carry no version field, and cache-busting rides the content
hash into `catalog_revision` (CLAUDE.md, "Versioning (pre-release)"). Its
`triggers` / `anti_triggers` lists are our `whenToUse` prose, with carve-outs
written as fully-qualified ids and audited by `bench lint` and
`test_runbook_catalog_hygiene.py`; its `capabilities.required` is our
`requiredTools`, which already drives both `Catalog.executable_with` and tool
projection. Its YAML `steps` are our Markdown bodies. **Adopt none of this.** The
one idea worth taking is `protected_constraints` — a runbook declaring which of
its content may never be dropped by a compiler — and even that should wait until
the compiler exists to consume it.

**§5's router is a re-run of something already deleted.** A `turn_kind` taxonomy
with `planning_required` is attractive, but the prompt-based `Selector` — a billed
model call on the turn's first-byte path — was deleted precisely because it was
slower and more expensive than a local classifier on every axis measured. Any
router must be local and in-process, like the current one. The cheap version of
this already half-exists: the set-wise head's cardinality output and the
fast-response eligibility gates (`session.round`, streaming, sub-agent, runbook
loaded) are together an implicit turn-kind classifier. Making it explicit is
plausible; making it a model call is not.

**§33's "do not hard-code Cerebras, Qwen, or DeepSeek assumptions" is half right.**
The *abstraction* must stay, and does. But model ids here are hardcoded on
purpose: which model, which posture, which prompt is a product decision with one
benchmarked right answer, and there are deliberately no env toggles for those. The
spec's warning applies to the compiler's *logic* — no unit-typing or reducer
should assume a particular model's tokenizer or wire quirks — and not to
configuration.

**§7's execution specification is unproven here.** The preamble half shipped and
is measured; the structured plan half never existed. `FAST_RESPONSE_MODE` still
defaults to `off`. Do not build a compiler that depends on an execution spec until
the stage that would produce one is switched on and shown to earn its latency.

**§6/§35's "mine successful plans into improved runbooks" is a retread.** A
runbook-learning builder existed and was removed; it is the reason the `risk` key
survives with no consumer. Not a reason never to revisit it, but it is not new
ground and the previous attempt should be read first.

## 5. The mismatch that matters: nobody here owns history

The spec's architecture diagram has every event flowing into an immutable trace
store, and the Context Compiler reading out of it. This server stores **nothing**:

- No database, cache, or session store of any kind.
- The signed state token is carried by the *client* and holds only active runbook
  ids and refs, `catalog_revision`, turn/instruction bookkeeping, profile, the
  turn's docs query and expiry — pointers, no content.
- Compaction blocks are deliberately never persisted. The span start is re-derived
  every turn by scanning for the reserved `daintree_compaction` user message the
  client splices back in, so a frozen segment is never re-compacted without any
  server-side state.
- The trace plane is the only durable content store, and it has never been enabled.

**The resolution is a split, and it should be stated plainly whenever this work is
picked up:**

- **Serving** — the compiler is a *pure function over the transcript the client
  sends this round*. Units are indexed, scored and selected in-process; nothing is
  remembered between rounds beyond what the state token already carries. This
  keeps Cloud Run statelessness intact and needs no new storage.
- **Training** — the immutable trace store is real, and is where the labels come
  from. It runs **locally only** (§6), offline, in batch.

Provenance is what makes the split work. Within one request, a unit id can address
a byte range in the transcript that request carries. Across requests it need not
resolve at all — the next round re-indexes from the transcript it is given.

## 6. Data capture: what exists, and what "we're logging for this" actually means

**Nothing has been captured yet.** The plane is built and correct; it has never
been switched on. Stating this precisely matters, because the dataset targets in
§27 (300–700 traces for a pilot) are measured from zero.

**Capture is local-only, deliberately, and the code already enforces it** — no new
guard is needed to keep this off Cloud Run:

- `cloudbuild.yaml` sets no `TRACE_*` variable, so production runs
  `NullTraceRecorder`.
- `TRACE_CAPTURE_MODE=full_internal` fails `/readyz` in a hardened environment
  (`production`/`staging`), and `eligibility.decide` refuses content capture again
  at the request level — "a readiness gate protects the deployment, this protects
  the request."
- `TRACE_SINK=filesystem` is separately forbidden in production/staging.

Local capture is `ENVIRONMENT=development`, `TRACE_CAPTURE_MODE=full_internal`,
`TRACE_SINK=filesystem`, `TRACE_FILESYSTEM_DIR=.tmp/traces` (gitignored),
`TRACE_IDENTITY_KEY` set. That combination is the only one that yields
`training_eligible=True`.

**How capture behaves, and why it suits a batch job.** One trace is one backend
request *round*, written to `<root>/<trace_id>/` as `header.json` first, then
`events.jsonl` appended during the turn, content-addressed gzipped blobs in
`blobs/`, then `manifest.json` and finally `complete.json` — the marker whose
presence means every blob the manifest names is stored.

Rounds do **not** accumulate into one growing record: each is an independent,
self-contained directory, and blob dedup is within a trace only. That is fine, and
arguably better than accumulating, because the client resends the full transcript
every round — so `request.validated` on round *N* already contains rounds 1..*N*,
and the last round of a session is a complete conversation on its own.
Reassembling conversations is an offline group-by: `header.session_key` (an HMAC,
never the raw id) with `header.round` for ordering. The server is stateless and
has no session-end signal, so "one record per conversation" is a **dedupe on
export**, not a capture-time decision. Locally, the wasted bytes do not matter.

Two things the capture already does that the spec asks for and that would be
expensive to add later. `runbook.active_set.resolved` records the **rejected**
candidates and the prior active set alongside the winners — a future
context-selection model has to learn what was passed over as much as what was
picked, and nothing can recover that after the turn ends. And identity is
pseudonymous at the capture boundary: raw session id, raw turn id and the signed
state token are scrubbed out of the stored request by exact server-owned path.

### Gaps that block the dataset

Four, all small, all on the capture side rather than the design side:

1. **`/v1/daintree/tasks` is not traced at all.** No trace calls in
   `services/task_runner.py`. So every `context_compact` call — a real
   transcript-in / reconciled-state-out pair, the closest thing to labelled
   compaction data obtainable without hand-labelling — is computed and discarded.
2. **No session-level assembly.** `training/export_context_traces.py` maps one
   trace directory to one example. Nothing groups on `session_key`, and the
   last-round-wins dedupe described above does not exist.
3. **`tool.call.observed` / `tool.result.observed` are never emitted.** They appear
   only in `tests/unit/test_trace_exporter.py`, so the exporter's
   `_tool_graph_is_sound` check is inert against every real trace — and tool
   call/result pairing is exactly what §13.4 needs to be able to trust.
4. **`ModelInvocationTrace.wire_request_ref` is always `None`** (`TODO.md`, "Capture
   the exact provider wire body in traces"). The assembled request is captured;
   the exact bytes sent upstream are not.

## 7. The fork in the road: select, or reconcile?

This has to be decided before either path gets built, because the two answers are
not compatible and both are currently half-present.

**Reconciliation (what exists).** The `context_compact` task takes a transcript and
*generates* a JSON object — `original_ask`, `agents`, `terminals`, `decisions`,
`blockers`, `next_actions`, `preserved_ids`. It is state reconciliation rather than
prose summary, it is already tuned hard for identifier preservation, and it is
shipped and callable. `TODO.md`'s "Compact the settled prefix instead of predicting
finality" extends this design.

**Selection (what the spec argues for).** §38 states the opposite position
directly: ordinary summarisation should *not* be the primary long-context strategy,
and retained content must be exact source bytes.

The honest comparison:

- Reconciliation is *better* where history is a stale snapshot of a mutable world —
  agent statuses, terminal output, queue state — because the newest observation
  genuinely supersedes the older ones and there is no source range that expresses
  "the current state" as well as a generated sentence does. This is the argument
  from issue #60, and it is a good argument about Daintree's specific history.
- Selection is *better* where the loss is unrecoverable and undetectable: a
  paraphrased identifier, a dropped path, a drifted user constraint. The
  `context_compact` prompt is full of instructions fighting exactly this decay —
  "quote, don't paraphrase", "never shorten a terminal id to its prefix", carry a
  prior block forward rather than re-summarising it. Every one of those
  instructions is a symptom of a guarantee the extractive design gets for free.

**The likely synthesis, and the recommendation to test first:** they are not
competitors at the same layer. Selection should govern *what history reaches the
model at all*, and reconciliation should govern *the small typed state ledger*
(§8) that sits alongside it — which is generated, small, and cheap to regenerate,
so its drift is bounded and correctable. That gives exact bytes for evidence and a
compact generated ledger for state, and it is close to what both designs are
reaching for from opposite ends.

## 8. Sequencing

The spec's §35 order assumes greenfield. Adapted to what exists, and ordered so
each step is independently useful if the next is never taken:

1. **Turn on local capture and start collecting.** Zero code. Everything else is
   gated on having traces, and traces only accumulate with wall-clock time.
2. **Close the four capture gaps (§6).** Trace the tasks path first — it is the
   only source of pre-labelled compaction pairs and every untraced call is
   permanently lost.
3. **Add session-grouped export.** Group on `session_key`, order by `round`,
   last-round-wins. Turns "every turn logged" into "every conversation exported",
   which is the unit the teacher needs.
4. **Build structural reducers (§16).** No model, no compiler, no dataset. They
   reduce context today and their outputs are cleaner training inputs tomorrow.
5. **Add typed source-unit indexing with byte provenance (§10, §12) and the
   fidelity check (§11).** Pure library code over a transcript, testable offline,
   with no serving change. This is the real foundation.
6. **Build the teacher-labeling batch job (§19–§23) on the Mac Studio.**
   Requirements → evidence → coverage, IDs only, deterministic validation and mask
   expansion. Read `training/EXPERIMENTS.md` before designing the corpus — the
   query-construction lesson in §3 is the one most likely to be re-learned the
   expensive way.
7. **Train a student selector, reusing `training/`.** The pipeline, the ONNX
   export path and the in-process CPU inference all exist and generalise.
8. **Shadow-evaluate against full context** — which needs the behavioural
   benchmark `TODO.md` already owns, since a compiler is exactly the change whose
   damage a golden-snapshot diff cannot detect.
9. **Deploy behind confidence bands (§30), low-risk content first**, with the
   assembly order of §4 preserved.

Steps 1–5 are worth doing whether or not a learned selector is ever trained.
