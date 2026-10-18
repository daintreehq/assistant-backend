---
id: daintree.hosts.hand-off-swarm
title: 'Hand a run on the Remote Hosts to a replacement session'
summary: 'Move coordination of a run already out on the Remote Hosts to a replacement session on the same Shell, Claude Code to Codex and back: the outgoing session freezes and writes a hand-over, the person switches, and the successor reconciles it with each host''s live terminals with nothing resent, then supervises on.'
whenToUse: 'Use when the session coordinating a run ALREADY OUT on the Remote Hosts is being REPLACED BY ANOTHER SESSION NOW while the hosts keep working: the swarm manager moving from Claude Code to Codex or back mid-run, its outstanding tasks carried over, "your context is nearly full, hand the cross-machine swarm to a fresh assistant", "you are the new session, take over the run from the hand-over the last one wrote", or whether a Shell on another machine can take over a run this one sent. The deliverable is a hand-over between the two sessions: the outgoing one frozen, the hand-over kept where the person chooses (where each unit stands and on which host, open questions, what must never be redone) and the person''s switch steps, then the successor reconciling it with each host''s live terminals, nothing resent, and supervising on. Do not use for putting the run on hold to resume later (daintree.hosts.park-swarm), an account of a run after a restart or dropped link with no hand-over, or a cleanup (daintree.hosts.recover-and-clean-up), following a run this session sent (daintree.hosts.supervise-swarm), or a coordinator pane''s agents on this machine (daintree.orchestration.hand-off-orchestration).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
Ownership never moves between sessions: an agent the outgoing session launched on a host stays its own. The successor can read it but not send to, interrupt or close it, and its notices reach only the session that armed them. A host's assistant is no one's: the successor sends to and interrupts it, and closes only one it started. So hand each unit over in a state that needs no send from the successor.

Outgoing session:
1. Freeze: no new launch, send or stop; tell the person. Answer only a question already pending, within scope.
2. Live state, per host and project through `host.call`: `terminal.list`, `worktree.list`, one `terminal.getStatus` over the run's terminals with `includeOutput:{"lines":30}`. From your shell, per unit: `git ls-remote origin <branch>`, `gh pr list --head <branch> --state all`.
3. A unit that will need a send (a question, a correction, another round): with the person, let it finish first, or have it commit its work on its branch and stop (pushed only on their word), so the successor continues it with a fresh agent of its own from that SHA. A unit that only needs watching runs on.
4. Write the hand-over where the person names, outside any repository (the sidebar scratch folder does not survive a restart): goal; what the person allowed; per unit host name and `hostId`, `projectId`, terminal id and title, worktree id, branch, verified SHA or PR, brief in full (no host keeps it), state, what it waits on; open questions verbatim, answers sent; never redo: launches, sends, pushes, PRs, merges.
5. Tell the person: open the successor (a new sidebar session, either CLI), or take over themselves, and say "Take over the run <run> from <path>". Then act no more: a later send would make the hand-over untrue.

Successor:
6. Read the hand-over (none: ask the person for scope; never build units from pane titles or branch names). Reconcile each unit with step 2's reads and the remote: moved since, finished, stopped, gone. Where they disagree, live state wins; say so.
7. Inherited units, by reads only, paced to the work: `terminal.getStatus` through `host.call`; a finish is verified by branch, SHA or PR. One that needs a send: the person types it into that pane (the host's project open in this Shell's window); or, once it is stopped at a commit, a fresh agent of yours continues there:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"codex","name":"<unit>: continue","worktreeId":"<worktreeId>","prompt":"Continue <unit> on <branch> from <SHA>; never restart it. <brief and acceptance>. Report the branch and full SHA.","handback":true,"notify":true}}
```

   Close nothing inherited: list it for the person.
8. Your own launches and sends continue under daintree.hosts.supervise-swarm.

Another Shell: it reaches these hosts only if they are its hosts too, owns none of the run's terminals, and its changes in a project another machine drives are refused `DRIVEN_ELSEWHERE`. With the person: finish or stop every unit from this Shell, each confirmed by branch and SHA; the other Shell then briefs only the unfinished ones afresh by shared reference, never this Shell's ids.

On notice or resume: outgoing, a notice before you stop: add its outcome to the hand-over, dispatch nothing. Successor: no notice comes for an inherited unit; recheck it by reads. A notice for your own agent: verify as the foundation's proving a finish says.

Stop and recovery: one hand-over, one successor. Never relaunch a unit that may still be running, or replay a listed write. A unit missing from both the hand-over and the hosts is asked about, never reconstructed.

Report back: outgoing, where the hand-over is, each unit let finish or stopped and at which SHA, and the person's steps; successor, per unit its state, what moved since the hand-over, which it can only watch, what waits on the person.
