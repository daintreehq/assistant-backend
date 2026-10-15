---
id: daintree.hosts.diagnose-messaging
title: Diagnose why another machine's assistant can't be messaged
summary: 'From this Shell, find which gate stops messages or starts from reaching a Remote Host''s assistant, from what the host list, that host''s assistant listing and the exchange''s own record show, and name the one setting or reconnect step the person takes, sending nothing.'
whenToUse: 'Use when the user can SEE another machine from this Shell (a Remote Host listed or connected here) but cannot message or start the Daintree assistant on it, and wants to know which gate is in the way: a send or start that came back opted_out, paused, lane_unavailable, policy_rejected, unsupported or rate_limited, this sidebar session refused as not enrolled or as Core, or that host''s assistant listed as unavailable or without a generation. The deliverable is a diagnosis per host and assistant: the gate, what the tools showed, and the one setting or reconnect step the person takes, sending nothing. Do not use for Daintree itself misbehaving on this machine (daintree.diagnostics).'
risk: read
requiredTools:
  - host.list
  - host.listAssistants
optionalTools:
  - host.getTask
  - host.listTasks
  - mcp.surface
---
Reads only: never send, start, cancel or resend. Quote `outcome` and `detail`; fixed `detail` beats a guess.

1. No `host.sendMessage` (`mcp.surface` `tier: core`): Tool set Full (Settings, Daintree Assistant, Security), then a new session.
2. `host.list`. Host missing: not added, or foundation step 1 (Settings, Hosts, that host); a new SSH target ("Change target…") resets both its switches. `connecting`/`disconnected`: the link (daintree.hosts.connect-host); give `lastSeenAt`.
3. `host.listAssistants`: match project, slot. No project: see `projectsOmitted`; no assistant: no lane. `generation: null`: not enrolled for this Shell; the person enrolls it (foundation step 2). A generation, `available:false`: stopped, or paused on either machine; every host at once points to this Shell. Pause: "Pause messages between machines", More actions, that machine's assistant.
4. A known exchange: `host.getTask` (lost id: `host.listTasks`); a `taskId` with `task: null` is not acceptance. `delivery`: `acceptedAt` host has it, `wakeTypedAt` lane notified, `inboxFetchedAt` lane read it, `replyReceivedAt` completed (a question leaves it null). Null is unobserved; later stamps can exist without it. `outcomeCode`: `wake-held` (notice held, cause unknown; stale if `wakeTypedAt` is set), `wake-expired` (no notice in 10 minutes; still in its inbox), `wake-unavailable` (not enrolled, relaunched, notices off...), `inbox-refused`, `expired` (24 h). `not_found`: no record visible now.

Outcomes:
- Sends: `opted_out` step 2; `paused`, `policy_rejected` (not enrolled for this Shell) step 3; `lane_unavailable`: stopped or stale generation, list again.
- Starts: the `detail` names the gate (switch, window, project, slot, CLI); list again.
- `disconnected`: a send never left; reads may be cached (`observedAt: null`). `unsupported`: the host needs a newer Daintree or lacks messaging.
- `rate_limited`: sends and starts 10 per host, 30 in all, a minute; reads 120; wait `retryAfterMs`.
- `ambiguous`: step 4. `conflict`, `expired`, `too_large`: that message, not a gate.
- `SESSION_BINDING_GONE`, `NO_FRONTEND_ATTACHED`, `CONFIRMATION_TIMEOUT`, a project driven elsewhere: only from a lane's reply or a start's `detail`; quote, never infer or take over.

Never route around a gate: no SSH, host MCP key, other Shell or window, terminals, bypass.

Report back per host and lane: gate, evidence, fix and where, unknowns.
