---
id: daintree.runtime.migrate-worktree-database
title: Run migrations or reseed a worktree's database
summary: Run a branch's database migrations, seed, rollback or reset, or restore it from a chosen backup, against the database THAT worktree is configured for, after proving it is local and not shared with another checkout, with a backup before anything destructive and the schema version read back after.
whenToUse: 'Use when the user, or the hub reaching this machine''s lane, wants a MIGRATION, SEED, ROLLBACK, DATABASE RESET or RESTORE FROM A BACKUP run against a worktree''s local database now — "run the migrations in the feature worktree", "seed the db for this branch", "reset my worktree''s database", "apply the new migration and check it works", "roll back the last migration on this branch", "restore my worktree''s database from the backup you took", "load yesterday''s dump back into this branch''s db", "which database will this worktree migrate?", "the db host is 127.0.0.1 but may be a tunnel to another environment: which database would a reset hit?". The deliverable is the operation applied to that worktree''s own local database, proven on the machine that runs it to be its own and not shared, with the resulting schema version. Do not use to give each worktree its own database in the first place, or when branches keep clobbering one shared database (use daintree.worktree.provision-local-environment), to fix a migration test that fails in CI (use daintree.ci.diagnose-and-fix), to write a new migration as code work (use daintree.edits.spawn-visible-agent), for a multi-phase data migration plan tracked across sessions (use daintree.orchestration.execute-durable-workflow), to put a deployed environment back on an earlier release (use daintree.release.rollback-deployment), for the worktree''s cloud devbox or remote resource lifecycle (use daintree.worktree.manage-resource), or for a demo the hub asks this host to prepare (use daintree.hosts.demo-for-hub).'
risk: project
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
---
A visible agent in that worktree proves the target and runs the operation; you pick the checkout, brief it and check its evidence. The danger is the target: a shared database breaks for every checkout on it. The requester is the user, or the hub through `daintree.hosts.handle-hub-message`: each stop below is then an `input-required` reply, never a question to a person here. No reply carries a connection string or secret; a backup is a host-local path.

Procedure:
1. Resolve the checkout the requester named (`worktree.list`), the main one or yours if named. `terminal.list` with its `worktreeId`, then `terminal.getStatus`: an agent `working` there may be editing the migrations; ask before launching beside it.
2. Pin the request: target identification only ("which database will it migrate?"), or exactly one of migrate, seed, rollback (to which revision), reset, or restore (one backup: the file named, or one this runbook made, by path and time; never a guessed "latest"). Destructive steps (reset, drop, rollback, restore, a truncating seed or data-dropping migration) need the requester's explicit word even locally, and a backup unless waived. Write the expected endpoint now: head, rollback revision, clean reset, seed rows or the backup's schema version.
3. Launch one agent: the one named, else `claude`; `name:"DB: <branch>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the request, the endpoint and:
   - "Work only in <path>, in this one shell and environment; never switch runner.";
   - "Find the connection as the app resolves it on this host: env files in the framework's precedence, exported variables, names setup derived from the worktree; prefer the framework's answer. Never print a password or a full URL.";
   - "Prove it is this checkout's own. Loopback is not ownership. Name what listens on the port (`lsof -nP -iTCP:<port> -sTCP:LISTEN`); `ssh`, `kubectl`, `socat` or a SQL proxy is a forward: follow it to the real endpoint. Ask the server who it is (Postgres `inet_server_addr()`, `current_database()`, `system_identifier` of `pg_control_system()`; MySQL `@@server_uuid`, `DATABASE()`; SQLite's real path and inode) and compare server plus database or schema with the main checkout and every other worktree. A name is a hint, not proof. Shared, remote, staging, production, equal to another checkout's or unresolved (listener unreadable, server unaskable): stop and report, running nothing.";
   - "Status first: version and pending migrations; dirty or half-applied, or a migration or seeder that drops, truncates or rewrites data I did not name: stop, report it (id, effect).";
   - destructive: "Back up outside the repository first (`pg_dump`, `mysqldump`, SQLite `.backup`; never a copy of a live file); record path, size, time; verify it (`pg_restore --list` for an archive, `PRAGMA integrity_check` on the copy, a plain dump's complete tail) or stop.";
   - restore: "Read <backup> first: origin, engine, version, schema version. After the current-state backup, restore with the project's command naming <backup> and the proven target, shown to me redacted. Either unbound: stop.";
   - "Run the project's own command for exactly that operation; answer no data-loss prompt I have not answered; then check <endpoint> and one smoke query.";
   - "Edit no migrations, config or migrations table; commit nothing. Reply: `git rev-parse HEAD`, `git status --short`, the target (redacted) and isolation evidence, backup path, commands run, version before and after, endpoint check and smoke output, any failure and the state it left."
   Identification only: the brief stops after the isolation proof, "run nothing that writes". End your turn.

On notice or resume:
- Its account is a claim. Isolation first: every other checkout's server and database compared, none equal. A stop at the proof ran no write: check its commands.
- Re-run the framework's status read yourself in <path> against the endpoint; see the backup at its path. A seed: query for the expected rows; a restore: its schema version and key tables' row counts. A different answer is a finding.
- A question inside the request: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`. A new destructive step, a shared or unresolved target or a failure goes to the requester.

Stop and recovery: one corrective round. A failure midway: report the tool's status and the backup path. Never re-run, hand-edit the migrations table or restore a backup the requester did not pick.

Report back: worktree, terminal id; tested SHA, dirty or clean; the target (redacted) and the evidence it is not shared, or what stayed unresolved; backup path or waiver; commands run; version before and after and the endpoint you checked yourself; any failure and the state it left.
