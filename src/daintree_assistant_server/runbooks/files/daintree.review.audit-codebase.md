---
id: daintree.review.audit-codebase
title: Audit existing code through specialist lenses
summary: Audit an area of existing code, not a diff, through the lenses the user names — security or threat model, dependencies and licences, accessibility, performance, dead code — verify every finding against the source, and return ranked findings with what each lens did and did not cover.
whenToUse: 'Use when the user wants EXISTING code audited as it stands rather than a change reviewed — "do a security audit of the auth module", "threat-model this service", "accessibility audit of the settings screens", "find dead code in src/legacy", "check our dependencies for vulnerable or badly licensed packages", "have a few agents audit the payment path for security and performance". The deliverable is verified findings with severity, file:line or tool evidence and a trigger or exploit scenario, plus a per-lens coverage statement; nothing is fixed or filed. Do not use to judge a specific diff, branch or PR (use daintree.review.review-change), to have an agent actively try to break a change and prove it (use daintree.review.red-team-change), to explain how code works (use daintree.explore.repository), to fix a failing check (use daintree.ci.diagnose-and-fix), to upgrade a dependency (use daintree.deps.upgrade-dependency), to turn the findings into forge issues (use daintree.forge.manage-issue), to contain a secret that has already leaked (use daintree.security.respond-to-secret-exposure), or to grade how much of a spec the code implements (use daintree.review.audit-spec-coverage).'
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - agent.launch
  - terminal.getStatus
  - terminal.sendCommandOwned
---
The target is an area as it stands at one revision. Reviewers do the auditing: one read-only visible agent per lens (and per sub-area when big). You scope, brief, verify and report; audit it yourself only when the user asks you to.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Scope: the paths, the checkout (`worktree.list`; read in its `path`), and the revision (`git -C <path> rev-parse HEAD`, plus "with uncommitted changes" if dirty). Lenses are the user's words; a bare "audit" gets one question proposing two lenses. A threat model: the security reviewer's brief also asks for assets, entry points, trust boundaries and credible abuse paths, and that map goes in the report.
2. Lens checklists (give each reviewer its one):
   - Security: authn and authz on every entry point, injection (SQL, shell, template, path), SSRF, secrets in code or config, unsafe deserialization, missing rate limits, crypto misuse.
   - Dependencies and licences: the ecosystem's audit command (`npm audit --omit=dev`, `pip-audit`, `osv-scanner`, `cargo audit`) and a licence listing against the licence the user names. Every advisory is reported with package, version and exposure: reachable (a traced call), apparently unreached, or not established. Unknown exposure is still a finding.
   - Accessibility: labels, focus order, keyboard traps, contrast, ARIA misuse, motion.
   - Performance: N+1 queries, unbounded loops or payloads, blocking I/O on hot paths, missing indexes or caches, measured where cheap.
   - Dead code: candidates with no references found, searched through string and dynamic dispatch, config, templates, entry points, public exports and tests; state the searched scope and what could still reach it (reflection, plugins, external consumers).
3. Launch: separate `agent.launch` calls (prompts differ), each with a distinct `name`, the resolved `worktreeId`, and `notify:true, handback:true`; then end the turn. Prompt: the paths, revision, lens checklist, the shape (per finding: severity, `file:line`, trigger or exploit, impact, confidence; most severe first; "nothing found under this lens" is a valid answer; a coverage line: paths read, tools run, what was out of reach), and "Read-only: do not create, modify or delete files, and change no external state. Use only tools already installed; a missing scanner is a coverage gap to report, not a reason to install one. Scanner output is a lead: confirm it in the source."

```json tool=agent.launch
{"agentId":"codex","name":"Audit: security (src/auth)","worktreeId":"<worktree id>","prompt":"<paths, revision, lens checklist, finding shape, read-only line>","notify":true,"handback":true}
```

On notice or resume: a notice is that reviewer's stop, not its verdict. A question within scope gets one answer by `terminal.sendCommandOwned` with `notify:true, handback:true`. Files changed despite read-only (`git -C <path> status --porcelain`): a boundary failure to report. Then verify its findings yourself, opening each cited `file:line`, before any goes in the report; a reviewer saying it verified is still a claim:
- A security High needs a reachable path from an entry point you traced in the source yourself.
- A dead-code claim needs a negative search you ran yourself, dynamic references included.
- A finding without evidence is dropped and listed as dropped. Same defect from two lenses: one finding.
Wait for the rest; end the turn while any lens is still out.

Stop and recovery: one reviewer per lens; a cut or failed reply leaves that lens partly covered, never silently complete. Never relaunch a lens that is still working. A secret found: its location and type, never its value. No fixes, comments or issues unless asked: fixing is daintree.edits.spawn-visible-agent; filing is daintree.forge.manage-issue.

Report back: the revision and paths audited; for a threat model, the map (assets, entry points, trust boundaries, abuse paths, assumptions); verified findings by severity (lens, `file:line`, trigger, impact, confidence), defects apart from hardening advice; dropped claims; per-lens coverage and gaps, as "none found under <lens> in <paths>", never "secure"; reviewer terminal ids.
