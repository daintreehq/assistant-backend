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
speak to whether the serving model does.

**Read the settled-prefix entry below before starting this one.** They are two designs
for one job and only one can govern #60: this entry gates a producer that BELIEVES the
model's prediction, while the other proposes a producer that never asks for one. If the
settled-prefix design is taken, this entry is superseded and the measurement below is not
owed — there is nothing left to be right or wrong about. It survives because the
predictive optimisation is still the cheaper design when the marker IS honoured, and
nobody has measured whether it is.

The gap is cheap today and expensive later. `NullCompactor` now omits the partial
entirely, so a missing declaration currently costs nothing at all — not even the prompt
bytes it used to. Once a producer exists, a false `FINAL` buys a discarded upstream call,
and a `FINAL` that is *believed* freezes a conversation prefix mid-task — and append-only
means it does not unfreeze.

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

## Turn account authentication on

**Trigger: a provisioned staging Supabase project and a CLI that can complete a
login. The backend half is done; nothing here is a code task.**

The door is still open, and now that is a SETTING rather than an absence.
`AUTH_MODE` defaults to `open`, under which the `Authorization` header is not read
at all; `observe` verifies a present credential and changes nothing a caller can
see; `enforce` requires one. All three are built and tested
(`tests/integration/test_account_auth.py`), and the question this entry used to
pose — whether an authenticated caller spends Daintree's key or brings their own —
is answered: an account is an ENTITLEMENT. `Principal` carries an identity and no
credential, and the server funds every turn from `TOGETHER_API_KEY` regardless.

What is left on THIS side is not code — but two entries below are, and they are
sequenced into the list:

1. Provision a staging Supabase project, enable its OAuth server with asymmetric
   signing keys, and register the Assistant as a public client with the exact
   loopback callback (`config.OAUTH_REDIRECT_URI`).
2. Set the `SUPABASE_*` and `DAINTREE_*` values on the staging deployment. Getting
   any of them wrong fails readiness rather than a request.
3. Move to `observe` and read `auth_observed` for a while. It is the only mode that
   produces evidence without changing a verdict, and skipping it wastes the one
   safe measurement available. Not literally free: a well-formed token costs a
   verification and, on a cold cache, a JWKS fetch, so watch latency as well as
   the outcome counts.
4. Then `enforce`. On its own that still means "a genuine, unexpired token" and
   not "a live session" — see the entry below, which entitlement only partly
   overtakes, and only once entitlement is actually switched on.
5. Then `ENTITLEMENT_MODE=observe`, and only then `enforce`. Readiness enforces
   the DEPENDENCY — neither is permitted without `AUTH_MODE=enforce` and a URL —
   but not the sequence: nothing stops a deployment going from `off` straight to
   `enforce`, so taking `observe` first is a discipline rather than a gate.

A fifth step used to sit at the end of this list: teach the CLI the
`auth_permission_denied` code, without which the backend's second 403 fell through
to generic handling and read as "sign in again" — the exact loop the account codes
exist to prevent. It is done. The pin now names a CLI carrying the whole account
taxonomy, and the cross-repo gate that skipped for want of it runs.

**Entitlement has since landed and needed neither.** `ENTITLEMENT_MODE` stages the
billing question on its own axis, `services/entitlement.py` asks the WEBSITE — which
owns the Polar relationship — and the answer is cached in process for at most 60
seconds. The database and the Redis this entry expected were for holding
subscription state here; not holding it turned out to be the design rather than a
compromise, and the per-instance cache is sound because every entry is at most its
TTL old however many instances are warm.

What remains of that body of work is USAGE accounting: the server records what a
turn cost against a pseudonymous account and enforces no ceiling, because numeric
Standard and Pro allowances are an undecided product question. `usage_limit_reached`
stays defined and unreachable until somebody decides what the numbers are.

---


## Check that a verified session still exists

**Trigger: before `AUTH_MODE=enforce` reaches anyone real. Verification without
it is a 60-minute window in which logout does nothing.**

`services/supabase_auth.py` establishes that a token is genuine and unexpired. It
cannot establish that the session behind it still exists, because nothing reaches
back into an already-minted JWT: after a logout, a grant revocation or a security
action, the signature keeps verifying until `exp`. With one-hour access tokens
that is up to an hour of accepted requests on a credential the user believes they
revoked.

The master spec's design is a `session_id` lookup against Supabase's own session
table, cached (60s positive, 5s negative) with a bounded stale-positive grace on a
database outage and `auth_dependency_unavailable` — never a fake logout — when
there is no evidence either way. `auth_session_revoked` already exists in the
error taxonomy and the CLI already maps it to "delete the local credential", so
the wire contract for this is finished; only the lookup is missing.

**Partly overtaken by entitlement, and it is worth being exact about which part —
including what this side of it can actually promise.**

The locked contract says the website's entitlement endpoint performs a fresh
Supabase user check. THIS server does not verify that and cannot: all it does is
map a 401 from that endpoint — against a token it has already verified locally —
to `auth_session_revoked` (`services/entitlement.py`). The mapping is tested
against a mocked 401; whether a revoked session actually produces one is the
website's half of the contract and belongs to the cutover matrix.

Where the website does behave as agreed, the window on the routes that CALL it
stops being `exp` and becomes the cache TTL — a refusal purges the entry rather
than waiting it out, though "at most 60 seconds" is the TTL rather than a strict
wall-clock bound, since a lookup already in flight can be up to five seconds older
than the entry it writes.

It covers `/respond` and `/tasks` only under `ENTITLEMENT_MODE=observe` or
`enforce` — under `off` those routes ask nothing at all — and `/v1/daintree/account`
whenever a URL is configured. It is a side effect of asking a different question
rather than a revocation check, so identity-only deployments keep the full `exp`
window.

A direct `session_id` lookup would close it everywhere and needs the database this
service does not have. It is now a smaller and less urgent piece of work than this
entry was written for.

---

## Move the CLI onto the restatused provider-account error

**Trigger: the same release that ships the current backend. The backend half has
LANDED; this entry is now only the CLI's side of it, and the two must not reach
users apart.**

`provider_invalid_api_key` answered HTTP 401 for as long as it forwarded the
upstream's status. The key is the server's own, so that made an operator fault
look like a caller credential problem, and it was the one 401 on this surface
with no `WWW-Authenticate` — a 401 that cannot honestly challenge was never a
401. It is now **503 / `api_error`**, beside `provider_not_configured`: the same
operator fault at two moments, never set and no longer accepted. Still not
retryable, because waiting does not fix a key.

`subscription_required` and `subscription_inactive` were typed
`authentication_error` because the OpenAI taxonomy this envelope borrows has no
billing bucket. Both are states where authentication SUCCEEDED, so the type
contradicted the code. They now carry **`billing_error`**, a new member of the
documented type union.

**What the CLI still owes — exactly one behavioural line.**
`internal/backend/client.go`'s `taskMayHaveBilled` has a numeric arm matching 401
and 403 and returning false. Under a 503 `provider_invalid_api_key` falls through
to `return true`, so the CLI would start reporting every rejected-server-key task
as possible unknown spend. The fix is to extend the arm that already keys on
`CodeProviderInsufficientCredit` to include `CodeProviderInvalidAPIKey` — the
neighbouring code already has exactly this treatment. Its fixtures move with it
(`internal/backend/account_test.go`, `internal/backend/cost_test.go`), and
`internal/backend/errors.go` carries a now-wrong `// 401 upstream` comment.

Everything else in the CLI was verified inert against the pin at the time of the
change, by reading the source rather than assuming the contract: `isRetriable`
consults `deterministicUpstreamCodes` before any 5xx-is-retryable arm, `IsAuth()`
short-circuits on the provider-account codes before looking at a status,
`AuthRemedy()` gates on 401 so a 503 never reaches it, and `Error.Type` is read
in exactly one place (`IsRateLimited`) against a different literal. The envelope
decodes leniently, so the new type is not a wire break.

**One mismatch deliberately left alone.** `provider_insufficient_credits` is a
402 typed `authentication_error` and does NOT become `billing_error`. That type
names the READER's billing state; this code is the DEPLOYMENT's upstream
balance, and a consumer rendering it as a billing problem would send a caller to
their own portal over an account they do not own — the same class of failure the
split codes exist to prevent. Its residual type mismatch is real and is the
lesser of the two evils until the envelope grows a way to say "billing, but not
yours".

---


## Refuse `AUTH_MODE=open` in production at the cutover

**Trigger: the release that sets production to `enforce`. Not the release that
made `enforce` possible — those are different days, and an earlier revision of
this entry got that wrong.**

`Settings.auth_problems()` deliberately does NOT implement the master spec's rule
that `open` and `observe` are refused in production. The spec is right about the
destination and wrong about today: `open` IS the deployed production posture, it
is documented as such in `docs/DAINTREE_API.md`, and `infra/gcp/smoke.sh` asserts
it. Adding that gate now would fail `/readyz` on the very next release and the
revision would never take traffic.

This entry previously said the gate had to land in the same commit as the
verification code, on the reasoning that lifting the "mode not implemented"
refusal without it was a regression. That was a mistake, and worth naming rather
than quietly editing: it conflated the code EXISTING with the fleet being SWITCHED
to it. Between those two events `open` is not merely acceptable in production, it
is the only correct value — the verifier is built and tested, and no Supabase
project is provisioned for it to verify against.

The gate belongs to the release that actually flips production, and it is what
stops the fleet silently sitting on `open` afterwards.

---


## Retrain the selector so a reserved candidate can actually be kept

**Trigger: the next selector retrain — this rides along with it, and should not
be a retrain of its own.**

`runbooks/local/candidates.py` now guarantees that an active runbook dense retrieval
missed still reaches the set-wise head. That is a policy change and it is
complete. Whether the head then RETAINS such a candidate is a weights question,
and the shipped artifact was trained before reservation existed, so today it does
not. Measured by blending one active runbook's retrieval vector toward a target
cosine and re-running the selection ("how is that going?", that runbook loaded):

    cosine   p(runbook)   cardinality   admitted
     0.40      0.994         1         yes
     0.30      0.989         0         no
     0.10      0.613         0         no

The per-candidate probability tracks the cosine feature hard, and the
CARDINALITY head calls the turn empty from 0.30 down even while the runbook still
scores 0.989. So a genuinely low-cosine reserved candidate is seated, scored, and
then dropped by a count of zero.

Nothing is broken by this. Reservation never fires on the live 38-runbook catalog
(`test_reservation_stays_dormant_on_ordinary_follow_ups_today` pins that) and the
canonical evaluation is unchanged by RESERVATION. (The absolute number moved
from 164/177 to 162/177 with the runbook rename, for an unrelated reason — see
the entry below.) It is inert, not wrong.

**The training half is already done.** `training/train_setwise.py` builds its
ballots with the same `assemble_candidates`, so reserved candidates now appear in
training rows carrying `active=1`, `rank_norm=1.0` and a real low cosine — the
exact combination the current weights have never seen. A retrain picks it up with
no further code change. What to check afterwards: active-runbook retention on
continuation rows where the ballot reserved, against topic-shift eviction on rows
where it also reserved. Those two move in opposite directions and the point is to
buy the first without paying for the second.


---

## Retrain the selector on the renamed routing text

**Trigger: the next selector retrain — this rides along with it. Do not retrain
solely for this; 2 cases is not worth an artifact churn on its own.**

The skill -> runbook rename edited `whenToUse`/`summary` on 13 of 38 runbooks, and
those two fields ARE the routing document (`runbooks/local/documents.py`). The shipped
encoders were trained on the pre-rename text, so 13 documents now embed slightly off
the space the weights learned. `vectors.npz` was re-embedded through `sync()` at the
same commit, so the shipped cache is consistent — this is about the WEIGHTS, not the
cache.

Measured with `bench local`, same 177 cases, before and after the rename:

    recall-pass   164/177  ->  162/177
    strict        146/177  ->  146/177
    candidate_miss    0    ->      0      (the CI gate; still clean)
    extra            27    ->     27
    docs          19/21    ->  19/21

Exactly two cases regressed and none improved:

    cancel-one-named-watcher-is-foundation   rank_miss  p=0.356  daintree.foundation
    injection-in-tool-output                 rank_miss  p=0.149  daintree.foundation

Both expect `daintree.foundation`, and its `whenToUse` changed by exactly two words
("every daintree.* skill" -> "runbook", "their own skill" -> "runbook"). The prose is
not worse; the embedding simply moved. That is why the fix is a retrain and NOT a
rewrite: hand-tuning a foundation's trigger text to recover two benchmark cases is
fitting the prose to the artifact, which makes the text worse and the measurement
meaningless. `.claude/skills/retrain-selector` is the mechanism.

What to check afterwards: that both cases come back, and that nothing in the
`foundation-only` or `routing-trap` categories pays for it.


---

## Widen the shortlist to k=64

**Trigger: when the catalog passes ~200 runbooks.** Measured, not guessed — and
measurably NOT needed before then.

`bench scale` sweeps candidate recall across synthetic catalog sizes
(`training/EXPERIMENTS.md`, "Catalog scale"). Counted over the 154 cases that
have a required runbook, `k=32` misses 0 at 200, **2 at 500** and 4 at 1,000;
`k=48` is clean at 500 and misses 3 at 1,000; `k=96` is clean throughout.

So this is a real change with a real trigger rather than a standing worry. It is
not free: `k` is baked into the exported head's graph (MultiheadAttention bakes
the sequence length into its reshapes), so moving it is a retrain plus a
re-export, and the admission floors are calibrated to the resulting sigmoid and
must be re-derived with `training/calibrate_floors.py`. Fold it into the retrain
that is already owed for candidate reservation rather than spending a separate one.

Re-run `bench scale` first. The numbers above are against today's catalog padded
with derived distractors; the real catalog at 200 runbooks will have its own
confusion structure, and the point of the harness is that the decision comes from
a measurement taken at the time.

## Give the foundation tier its own domains

**Trigger: the same ~200-runbook mark, or sooner if `foundation-only` regresses.**

The scale sweep's per-category breakdown shows `foundation-only` losing a case at
500 runbooks (5/6) and holding there, and `daintree.foundation` is the deepest
required runbook at every size — rank 26 of 200 under int8, rank 31 under fp32,
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
foundations plus two specifics against `max_active_runbooks` of 3. That budget
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
positive is ~16x rarer on a row with an active runbook (0.4%, 7 of 1,956) than
without (6.3%, 492 of 7,818).

That is the same shape as the shortcut that caused the original docs outage, on
the other axis, and it is why `setwise.py` still runs a whole second pass over
the bare user question to re-read the docs logit — ~8ms, on every turn whose bare
question differs from its contextual query, which is most turns with any history
and not only docs ones. With seven active-bearing positives the head is severely
underexposed to that combination rather than never having seen it, so the
workaround is load-bearing rather than a historical note.

The fix is generation, not thresholds: docs questions asked mid-workflow, with
one and two runbooks loaded, across the same phrasings the bare rows already cover.
The guard is a ratchet, not a tolerance: the ceiling is 20x against a measured
17.6x, so losing even one of the seven positives fails it. The next augmentation
cannot improve something else at this one's expense.

---

## Gate routing traps per EDGE, not per runbook

**Trigger: when the catalog passes ~75 runbooks, or sooner if a confusion pair
starts failing the benchmark.**

`bench coverage` requires a runbook with any declared neighbour to have one
routing-trap case that excludes one of them. Per RUNBOOK, not per edge — so a runbook
that names six neighbours discharges the obligation with a single trap against
whichever one somebody wrote a case for, and the other five confusion claims are
readable but untested.

That was invisible until the carve-outs were rewritten to name ids: the rewrite
added 19 directed edges and moved the trap counts for four of the six runbooks not
at all. The graph is now machine-readable, which is the precondition for gating
it properly and was the point of the rewrite; the gate itself has not moved.

What it needs is edge metadata on the trap cases — a `confuses_with: [source,
target]` field, or an inference from a case that requires one and forbids the
other. Inferring it from `allowed` alone does not work: nearly every case
excludes nearly every unrelated runbook, so every trap would appear to cover every
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
projection measures 79.3 KiB → 22.2 KiB runbook-less (72%) and → 46.8 KiB with a
runbook and its foundation (41%). The median depends entirely on the mix of those
two shapes in real traffic, which is the thing only production can report.

---

## Turn-boundary checkpointing for `runbook.step.advance`

**Trigger: when the CLI's `upsertStep` learns to backfill.** Not scheduled.

`loaded_runbooks.md.j2` used to tell the model to call `runbook.step.advance` ONLY at a
turn boundary — one checkpoint instead of one call per step, which is materially
cheaper on a long runbook. It contradicted the CLI tool description, the
`runbook_step_consistency` task and that task's whole benchmark corpus, all three of
which describe a per-step record, so the scaffold was the surface that moved
(`tests/unit/test_policy_contradictions.py` now pins the agreement).

The scaffold was the outlier, but it was not the worse idea. What blocks it is the
data model: `internal/tools/skill/skill.go`'s `upsertStep` records ONLY the step
named in the call. Be precise about what that breaks — `currentStep` DOES advance to
`nextStep`, so a consumer that resumes from the frontier alone still resumes
correctly. What goes wrong is the per-step ledger beside it: complete steps 1-5 in
one turn, checkpoint at 5, and the `steps` array `runbook.run.get` returns has 2-4
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
2. `runbook_step_consistency`: a forward jump stops being evidence of a mistake, and
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
and stays fast when a runbook matches cleanly, so quality is bought only where it is
needed. The MECHANISM is nearly free: DeepSeek V4 Flash reasons unless told not
to, `ProviderChatRequest.thinking_disabled` already exists, and it is decided per
request. What is missing is a trustworthy input.

`SelectionDecision.confidence` is not it, and that is measured rather than
suspected — see "Selector confidence cannot gate reasoning" in
`training/EXPERIMENTS.md`. The head scores 1.00 on turns where it MISSED a required
runbook just as readily as on turns it got right, so a 0.65 threshold reasons on 1 of
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
core** (`tool_projection.ALWAYS_AVAILABLE` + `RUNBOOK_SCAFFOLD_TOOLS`), so declaring
them costs zero projection bytes. The foundation ADDS 18.8 KiB of schemas through
ten tools, half of that in two: `watcher.terminal.create` (5.3 KiB) and
`terminal.extract` (4.4 KiB).

**The standard test for trimming it does not apply.** The advice is "can a
foundation-ONLY turn legitimately need this capability?" — and by that test the
supervision and wait tools should go, since the foundation's own `whenToUse` routes
watching to `watch-outcome` and cohorts to the orchestration runbooks. But the
foundation is loaded ALONE on **7% of benchmark turns** (12 of 177). It is a layer,
not a standalone runbook. Cutting to fit the 7% removes the concrete mechanism from
the supervision, finish-detection and bounded-wait rules on the other 93%, where
those rules are the point of loading it. The right test for a layer is "does the
paired turn need this", and by that test the list is close to correct.

What a real reduction needs is the thing that is actually missing: an end-to-end
behavioural eval for the assistant prompt. Body text can then be moved into specific
runbooks and the trade measured, instead of trading a known 18.8 KiB against an
unmeasured quality risk. Until that exists, cutting the foundation is a guess with a
number attached.

A false lead worth recording, because it looks like a bug every time: twelve runbooks
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

---

## Re-establish the privacy claim against Together

**Trigger: before any external statement about data handling.** Blocking for that,
not for engineering.

`docs/PROVIDER_POLICY.md` §4 carries Daintree's no-training claim. Under OpenRouter
the server ENFORCED it per request (`data_collection: "deny"` plus endpoint
filtering). A single host has no such lever, so the claim now rests entirely on the
host's published policy — which makes the identity of the host a privacy fact.

The section was written when Cerebras was the sole serving host and still cites
Cerebras's trust centre. That is no longer the whole picture rather than simply
wrong: Together now answers every executor/utility turn, and Cerebras has RETURNED
in a narrower role — it writes the fast-response preamble on eligible streaming
turns (`FAST_RESPONSE_MODEL`), on every eligible turn. Nobody has read either host's terms
and rewritten this section, so it stays marked unverified rather than re-vendored:
swapping in a confident guarantee nobody had checked would be the worst possible
form of this document to be wrong.

To close it: read <https://www.together.ai/privacy> and
<https://www.together.ai/terms-of-service>, and do the same for Cerebras's current
terms (not the old trust-centre citation — re-read it, since the relationship
changed). State what each host actually commits to (training, retention, caching,
sub-processors) for the content it actually receives, and say plainly which parts
Daintree can promise and which are each host's to change. This is now a two-host
privacy statement, not a one-host swap.

## Shed context instead of only refusing it

**Trigger: the first `context_window_exceeded` on real traffic, or the compaction
producer landing — whichever comes first.**

`services/context_budget.py` measures the assembled request and refuses a turn
whose INPUT alone clears the window. That is the honest half of the job: it names
a failure the caller can act on instead of spending an upstream call to be told
the same thing in worse words. It does not make an oversized turn fit.

The shape of the missing half is a priority order over semantic units, not a
truncator:

- **never dropped** — the permanent system layers, the current user instruction,
  any unresolved tool call with its result as a complete transaction, a forced
  tool, the active runbook bodies as coherent wholes, the current turn's goal, and
  enough of `startup` to know which project this is;
- **dropped last** — recent settled conversation, active workflow digests,
  relevant memories, retrieved documentation, integration instructions for tools
  actually in play;
- **shrunk first** — older settled history that has a compacted replacement,
  low-ranked documentation sections, stale runtime snapshots, excess terminal
  rows.

Two reasons it is not built yet. It is a PRODUCER of prompts rather than a check
on one, so it belongs beside compaction (#60) — which already owns "replace
settled history with a summary" and would otherwise be reimplemented here. And
the trigger matters: shedding is only worth its complexity once something says
turns are actually hitting the ceiling, and nothing does yet. The
`turn_usage.estimated_prompt_tokens` field logged beside the upstream's own
`prompt_tokens` is what will say so, and it also calibrates the characters/4
estimate the refusal currently rests on.

---

## A behavioural benchmark for the main assistant

**Trigger: before the first deliberate reduction of `main/base/*.md`,
`main/response_contract.md.j2` or `daintree.foundation`. Not before, and not as
a prerequisite for anything else.**

The golden suite answers *what bytes changed*. It does not answer whether the
model still chose the right tool, respected a permission boundary, converged,
stopped, delegated, verified success, or reported clearly — and that gap is the
reason "shrink the largest partial" keeps being deferred rather than done.
`20-tool-discipline.md` is ~9.4 KiB and the foundation ~16 KiB, both plainly
prunable and neither safely prunable, because a snapshot diff cannot tell a
removed redundancy from a removed load-bearing rule.

Shape: `benchmarks/main_behavior/`, cases in YAML, two modes. **Isolation** pins
the intended runbook set so a selector error cannot be mistaken for a prompt
regression; **end-to-end** runs real selection, projection and assembly. A
prompt edit that REDUCES a permanent layer must pass isolation, and a release
that changes the assembly or selection architecture should pass both once the
suite exists. Neither is a gate on work that does not touch those layers — the
trigger above is the whole scope. Score
deterministically wherever possible — first-correct-tool rate, forbidden-call
rate, invalid-argument rate, rounds to completion, premature finish, failure to
stop, exact-ID preservation, response-contract conformance, injection canary
leakage — and reserve a judge for report usefulness and unsupported claims.

The cases that matter first are the rules with no other guard: act in the same
reply rather than promise later work, delegate expensive search early, preserve
terminal ownership, do not close a resource nobody asked to close, distinguish
working from blocked from failed from complete, verify before declaring
completion, retain the active runbook on continuation and replace it on a topic
shift.

**Why this is not in CI**: it needs paid completions, and a suite that costs
money on every push either gets skipped or gets disabled. It belongs beside the
live provider-contract suite on a low-spend key, run at qualification
milestones, committing a content-free result artifact (build sha, prompt hash,
catalog revision, tool-inventory hash, selector artifact revision, pass counts)
so a dated claim is traceable — the gap `benchmarks/results/` being gitignored
leaves today.

---

## Compact the settled prefix instead of predicting finality

**Trigger: when #60's compaction producer is built. This is a design note for
that work, not separate work — and if it is adopted it SUPERSEDES "Prove
declaration compliance before compaction spends anything" above, which gates a
producer that believes a prediction this one never asks for.**

The shipped design asks the model to open a reply with `[[DAINTREE:FINAL]]` or
`[[DAINTREE:TOOLS]]`, and the backend acts on the prediction. It is currently
switched off entirely (`NullCompactor` omits the partial), because the serving
model was measured emitting the marker on 0 of 20 tool-calling turns.

There is a cleaner boundary available that needs no prediction at all. **At
request time, everything before the latest user instruction is already
settled.** Nothing the model is about to say can change it. So the producer can
compact THAT prefix, concurrently with the main call, on any turn — including a
tool-calling one, because the block replaces only history that was finished
before the current instruction began.

What it still owes:

- the boundary stated and pinned, not scanned for heuristically — the last user
  message in the validated transcript, or a bounded structural hint from the
  client, which owns the history;
- the existing safety rules unchanged: never compact an unresolved tool-call
  batch, the current instruction, or anything after it;
- a trigger that is economic rather than a fixed token count. Prefix caching
  changes the arithmetic — replacing a long cached prefix with a short block is
  a one-time miss against a saving taken on every later round — so the question
  is expected remaining rounds times bytes saved against one compaction call
  plus one cache miss. Capacity pressure overrides it;
- a faithfulness gate before delivery, run in shadow first: exact ID/path/PR
  preservation, user-constraint recall, completed-vs-pending distinction, no
  invented completion, no captured injection, and an offline replay showing the
  assistant can resume a scripted workflow from the block alone.

The declaration machinery does not have to be deleted for this: the scanner and
stripper cost a prefix probe and a model emitting the marker unprompted should
still not show it to a user. What changes is that nothing DEPENDS on the
prediction.

---

## Start capturing local traces for the context compiler

**Trigger: now, for the capture half. The compiler itself is gated on having
traces, and traces only accumulate with wall-clock time.**

`docs/CONTEXT_COMPILER.md` assesses an external design — a query-aware context
compiler that selects exact source units instead of summarising — against what
this repository already has. Read it before acting on anything here; it records
which parts are already built under other names, and which parts contradict
measured decisions (the assembly order, the tool-withholding boundary, the
unversioned runbook schema).

Everything downstream needs a corpus, and there is none: the trace plane is built
and correct and has **never been switched on**, so the dataset targets start from
zero. Capture is local-only and the code already enforces that — `cloudbuild.yaml`
sets no `TRACE_*` variable, `full_internal` fails `/readyz` in a hardened
environment and is refused again per-request by `eligibility.decide`, and
`TRACE_SINK=filesystem` is separately forbidden in production/staging. Nothing new
is needed to keep this off Cloud Run.

What is owed, in the order it pays off:

- **Turn capture on locally.** `ENVIRONMENT=development`,
  `TRACE_CAPTURE_MODE=full_internal`, `TRACE_SINK=filesystem`,
  `TRACE_FILESYSTEM_DIR=.tmp/traces`, `TRACE_IDENTITY_KEY` set. Zero code.
- **Trace `/v1/daintree/tasks`.** It is untraced today, so every `context_compact`
  call — a real transcript-in / reconciled-state-out pair, the closest thing to
  pre-labelled compaction data that exists — is computed and discarded. Every
  untraced call is permanently lost, which is what makes this the first code
  change rather than a later one.
- **Session-grouped export.** `training/export_context_traces.py` maps one trace
  directory to one example. Group on `header.session_key`, order by
  `header.round`, last-round-wins — the client resends the full transcript every
  round, so the last round of a session already IS the conversation. The server is
  stateless and has no session-end signal, so this is a dedupe on export, never a
  capture-time decision.
- **Emit `tool.call.observed` / `tool.result.observed`.** They exist only in
  `tests/unit/test_trace_exporter.py`, so the exporter's `_tool_graph_is_sound`
  check is inert against every real trace — and call/result pairing is exactly
  what the compiler's atomicity rule needs to be able to trust.

Two entries already point the same way and should be read together: "Capture the
exact provider wire body in traces" below (`wire_request_ref` is always `None`)
and "Shed context instead of only refusing it" above, whose priority ordering is
already most of the compiler's budget-allocation stage.

The fork this eventually forces is in `docs/CONTEXT_COMPILER.md` §7 and is worth
deciding before either side is built: the shipped `context_compact` task
*generates* reconciled state, and the compiler design says retained history must
be exact source bytes. They are not competitors at the same layer — selection over
evidence, reconciliation over a small state ledger — but nothing has tested that.

---

## Verify the serving model's real context window

**Trigger: with the live provider-contract suite below.**

`config.EXECUTOR_CONTEXT_TOKENS` is 131,072 — the published figure for DeepSeek V4
Flash, never asked of Together. That gap is why `exceeds_window` refuses only
when the input alone clears the whole window rather than at the reservation
boundary: an unverified ceiling is allowed to fail a paid turn only in the cases
no reading of it could rescue.

Verify it and two things follow: the budgeter can tighten to the reservation
boundary and start catching turns that will fail rather than only turns that
cannot possibly fit, and the `_TEMPLATE_OVERHEAD_TOKENS` guess can be replaced by
a measurement (send a known payload, read the upstream's own `prompt_tokens`, and
the difference is the template's real cost).

Related and unpinned in the same way: `_CONTEXT_LENGTH_MARKERS` in
`providers/openrouter.py` matches Together's length-rejection prose by guess. One
deliberately oversized live request settles what that host actually says.

---

## Land the CLI half of protocol 3, the runbook rename, and the preamble

**Trigger: before this branch can ship at all. Nothing here is optional — the
backend on this branch cannot serve the CLI as it is pinned today.**

The backend moved and the Go CLI has not. Concretely, the CLI must:

- **speak protocol 3.** `PROTOCOL_VERSION` is 3 and `PROTOCOL_MIN` is 3, so a
  protocol-2 client is refused with `426 unsupported_daintree_protocol`. This is
  the one that makes the branch unshippable rather than merely incomplete.
- **read `runbooks`, not `skills`** — the response block, `selection.pinned_runbook_ids`,
  the `/v1/daintree/runbooks*` routes, and the `*_runbook_*` warning codes.
- **rename its own run-tracking tools** to `runbook.step.advance` and
  `runbook.run.get`. NOTE: `tests/fixtures/cli_tool_names.txt` and
  `benchmarks/_harness/fixtures/cli_tool_inventory.json` ALREADY carry the new
  spellings — they run ahead of the pinned submodule, which
  `test_the_gate_above_retires_itself_when_the_cli_renames` documents and enforces.
  That test FAILS the moment the Go side renames; that failure is the cue to
  re-capture both fixtures and delete the pair of tests along with the header note.
- **STOP COMMITTING the `preamble` event.** It already DECODES it — the vendored
  `cli/` pin is stale, and the live repo has `sseEventPreamble` handling in
  `internal/backend/sse.go`, so the older note here claiming unknown events are
  dropped is wrong and shipping the backend half is NOT free. What it does with
  the line is the problem: `internal/backend/client.go` splices it into the
  committed assistant message (`result.Message.Content = shownPreamble + "\n\n" +
  body`), which was correct while the preamble was part of the executor's input
  and is now exactly the failure mode. Render it in the active assistant bubble
  as EPHEMERAL DISPLAY STATE and
  **never commit it to conversation history** — not on `done`, not on `error`. The
  executor never saw the line (the two calls run concurrently now), so a committed
  preamble becomes, on the next turn, an assistant turn the model has no memory of
  writing. Commit exactly what would be committed with no preamble at all. Keep the
  turn replay-safe until the first executor delta. `provisional`/`commit_on` are
  still on the payload for compatibility; a client following this rule never
  reaches a commit. At most one arrives per turn and never after the first `delta`
  — the server drops a late one — so there is no "what if it lands mid-answer" case
  to handle.
  NOTE: this REPLACES the older contract, which had the CLI join the preamble to
  the executor content with `"\n\n"` and commit one message. That was correct only
  while the preamble was part of the executor's input.
  `/v1/daintree/capabilities` now advertises the new rule and, deliberately, no
  longer advertises the old one: `respond.fast_response.commit` is `"never"`, and
  `same_assistant_message` / `join_separator` are GONE rather than set to false —
  a client still reading them was built against the sequential design, and an
  absent key is what tells it so.

Then move the submodule pin and re-capture both tool fixtures together.


## Measure what the preamble actually costs and buys

**Trigger: the stage now runs on every eligible turn of every deployment, so
this is owed on the next paid measurement window rather than gated on a rollout
step that no longer exists.**

The stage is built, gated and off. What it does to real turns is unmeasured, and
every number below needs paid calls, which is why none of it is in the offline
dashboard (`CLAUDE.md`: "anything needing a paid call" is deliberately not gated
there).

Owed, in the order the risk runs:

1. **Latency.** `p50/p90/p99 first visible preamble`, against the same turns'
   time-to-first-executor-output. Report BOTH — the trade is a large improvement in
   time-to-visible-text for a small regression in time-to-executor-output, and
   quoting the first alone would misrepresent it. Proposed SLO: p50 <= 0.8s,
   p90 <= 1.5s, p99 <= 3.0s.
2. **Drop rate.** How often the line loses the race and is discarded unshown
   (`fast_response_dropped`, reason `executor_output_first`). This is the number
   that decides whether the stage is worth its spend at all: a high drop rate means
   we are paying Cerebras for text nobody reads. Distinct from the failure rate and
   from the decline rate on purpose — all three are "no preamble" and only one of
   them is a problem.
3. **Decline rate.** How often the model answers `NONE`. Too high and the stage is
   silent on turns that wanted a line; too low and it is writing filler. There is
   no target yet, which is itself the point of measuring it.
4. **Executor non-inferiority — now free.** The executor's request is
   byte-identical whether or not the stage ran, so this no longer needs a paired
   corpus. `test_the_executor_request_is_byte_identical_in_every_mode` asserts it
   offline; nothing further is owed unless that test is ever relaxed.
5. **Repetition — MEASURED, UNSOLVED, and one fix already tried and reverted.**
   The executor cannot see the preamble, so it opens with its own announcement:
   `I'll start by reading the current state`, `I'll check the current state of the
   project`. Observed on most real turns.

   **THREE attempts, all reverted, and none of them worked.** That is the finding —
   the executor announces with the stage on, with the stage off, and with every
   prompt intervention tried so far.

   **A causal claim recorded here earlier was WRONG and is retracted.** It said
   those interventions caused tool-call syntax to leak into visible content
   (`<｜DSML｜tool_calls>`, `<tool_use>{...}`), on evidence of 2 leaks in 3 turns
   with them versus 0 in 3 without. A larger sample after reverting everything
   shows the leak at **1 in 8 turns on the same request with no intervention
   present**, so it is a pre-existing, intermittent DeepSeek behaviour that the
   small samples happened to straddle. It is a real bug — see the separate entry
   below — but it is not a reason those attempts failed, and it must not be used
   to argue against retrying this class of fix.

   **What was tried:** a conditional base-prompt partial
   (`45-opening-already-shown.md`) withdrawing the opening-clause permission that
   `40-communication.md` grants, plus a per-turn reminder in the volatile tail for
   proximity. Both were reverted, for two independent reasons and neither is a
   reason to retry the same shape:
   - **It did not work.** The executor kept announcing with the rule present. It
     also announced identically with the stage disabled, so the announcement
     is baseline behaviour rather than something the preamble provokes.
   - **Editing `20-tool-discipline.md` at the source.** That rule pairs "emit its
     call in the SAME reply" with `"I'll check the logs"` as its example, so it
     arguably teaches the announcement it is trying to bound. Adding "prefer
     emitting it with no preface at all" changed nothing about the announcements,
     so it was reverted too. The rule text stands as it was — but this is the most
     promising thread of the three, because it addresses the rule that DEMONSTRATES
     the behaviour rather than one that merely forbids it, and it was abandoned on
     a single sample plus the leak scare that has since been retracted.
   - **It changed nothing measurable.** The executor announced at the same rate
     with the partial as without it.

   What DID land, and is kept: `40-communication.md` no longer grants the opening
   round a clause for orienting the user, since the preamble now always occupies
   that slot. It does not stop the executor announcing — nothing tried so far does
   — but it is correct on its own terms, costs nothing, and showed no leak.

   So the repetition is a real, accepted cost of the concurrent design today. Do
   NOT strip executor text post hoc because it resembles the preamble; that
   removes substantive content. The measurement owed is the 5-gram overlap between
   the preamble and the first 200 executor words, and any fix needs a paid A/B
   with the DSML leak as an explicit guardrail metric.
6. **Overclaim rate on real traffic.** `validate_preamble` is measured against
   hand-written phrasings today. Shadow mode exists to produce the real corpus.
   One live rejection is already recorded (`reason=overclaim` on an edit request,
   from perfect-tense phrasing like "once the flag has been added"), which is what
   the prompt's explicit tense rule was added for. Rejections are silent by
   design — no fallback — so this rate IS the feature's availability.

7. **The executor leaks raw tool-call syntax into visible content, ~1 turn in 8.**
   Measured on the current build with no prompt intervention present: 1 of 8
   identical "Add a --verbose flag" turns put `<｜DSML｜tool_calls>` /
   `<tool_use>{…}` into the delta stream instead of emitting a structured tool
   call. The user sees markup mid-reply.

   Pre-existing and independent of the fast-response work — it was found while
   investigating that, and mis-attributed to it at first (see 5 above). Nothing in
   this repo detects it: the output lint does not look for provider tool-call
   syntax, and `content_parts` happily accumulates it. Cheapest first step is a
   lint rule that COUNTS it so the real rate is known across request shapes, since
   one request shape at n=8 is not a rate. A stripper is tempting and probably
   wrong: the model failed to make the call at all, so hiding the evidence leaves
   a turn that silently did nothing.

8. **Cost, and where it actually goes.** Measured: the fast prompt is 9.8-17.7 KB,
   of which the three runbook bodies are ~85%. The user-message history added
   later is ~4 KB at its ceiling and usually far less. If this stage needs to get
   cheaper, the runbooks are the only lever worth pulling — sending summaries or
   `whenToUse` lines instead of bodies is the obvious experiment, and it trades
   directly against how specific the text can be.

`shadow` is the mode that produces all four without showing anyone anything.


## Capture the exact provider wire body in traces

**Trigger: the next time a trace is used to replay a turn, or before the first
training export is taken seriously.**

`ModelInvocationTrace.wire_request_ref` is defined and always `None`.
`request_ref` holds the NORMALIZED `ProviderChatRequest`, which is not what the
provider saw: the adapter renames `max_tokens`, Together adds its `reasoning`
object, Cerebras uses `max_completion_tokens`, and `cache_session_id` is dropped.
A replay built from the normalized form is confidently wrong about the request.

The fix is the observer hook the design already anticipates
(`ProviderRequestObserver` in section 7.16 of the spec) — an optional callback on
the provider boundary, NOT the orchestrator reaching into a private `_build_body`.
It is left `None` rather than filled with the normalized bytes on purpose: a field
that claims to be the wire body and is not would be worse than a visible gap.


## Verify the GCS trace sink against a real bucket

**Trigger: before `TRACE_SINK=gcs` is used for anything whose loss would matter.**

`tracing/sinks/gcs.py` has never run against Cloud Storage. What IS pinned offline
is the half that is ours — object paths, content types, the auth header, the
token-refresh path, and that the completion marker is written LAST
(`tests/unit/test_tracing_gcs.py`). What the API accepts, its error shapes, and
whether the metadata-server token behaves as assumed are unpinned.

Also owed on that first deployment, and not enforceable from code: a write-only
IAM posture for the runtime identity (`storage.objects.create`, no list, no read,
no delete), a separate offline identity for the export path, bucket lifecycle
rules for `TRACE_RAW_RETENTION_DAYS`, and a bucket that is not shared with any
public artifact.


## Write the live provider-contract suite that no longer exists

**Trigger: before relying on any wire fact in `docs/PROVIDER_POLICY.md`, and
before the next model or host change.**

`tests/live/` is EMPTY. The suite that lived there pinned the CEREBRAS wire —
which parameters were accepted, which were rejected with a 400, the flat error
body, and that the serving model reported zero reasoning tokens — and it was
deleted with that adapter, correctly: every one of those rules was the opposite
of, or absent from, what Together wants.

Nothing replaced it, so these are now remembered rather than checked:

- that `reasoning: {"enabled": false}` actually suppresses reasoning on
  DeepSeek V4 Flash (§2 of the provider policy asserts it, from a hand
  measurement);
- whether Together reports a billed `cost`, which decides whether every
  cost field in a response is populated or absent (§5c);
- which of the two documented cached-token shapes the serving model reports
  — nested under `prompt_tokens_details` or flat at the top level. The parser
  reads both and stamps `cached_tokens_source` on every record, so the live
  answer is one log query away the moment a suite can make the call;
- the error-body shape the adapter parses;
- streaming, tool calls, forced tool choice and JSON mode against the real API.

A mocked test proves the adapter builds what we THINK the API wants. Only a live
one proves the API agrees, and the gap is widest right after a host change —
which is exactly when this one was removed.


---

## Account failed and aborted work

**Trigger: the first billing question that a partial answer cannot settle — a
disputed charge, or a per-account ceiling that has to count what a turn spent
before it broke.**

A SUCCESSFUL turn logs `turn_usage` on both paths. What does not log is failure:
the streaming record sits behind an `if completed` gate, so a stream that errored,
timed out or was abandoned writes nothing; the non-streaming path raises past its
record for the same outcomes; and a task that fails its second parse raises before
`task_run` is written. Both can have spent real money first: a
mid-stream error, a deadline, a client disconnect, a repair whose second call was
billed and then rejected.

That was survivable while the figure was diagnostic. It is less so now that a cost
sits beside a pseudonymous account and a plan, because the records that exist are
the ones a query will sum — and a sum that silently omits the expensive failures
is a wrong answer rather than a partial one.

The shape is one outcome-aware accounting record per dispatched turn, emitted on
every exit including cancellation, carrying whatever usage is known and marking
the cost incomplete when it is not. The reason it is not done here is that "what
did an aborted stream cost" has no answer the provider gives us — the usage
arrives with the final chunk that never came — so this needs a decision about what
to record rather than a place to record it.

---

## Decide the Standard and Pro allowances, or stop promising a difference

**Trigger: before anything is sold. Not before staging, which can run both active
plans as the same binary access.**

The website promises Standard has "enough usage for a normal day" and Pro "five
times the usage". No numeric allowance, usage ledger or quota enforcement exists
anywhere — this server records what a turn cost against an account and enforces no
ceiling, which is the honest state and not a gap to be closed casually.

`usage_limit_reached` is defined on both sides and unreachable. It stays that way
until somebody decides what the numbers are: inventing them here would be setting
prices, which is not a decision code gets to make.

Two ways out, and they are both real: define and implement exact allowances, or
remove the differential usage promise from the site. What must not happen is a
public launch with the promise intact and nothing behind it.
