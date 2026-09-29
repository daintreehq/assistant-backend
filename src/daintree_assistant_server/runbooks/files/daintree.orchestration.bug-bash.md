---
id: daintree.orchestration.bug-bash
title: Run a multi-agent bug bash
summary: Give several agents distinct exploratory-testing charters on the running app or CLI, collect their bug reports, merge duplicates, reproduce each yourself and hand back a confirmed list ranked by severity.
whenToUse: 'Use when the user wants several agents to EXPLORE the running product and try to break it — "bug bash the new release with a few agents", "have agents poke at the app and find bugs", "exploratory testing: one on onboarding, one on billing, one on weird inputs", "QA the CLI with three agents before we ship". Each agent gets its own charter (an area and a way of attacking it) and changes no code. The deliverable is a de-duplicated list of bugs you reproduced yourself, ranked by severity, with repro steps. Do not use to attack one specific change or PR (use daintree.review.red-team-change), to review a diff (use daintree.review.review-change), to audit existing code by reading it (use daintree.review.audit-codebase), to run the existing end-to-end suite (use daintree.ci.run-e2e-suite), to reproduce one reported bug (use daintree.debug.reproduce-bug), for a broken dev preview (use daintree.runtime.debug-web-app), to file the findings as issues (use daintree.forge.manage-issue after this), or to install and smoke-test the built release artifact in a clean setting (use daintree.release.test-distributable).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.sendCommandMany
  - terminal.interruptOwned
---
Agents explore and report; they fix nothing. A bug counts only once someone other than its reporter has reproduced it.

Procedure:
1. Target: the worktree and revision under test (`worktree.list`, then `git -C <path> rev-parse HEAD`), how it runs (the CLI invocation, or the dev-server command and address), and a time bound per agent. Never point agents at production, real customer data or real external accounts unless the user said so.
2. Charters, one per agent: an area crossed with a way to attack it (a first-time user, invalid or huge input, interrupt and retry, two things at once, keyboard only, a slow or failing network). Distinct charters are the point: one shared prompt sends every agent down the same happy path. Use the user's areas; state any charters you derived in your reply.
3. Isolation. Agents driving a stateless CLI can share the target worktree. When the app writes shared state (one database, one port, one upload folder), give each agent its own worktree from the same commit (`worktree.createWithRecipe` new branch, `collisionPolicy:"error"`, then `worktree.waitUntilReady`: only `setupState:"ready"` launches; `timedOut:true`: call again; any other state: that charter is reported blocked). A new worktree does not get its own port, database or upload path by itself: provision them (daintree.worktree.provision-local-environment) and check they differ before launch. Where you cannot, run write-capable charters one after another, or give that charter nothing that writes.
4. Launch each agent with its own `agent.launch` in the same turn, then end it:

```json tool=agent.launch
{"agentId":"<agent id>","name":"Bash: <charter>","worktreeId":"<worktree id>","prompt":"<target, how to run it, charter, bound, rules, report shape>","focusPolicy":"preserve","notify":true,"handback":true}
```

   The prompt: the revision and how to run it; the charter; "Explore for up to <N> minutes"; "Change no source files and fix nothing; put any scripts under <a scratch directory outside the repository>"; a report of at most 40 lines: per bug a title, exact repro steps or command, observed versus expected, how often it reproduced, a severity guess; then what you tried that worked.

On notice or resume:
1. Map the terminal to its charter. Its report is final for that charter. Source files changed are a boundary failure: report them, revert nothing. A question: answer once within the charter (`terminal.sendCommandOwned`, `notify:true, handback:true`), otherwise ask the user.
2. Once every charter is in or recorded missing, write down every bug each report lists (charter, title, repro): each ends in exactly one list below, none dropped, the worst included. Merge duplicates by root cause (the same failing code path or error), keeping every reporter as the reports name them.
3. Reproduce each by someone other than its reporter, at the same revision in an isolated environment. A single command or request: re-run it yourself. Longer steps: one `terminal.sendCommandMany` (`notify:true, handback:true`) giving each report to a charter agent that did not file it, in its own worktree: "Reproduce exactly these steps. Report reproduced or not with the observed output. Fix nothing." End the turn. Over about fifteen reports: most severe first, the rest listed unverified. Confirmed means you reproduced it yourself; a second agent's reproduction alone is "reproduced by an agent", listed apart. Label each confirmed, reproduced by an agent, not reproduced, by design (cite the code or docs that intend it) or needs the user (an account, hardware or data you lack, or steps that would touch real external state). Rank confirmed bugs: data loss or security, crash, wrong result, degraded behaviour, cosmetic. An unreproduced report never enters the confirmed list.
4. Filing issues (daintree.forge.manage-issue) or fixing is a separate request: do neither here.

Stop and recovery: one launch per charter; no second wave without the user. An agent past its bound: recheck it as the foundation says; stop it (`terminal.interruptOwned`) only if the user said to stop at the bound, then ask once for its findings so far. Close terminals or delete worktrees only on request (daintree.orchestration.workspace-cleanup).

Report back: the revision tested; each charter with its agent and terminal id; confirmed bugs by severity with repro steps, observed versus expected and which charters hit them; the reproduced-by-an-agent, not-reproduced, by-design and unverified lists; needs-user items with what each lacks; charters that came back empty or missing; any boundary failures.
