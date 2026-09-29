---
id: daintree.runtime.relieve-resource-contention
title: Relieve machine load from agents and test runs
summary: When agents, test runs and servers bog the machine down, measure CPU, memory, disk and process pressure, attribute it to Daintree terminals, worktrees and agents, throttle or serialize only what is authorized, and show a before and after reading.
whenToUse: 'Use when the MACHINE is struggling under the agents'' load and the user wants it relieved — "the agents have made my machine unusable", "everything crawls when the test workers start", "disk is full since the agents started building", "fans are screaming, what''s eating the CPU", "too much is running and it''s swapping". The deliverable is a reading of the pressure (CPU, memory, disk, process count) attributed to Daintree terminals, worktrees and agents; what was throttled, serialized or stopped, with the user''s word for anything this session did not start; and a second reading showing the change. Do not use for one port clash (use daintree.runtime.free-busy-port), one hung agent (use daintree.agents.recover-agent), a status sweep of the agents (use daintree.orchestration.supervise-fleet), stopping everyone for the day (use daintree.orchestration.park-work), closing terminals or deleting worktrees (use daintree.orchestration.workspace-cleanup), benchmarking the project''s code across revisions (use daintree.runtime.measure-performance), or Daintree itself being slow or broken (use daintree.diagnostics).'
risk: system
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandMany
optionalTools:
  - terminal.sendCommandOwned
  - terminal.interruptOwned
  - terminal.closeOwned
  - agent.launch
---
You measure, attribute and run Daintree operations on what this session owns; anything else is the user's call. Never `pkill`, `killall` or `kill -9`; delete no cache, `node_modules`, build output or Docker data unasked; never stop the user's or another checkout's server. Daintree itself as the heavy process: daintree.diagnostics.

Procedure:
1. Reading 1, with the time:
   - Load and memory: `uptime`; macOS `vm_stat`, `memory_pressure` and `sysctl vm.swapusage`; Linux `free -h`.
   - Top processes: macOS `ps -Aro pid,ppid,pcpu,rss,etime,command | head -30` (`-r` sorts by CPU, `-m` by memory); Linux `ps -eo pid,ppid,pcpu,rss,etime,args --sort=-pcpu | head -30`. Count: `ps -A | wc -l`.
   - Disk: `df -h <project path>`; when low, `du -sh` over each worktree's build, cache and dependency directories (paths from `worktree.list`), never the whole disk.
   Name the dominant pressure: CPU, memory and swap, disk, or process count.
2. Attribute the top offenders. Each PID's cwd (`lsof -a -p <pid> -d cwd`; Linux `readlink /proc/<pid>/cwd`) against `worktree.list` paths, walking `ppid` up to its shell; on Linux `DAINTREE_PANE_ID` in `/proc/<pid>/environ` names the terminal. Then `terminal.list` (also `owned:true`, and `location:"background"`) and one `terminal.getStatus` with `includeOutput:{"lines":20}` over that worktree's terminals, matching the command on screen. Classify: this session's (a terminal `owned:true` returns), another agent's or the user's, a process whose panel closed, or outside Daintree (browser, Docker VM, indexer, another checkout). Screen and cwd corroborate; they do not prove.
3. Propose in one message, least disruptive first, each with its expected effect:
   - Throttle an owned agent: fewer test workers (its runner's own flag, such as `--maxWorkers=2`, `-n 2`, `-j 2`), stop a watch mode it started, no second build while one runs. Queued until its turn ends, it limits the next run, not the one burning CPU now.
   - Serialize: owned agents stop and ask before their next full test or build run, and you release one at a time.
   - Immediate relief from a run in progress: `terminal.interruptOwned`, then confirm from its screen and re-read the process list (a test or build child can outlive the interrupt). Or pause lower-priority owned agents at their next turn.
   - Close a server this session started: `terminal.closeOwned`, only on request; its process ends about 20 s later.
   - Anything else: its owner and what the user can do (stop it in its pane, `docker stop <name>`). An orphan: its PID, command, cwd and start time, and `kill <pid>` for the user to run; only when the user asks you to, re-read those four and send TERM once yourself.
   - Disk: the largest directories by worktree, marked regenerable (build output, caches) or not. Deleting a worktree is daintree.orchestration.workspace-cleanup.
   Throttling and serializing owned agents keep their work and need no extra yes; interrupting, pausing, closing and removal wait for the user's pick.
4. Send the agreed changes in one `terminal.sendCommandMany` with `notify:true, handback:true`:

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<agent terminal id>","command":"The machine is overloaded. Keep your task, but run tests with at most 2 workers, stop any watch process you started, and before your next full test or build run, stop and ask me. Reply in one line with what you changed."}],"notify":true,"handback":true}
```

   Disk removal the user approved path by path: one `agent.launch` in that worktree, `notify:true, handback:true`, briefed with the exact paths, "remove only these; skip any directory an agent is working in; no git clean; report each path removed and its size".
5. End the turn; when you sent nothing, take reading 2 now.

On notice or resume:
- A throttle confirmed: the process list shows it (the worker flag in the command, the watch process gone), not only the reply.
- An agent asking to run its suite: one running suite at a time across the set; release it with `terminal.sendCommandOwned`, `notify:true, handback:true`; the next asker waits for that notice.
- A removal notice: re-run `du -sh` on those paths and `df -h` yourself before claiming space.
- Reading 2, once the notices are in: the same commands and fields as reading 1. No real improvement: say so and name the next offender, never declare victory.

Stop and recovery: one throttle message per agent; one TERM per approved PID, never escalated. No notice and load still high: report the pending agents with the current reading; do not wait on. Throttles stay until lifted: tell the user which agents carry one.

Report back: both readings with times; each offender's PID, command, pressure, worktree, terminal and owner; what changed and by whom; what awaits the user's decision; the throttles still standing.
