# TODO

Deliberately-deferred work: things we have decided to do, but not yet. Each entry
names its **trigger** — the condition that makes it worth doing — because the
reason something is deferred is usually more useful than the task itself.

This file is *not* covered by `tests/unit/test_docs_drift.py`. Everything here is
future tense by construction, so nothing in it is pinned to the code. Check any
claim about today's behaviour against the source before acting on it.

---

## Prove declaration compliance before compaction spends anything

**Trigger: before #60's producer is switched on — i.e. before `context_compaction.enabled`
is ever true in a real deployment.**

The turn-finality declaration (#61) shipped as a PROBABILISTIC contract: the base prompt
asks the model to open every reply with `[[DAINTREE:FINAL]]` or `[[DAINTREE:TOOLS]]`, and
the backend strips it and acts on it. Nothing yet shows the serving model actually complies.
The unit tests prove what happens WHEN a marker arrives; a fake emits it, so they cannot
speak to whether `gemma-4-31b` does.

That gap is cheap today and expensive later. With `NullCompactor` a wrong or missing
declaration costs only the prompt bytes. Once a producer exists, a false `FINAL` buys a
discarded upstream call, and a `FINAL` that is *believed* freezes a conversation prefix
mid-task — and append-only means it does not unfreeze.

The `response_declaration` log line already emits the raw material, content-free: `declared`,
`had_tool_calls`, `finish_reason`, and `contradicted`. What is owed is reading it — over real
traffic, broken out by turn shape: final prose, tool-only rounds, prose-plus-tools, JSON-mode
turns, continuation rounds, and the adversarial cases (a user asking what the marker means, a
prompt-injection attempt targeting it). A missing-declaration rate is tolerable and simply
costs the optimisation; a CONTRADICTION rate is the number that gates enabling anything.

Also unproven here: the CLI has never parsed a `compaction` event. The vendored submodule was
not touched by #61, so the only client's handling of the new event is entirely untested. That
is fine while the event can never be emitted, and is a blocker the moment it can.

---

## Account authentication and sign-in

**Trigger: now. This is the next piece of work, not a someday.**

The door is currently open. The server holds the upstream credential
(`CEREBRAS_API_KEY`) and funds every turn from it, and `auth.py` serves a request
with no `Authorization` header as an anonymous principal. Combined with a public
Cloud Run service that is an open, Daintree-funded endpoint with no rate limiting
— which is the accepted state for this stage and the reason this entry is first.

**The seam already exists**, deliberately, and nothing about it is a shim:

- `auth.authenticate` is the one place a request becomes a `Principal`. It
  currently ignores the `Authorization` header entirely — see `docs/KEY_HANDLING.md`
  for why *ignore* rather than *optional*: a CLI mid-migration is still sending
  its old OpenRouter key, and reading it would forward that key to Cerebras and
  blame the caller for the rejection. When accounts land, this function starts
  reading the header again and the rest of the server does not move.
- `ProviderChatRequest.api_key` still overrides the server's default in
  `CerebrasProvider._resolve_key`. So a per-account credential is a **value**
  flowing through existing plumbing, not new plumbing. Benchmarks and live tests
  are its only callers today, which is what keeps it from rotting before it is
  used.

What has to be decided rather than merely built: whether an authenticated caller
still spends Daintree's key (an account is an entitlement) or brings their own
(an account is a credential mapping). The code supports either; the tests that
enumerate the open door are the ones that must start failing when this
lands, and they are written to be found:
`test_every_authorization_header_is_ignored`,
`test_no_route_hands_a_caller_supplied_key_to_the_provider`,
`test_every_route_ignores_an_authorization_header_entirely`, and the smoke
script's "Authentication posture" block.

---

## Watch `gemma-4-31b`'s replacement

**Trigger: when Cerebras retires the preview model.** Announced, not scheduled.

`config.CEREBRAS_MODEL` is the whole code change, but not the whole job — see
`docs/PROVIDER_POLICY.md` §9. Two things beyond output quality have to be
re-decided:

1. **Thinking posture.** Nothing in this server thinks, and that is a property of
   *this model* rather than a flag: `gemma-4-31b` does not reason, and Cerebras
   offers no way to switch reasoning off (a bare `reasoning_effort` string with no
   "off" value). A reasoning-capable replacement is therefore a deliberate posture
   decision with a real latency cost, not a drop-in.
   `tests/live/test_live_cerebras.py::test_live_serving_model_reports_no_reasoning_tokens`
   asserts zero reasoning tokens so this cannot be missed silently.
2. **Prompt budgets and the baseline.** The per-file budget tests and
   `benchmarks/baseline.json` are sized against this model's context window and
   its behaviour under the current prompts. Refresh the baseline deliberately
   (`python -m tools.baseline capture`) and explain the move.

---

## Retrain the selector so a reserved candidate can actually be kept

**Trigger: the next selector retrain — this rides along with it, and should not
be a retrain of its own.**

`skills/local/candidates.py` now guarantees that an active skill dense retrieval
missed still reaches the set-wise head. That is a policy change and it is
complete. Whether the head then RETAINS such a candidate is a weights question,
and the shipped artifact was trained before reservation existed, so today it does
not. Measured by blending one active skill's retrieval vector toward a target
cosine and re-running the selection ("how is that going?", that skill loaded):

    cosine   p(skill)   cardinality   admitted
     0.40      0.994         1         yes
     0.30      0.989         0         no
     0.10      0.613         0         no

The per-candidate probability tracks the cosine feature hard, and the
CARDINALITY head calls the turn empty from 0.30 down even while the skill still
scores 0.989. So a genuinely low-cosine reserved candidate is seated, scored, and
then dropped by a count of zero.

Nothing is broken by this. Reservation never fires on the live 38-skill catalog
(`test_reservation_stays_dormant_on_ordinary_follow_ups_today` pins that) and the
canonical evaluation is unchanged at 164/177. It is inert, not wrong.

**The training half is already done.** `training/train_setwise.py` builds its
ballots with the same `assemble_candidates`, so reserved candidates now appear in
training rows carrying `active=1`, `rank_norm=1.0` and a real low cosine — the
exact combination the current weights have never seen. A retrain picks it up with
no further code change. What to check afterwards: active-skill retention on
continuation rows where the ballot reserved, against topic-shift eviction on rows
where it also reserved. Those two move in opposite directions and the point is to
buy the first without paying for the second.


---

## Widen the shortlist to k=64

**Trigger: when the catalog passes ~200 skills.** Measured, not guessed — and
measurably NOT needed before then.

`bench scale` sweeps candidate recall across synthetic catalog sizes
(`training/EXPERIMENTS.md`, "Catalog scale"). Counted over the 154 cases that
have a required skill, `k=32` misses 0 at 200, **2 at 500** and 4 at 1,000;
`k=48` is clean at 500 and misses 3 at 1,000; `k=96` is clean throughout.

So this is a real change with a real trigger rather than a standing worry. It is
not free: `k` is baked into the exported head's graph (MultiheadAttention bakes
the sequence length into its reshapes), so moving it is a retrain plus a
re-export, and the admission floors are calibrated to the resulting sigmoid and
must be re-derived with `training/calibrate_floors.py`. Fold it into the retrain
that is already owed for candidate reservation rather than spending a separate one.

Re-run `bench scale` first. The numbers above are against today's catalog padded
with derived distractors; the real catalog at 200 skills will have its own
confusion structure, and the point of the harness is that the decision comes from
a measurement taken at the time.

## Give the foundation tier its own domains

**Trigger: the same ~200-skill mark, or sooner if `foundation-only` regresses.**

The scale sweep's per-category breakdown shows `foundation-only` losing a case at
500 skills (5/6) and holding there, and `daintree.foundation` is the deepest
required skill at every size — rank 26 of 200 under int8, rank 31 under fp32,
against a 32-slot shortlist. The cause is structural rather than a bug: there is one broad `daintree.foundation`, its
routing document is deliberately the most generic in the catalog, and a generic
document is the one a crowd of near-neighbours buries.

Domain foundations (`daintree.orchestration.foundation`, and so on) would each
carry a narrower, more distinctive routing surface. The catalog already supports
them — `Catalog.domain_foundation` walks UP the dotted namespace nearest-first,
so a domain foundation is picked up with no code change — and `active_set.py`
already pairs a selected specific with whichever foundation it resolves to.

What has to be decided rather than built is the ontology: which domains earn one,
and how the prompt-byte budget behaves when a two-domain task pulls in two
foundations plus two specifics against `max_active_skills` of 3. That budget
question is the real work here, not the routing.

---

## Role and channel markers in the selector's evidence

**Trigger: the next selector retrain — it changes the query string, so it cannot
land without one.**

`query_document` joins prior turns with spaces, so the encoder cannot tell a tool
error from an assistant plan from a user instruction. They carry very different
authority and currently read alike. Explicit role markers are the audit's V1
evidence change and the cheapest half of its V2 proposal.

The related token-cap defects are FIXED rather than deferred — see
`documents.fit_query`. The renderer now shrinks deliberately (history first, then
head-and-tail on the newest message, never the turn context) instead of letting
the tokenizer cut the tail. That needed no retrain because the default render is
returned byte-identical whenever it fits, and nothing the model has been judged
on comes close: the 9,774-row training corpus peaks at 155 tokens and the 177
benchmark queries at 130, against a cap of 256. Role markers are different — they
change every string, including the ones that fit.

## Give the docs head training rows that carry an active set

**Trigger: the next corpus generation pass.**

`tests/unit/test_training_corpus_guards.py` now cross-tabs the structural
features against the labels. The history axis is repaired — a docs positive is
about as likely with history as without. The ACTIVE axis is not: a documentation
positive is ~16x rarer on a row with an active skill (0.4%, 7 of 1,956) than
without (6.3%, 492 of 7,818).

That is the same shape as the shortcut that caused the original docs outage, on
the other axis, and it is why `setwise.py` still runs a whole second pass over
the bare user question to re-read the docs logit — ~8ms, on every turn whose bare
question differs from its contextual query, which is most turns with any history
and not only docs ones. With seven active-bearing positives the head is severely
underexposed to that combination rather than never having seen it, so the
workaround is load-bearing rather than a historical note.

The fix is generation, not thresholds: docs questions asked mid-workflow, with
one and two skills loaded, across the same phrasings the bare rows already cover.
The guard is a ratchet, not a tolerance: the ceiling is 20x against a measured
17.6x, so losing even one of the seven positives fails it. The next augmentation
cannot improve something else at this one's expense.

---

## Gate routing traps per EDGE, not per skill

**Trigger: when the catalog passes ~75 skills, or sooner if a confusion pair
starts failing the benchmark.**

`bench coverage` requires a skill with any declared neighbour to have one
routing-trap case that excludes one of them. Per SKILL, not per edge — so a skill
that names six neighbours discharges the obligation with a single trap against
whichever one somebody wrote a case for, and the other five confusion claims are
readable but untested.

That was invisible until the carve-outs were rewritten to name ids: the rewrite
added 19 directed edges and moved the trap counts for four of the six skills not
at all. The graph is now machine-readable, which is the precondition for gating
it properly and was the point of the rewrite; the gate itself has not moved.

What it needs is edge metadata on the trap cases — a `confuses_with: [source,
target]` field, or an inference from a case that requires one and forbids the
other. Inferring it from `allowed` alone does not work: nearly every case
excludes nearly every unrelated skill, so every trap would appear to cover every
edge. `bench lint` ranks which edges to write cases for first.

---

## Stage the tool-projection rollout, then delete the percentage

**Trigger: `TOOL_PROJECTION_MODE=enforced` has held at
`TOOL_PROJECTION_ROLLOUT_PERCENT=100` for a sustained window with no rollback.**

The gate, the telemetry and the fail-open paths all shipped with issue #64. What
did NOT ship is the rollout itself, because it is a production measurement rather
than a code change: the default stays `shadow`, and every deployment is measuring
until somebody deliberately turns it on.

**Promote** a stage (1 → 5 → 25 → 50 → 100) only when, over ≥24h and ≥1,000
enforced turns including ≥200 measured tool-calling rounds:

* `outside_proposed` is empty on every ENFORCED round — a nonzero one means the
  model reached for something the turn had already taken away;
* every `tool_projection_reason` other than `selector_degraded` is absent from the
  fail-open population, and the aggregate fail-open rate is under 1%;
* `measured=false` rounds are under 0.1% (an unmeasured round counted as clean is
  a false all-clear, which is the one thing this number may not be);
* no projection-attributable provider 4xx.

**Read `outside_proposed` from the SHADOW window when deciding the FIRST stage.**
An enforced turn cannot call a tool it was never offered, so enforced traffic
grades itself clean by construction and cannot authorize its own expansion. That
is why `tool_projection_result` stamps `enforced` on every record.

**Roll back** — `TOOL_PROJECTION_ROLLOUT_PERCENT=0`, or `TOOL_PROJECTION_MODE=disabled`
for the bigger hammer; both are Cloud Run environment updates, a config-only
revision with no image rebuild — on any confirmed projection-caused failed turn,
any `forced_tool_missing` / `unresolved_tool_missing`, or any `outside_proposed`
call on an enforced round.

**Note `cloudbuild.yaml` sets its env with `--set-env-vars`**, so the next release
resets a hand-set rollout value. That resets it to `shadow`, which is the safe
direction — but it means a stage held by hand does not survive a deploy, and the
value belongs in the build's own substitutions once a stage is meant to stick.

Two things follow the rollout out of the tree. The **percentage** is scaffolding
for exactly this graduation and should be deleted at a stable 100% rather than
left as config nobody sets; the three-mode switch stays, because the kill switch
is the permanent part. And the **79.3 KiB → 34.9 KiB** figure in the issue needs
replacing with what production actually reports (`tools_full_bytes` vs
`tools_projected_bytes`, p50 over healthy assistant turns) — the deterministic
projection measures 79.3 KiB → 22.2 KiB skill-less (72%) and → 46.8 KiB with a
skill and its foundation (41%). The median depends entirely on the mix of those
two shapes in real traffic, which is the thing only production can report.

---

## Turn-boundary checkpointing for `skill.step.advance`

**Trigger: when the CLI's `upsertStep` learns to backfill.** Not scheduled.

`loaded_skills.md.j2` used to tell the model to call `skill.step.advance` ONLY at a
turn boundary — one checkpoint instead of one call per step, which is materially
cheaper on a long runbook. It contradicted the CLI tool description, the
`skill_step_consistency` task and that task's whole benchmark corpus, all three of
which describe a per-step record, so the scaffold was the surface that moved
(`tests/unit/test_policy_contradictions.py` now pins the agreement).

The scaffold was the outlier, but it was not the worse idea. What blocks it is the
data model: `internal/tools/skill/skill.go`'s `upsertStep` records ONLY the step
named in the call. Be precise about what that breaks — `currentStep` DOES advance to
`nextStep`, so a consumer that resumes from the frontier alone still resumes
correctly. What goes wrong is the per-step ledger beside it: complete steps 1-5 in
one turn, checkpoint at 5, and the `steps` array `skill.run.get` returns has 2-4
missing entirely. Anything reading that array — a wake turn reconstructing what was
done, and the consistency auditor, to which the same gap looks exactly like an
illegitimate jump — is reading holes as "never done".

Backfill is the cleanest migration, not the only possible one: `steps` could instead
be redefined as deliberately sparse, with the auditor and every reader taught that a
gap means "not individually recorded" rather than "not done". That contradicts the
current tool contract, which is why backfill is the assumption below.

Reviving it needs, in order:

1. CLI: `upsertStep` marks the intervening steps (a `throughStep`, or backfill from
   `prevCurrent + 1`), so the progress array has no holes.
2. `skill_step_consistency`: a forward jump stops being evidence of a mistake, and
   the corpus case `consistency-illegit-jump` needs a replacement signal.
3. The scaffold, the CLI tool description and this repo's contradiction gate move
   together, in one release pair.

Doing (3) alone is what created the contradiction in the first place.

---

## Reason on hard turns, once something can tell us which they are

**Trigger: the next selector retrain, if calibration is added to it.** Not
scheduled on its own.

The serving model is non-thinking on every call. The wanted behaviour is that it
reasons when routing is weak — no runbook, or one the classifier is unsure of —
and stays fast when a skill matches cleanly, so quality is bought only where it is
needed. The MECHANISM is nearly free: DeepSeek V4 Flash reasons unless told not
to, `ProviderChatRequest.thinking_disabled` already exists, and it is decided per
request. What is missing is a trustworthy input.

`SelectionDecision.confidence` is not it, and that is measured rather than
suspected — see "Selector confidence cannot gate reasoning" in
`training/EXPERIMENTS.md`. The head scores 1.00 on turns where it MISSED a required
skill just as readily as on turns it got right, so a 0.65 threshold reasons on 1 of
154 turns and catches 8% of the errors. Margin and the cardinality head are no
better.

What would make this shippable, cheapest first:

1. Train the head for calibration as well as rank, and gate on measured ECE or a
   Brier score over HELD-OUT cases. The current corpus cannot answer this — the
   head has effectively seen it.
2. Or give it a discrete difficulty target (`clear` / `ambiguous` / `insufficient`)
   that is fitted to the question actually being asked, instead of reading a
   ranking score as a probability.

Whichever lands, note the continuation problem before wiring it: the selector does
not run on tool-continuation rounds, so a turn that starts reasoning has no fresh
signal on its later rounds. The posture has to ride the state token or be fixed for
the turn — flipping mid-turn means a model that reasons about a plan and then
executes it without.

---

## Shrinking `daintree.foundation`: measured, and the obvious target is wrong

**Trigger: a behavioural eval for the main assistant.** Blocked on that, not on
appetite.

The foundation is 14.7 KB of body and 25 `requiredTools`, paired onto almost every
Daintree turn, so it is the largest single text lever in the catalog and an obvious
place to cut. Two measurements say the obvious cut is the wrong one.

**Its tool list is not padding.** Every one of the 25 is named by the body, so tools
and text can only move together. Of the 25, **15 are already in the always-available
core** (`tool_projection.ALWAYS_AVAILABLE` + `SKILL_SCAFFOLD_TOOLS`), so declaring
them costs zero projection bytes. The foundation ADDS 18.8 KiB of schemas through
ten tools, half of that in two: `watcher.terminal.create` (5.3 KiB) and
`terminal.extract` (4.4 KiB).

**The standard test for trimming it does not apply.** The advice is "can a
foundation-ONLY turn legitimately need this capability?" — and by that test the
supervision and wait tools should go, since the foundation's own `whenToUse` routes
watching to `watch-outcome` and cohorts to the orchestration skills. But the
foundation is loaded ALONE on **7% of benchmark turns** (12 of 177). It is a layer,
not a standalone skill. Cutting to fit the 7% removes the concrete mechanism from
the supervision, finish-detection and bounded-wait rules on the other 93%, where
those rules are the point of loading it. The right test for a layer is "does the
paired turn need this", and by that test the list is close to correct.

What a real reduction needs is the thing that is actually missing: an end-to-end
behavioural eval for the assistant prompt. Body text can then be moved into specific
skills and the trade measured, instead of trading a known 18.8 KiB against an
unmeasured quality risk. Until that exists, cutting the foundation is a guess with a
number attached.

A false lead worth recording, because it looks like a bug every time: twelve skills
reference fifteen tools they do not declare, which reads as runbooks steering the
model at tools enforcement would withhold. It is not. Those references are
deliberate and already classified by hand in
`tests/unit/test_tool_projection.py::_REVIEWED_UNDECLARED` — most are PROHIBITIONS
(`fan-out-work` names `terminal.awaitAll` precisely to forbid it) or EXPLANATORY
(describing how a sibling tool differs). Declaring them would be worse than the
apparent bug: it would offer the model exactly what the runbook tells it not to
reach for. A regex cannot tell "call X" from "never call X", which is why that set
is reviewed by a person and why a detector built on backtick matching should not be
added beside it.
