---
id: daintree.hosts.demo-for-hub
title: Prepare a demo on this host for the hub
summary: On a Remote Host's lane, get the feature the hub named running here as a demo at its exact commit with synthetic data, and reply with the SHA served, the host listener and how the Shell would reach it, never a localhost URL claimed for the person's browser.
whenToUse: 'Use on a Remote Host''s lane when the hub asks THIS machine to prepare a DEMO of a feature, or leave it running, for the person to look at from their main machine: "the hub wants a demo of the new checkout flow up on this host so the person can click through it from the Shell", "hub message: run the dashboard branch here as a demo with sample data and reply with the port", "demo the hub''s commit on this machine and say how to reach it". The deliverable is a demo served on this host and the hub told how to reach it: the worktree at the exact SHA the hub named, synthetic seed data proven confined to that worktree''s own database, only the requested services running with their panel ids, start command and who stops them, and one reply naming the SHA served, the host listener and port, and that the Shell reaches it only through its own forward. Do not use for reading the inbox, or answering a hub message that asks for no demo (daintree.hosts.handle-hub-message), code the hub asks this host to implement, fix or build (daintree.hosts.do-delegated-work), checks of a commit that leave nothing running (daintree.hosts.verify-for-hub), a demo the person asks for at this machine (daintree.workflow.prepare-feature-demo), a port another process holds (daintree.runtime.free-busy-port), or the hub''s own side of handing a host work (daintree.hosts.delegate-to-host).'
risk: project
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.launch
  - assistant.reply
optionalTools:
  - worktree.setActive
  - devPreview.restart
  - devPreview.reloadPreview
---
Build the hub's demo by daintree.workflow.prepare-feature-demo with these changes; the exchange and its one reply are daintree.hosts.handle-hub-message's (`get_runbook` either if absent). That reply carries the demo's Report back and step 5.

1. Pin from the brief: remote, branch, full SHA, feature, services wanted. The checkout (`worktree.list`) is on that branch and clean, with `git -C <path> rev-parse HEAD` equal to the SHA; none, or HEAD elsewhere: ask back; never reset, pull or switch one to fit.
2. Data: the prep brief's proof stands (invented data; never a shared, staging or production database), and here a `localhost` database URL or a listener's cwd proves no isolation (an `ssh` forward, a shared container).
3. Run only what was asked: one Dev Preview panel, or the named servers, on loopback. Never `0.0.0.0`, a public tunnel or a forward through another machine. A preview starts only while its worktree is active in this project's view (`worktree.setActive`; say so); `NO_FRONTEND_ATTACHED`: no view attached, a blocker.
4. Before exercising, the listener it finds (PID and port; no action reports them) must have started after step 1 (`ps -p <pid> -o lstart=`). Older: `devPreview.restart` its panel and find it again; not a preview: report it. HEAD is still the SHA when you reply.
5. In the reply:
   - host, project, worktree, branch, the SHA served;
   - the host listener: `localhost:<port>` on this host, PID, panel and pane ids, start command;
   - the Shell's route: only its own forward, made on demand when this project's Dev Preview shows in a Shell window, or Forward port in its Ports view for host port <port>. Its local port may differ, no other host can use it, and no tool here makes or sees it: never offer a URL as the person's;
   - what runs, kept until a new hub message asks for a stop; closing the panel or quitting this host's app stops it, and a preview also stops when backgrounded or hibernated (shown again it may restart: re-probe).

On notice or resume: the prep agent's notice continues the demo runbook, with step 4 before its exercising.

Stop and recovery: one prep agent and one preview per exchange; before any launch, `terminal.list` with `worktreeId` (also `location:"trash"` and `"background"`): the prep agent by `title`, a preview by `kind`. Lost prep state is a blocker, never a second seed.

Report back: the reply as sent and its result.
