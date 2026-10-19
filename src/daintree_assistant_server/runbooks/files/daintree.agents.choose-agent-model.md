---
id: daintree.agents.choose-agent-model
title: Choose the agent, model and effort for a task
summary: Pick the agent CLI, model and reasoning effort a task should launch with, from what is ready on the machine that runs it and sized to the task's difficulty, risk and cost, then add only those choices to the owning workflow's launch or report them as a recommendation.
whenToUse: 'Use when the user hands you the CHOICE of agent CLI, model or reasoning effort for work about to be launched, or asks which one fits: "use whichever model is best for this", "a cheap fast model for the easy fixes and the strongest one for the hard refactor", "should Codex or Claude take this", "match each task to the right agent", "spend Codex in proportion to the stakes", "which model should review this", "what models can I launch here and which suits a migration", on this machine or a Remote Host. It is the selection step beside the workflow that owns the work: the deliverable is, per task, the agent, model id and effort picked from what is ready there, with the reason, added to that workflow''s launch or reported as a recommendation. Do not use when nothing is left to choose, the user having named the agent and any model or effort or asked for no choice (daintree.edits.spawn-visible-agent), to switch the model of an agent that is already open (daintree.agents.run-agent-command), for how much plan allowance is left (daintree.agents.check-usage-limits), to decide which machine takes each piece (daintree.hosts.place-work), when several agents each attempt the same task and a winner is picked (daintree.orchestration.competition) or each prototypes a different option (daintree.orchestration.spike-options), or to move a task off an agent that hit its limit (daintree.agents.hand-off-task).'
risk: read
requiredTools:
  - agent.listAvailable
  - agent.listModels
optionalTools:
  - agent.launch
  - agent.listPresets
  - terminal.getStatus
---
Decide what a launch runs on (the agent CLI, its model, its reasoning effort) when the user hands you that choice. You return the choice and the launch arguments it adds; the runbook that owns the work (daintree.edits.spawn-visible-agent for one change, daintree.orchestration.fan-out-work for a batch: `get_runbook` if it did not come back with this one) launches, briefs, waits and verifies.

On a host: call these tools, and run any `--help` read, on the machine that will run the work, per the hosts foundation.

Procedure:
1. What is yours to choose. Whatever the user named (agent, model, effort, a launch preset, a standing preference) is used as given, never upgraded. Set a model or effort only when the user asked you to choose it: `agent.launch` keeps `model` for a model the user names, since one set unasked overrides the default they configured in that CLI. Asked only which CLI: choose the CLI and keep its default model. Asked only which would fit: recommend and launch nothing.
2. What can run: one `agent.listAvailable`; only `availability:"ready"` counts (`availabilityComplete:false`: read once more, then call the rest unknown). One `agent.listModels` with no `agentId` gives the candidates, spelled as listed (Claude's are aliases). Listed is not entitled: a model the plan lacks fails at start. An empty list means unknown: that CLI keeps its default unless the user names an id.
3. Size each task:
   - Light: mechanical and fully specified, small blast radius: a rename, a lint or typo fix, a version bump, a summary.
   - Standard: a feature or bug fix in code the agent can read and test.
   - Heavy: unknown cause, a cross-cutting refactor or migration, concurrency, security, data or schema changes, architecture or a plan others build on, a long unattended run.
   Note the traits: screenshots or mockups to read (Claude or Codex: their panes take images), visual frontend work, prose, judging another agent's work.
4. Tier within a CLI, read off the listed names: Fable, Opus, Astra, Argon and Gemini's Pro are frontier; Sonnet and Sol mid; Haiku, Luna and Flash fast.
   - Light: the fast tier, or the mid model at `low`.
   - Standard: add nothing; the CLI's default model and effort.
   - Heavy: the frontier tier at its default effort, `high` for long or subtle work. Measured on Opus and Sol, `xhigh` cost two to four times `medium` for equal or worse results, so `xhigh` and `max` only when the user asks. Never Codex `ultra` unasked: it starts subagents of its own.
5. Across CLIs, when several are ready and nothing ties the work to one. Evidence as of October 2026 turns over in weeks: say "current benchmarks suggest", never "best".
   - A reviewer, judge or second opinion: a different provider from the author's model (the model configured, not the CLI's name), less likely to share its blind spots.
   - Visual frontend work: Claude's frontier tier led the October web-design rankings.
   - Many heavy tasks on a budget: Codex's mid model measured cheapest per task near the frontier.
   - Otherwise the CLI the user already works in.
6. Allowance: Fable and Astra list at about five times Sonnet and Sol per token, and Codex's plan credits keep that ratio. When the user wants allowance weighed, read it first (daintree.agents.check-usage-limits); under about 20% left in a window, move heavy work down a tier or to another ready CLI and say so. Allowance not read is unknown, not ample.
7. The arguments, added to the owning runbook's launch, one `agent.launch` per agent (`agent.launchMany` takes no model). Only what you chose goes in. `model` is the listed id; Daintree passes it as `--model`, and a default preset or the user's own CLI flags that set a model still win. Effort goes in `agentLaunchFlags`, which come last and stay with that terminal across resume and restart, never in the CLI's config:
   - Claude Code: `["--effort","<level>"]`, levels low, medium, high, xhigh, max.
   - Codex: `["-c","model_reasoning_effort=<level>"]`, the same levels.
   - Antigravity: `["--effort","<level>"]`. Grok: `["--reasoning-effort","<level>"]`.
   - Another CLI: an effort flag only if its `--help` shows one; else none, and say effort was not set.

```json tool=agent.launch
{"agentId":"codex","model":"<id from agent.listModels>","agentLaunchFlags":["-c","model_reasoning_effort=<level>"]}
```

8. After the owner's launch: a start error naming your id or flag is retried once without it, and you say so. The CLI's header on the pane's first screen may name the model: report what it shows, else "model unverified".

Report back: per task, the agent, model and effort, each named or chosen, with a one-line reason; CLIs not ready or unknown; allowance read or unknown; what each header showed, or for a recommendation the arguments you would pass.
