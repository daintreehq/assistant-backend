---
id: daintree.release.publish-package
title: Publish a package to its registry
summary: Get a package or SDK version built from a clean pinned tree, its artifact inspected, published once to the right registry and channel from a visible terminal with the user's own credentials, and proven installable from a clean consumer.
whenToUse: 'Use when the user wants a library, SDK or package RELEASED TO A REGISTRY — "publish 1.4.0 to npm", "release the SDK to PyPI", "cut a crates.io release of the parser", "push a beta to the next tag", "ship the new version of our client library". The deliverable is that exact version on the registry under the intended dist-tag or channel, built from a clean pinned commit, its packed contents inspected before upload (right files, no secrets, no junk), and a fresh consumer project installing it from the registry and importing it. Also when a swarm of hosts shares the work and exactly one host is the publisher of a publish authorised now: the hub''s brief to it, its check of its own registry account and channel, and its lost reply reconciled against the registry. Do not use to write the notes or changelog for the version (use daintree.forge.draft-release-notes), to ship a service or site to an environment (use daintree.release.deploy-and-verify), to try the built artifact without publishing it (use daintree.release.test-distributable), to move this project onto a newer version of someone else''s package (use daintree.deps.upgrade-dependency), or to merge the release pull request (use daintree.forge.land-pull-request).'
risk: external
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - terminal.new
  - terminal.sendCommandOwned
  - terminal.getStatus
optionalTools:
  - worktree.list
  - terminal.list
  - host.sendMessage
  - host.getTask
  - assistant.reply
---
A published version is permanent. An agent prepares and inspects the release files; you verify them, run the one publish command where the user sees it and types their own OTP, then prove it from the registry.

Across hosts, one designated host per package and channel publishes (the person names it); the others only build and inspect, read-only (daintree.hosts.run-validation-matrix). Hub: keep a line per package and channel (host, `taskId`, SHA, run id) and one brief to the owner, by daintree.hosts.delegate-to-host's pins: package, version, registry, channel, full SHA, "you alone publish it", no credentials. The rest is the owner's; you only read the registry. Lane: step 1 first, replying with what it resolved; anything differing (registry, account, channel, SHA) is `assistant.reply` input-required, nothing run.

Procedure:
1. Pin the request: package (and its directory), version, registry (the project's, never assumed), channel (npm dist-tag; a prerelease such as `2.0.0-beta.1` never goes out as `latest`), and the base branch, its tip recorded now as <sha> (a hub's SHA is the pin). No version: the agent proposes one; step 4 asks. Preflight, reading only: the registry and account this machine publishes as (`npm config get registry`, `npm whoami`) match and may publish this package; else ask back.
2. A clean tree: `worktree.createWithRecipe` with `source:{"kind":"newBranch","branchName":"release/<version>","baseBranch":"<base or pinned sha>","collisionPolicy":"error"}`; `worktree.waitUntilReady` until `setupState:"ready"`. A branch base is its tip: the agent's first check catches a move. Never publish from the user's or an agent's checkout.
3. Launch one agent there, `name:"Release: <pkg> <v>"`, `notify:true, handback:true`, end your turn. The brief:
   - "HEAD must be <sha> and `git status --porcelain` empty, else stop and report.";
   - "Check <v> is free by exact-version query (`npm view <pkg>@<v> version`, PyPI `/pypi/<pkg>/<v>/json`, crates.io `/api/v1/crates/<crate>/<v>`); not found is free. Set it per the project's scheme, one commit, only if not already <v>.";
   - "Delete old output, build fresh, run the checks, make the real release files (`npm pack`, wheel and sdist in `dist/`, `cargo package`), list each (`tar -tzf`, `unzip -l`, `twine check`) and flag env files, keys, unshipped tests, anything over 1 MB, missing entry points.";
   - "No login, publish, tag or push. Reply: HEAD, where the version is set, each file's path and `shasum -a 256`, check results, flags, and the project's own command that uploads exactly those files (`npm publish <file>.tgz --tag <channel>`, `twine upload <files>`; `cargo publish` repacks) or its CI tag pattern."
4. On the notice, verify yourself: `git -C <path> status --porcelain` empty, HEAD <sha> or one bump commit on it; the files' version is <v> and their sha256 match; read their listing; re-run the exact-version query. Show the user version, registry, channel, files, command.
5. Only if the user said to publish ("get it ready" did not; never re-ask), once, visibly: `terminal.new` with `cwd` = the package directory, then `terminal.sendCommandOwned` with step 4's command and a sentinel (`; printf '__DT_%s rc=%d\n' r7q2 $?`); a failure is reported, never re-sent or swapped. CI-published: tag the bump commit with the workflow's pattern and push the tag; the run's `headSha` must be that commit (`gh run list --json headSha,status`).
6. Read `terminal.getStatus` (`includeOutput:{"lines":50}`) paced until the sentinel; a shell sends no notice, and a pending OTP is the user's. `rc=0` is not yet proof.
7. Prove it: <v> under the intended tag (`npm view <pkg> dist-tags`) with your file's hash (npm `dist.shasum` = `shasum <tgz>`; PyPI JSON `digests.sha256`; crates.io `checksum`); then in a fresh `mktemp -d` outside every checkout, a clean consumer installs and imports it (`npm i <pkg>@<v>`; `pip install --no-cache-dir <pkg>==<v>` in a new venv; `cargo add <crate>@=<v>`, build). Registry lag: re-read, never re-publish.

On notice or resume: a question within scope: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; a flagged file or taken version goes to the user.

Stop and recovery: one corrective round with the agent. Publish runs once; after an ambiguous end (lost reply) read the registry: <v> with your hash means published, go to step 7; absent or another hash: report, never retry blind or from another host; an unreachable owner: you read the registry. Never unpublish, yank, deprecate or move a dist-tag unasked; a broken release gets a new patch.

Report back: package, version, registry, channel; commit published, pushed or not; listing, hashes, flags; publish terminal id and output; registry read and clean-consumer result, with commands; the owner host's claims vs what you read; what is unverified.
