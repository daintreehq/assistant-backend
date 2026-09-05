# Intelligence report application — 5 September 2026

Reviewed the complete 1,523-line **Daintree Assistant: Backend Intelligence and
Runbook Roadmap**, supplied as `daintree-backend-intelligence-report.md` (SHA-256
`004a670d3c4c7ad42732120b1ff9f6c65a14a819b9d287c3086e207991d50ab1`). This records the selected implementation, not a claim that the
whole roadmap has shipped.

The first commit, `c22a8d9`, audits all 39 procedures against the pinned CLI and
Daintree source; see [RUNBOOK_AUDIT.md](RUNBOOK_AUDIT.md). This follow-up applies
the report's remaining Phase A corrections and brings forward the behavioral
qualification harness. The catalog remains at 39 entries.

## Decisions on the immediate findings

| Report section | Applied result |
|---|---|
| 3.1 Clean tree/no-op | First audit removed the clean-tree shortcut. Follow-up requires the recorded baseline, preserves unknown baselines, permits legitimate no-ops and reports PASS/FAIL/UNVERIFIED per criterion. A missing diff alone cannot contradict implementation. |
| 3.2 Explore isolation | First audit distinguishes visible-agent prompt constraints from the private subagent's restricted tools and reports unexpected edits. |
| 3.3 Targeting | Follow-up fixes the conflicting permanent rule: immediate spawns may use the turn-bound default, alternate targets are resolved explicitly, and terminal sends do not receive unsupported worktree fields. Tool-specific delayed/recipe/forge/resource defaults remain distinct. |
| 3.4 Missing watcher | First audit reconciles actual launch/supervisor state and adopts the same terminal. The new fixture requires watcher and async inspection before adoption and forbids another spawn. |
| 3.5 Missing terminal | Follow-up removes the surviving cancellation shortcut. Terminal absence and task outcome are separate; unavailable evidence preserves the supported ledger status and is recorded as a note/blocker, without inventing an enum. |
| 3.6 Consuming observation | First audit makes report-only fleet checks observational and calls out awaitAll's watcher retirement. The fixture prohibits consuming waits/cancellations during a report-only sweep. No new observation tool is claimed. |
| 3.7 Account/provider ownership | First audit separates MCP, account/entitlement and backend-provider health and removes personal-key top-up advice. |
| 3.8 Teardown cascades | Follow-up requires consequence disclosure for indirect worktree deletion too; when hook effects or native confirmation coverage cannot be established, leave the worktree standing and use native inspection. This is a procedure boundary, not a proven/fixed desktop enforcement vulnerability. |
| 3.9 Required tests | Follow-up explicitly includes tests required by the user's acceptance contract and distinguishes them from unrelated framework migration. |
| 3.10 Cleanup consequences | Follow-up replaces the mixed safe/unsafe classification with separate identity, authority and consequence checks. Already authorized exact actions do not acquire another confirmation question. |
| 3.11 CopyTree discovery | First audit delegates broad discovery while retaining exact include/filter/scope semantics and checkout provenance. |
| 3.12 Wait budget | First audit stops equating the foreground limit with a hang; the new fixture forbids another foreground wait or restart when a progressing job needs async handoff. |

The README and `dev` wrapper’s remaining mandatory-Cerebras setup instructions
and warning were corrected
against the current disabled fast-response stage and readiness checks. Baseten
remains required; OpenRouter supplies optional failover.

The permanent prompt also stops telling an unattended actor to simply decide a
missing user choice, and requires task evidence before claiming async success.
Only the three affected prompt goldens were refreshed. Existing prompt ceilings
were preserved.

## Behavioral qualification delivered

[`benchmarks/main_behavior`](../benchmarks/main_behavior/README.md) implements
isolation and end-to-end modes through `ChatOrchestrator.complete`, with normal
projection/assembly and signed continuation state. Its engine returns synthetic
tool results; it cannot call the real CLI, desktop, forge or filesystem tools.
The paid `live` mode uses the existing serving-provider helper and is explicitly
separate from offline validation/replay.

The twelve cases cover baseline/no-op verification, failed-check envelopes,
wrong check directories, partial rosters, failed watcher attachment, approval
prompts, progressing work beyond a wait budget, report-only fleet status,
head-unbound CI, hidden teardown, required regression tests and fake tool-output
approval. Mutated-trace unit tests verify that wrong targets, unsupported tools,
forbidden calls, missing evidence/prerequisites, repeated actions and exhausted
rounds fail the checks.

The harness deliberately does not grade nuanced final answers using a keyword
or an uncalibrated second model. Each scenario has a human-review requirement.
Generated content is omitted from summary artifacts and is written locally only
with explicit `--transcripts`. Hashes identify source prompts, catalog, CLI
inventory/pin, selector, harness and cases. Partial runs cannot declare full
coverage; offline replay cannot declare model quality or release qualification.

Initial replay: **12/12 isolation checks; 9/12 with real selection**. The latter
misses the intended runbook for missing-watcher recovery, exhausted-wait handoff
and remote teardown. The resource runbook selected for the last case may be a
reasonable alternate. These are admission findings with scripted main replies,
not measurements of live model safety or actual tool effects. Following the
user’s request, paid Baseten trials were then run; see the verification record
below.

## Roadmap disposition

| Report area | Decision |
|---|---|
| 1–2 Scope and architecture | Preserve backend selection/prompts and local execution/state ownership. No duplicate planner, scheduler, memory service or coding-agent role. |
| 4 Authoring standard | Applied through RUNBOOKS.md, source checks and tagged schema-validated examples. Metadata remains strict; no speculative frontmatter keys. |
| 5 First-wave additions and 13 drafts | Retain as candidates. Do not package the brief/reproduction/handoff drafts merely because they parse. Next batch should add independent neighbor examples and live isolated qualification together with the coordinated retrain. |
| 6 Existing catalog refinements | Addressed across the initial 39-entry audit and the immediate corrections above. Actual capability additions remain owned by their runtime layer. |
| 7 Conditional additions | Keep gated: image transport, exact-head CI, recipe validation, capacity observations, plugin/workspace control and cross-project release views need verified capabilities. |
| 8 Task/evidence records and capability/phase awareness | Baseline and evidence language now use existing briefs/records. New wire shapes, writer-ownership transitions and phase/capability consumers remain implementation proposals; adding model-authored fields to current strict tools would be incorrect. |
| 9 Selector/docs training | No retrain or artifact change in this commit. Preserve the new admission counterexamples and combine them with new-runbook, active-set and documentation training work; do not tune catalog prose to memorize them. |
| 10 Evaluation | Small harness, explicit review gaps, bounded runs and reproducible summaries now exist. Live model trials now exercise the backend; actual desktop-effects qualification remains separate. |
| 11 Sequence | Phase A corrections and evaluation infrastructure are the selected second commit. Phase B's first four new outcomes follow qualification; Phase C/D additions stay separate. |
| 12 Metrics/do-not-build | Measure supported outcomes and interventions, not catalog size or agent count. Replay pass counts do not claim verified-outcome rate; no automatic production learning or broad reasoning escalation was introduced. |
| 14 Sources | The report's export citations were treated as dated evidence and checked against available current source, not as live deployment observations. |

One material source-version correction: the report discusses timer `message`
payloads from its export. The actual pinned Go CLI and freshly generated inventory
accept only `enqueue` and `call_safe_tool`. The first audit teaches those supported
forms; this follow-up does not restore unsupported timed assistant conversations.

## Verification record

The first commit's full-suite result is preserved separately in RUNBOOK_AUDIT.md.

- Backend full suite: **4,445 passed, 1 skipped** (two existing warnings). Final affected-scope checks also passed **537 tests** after the README/dev readiness correction; `bash -n dev`, Ruff lint/format, strict mypy (93 source files) and `git diff --check` passed.
- Pinned CLI: `go test ./...` passed, including its hermetic integration coverage. Seven relevant packages also passed `go test -race`: watcher, timer, workflow tools, workflow graph, wait budget, terminal observation and MCP wrappers.
- Daintree action and MCP contract suites: **2,912 tests in 91 files passed** via `npm test -- src/services/actions/definitions/__tests__ electron/services/mcp-server/__tests__`, against sibling source revision `07b356b4dba89c9cc327137dbb117af2cd2c5a51`. No desktop source was edited.
- Real CLI-to-HTTP/SSE-to-Baseten integration: streaming and utility summarization passed. The original capabilities test failed because it hardcodes protocol 2 although `backend.ProtocolVersion` is 3 and the server advertises `[3,3]`. A temporary Go overlay changed only the local test port to 18473 and that stale expectation to `backend.ProtocolVersion`; all **three live tests passed**. The CLI pin was preserved. This test defect remains to fix in the CLI repository.
- Live server was a fresh local instance of this checkout, bound to loopback on 18473; the user's existing server on 8473 was left running. Model credentials came through the root environment loader and were never printed or committed. Training capture was disabled.

The first live pilot exposed missing runner-discovery fixtures. The initial full
attempts were incomplete after Baseten HTTP 429 responses; completed cases were
retained, not counted as a full pass. Model traces also exposed supervisor adoption
without both prerequisite reads. Foundation and recovery now explicitly require
current watcher AND async results even after a watcher-storage error. The engine
still rejects premature adoption. Safe runner discovery and an unsuccessful
terminal read after partial roster failure were added to the synthetic cases;
these are fixture corrections, not claimed model improvements. The synthetic MCP
runtime now reports its available inventory instead of implying zero tools.

Paid requests are paced at ten seconds per main call. End-to-end admission misses
continue the response loop so an alternate procedure's behavior can be assessed
separately. Initial and revised fixtures are different: these trials are diagnostic,
not a paired causal estimate of prompt improvement.


The completed paced isolation diagnostic passed 8/12 action checks. Three other
cases stopped at unmodeled but legitimate reads; one used unsupported
`worktreeId` on `project.runCheck`. Subsequent fixtures admit bounded orientation
and coverage reads, and verification now names the supported check fields
explicitly. Manual review also found overstatement: a working roster was called
“all good,” and a successful send was treated as resumed execution. Fleet and
waiting-agent procedures now distinguish the observed state/send from those
stronger claims. These findings remain in the preserved diagnostic records.

Content-free live summaries are tracked under
[`benchmarks/main_behavior/qualification/2026-09-05`](../benchmarks/main_behavior/qualification/2026-09-05).
They retain failed and incomplete trials, source identities, actual usage and
admission results. Generated transcripts remain local; the review record reports
criteria and outcomes without publishing model output. Provider-reported cost
was unavailable, so no zero-cost claim is made.


The completed end-to-end diagnostic had 6/12 combined admission/action passes,
with three intended-runbook admission misses and several safe fixture
alternatives. Manual review caught invented/shortened handles despite valid tool
actions; exact returned-ID assertions now cover those outcomes. Prerequisite
reads must also complete an earlier model round: sibling calls cannot supply
already-observed evidence. New mutated-trace tests cover both gaps. The cleanup
procedure no longer suggests generic acceptance of an undisclosed teardown risk,
or promises the native deletion dialog displays lifecycle commands. Source
inspection of `WorktreeDeleteDialog.tsx` and `worktreeDeletePreview.ts` supports
local-change/submodule disclosure, not that stronger hook-preview claim.


The final isolation trial completed all twelve cases: **10/12 action checks**.
The two failed checks concern excess calls after a failed roster retry and after
sending an authorized regression-test answer. Exact watcher/async ids were
preserved, and undisclosed teardown stayed blocked. Manual review still flags
the fleet report's unsupported absence-of-blockers claim. Those are remaining
qualification findings, not a claim that every model scenario now works. The
full test suite, contract tests and live CLI integrations establish their stated
layers; they do not erase these model-behavior findings.


The full end-to-end trial completed with **9/12 behavior checks** and **7/12
combined behavior/admission checks**. Watcher and wait-handoff behavior passed
despite missing the intended runbook; hidden teardown stopped at an unmodeled
but legitimate tool-discovery read. The other failed checks repeated the excess
roster retry and post-answer wait. Available final reports were reviewed, with
details in the qualification record. A final procedure correction now explicitly
ends answer-only requests after the send and limits unavailable-roster recovery
to one targeted alternative; focused repeated trials assess this later revision.


Focused three-trial checks after that correction: **answer-only 3/3 action
passes; partial-roster 2/3**, with the latter failure using an excluded raw read
fallback. Report review still catches unobserved resumption and supervision
claims, so these action passes do not certify report truthfulness. The last
runbook edits passed **151 affected-scope tests**. The preserved diagnostics
contain **133 completed live main responses with provider usage**; failed
requests/retries and the separate live CLI tests are additional. All paid
batteries have finished and the temporary local integration server was stopped.

Remaining work is explicit: improve independent routing examples/qualification,
cover legitimate alternative reads, and address unsupported state inferences.
No selector artifact was retrained, no CLI pin changed, and no live desktop
mutation was used as a test. The two requested backend commits therefore contain
a complete catalog audit followed by applied Phase A corrections and repeatable
qualification infrastructure, rather than an assertion that the entire roadmap
or every stochastic model behavior is solved.
