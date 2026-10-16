---
id: daintree.hosts.coordinate-dev-services
title: Get dev services on different Remote Hosts reaching each other
summary: From this Shell's hub, map the development services running on different Remote Hosts, start dependencies first through each host's assistant, give every connection an address checked to route from the process that uses it, and prove one request round-trips with a marker the provider itself saw; an address that does not route is reported as the blocked edge.
whenToUse: 'Use on the hub, the person''s own Shell, when development services that live on DIFFERENT Remote Hosts have to reach each other: a web client on one machine and the API it calls on another, a service and the backend it depends on: "get the frontend dev server on one host talking to the API dev server on the other and prove it reaches the right one", "run these two projects on different machines and verify the client reaches the right API", "bring up a cross-project development stack through the assistants that own each project, backend first", "the client on one host cannot reach the backend on localhost, find out why and what address is right". The deliverable is the stack''s connections made honest: per service its host, project, commit, listener and who stops it; per connection the process that calls and an address checked to route from where that process runs; providers started before consumers; readiness kept apart from one request carrying a marker that the provider''s own log shows; an address that does not route reported as the blocked edge. Do not use for a stage-by-stage plan of dependent code units (daintree.hosts.execute-distributed-workflow), agreeing or changing the interface between the sides (daintree.hosts.coordinate-contract-change), one service or demo on one host (daintree.hosts.delegate-to-host), a host''s own side of a demo (daintree.hosts.demo-for-hub), services on this machine (daintree.worktree.provision-local-environment), a port another process holds (daintree.runtime.free-busy-port), or one Dev Preview''s own runtime failure (daintree.runtime.debug-web-app).'
risk: external
requiredTools:
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.list
  - host.listTasks
---
Lanes run their own services (daintree.hosts.demo-for-hub per service, no walkthrough). No tool here makes or sees a forward or a host's network.

1. Map, per service: host, project, lane (`host.listAssistants`), remote, branch, full SHA (pushed: `git ls-remote`), start command, port, services it calls. Per edge, the caller: a host's server (its `localhost` is that host's own) or a browser (a Shell window reaches only its own host's forwards).
2. Address each edge, assuming nothing:
   - same host: `localhost` there.
   - browser: a Shell forward, loopback here, local port may differ, no other host reaches it; the person reads it in Ports, you `curl` it; clicks stay unverified. Never a browser to another host's API (such pages are refused private-network addresses): a server calls it.
   - server to server across hosts: only an existing route, an address the person names or the callee's lane reports; ask the caller's lane for one request to it from its own machine. A loopback bind, refusal or timeout is the blocked edge: report hosts, address, result; the person picks co-location or names one private address (the only exception to demo-for-hub's loopback). Never `0.0.0.0`, a firewall, a tunnel or a forward.
3. Start each service once, when what it calls is ready and those edges verified: one `host.sendMessage`, `taskId` `<run>-<service>`, no `waitMs`: remote, branch, SHA; verified addresses in untracked dev config; invented data; no credential from another host; never a shared, staging or production endpoint or database. Reply as demo-for-hub does, plus the bind, a good readiness request and who stops it. Probes and starts are separate rounds; end the turn each time.

On notice or resume: `completed` without that evidence is a blocker, not ready. A changed address or SHA is a new `taskId`, facts in its text.

4. All ready: the caller's lane sends one request through its service to its callee with header `X-Dev-Marker: <run>-<sha>`, replies status and body; the callee's lane replies whether its log shows the marker. Compatible: expected response, marker seen, SHAs the pins. Unseen: unverified.

Stop and recovery: one start per service. On request, message each lane to stop its service and reply its listener is gone, callers first.

Report back: per service, SHA pinned vs served, owner; per edge, caller, address, how verified; readiness and end-to-end apart; blocked edges; what runs.
