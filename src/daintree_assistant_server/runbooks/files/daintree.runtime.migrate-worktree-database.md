---
id: daintree.runtime.migrate-worktree-database
title: Run migrations or reseed a worktree's database
summary: Run a branch's database migrations, seed, rollback or reset, or restore it from a chosen backup, against the database THAT worktree is configured for, after proving it is local and not shared with another checkout, with a backup before anything destructive and the schema version read back after.
whenToUse: 'Use when the user wants a MIGRATION, SEED, ROLLBACK, DATABASE RESET or RESTORE FROM A BACKUP run against a worktree''s local database now — "run the migrations in the feature worktree", "seed the db for this branch", "reset my worktree''s database", "apply the new migration and check it works", "roll back the last migration on this branch", "restore my worktree''s database from the backup you took", "load yesterday''s dump back into this branch''s db", "which database will this worktree migrate?". The deliverable is the operation applied to that worktree''s own local database, with the target proven and the resulting schema version. Do not use to give each worktree its own database in the first place, or when branches keep clobbering one shared database (use daintree.worktree.provision-local-environment), to fix a migration test that fails in CI (use daintree.ci.diagnose-and-fix), to write a new migration as code work (use daintree.edits.spawn-visible-agent), for a multi-phase data migration plan tracked across sessions (use daintree.orchestration.execute-durable-workflow), or to put a deployed environment back on an earlier release (use daintree.release.rollback-deployment).'
risk: project
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
---
Running it is work: a visible agent in that worktree proves the target and runs the operation; you pick the checkout, brief it and check its evidence. The danger is the target: a shared database breaks for every checkout on it.

Procedure:
1. Resolve the checkout the user named (`worktree.list`: id, path, branch), the main one or yours if named. `terminal.list` with its `worktreeId`, then `terminal.getStatus`: an agent `working` there may be editing the migrations; ask the user before launching beside it.
2. Pin the request: target identification only ("which database will it migrate?"), or exactly one of migrate, seed, rollback (to which revision), reset, or restore (one backup: the file the user named, or one this runbook made, by path and timestamp; never a guessed "latest"). Destructive steps (reset, drop, rollback, restore, a truncating seed or data-dropping migration) need the user's explicit word even locally, and a backup unless waived. Write the expected endpoint now: the migration head, the rollback revision, a clean reset, the seed rows, or the backup's schema version.
3. Launch one agent: the one the user named, else `claude`; `name:"DB: <branch>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the request, the endpoint and:
   - "Work only in <path>, all of it in this one shell and environment; never switch runner.";
   - "Find the connection as the app resolves it here: env files in the framework's precedence, exported variables, names setup derived from the worktree. Prefer the framework's answer (`php artisan db:show`, `npx prisma migrate status`, `alembic current`, `rails db:version`). Never print a password or a full URL.";
   - "Prove it is this checkout's own: resolve the same for the main checkout and every other worktree, defaults included, and compare canonical identities: the real server endpoint plus database or schema (trace localhost forwards and containers to the real server), or SQLite's absolute real path. A name is a hint, not proof. Shared, remote, staging, production or unresolved: stop and report, running nothing.";
   - "Status first: current version and pending migrations; dirty or half-applied: stop, report. A pending migration or seeder that drops, truncates or rewrites data I did not name: stop, report its id and effect.";
   - destructive: "Back up outside the repository first: a consistent dump (`pg_dump`, `mysqldump`, the project's) or SQLite's online backup (`sqlite3 <db> \".backup '<file>'\"`), never a copy of a live file. Record path, size, time; prove it restorable (`pg_restore --list`, `PRAGMA integrity_check`, a dump's complete tail); else stop.";
   - restore: "Read <backup> first: its origin, this engine and version, its schema version. After the current-state backup, restore with the project's documented command or the engine's tool, naming <backup> and the proven target; show it to me redacted. Either unbound: stop.";
   - "Run the project's own command for exactly that operation; answer no data-loss prompt I have not answered. Then check <endpoint> against the same database, plus one smoke query.";
   - "Edit no migrations, config or migrations table; commit nothing. Reply: the target (redacted) and the isolation evidence, backup path, commands run, version before and after, the endpoint check and smoke output, any failure and the state it left."
   Target identification only: the brief stops after the isolation proof, with "run nothing that writes".
4. End your turn.

```json tool=agent.launch
{"agentId":"claude","name":"DB: feat-auth","worktreeId":"<worktree id>","prompt":"<request, endpoint, rules, reply>","notify":true,"handback":true}
```

On notice or resume:
- Its account is a claim. Isolation first: every other checkout compared by canonical identity, none equal; names are not proof.
- Re-run the framework's status read yourself in <path> against the endpoint; see the backup at the path given (a `cp` of the live file is not the backup asked for: say so). A seed: query for the expected rows; a restore: its schema version and row counts of key tables. A different answer from yours is a finding.
- A question inside the request: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`. A new destructive step, a shared target or a failure goes to the user.

Stop and recovery: one corrective round. A failure midway leaves whatever the tool's status says: report it with the backup path. Never re-run, hand-edit the migrations table or restore a backup the user did not pick.

Report back: worktree, path and the agent's terminal id; the target (redacted) and the evidence it is not shared; backup path or waiver, any backup restored; commands run; version before and after and the endpoint you checked yourself; any failure and the state it left.
