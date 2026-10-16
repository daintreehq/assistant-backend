---
id: daintree.hosts.prepare-remote-demo
title: Have a Remote Host prepare a demo and show the person how to open it
summary: From this Shell's hub, have one Remote Host's assistant serve a change as a demo at an exact commit with invented data, check its reply, and tell the person how this Shell reaches it, never a localhost URL claimed for their browser.
whenToUse: 'Use on the hub, the person''s own Shell, when the person wants to SEE a feature running on another machine, a Remote Host this Shell reaches, and look at it from here: "get the new settings page running as a demo on my other machine and tell me how to open it from this Mac", "the remote assistant built the checkout flow, show me it running through the right forwarded port", "prepare a demo of the invoicing branch on the remote host with sample data", "demo it on the other host and make sure I''m looking at the right commit, not an old server". The deliverable is a demo served on that host at the commit named, its assistant''s reply naming the SHA served, and the person told how to open it through this Shell''s own forward, which may land on a different local port because localhost on the host is not localhost here. Do not use for a demo on THIS machine (daintree.workflow.prepare-feature-demo), debugging a preview that is already open (daintree.runtime.debug-web-app), a port another process holds (daintree.runtime.free-busy-port), a question for that host''s assistant whose deliverable is its answer (daintree.hosts.ask-host-assistant), work on that host that leaves no demo (daintree.hosts.delegate-to-host), or the host''s own side of a demo the hub asked for (daintree.hosts.demo-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
The host's lane builds the demo (daintree.hosts.demo-for-hub); you brief it, check its reply and give the person the route. No tool here makes, lists or sees a port forward: the Shell makes it, the person opens it.

1. Resolve the host and lane as daintree.hosts.delegate-to-host's step 1. Pin the remote URL, branch, full SHA (`git ls-remote` shows it pushed; else ask, never push) and the services wanted. A demo already running: step 2 asks for evidence only.
2. One `host.sendMessage`, no `waitMs`: the person's words as a demo prepared for the hub, remote and branch at SHA, then "Use only the existing clean checkout of that branch at the SHA, else ask back. Invented data only, proven in that worktree's own database, else seed nothing. Only the services asked, on loopback; leave other listeners alone; keep running until I say. Reply with the full SHA served, host port, PID and start time, panel and pane ids, start command, seed record, where the page shows its revision, and what you exercised." A running demo: no seeding, restart or start. End the turn.

On notice or resume: `completed`: the SHA equals the pin and the listener began after the checkout, as the lane claims. Less, or `input-required` (no checkout, unproven database): one narrow question, else the person decides.

3. Route: in a Shell window on that host (host menu, Cmd/Ctrl-click its row) a Dev Preview panel is forwarded on demand: the person reads the address it shows. Or Hosts overview, Ports, Forward port for host port <n>: "On this machine" is the local port. It can differ from the host's, and is loopback only: no other host reaches it. Never offer `localhost:<host port>`, an ssh tunnel or a guessed port; a local process on that number stays (daintree.runtime.free-busy-port).
4. Take the address they read; one `curl --max-time 5` of exactly it, for the revision marker. A panel or forward proves no service; a 502 is the Shell's proxy not reaching the host's server: a new message to the lane, not a retry. Clicks stay theirs, unverified until they say.

Stop and recovery: one demo per request; a changed SHA, seed or service is a new message citing the first in `referenceTaskIds`. Stop it by message, never `host.cancelTask`.

Report back: host, project, SHA pinned vs reported, the lane's port, PID and panels as claims, the route, what curl and the person confirmed, what runs and who stops it.
