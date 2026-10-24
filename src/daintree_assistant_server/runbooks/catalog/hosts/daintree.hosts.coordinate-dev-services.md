---
id: daintree.hosts.coordinate-dev-services
title: Get dev services on different Remote Hosts reaching each other
summary: From this Shell, map the development services running on different Remote Hosts, start dependencies first on their hosts, give every connection an address checked to route from the process that uses it, and prove one request round-trips with a marker the provider itself saw; an address that does not route is reported as the blocked edge.
whenToUse: 'Use from the person''s own Shell when development services that live on DIFFERENT Remote Hosts have to reach each other: a web client on one machine and the API it calls on another, a service and the backend it depends on: "get the frontend dev server on one host talking to the API dev server on the other and prove it reaches the right one", "run these two projects on different machines and verify the client reaches the right API", "bring up a cross-project development stack through the assistants that own each project, backend first", "the client on one host cannot reach the backend on localhost, find out why and what address is right". The deliverable is the stack''s connections made honest: per service its host, project, commit, listener and who stops it; per connection the process that calls and an address checked to route from where that process runs; providers started before consumers; readiness kept apart from one request carrying a marker that the provider''s own log shows; an address that does not route reported as the blocked edge. Do not use for a stage-by-stage plan of dependent code units (daintree.hosts.execute-distributed-workflow), agreeing or changing the interface between the sides (daintree.hosts.coordinate-contract-change), one service or demo on one host (daintree.hosts.delegate-to-host), per-worktree databases, ports or setup scripts on this machine (daintree.worktree.provision-local-environment), a port another process holds (daintree.runtime.free-busy-port), or one Dev Preview''s own runtime failure (daintree.runtime.debug-web-app).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
You map, address and verify; on each service's host one agent you launch there through `host.call` readies and runs it. No tool makes or sees a forward or a host's network, and a host's `localhost` and ports are that host's own, never this machine's.

Procedure:
1. Map, per service: host and project (`host.list`), remote, branch, full SHA (`git ls-remote` in your shell), start command, port, the services it calls. Per edge, the caller: a host's server, or a browser (a Shell window reaches only its own host's forwards).
2. Address each edge:
   - same host: `localhost` there.
   - browser: a Shell forward, loopback on this machine, its local port may differ, no other host reaches it; the person reads it in Ports, you `curl` it from your shell; clicks stay unverified. Another host's API is never browser-direct (private-network addresses are refused): a server calls it.
   - server to server across hosts: an address the person names or the callee's host reports, checked from the CALLER's host once the callee is ready. A loopback bind, refusal or timeout is the blocked edge: report hosts, address, result; the person picks co-location or names one private address the callee rebinds to. Never `0.0.0.0`, a firewall change, a tunnel or a forward.
3. Start providers first, each once what it calls is ready and those edges verified. On its host the worktree on that branch (`worktree.list` there; none: ask, never reset or switch one to fit), then one agent there:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"<run>: <service>","worktreeId":"<worktreeId>","prompt":"<brief>","notify":true,"handback":true}}
```

   The brief: HEAD must be <SHA>, else stop and report; verified addresses in untracked dev config only; invented data in its own local database; no copied credential; never a shared, staging or production endpoint or database; start <command> in a Daintree terminal of its own (`terminal.new`), bound to loopback or the one named address, and leave it running; reply with the SHA served, the bind (address, port, PID), that terminal's id, and one readiness request with its result. End the turn.

On notice or resume:
- A reply without that evidence is a blocker, not ready. Readiness from the service's own host, one bounded request there:

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://localhost:<port>/<path>","timeoutMs":15000}}
```

  An edge across hosts is checked the same way from the caller's host, to the callee's address.
- All ready: from the caller's host one request through its service to the callee with header `X-Dev-Marker: <run>-<sha>`, its status and body; the callee's log must show the marker answered 2xx (`terminal.getOutput` there on its service terminal, or its agent reads the log). Compatible: the body carries the callee's SHA, the log agrees, the SHAs are the pins. Else unverified.
- A question within scope: answer once with `terminal.sendCommandOwned` there, `notify:true, handback:true`; a new endpoint, credential or shared database is the person's.

Stop and recovery: one agent and one start per service; before any launch, `terminal.list` there. A rebind or new SHA: its agent stops the old listener and shows it gone before restarting. Stop only on request, callers first: each agent stops its service and reports the listener gone (`lsof -nP -iTCP:<port> -sTCP:LISTEN` there by `project.runCommand` confirms).

Report back: per service its host, SHA pinned vs served, listener, terminal and who stops it; per edge the caller, address, and from which machine it was verified; readiness and end-to-end apart; blocked edges.
