---
id: daintree.orchestration.execute-durable-workflow
title: Execute a durable workflow graph
summary: Plan and advance dependency-aware work across turns using the optional workflow graph tools, with exact resource links, evidence and reconcile checkpoints.
whenToUse: Use for a substantial multi-step goal with dependent phases that must survive turns, restarts or asynchronous waits — migrations, delegate-then-verify work, several ordered changes, or "track this plan and resume it later". Use when the user explicitly asks for a durable plan, dependencies, progress tracking, reconciliation or a workflow graph. Do not use for a one-step action, independent parallel fan-out (use daintree.orchestration.fan-out-work), a status-only "where were we" sweep of the flat workflow ledger (use daintree.orchestration.status-and-resume), simple agent supervision (use daintree.orchestration.supervise-fleet), or a single future notification (use daintree.orchestration.watch-outcome).
confirmation: "Carrying a multi-step plan across turns. The aim is to keep dependency order and progress intact through restarts and long waits, while the plan recommends the next step rather than running it."
risk: project
requiredTools:
  - context.snapshot
  - workflow.list
  - workflow.get
  - workflow.plan
  - workflow.getGraph
  - workflow.next
  - workflow.attachResource
  - workflow.recordEvidence
  - workflow.reconcile
  - workflow.cancel
  - user.askMultipleChoice
  - timer.schedule
---
Use when: several dependent phases need a durable execution graph rather than an in-memory plan.

The seven graph tools are REAL Assistant CLI tools and they are registered by DEFAULT; the trusted environment flag `DAINTREE_WORKFLOW_INTELLIGENCE=0` is what turns them off. Feature-detect anyway: an operator who has turned them off leaves you naming tools that are not offered, and withholding is final for the turn. `wfg_...` graph ids are NOT the older flat-ledger `wfr_...` ids; never send one family to the other's tools.

Procedure:

1. FEATURE-DETECT FIRST. Require the currently offered tool surface to contain at least `workflow.plan`, `workflow.getGraph` and `workflow.next`. If absent, say Workflow Intelligence is unavailable in this session (disabled, unsupported or withheld) and continue phase-by-phase with the applicable domain runbook; do not claim a durable graph exists. Do not route graph operations through `daintree.call`: these are CLI-local tools, not Daintree MCP actions. `workflow.plan` and `workflow.reconcile` also require a compatible Assistant backend; the other five graph operations are local.

2. REUSE BEFORE CREATING. Read the durable workflow digests already injected into the turn. If an open graph covers this same goal, call `workflow.getGraph({"id":"wfg_...","view":"compact"})` and reconcile it rather than planning a duplicate. Use `view:"full"` only when exact nodes, edges, resources or evidence are needed.

3. PLAN ONCE after the goal, target and scope are concrete:
   `workflow.plan({"goal":"<user goal, verbatim or lightly summarized>","scope":"<bounds>","activeRunbookIds":["<actually loaded ids>"],"notes":["<constraints>"]})`.
   Optional fields are `existingWorkflowId` and `forceReplan`. Capture the returned `workflowId`, status, nodes, active ids, warnings and next action. A returned `userQuestion` means the graph was ALREADY stored. In an interactive turn, use `user.askMultipleChoice` only when the returned options fully represent the choice. When `allow_other:true` or a free-form answer is needed, ask one concise ordinary question and stop the turn; after the user's answer, call `workflow.reconcile` with reason `user_interjection` and `latestUserMessage`. On an unattended wake, report the question as a blocker instead of invoking an interactive prompt.

4. READ WHAT CAN RUN NOW with `workflow.next({"id":"wfg_..."})`. It returns ready nodes, waiting nodes, open blockers and an advisory next action. Never execute a dependency-waiting node. A next action is advice, not authority: confirm its tool is currently offered, resolve exact live arguments, and respect the tool's normal risk confirmation and the user's scope. If a ready node lacks enough detail, read the full graph.

5. EXECUTE THROUGH NORMAL DOMAIN TOOLS. The graph records and recommends; it never commits, spawns, merges, edits or waits by itself. Follow the loaded domain runbook for each node. Do NOT add `workflowId` or `workflowNodeId` to strict domain-tool calls — current non-workflow schemas do not accept them. After a tool returns a real id, link it separately on the next continuation:
   `workflow.attachResource({"workflowId":"wfg_...","nodeId":"<node>","type":"terminal","ref":"<exact id>","label":"<short label>","status":"active"})`.
   Valid types are `terminal`, `agent`, `worktree`, `branch`, `pr`, `issue`, `watcher`, `timer`, `async`, `queue_event`, `artifact`, `memory`, and `grant`. Manually attach terminal/watcher/timer/async/worktree/PR resources whenever node attribution matters. Automatic capture is only reliable when exactly one open graph and one active node make attribution unambiguous; current timer/watcher naming can also evade automatic capture.

6. RECORD ONLY MISSING EVIDENCE. Use `workflow.recordEvidence({"workflowId":"wfg_...","nodeId":"<node>","kind":"manual_note","summary":"<one bounded sentence>","refId":"<source id>"})` for facts the observer cannot see. Valid kinds are `tool_call`, `tool_result`, `queue_event`, `watcher_event`, `async_completion`, `user_message`, `assistant_message`, and `manual_note`. Do not narrate every read or duplicate automatically captured mutation results.

7. RECONCILE AFTER MATERIAL CHANGE, not after every read:
   `workflow.reconcile({"workflowId":"wfg_...","reason":"tool_result","recentEventLimit":20,"apply":true})`.
   Reasons are `tool_result`, `queue_event`, `wake`, `manual`, `resume`, `failure`, and `user_interjection`. Use `failure` after a failed action, `wake` after async/watcher wake, `resume` on returning to stale work, and `user_interjection` when the user's new message changes execution. `apply:false` previews a patch; normal operation applies it. Reconcile PATCHES progress and blockers; it never replans or executes.

8. REPLAN ONLY FOR A CHANGED GOAL OR SCOPE. Call `workflow.plan` with the live `existingWorkflowId` and `forceReplan:true`; without force, planning over live work is refused. Do not use replan to paper over a failed node — reconcile can transition a failed node back to ready for retry. Preserve the returned `supersededWorkflowId` in the report.

9. USE NORMAL ASYNC SUPERVISION for long work. Spawn/watch/await using the relevant orchestration runbook, then attach the returned ids. Linked async settlement can update the linked node, but the wake still needs `workflow.reconcile({"workflowId":"wfg_...","reason":"wake","recentEventLimit":20,"apply":true})` to advance/finalize the plan. Never hand-poll just because a graph exists.

   Graph persistence means the work is AVAILABLE to resume in a later turn; it does not schedule or autonomously resume execution by itself. To resume later, schedule a `timer.schedule` `message` payload (daintree.orchestration.watch-outcome) naming the persisted wfg_ id: when due it starts an assistant turn that reconciles and advances the graph with normal tools and authority. A `call_safe_tool` timer runs one fixed tool and wakes nobody, so a scheduled reconcile only updates the graph. Only the interactive user turn can schedule; a wake cannot re-arm a timer.

10. CANCEL HONESTLY. `workflow.cancel({"workflowId":"wfg_...","nodeId":"<optional>","reason":"..."})` changes graph/node state only. It does NOT close terminals, cancel external work, remove worktrees, revoke grants or stop async handles. Those are separate explicit cleanup operations with their own authorization.

Validate every proposed next action against observed state and original authority; backend plan/reconcile output is advisory. After a failed resource attachment or reconcile, preserve the action’s actual result and ids and repair the bookkeeping rather than executing the domain action again. A “done” node without acceptance evidence is not a verified user outcome. On cancellation, report live linked resources separately and stop them only if that cleanup is requested.

Report back: the `wfg_` id, completed/total progress, active or blocked nodes, exact resources still live, evidence behind the current verdict, and the next action or wake checkpoint. If graph tools were unavailable, state that no durable graph was created.
