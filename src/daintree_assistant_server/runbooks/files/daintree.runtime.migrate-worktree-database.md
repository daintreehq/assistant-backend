---
id: daintree.runtime.migrate-worktree-database
title: Run migrations or reseed a worktree's database
summary: Run a branch's database migrations, seed, rollback or reset against the database THAT worktree is configured for, after proving it is local and not shared with another checkout, with a backup before anything destructive and the schema version read back after.
whenToUse: 'Use when the user wants a MIGRATION, SEED, ROLLBACK or DATABASE RESET run against a worktree''s local database now — "run the migrations in the feature worktree", "seed the db for this branch", "reset my worktree''s database", "apply the new migration and check it works", "roll back the last migration on this branch", "which database will this worktree migrate?". The deliverable is the operation applied to that worktree''s own local database, with the target proven and the resulting schema version. Do not use to give each worktree its own database in the first place, or when branches keep clobbering one shared database (use daintree.worktree.provision-local-environment), to fix a migration test that fails in CI (use daintree.ci.diagnose-and-fix), to write a new migration as code work (use daintree.edits.spawn-visible-agent), or for a multi-phase data migration plan tracked across sessions (use daintree.orchestration.execute-durable-workflow).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Running it is work: a visible agent in that worktree proves the target and runs the operation; you pick the checkout, brief it and check its evidence. The danger is the target: a branch's migration run against a database another checkout also uses breaks it for everyone. Only when the user asks you yourself to run it, follow the brief in your own shell.

Procedure:
1. Resolve the checkout the user named (`worktree.list`: id, path, branch), the main checkout or your own included when that is the one named. `terminal.list` with its `worktreeId`, then `terminal.getStatus`: an agent `working` there may be editing the migrations; ask the user before launching beside it.
2. Pin the request: target identification only ("which database will it migrate?"), or exactly one of migrate, seed, rollback (to which revision) or reset. Destructive steps (reset, drop, rollback, a truncating seed, a migration that drops or rewrites data) need the user's explicit word even locally, and a backup unless the user waived it. Write the expected endpoint now: the migration head, the rollback revision, a clean reset, or the seed rows to find.
3. Launch one agent: the one the user named, else `claude`; `name:"DB: <branch>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the request, the endpoint and:
   - "Work only in <path>, and do discovery, status, backup, the operation and verification in this one shell and environment; never switch to another runner.";
   - "Find the connection as the app resolves it here: env files in the framework's precedence, exported variables, names setup derived from the worktree. Prefer the framework's answer (`php artisan db:show`, `npx prisma migrate status`, `alembic current`, `rails db:version`, `python manage.py showmigrations`). Never print a password or a full URL.";
   - "Prove it is this checkout's own: resolve the same for the main checkout and every other worktree, those on defaults included, and compare canonical identities: the real server endpoint plus database or schema (trace localhost forwards and containers to the actual server), or SQLite's absolute real path. A name is a hint, not proof. Shared, remote, staging, production or unresolved: stop and report, running nothing.";
   - "Status first: current version and pending migrations; dirty or half-applied: stop and report. Read the pending migrations and seeds and list anything that drops or rewrites data.";
   - destructive: "Back up outside the repository first: a consistent dump (`pg_dump`, `mysqldump`, the project's command) or SQLite's online backup (`sqlite3 <db> \".backup '<file>'\"`), never a copy of a live file. Check the backup is readable; if not, stop.";
   - "Run the project's own command for exactly that operation; answer no data-loss prompt I have not answered. Then check <endpoint> against the same database, plus one smoke query.";
   - "Edit no migrations, config or migrations table; commit nothing. Reply: the target (redacted) and the isolation evidence, backup path, commands run, version before and after, the endpoint check and smoke output, any failure and the state it left."
   Target identification only: the brief stops after the isolation proof, with "run nothing that writes".
4. End your turn.

```json tool=agent.launch
{"agentId":"claude","name":"DB: feat-auth","worktreeId":"<worktree id>","prompt":"<request, endpoint, rules, reply>","notify":true,"handback":true}
```

On notice or resume:
- Its account is a claim. Isolation first: every other checkout compared by canonical identity, none equal; missing or name-only evidence is not proof.
- Before any report, re-run the framework's status command yourself in <path> (a read) and compare it with the endpoint; see the backup file at the path given (a `cp` of the live file is not the online backup asked for: say so). A seed: query for the rows. A different answer from yours is a finding.
- A question inside the request: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`. A new destructive step, a shared target or a failure goes to the user.

Stop and recovery: one corrective round. A failure midway leaves whatever the tool's status says: report it with the backup path. Never re-run, hand-edit the migrations table or restore the backup without the user. A lost launch reply: `terminal.list` before a second launch.

Report back: worktree, path and the agent's terminal id; the target (redacted) and the evidence it is not shared; backup path or the user's waiver; commands run; version before and after and the endpoint you checked yourself; any failure and the state it left.
