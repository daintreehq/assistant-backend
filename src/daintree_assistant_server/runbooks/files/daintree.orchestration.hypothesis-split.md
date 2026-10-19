---
id: daintree.orchestration.hypothesis-split
title: Test competing hypotheses with parallel agents
summary: For a bug or behaviour with several plausible causes, give each agent ONE hypothesis to confirm or refute with a discriminating experiment, re-run the deciding experiment yourself, and report which cause the evidence supports, without fixing anything.
whenToUse: 'Use when a problem has SEVERAL candidate causes and the user wants them chased in parallel — "have three agents each chase a different theory for the memory leak", "one agent per suspect for the slowdown", "split the investigation: is it the cache, the query or the serializer?", "rule out each possible cause at the same time". The signal is one symptom with rival explanations, one agent per explanation. The deliverable is a verdict per hypothesis (supported, refuted, inconclusive) with the experiment behind it, re-run by you, and no fix. Do not use for a failing test, build or CI check (use daintree.ci.diagnose-and-fix), to turn a bug report into a failing reproduction first (use daintree.debug.reproduce-bug), to find the commit that introduced a regression (use daintree.git.bisect-regression), for a production incident (use daintree.debug.investigate-outage), for agents debating toward one answer (use daintree.orchestration.multi-agent), for the same question put to several agents (use daintree.agents.poll-pinned-agents), for trying alternative designs or libraries (use daintree.orchestration.spike-options), or to explain how code works (use daintree.explore.repository).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.interruptOwned
---
The deliverable is evidence, not a fix. An agent's "found it" is a claim until you have re-run its experiment.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Fix the symptom: what is observed, where (worktree id and path from `worktree.list`, and its HEAD SHA), and how to trigger it. With no way to trigger or observe it, every agent will guess: say so and offer to reproduce it first (daintree.debug.reproduce-bug) unless the user wants to go on.
2. Hypotheses, 2 to 4: the user's, or yours from a quick read of the code, shown to the user before launch. Each is ONE falsifiable cause with what would confirm it and what would refute it ("if the cache is stale, disabling it makes the error vanish; if the error persists, refuted"). Two that predict the same observations are one hypothesis: merge them.
3. Where each runs. Only a hypothesis tested by reading code and by commands that write nothing (no build output, cache, log or database) shares the target worktree. Any other gets a throwaway worktree: `worktree.createWithRecipe` with a new branch `probe/<slug>` from the target's branch, `collisionPolicy:"error"`, no `recipeId`, then `worktree.waitUntilReady`: only `setupState:"ready"` launches (`timedOut:true`: call again; any other state: that hypothesis is reported blocked). Then check `git -C <probe path> rev-parse HEAD` equals the target SHA; if not, it tests another revision: say so or stop it. A probe holds committed code only: if the target has uncommitted changes, say the probes do not include them. Never instrument the user's checkout. Probes that run the app against one database or port collide: run those one after another, or see daintree.worktree.provision-local-environment.
4. Launch one agent per hypothesis in the same turn, each its own Daintree `agent.launch` (a visible pane, never your own built-in subagent tool), then end the turn:

```json tool=agent.launch
{"agentId":"<agent id>","name":"H1: <hypothesis in a few words>","worktreeId":"<probe or target worktree id>","prompt":"<symptom, trigger, hypothesis, confirm and refute conditions, rules, reply shape>","focusPolicy":"preserve","notify":true,"handback":true}
```

   The prompt: the symptom and how to trigger it; "Test ONLY this hypothesis: <h>. It is confirmed if <c> and refuted if <r>. Run the smallest experiment that tells them apart. Do not fix anything. Other hypotheses are being tested elsewhere: if you see evidence of a different cause, report it and do not chase it." A reading-only one adds "Read-only: do not create, modify or delete files." The reply, at most 30 lines: CONFIRMED, REFUTED or INCONCLUSIVE, the exact experiment (commands or code change), the observed output, and what it is unsure of.

On notice or resume:
1. Map the terminal to its hypothesis and read the verdict from the notice quote. A question: answer once within the brief (`terminal.sendCommandOwned`, `notify:true, handback:true`), otherwise ask the user.
2. CONFIRMED: re-run its experiment yourself in that worktree: the trigger and the deciding check it quotes, and nothing that writes to the checkout. Re-running is verification, not the work, so it is yours; until you have done it the verdict is the agent's claim, and your report says so. Ask whether the same result would also appear under a rival hypothesis; if so it discriminates nothing and is inconclusive. If it holds and discriminates, record it as supported by your re-run. If the user agreed that one proven cause ends the hunt, `terminal.interruptOwned` each probe still working and read its screen after (`requested` is not stopped); otherwise let them finish, since two causes can both be real.
3. REFUTED: accept it only if its experiment actually exercised the suspect path; otherwise it is inconclusive.
4. A different cause an agent reported is a lead, not a result: offer it to the user as a new hypothesis.
5. Report once every hypothesis has a verdict or is recorded missing.

Stop and recovery: one agent per hypothesis and at most one follow-up each; no new round of hypotheses without the user. Nothing is fixed here: the fix is a separate request (daintree.edits.spawn-visible-agent). All refuted or inconclusive is a valid result. Leave probe worktrees in place; removing them is daintree.orchestration.workspace-cleanup, on request.

Report back: the symptom and revision tested; per hypothesis its agent and terminal id, experiment, claimed verdict and your re-run result; the supported cause or "inconclusive" and what was ruled out; new leads; probe worktrees and instrumentation left for cleanup.
