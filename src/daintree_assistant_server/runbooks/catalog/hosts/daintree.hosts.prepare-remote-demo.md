---
id: daintree.hosts.prepare-remote-demo
title: Have a Remote Host prepare a demo and show the person how to open it
summary: From this Shell, serve a change as a demo on one Remote Host at an exact commit with invented data, check what it serves, and tell the person how this Shell reaches it, never a localhost URL claimed for their browser.
whenToUse: 'Use from the person''s own Shell when the person wants to SEE a feature running on another machine, a Remote Host this Shell reaches, and look at it from here: "get the new settings page running as a demo on my other machine and tell me how to open it from this Mac", "the remote assistant built the checkout flow, show me it running through the right forwarded port", "prepare a demo of the invoicing branch on the remote host with sample data", "demo it on the other host and make sure I''m looking at the right commit, not an old server". The deliverable is a demo served on that host at the commit named, the SHA served checked there, and the person told how to open it through this Shell''s own forward, which may land on a different local port because localhost on the host is not localhost here. Do not use for a demo on THIS machine (daintree.workflow.prepare-feature-demo), debugging a preview that is already open (daintree.runtime.debug-web-app), a port another process holds (daintree.runtime.free-busy-port), a question for that host''s assistant whose deliverable is its answer (daintree.hosts.ask-host-assistant), or work on that host that leaves no demo (daintree.hosts.delegate-to-host).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
You prepare and start the demo on the host through `host.call`, check it and give the person the route. Nothing is merged, pushed or deployed, and feature code does not change. No tool makes, lists or sees a port forward: the Shell makes it, the person opens it.

1. Pin from your shell: remote URL, branch, full SHA (`git ls-remote` shows it pushed; else ask, never push) and the services wanted. `host.list`: the host and the project holding that repository.
2. Checkout, on the host: `worktree.list` for one on that branch, used only if clean at the SHA; else `worktree.createWithRecipe` (`existingBranch`, or `newBranch` from `origin/<branch>` with `fromRemote:true`), then `worktree.waitUntilReady`. A demo already serving there at the SHA: its evidence only, no seeding, no restart.
3. Prep agent there, then end the turn:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","worktreeId":"<worktree id>","name":"Demo prep: <feature>","prompt":"<prep brief>","handback":true,"notify":true}}
```

   Brief: "Confirm HEAD is <SHA> and the tree clean, else stop and say so. Make <feature> runnable here with the project's own install and setup; do not start a server. Invented data only, in this worktree's own database, proven not shared with another checkout; unproven: seed nothing and say so. Change no feature code; commit and push nothing. Reply: setup results, the seed record and how to reset it, the server command and the port it uses, where the page shows its revision, a 3 to 8 step walkthrough, and what works, is stubbed or missing."

On notice or resume: the reply is a claim: HEAD still the SHA, nothing beyond setup artefacts (`project.runCommand` `git rev-parse HEAD; git status --porcelain` in the worktree). A question within scope: answer once by `terminal.sendCommandOwned`, `notify:true`; seeding an unproven database or changing code is the person's.

4. Serve, only the services asked, loopback, other listeners left alone: `agent.launch` there with `agentId:"terminal"`, the `worktreeId` and `name:"Demo server: <feature>"`, then `terminal.sendCommandOwned` with the server command. A Dev Preview panel opens only while a window on that host shows the project (refused otherwise: no frontend attached). The port it really took: `project.runCommand` `lsof -nP -iTCP -sTCP:LISTEN`, then `lsof -a -p <pid> -d cwd` matching the worktree, the listener started after the checkout.
5. Route: Hosts overview, Ports, that host, Forward port <host port>: "On this machine" is the local port. It can differ from the host's, and is loopback only. Or in a Shell window on that host, a Dev Preview panel is forwarded on demand: the person reads the address it shows. Never offer `localhost:<host port>`, an ssh tunnel or a guessed port; a local process on that number stays (daintree.runtime.free-busy-port).
6. Take the address they read; one `curl --max-time 5` of exactly it, for the revision marker. A panel or forward proves no service; a 502 is the Shell's proxy not reaching the host's server: read the server terminal there, never a blind restart. Clicks stay theirs, unverified until they say.

Stop and recovery: one demo per request, one corrective round to the prep agent; a changed SHA, seed or service is a new request. Reconcile an ambiguous launch with `terminal.list` there, never a second server. Leave it running; stop it only on request.

Report back: host, project, worktree, SHA pinned and served; server port, PID and terminal ids, kept with the host; the route; the walkthrough; what curl and the person confirmed, apart from what the prep agent claimed; what runs and who stops it.
