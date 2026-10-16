# TODO

Deliberately-deferred work: things we have decided to do, but not yet. Each entry
names its **trigger** — the condition that makes it worth doing. Check any claim
about today's behaviour against the source before acting on it.

---

## Bound anonymous traffic on the public endpoint

**Trigger: before the MCP endpoint is announced, or the first time request volume
from one source is visible in the logs.**

The endpoint is open by design — anyone can add it — and a search is cheap (~10ms
of CPU). What bounds abuse today is only Cloud Run: `--concurrency=16`,
`--max-instances=20`, the inference pool's queue depth of 16 (a full queue answers a
retryable tool error), and the 64 KiB body cap. That caps the bill but not one
caller's share of it, so a single noisy client can make search unavailable for
everyone. Options, cheapest first: Cloud Armor rate-limiting per IP in front of the
service; a per-IP token bucket in process (per-instance, so approximate); an
optional API key that raises a caller above an anonymous limit.

---

## Retire the old secrets and their IAM grants

**Trigger: once a release of this branch has been serving for a day without a
rollback.** Rolling back to a pre-cutover revision needs those secrets mounted,
so they stay until rollback is no longer plausible.

The deploy no longer mounts anything (`--clear-secrets`), but Secret Manager still
holds `baseten-api-key`, `cerebras-api-key`, `openrouter-api-key`,
`state-signing-key-current`, `state-salt` and `staging-allowed-subject-digests`, and
the runtime service account still holds `secretAccessor` on each. Delete the
secrets (and with them the grants), and remove `roles/secretmanager.viewer` from
the deploy service account, which `bootstrap.sh` no longer grants but does not
revoke. The provider keys should also be revoked at the providers.

---

## Metadata-only search

**Trigger: measured context cost — callers routinely receiving several full procedures
they never follow, now that every response also carries the foundation.**

`get_runbook` (exact retrieval, with `supporting_runbooks`) has landed, which makes a
two-step read possible: search returning metadata only, then one `get_runbook` for the
selected id. Only worth adding once real sessions show the full-body search is paying
for neighbours; the default `max_results` of 3 is the cheaper lever first.

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

Nothing is broken by this, and search never reserves at all (it sends nothing
loaded). Reservation never fires on the live catalog
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

## Widen the shortlist to k=64

**Trigger: when the catalog passes ~200 runbooks.** Measured, not guessed — and
measurably NOT needed before then.

`bench scale` sweeps candidate recall across synthetic catalog sizes
(`training/EXPERIMENTS.md`, "Catalog scale"). Re-measured 2026-10-16 on the real
154-file catalog with the hosts family in it ("Catalog 106 → 153"), over the 1,885
cases that have a required runbook: `k=32` misses 0 today, **3 at 200**, 6 at 500
and 9 at 1,000, all hosts-adjacent at 200 (ranks 33–37); `k=48` is clean at 200
and misses 3 at 500; `k=64` misses 1 at 500. The 2026-08-21 sweep (154 cases) had
`k=32` clean at 200, so the margin has thinned, but the trigger has not been reached.

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

Not fired as of 2026-10-16: `foundation-only` went 5/7 → 6/9 in `bench local` only by
gaining two `hosts.foundation`-alone cases, with the same two shared misses on both
sides, and it reaches the shortlist 9/9 at every scale size.

The 2026-08-21 scale sweep's per-category breakdown showed `foundation-only` losing a case at
500 runbooks (5/6) and holding there, and `daintree.foundation` is the deepest
required runbook at every size — rank 26 of 200 under int8, rank 31 under fp32,
against a 32-slot shortlist. The cause is structural rather than a bug: there is one broad `daintree.foundation`, its
routing document is deliberately the most generic in the catalog, and a generic
document is the one a crowd of near-neighbours buries.

Domain foundations (`daintree.orchestration.foundation`, and so on) would each
carry a narrower, more distinctive routing surface. The catalog already supports
them — `Catalog.domain_foundation` walks UP the dotted namespace nearest-first,
so a domain foundation is picked up with no code change.

What has to be decided rather than built is the ontology: which domains earn one.

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
the other axis. Search does not read the docs head, so this matters only to the
corpus and the benchmark's docs grading.

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

## Live-verify what the 2026-09-28 expansion could not

The 87-runbook catalog was trialled live with Haiku orchestrating in an isolated Daintree
(`training/EXPERIMENTS.md`, "Catalog 40 → 87"). Still owed:
- The five forge runbooks (`review.review-pr-queue`, `forge.address-pr-review`,
  `forge.triage-issue-backlog`, `forge.batch-dependency-updates`, `forge.draft-release-notes`)
  need a real GitHub repository to run against.
- Body fixes no trial has confirmed: `ci.hunt-flaky-test`, `ci.run-e2e-suite`,
  `agents.postmortem-run`, `git.prune-stale-branches` (ignored its keep list in all three runs),
  `orchestration.plan-then-implement`, `orchestration.contract-first-split`,
  `orchestration.test-first-split`, `agents.hand-off-task`.
- App-side, to report to the Daintree repo: `terminal.readLastMessageOwned` returns
  `terminal-unknown` for panes the caller launched or was handed; `terminal.new` returns the
  same panel for identical calls within two minutes without saying so; `terminal.list` shows
  an idle agent as `waiting`.
- The trial harness and the per-runbook trial reports are kept outside the repo at
  `~/.cache/daintree-runbook-e2e/kit/` (`E2E.md` is its guide).

