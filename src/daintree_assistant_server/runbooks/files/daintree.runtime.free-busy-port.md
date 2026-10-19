---
id: daintree.runtime.free-busy-port
title: Find and free a busy port
summary: When a server will not start because its port is taken, identify the process holding it on the machine that owns it, map it to a Daintree terminal, worktree or agent, and free it only when it is provably yours; otherwise report the owner and hand the choice to the user.
whenToUse: 'Use for a ONE-OFF port clash right now — "port 3000 is already in use", "EADDRINUSE", "what''s running on 5173?", "something is holding the port, free it", "which worktree''s server is on 8080?", "there''s a zombie dev server from a closed terminal" — on this machine or a Remote Host, where a remote server''s port and the same number on the local machine are two different ports: a remote worktree''s server that cannot bind, or a local port held by a forward or a local process. The deliverable is who holds the port and on which machine (process, its directory, the Daintree terminal and worktree, the owner) and what was done: stopped because it was yours, left because it is someone else''s, or the choice put to the user. Do not use to give every worktree its own port or database for good (use daintree.worktree.provision-local-environment), to start a server once the port is free, or to stop one you started yourself in a terminal (use daintree.terminals.run-visible-command), for a Dev Preview panel''s own server (use daintree.runtime.debug-web-app), for a worktree''s cloud devbox or configured remote resource (use daintree.worktree.manage-resource), when Daintree itself is unresponsive (use daintree.diagnostics), or when the whole machine is slow or out of memory or disk under the agents'' load (use daintree.runtime.relieve-resource-contention).'
risk: system
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
optionalTools:
  - terminal.closeOwned
  - host.list
  - host.call
---
Daintree's part is attribution (machine, worktree, panel, owner); the trap is killing whatever holds it, often the user's or another agent's server. Decide the ask first: identify-only ("what's on 5173?", "which worktree's server?") stops after step 3 with the evidence. Freeing continues to step 4; knowing the owner never authorizes a stop by itself.

Which machine: each has its own loopback; a host's 3000 and this Shell's 3000 are two ports, never one clash. Your shell and listings are where your session runs (a pane or sidebar in a host's window: that host). A host's server is inspected on that host, never here: each step through `host.call` there, as the hosts foundation says, its shell steps by `project.runCommand` (Full) or an agent there (Core), read-only until step 4 allows a stop.

On a host: shell steps via its command runner, or delegate.

1. Identify the listener: `lsof -nP -iTCP:<port> -sTCP:LISTEN`; Linux without lsof: `ss -ltnp 'sport = :<port>'`, then `readlink /proc/<pid>/cwd`. For each PID: `ps -o pid,ppid,lstart,command -p <pid>` and its cwd (`lsof -a -p <pid> -d cwd`). The port is free only when the inspection succeeded with no listener; a missing tool, a permission error or an unreadable process is unknown. A server that still fails may bind another address or port: quote its error.
2. Classify: a container (`com.docker.backend`, `docker-proxy`), a forward (`ssh`), a system service, or a project process whose cwd is inside a checkout. On a Shell, `ssh` or Daintree itself on a loopback port may be a Remote Host forward: the Ports view (Hosts overview) confirms it, as `localhost:<local>` and the host port. The host's number is kept only if free here; Dev Preview and page links clicked in a remote terminal follow another, a browser panel or sign-in callback refuses ("in use on this machine"). Loopback only: no other host reaches it.
3. Attribute through Daintree:
   - Environment, Linux only: a process from a Daintree terminal inherits `DAINTREE_PANE_ID` (its terminal id; `tr '\0' '\n' </proc/<pid>/environ`); matching a listed terminal is strong evidence, absent proves nothing.
   - Listings: the cwd against `worktree.list` paths; `terminal.list` with that `worktreeId`, then with `location:"trash"` and `location:"background"` (the default hides both), and `owned:true` for yours. A Dev Preview panel's server shows as that panel.
   - Screen: one `terminal.getStatus` with `includeOutput:{"lines":30}` over the candidates, for the start line and port. Screen and cwd are corroboration, not proof.
   The owner is yours (a terminal you opened), the user's or another agent's, or unknown. Absence from a listing proves nothing.
4. Act, only when freeing was asked and step 3 has run. Stop without asking ONLY a process in a terminal `terminal.list` with `owned:true` returns; anything else (the user's project, a closed panel, a "zombie") is not yours: ask first, even when the user said "free it".
   - Yours: `terminal.closeOwned` (check `closedIds`). The panel sits in the trash about 20 s before its process dies: recheck then.
   - A Dev Preview's server: restart or stop it through daintree.runtime.debug-web-app.
   - Someone else's: report whose and where, and ask: they stop it, or this server takes another port from its config or env.
   - Unknown: show the PID, command, cwd and the listings you checked, ask and end the turn. With the user's explicit yes for that PID: immediately before signalling, re-read the listener's PID, command, cwd and start time; any change from what was approved: stop. `kill <pid>` (TERM) once, recheck after a few seconds; `kill -9` only for that same verified process and if the user agrees.
   - Container, service or forward: name it and how the user stops it (`docker stop <name>`, a forward's Stop in the Ports view); never kill it (a forward's `ssh` is Daintree's link to the host).
   - A local process on a host server's number blocks only same-address views here: never stop it to keep a URL; offer Dev Preview or the forward's local port outside Daintree.
5. Re-check with the step-1 inspection. Free means no listener, not "kill returned 0". Start the server again only if asked (daintree.terminals.run-visible-command).
6. Two worktrees wanting one port is structural: free it once, then point to daintree.worktree.provision-local-environment.

Never `kill $(lsof -t -i:<port>)` (without `-sTCP:LISTEN` it hits every connected client), `pkill node` or `killall` (other worktrees' servers).

Report back: machine (host or Shell) and port, a forward's local and host ports, the URL and surface in use; PID, command, start time, cwd; terminal id and worktree (or none), the evidence and the owner; identify-only, what you did, or what you left and the options offered; the re-check result.
