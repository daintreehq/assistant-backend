---
id: daintree.runtime.free-busy-port
title: Find and free a busy port
summary: When a server will not start because its port is taken, identify the process holding it, map it to a Daintree terminal, worktree or agent, and free it only when it is provably yours; otherwise report the owner and hand the choice to the user.
whenToUse: 'Use for a ONE-OFF port clash right now — "port 3000 is already in use", "EADDRINUSE", "what''s running on 5173?", "something is holding the port, free it", "which worktree''s server is on 8080?", "there''s a zombie dev server from a closed terminal". The deliverable is who holds the port (process, its directory, the Daintree terminal and worktree, the owner) and what was done: stopped because it was yours, left because it is someone else''s, or the choice put to the user. Do not use to give every worktree its own port or database for good (use daintree.worktree.provision-local-environment), to start a server once the port is free, or to stop one you started yourself in a terminal (use daintree.terminals.run-visible-command), for a Dev Preview panel''s own server (use daintree.runtime.debug-web-app), or when Daintree itself is unresponsive (use daintree.diagnostics).'
risk: system
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
optionalTools:
  - terminal.closeOwned
---
Daintree's part is attribution: which worktree, panel or preview holds the port, and whose it is. The trap is killing whatever holds it: with several worktrees it is usually the user's or another agent's server. Decide the ask first: identify-only ("what's on 5173?", "which worktree's server?") stops after step 3 with the evidence. Freeing continues to step 4; knowing the owner never authorizes a stop by itself.

1. Identify the listener: `lsof -nP -iTCP:<port> -sTCP:LISTEN`; Linux without lsof: `ss -ltnp 'sport = :<port>'`, then `readlink /proc/<pid>/cwd`. For each PID: `ps -o pid,ppid,lstart,command -p <pid>` and its cwd (`lsof -a -p <pid> -d cwd`). The port is free only when the inspection succeeded and found no listener; a missing tool, a permission error or an unreadable process is unknown. A server that still fails may bind another host or port: quote its error.
2. Classify: a container or port forward (`com.docker.backend`, `docker-proxy`, `ssh`), a system service, or a project process whose cwd sits inside a checkout.
3. Attribute through Daintree:
   - Environment, Linux only (macOS `ps` no longer shows it): a process started in a Daintree terminal usually inherits `DAINTREE_PANE_ID` (the terminal id) and `DAINTREE_WORKTREE_ID`; `tr '\0' '\n' </proc/<pid>/environ`. A match with a listed terminal is strong evidence; absent or unmatched proves nothing.
   - Listings: the cwd against `worktree.list` paths; `terminal.list` with that `worktreeId`, then with `location:"trash"` and `location:"background"` (the default hides both), and `owned:true` for yours. A Dev Preview panel's server shows as that panel.
   - Screen: one `terminal.getStatus` with `includeOutput:{"lines":30}` over the candidates, for the start line and port. Screen and cwd are corroboration, not proof.
   The owner is yours (a terminal you opened), the user's or another agent's (terminal or preview), or unknown. Unknown with a cwd in this project is probably a process whose panel closed, but absence from a listing never proves it.
4. Act, only when freeing was asked and step 3 has run. Stop without asking ONLY a process in a terminal `terminal.list` with `owned:true` returns; anything else (the user's project, a closed panel, a "zombie") is not yours: ask first, even when the user said "free it".
   - Yours: `terminal.closeOwned` (check `closedIds`). The panel sits in the trash about 20 s before its process is killed: recheck after that, not at once.
   - A Dev Preview's server: restart or stop it through daintree.runtime.debug-web-app.
   - Someone else's: report whose and where, and ask: they stop it, or this server runs on another port the app reads from its config or env.
   - Unknown: show the PID, command, cwd and the listings you checked, ask, and end the turn. With the user's explicit yes for that PID: immediately before signalling, re-read the listener's PID, command, cwd and start time; any change from what the user approved: stop. `kill <pid>` (TERM) once, recheck after a few seconds; `kill -9` only for that same verified process and only if the user agrees.
   - Container or service: name it and how the user stops it (`docker stop <name>`); never kill it.
5. Re-check with the step-1 inspection. Free means no listener, not "kill returned 0". Start the server again only if asked (daintree.terminals.run-visible-command).
6. Two worktrees wanting one port is structural: free it once, then point to daintree.worktree.provision-local-environment rather than freeing it again each time.

Never `kill $(lsof -t -i:<port>)`: without `-sTCP:LISTEN` it also hits every client connected to the port. Never `pkill node` or `killall`: they take down other worktrees' servers.

Report back: port; PID, command, start time, cwd; terminal id and worktree (or none) with the evidence (pane id, listing, screen), and the owner; identify-only, or what you did, or what you left and the options offered; the re-check result.
