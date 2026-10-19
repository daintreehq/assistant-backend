---
id: daintree.hosts.diagnose-messaging
title: Diagnose why another machine's assistant can't be messaged
summary: 'From this Shell, find which gate stops messages or starts from reaching a Remote Host''s assistant, from what the host list, that host''s assistant listing and the exchange''s own record show, and name the one setting or reconnect step the person takes, sending nothing.'
whenToUse: 'Use when the user can SEE another machine from this Shell (a Remote Host listed or connected here) but cannot message or start the Daintree assistant on it, and wants to know which gate is in the way: a send or start that came back opted_out, paused, lane_unavailable, policy_rejected, unsupported or rate_limited, or that host''s assistant listed as unavailable or without a generation. The deliverable is a diagnosis per host and assistant: the gate, what the tools showed, and the one setting or reconnect step the person takes, sending nothing. Do not use for Daintree itself misbehaving on this machine (daintree.diagnostics).'
risk: read
requiredTools:
  - host.list
  - host.listAssistants
optionalTools:
  - host.getTask
  - host.listTasks
hostEligibility: configured
---
Reads only: never send, start, cancel or resend. Quote `outcome` and `detail`; fixed `detail` beats a guess.

1. No `host.*` tools, or `NOT_ELIGIBLE`: foundation step 1.
2. `host.list`. Host missing: not added, or "Allow assistant messaging with this host" off (Settings, Hosts, that host). `connecting`/`disconnected`: the link (daintree.hosts.connect-host); give `lastSeenAt`.
3. `host.listAssistants`: match project, slot. No project: see `projectsOmitted`; no assistant: none running. Running, `generation: null`: closed by its own "Accept messages from other machines" (its menu), its host's default (Settings, Daintree Assistant) or an old Daintree. A generation, `available:false`: not taking messages, or paused on either machine; every host at once points to this Shell. Pause: "Pause messages between machines", More actions, on that machine.
4. A known exchange: `host.getTask` (lost id: `host.listTasks`); a `taskId` with `task: null` is not acceptance. `delivery`: `acceptedAt` host has it, `wakeTypedAt` lane notified, `inboxFetchedAt` lane read it, `replyReceivedAt` completed (a question: null). Null is unobserved; later stamps can exist without it. `outcomeCode`: `wake-held` (notice held, cause unknown; stale if `wakeTypedAt` is set), `wake-expired` (no notice in 10 minutes; still in its inbox), `wake-unavailable` (lane not running, notices off), `inbox-refused`, `expired` (24 h). `not_found`: no record visible now.

Outcomes:
- Sends: `opted_out` step 2's switch, or this Shell's default (step 3); `paused`, `policy_rejected` step 3; `lane_unavailable`: list again.
- Starts: `opted_out` as sends, or "Let the assistant start assistants on this host" off (step 2); else `detail` names it (window, project, slot, CLI); list again.
- `disconnected`: a send never left; reads may be cached (`observedAt: null`). `unsupported`: an older Daintree there, or no messaging.
- `rate_limited` (per Shell): sends and starts 10 per host, 30 in all, a minute; reads 120; wait `retryAfterMs`.
- `ambiguous`: step 4. `conflict`, `expired`, `too_large`: that message, not a gate.
- `SESSION_BINDING_GONE`, `NO_FRONTEND_ATTACHED`, `CONFIRMATION_TIMEOUT`, a project driven elsewhere: only from a lane's reply or a start's `detail`; quote, never infer or take over.

Never bypass a gate (SSH, host MCP key, other Shell or window, terminals).

Report back per host and lane: gate, evidence, fix and where, unknowns.
