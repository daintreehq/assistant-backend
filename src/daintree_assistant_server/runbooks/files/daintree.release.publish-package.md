---
id: daintree.release.publish-package
title: Publish a package to its registry
summary: Get a package or SDK version built from a clean pinned tree, its artifact inspected, published once to the right registry and channel from a visible terminal with the user's own credentials, and proven installable from a clean consumer.
whenToUse: 'Use when the user wants a library, SDK or package RELEASED TO A REGISTRY — "publish 1.4.0 to npm", "release the SDK to PyPI", "cut a crates.io release of the parser", "push a beta to the next tag", "ship the new version of our client library". The deliverable is that exact version on the registry under the intended dist-tag or channel, built from a clean pinned commit, its packed contents inspected before upload (right files, no secrets, no junk), and a fresh consumer project installing it from the registry and importing it. Do not use to write the notes or changelog for the version (use daintree.forge.draft-release-notes), to ship a service or site to an environment (use daintree.release.deploy-and-verify), to try the built artifact without publishing it (use daintree.release.test-distributable), to move this project onto a newer version of someone else''s package (use daintree.deps.upgrade-dependency), or to merge the release pull request (use daintree.forge.land-pull-request).'
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
  - terminal.moveToWorktree
---
A published version is permanent. An agent prepares and inspects the release files; you verify them, run the one publish command where the user sees it and types their own OTP, then prove it from the registry.

Procedure:
1. Pin the request: package (and its directory in a monorepo), version, registry (from the project, never assumed), channel (npm dist-tag; a prerelease such as `2.0.0-beta.1` never goes out as `latest`), and the base branch, its tip recorded now as <sha>. No version given: the agent proposes one from the project's scheme, and step 4 asks the user.
2. A clean tree: `worktree.createWithRecipe` with `source:{"kind":"newBranch","branchName":"release/<version>","baseBranch":"<base>","collisionPolicy":"error"}`; keep `worktreeId` and path; `worktree.waitUntilReady` until `setupState:"ready"`. It branches from the base's tip, not <sha>: the agent's first check catches a moved base. Never publish from the user's or an agent's checkout.
3. Launch one agent there, `name:"Release: <pkg> <version>"`, `notify:true, handback:true`, and end your turn. The brief:
   - "HEAD must be <sha> with `git status --porcelain` empty; otherwise stop and report.";
   - "Find how this project publishes: release script, `publishConfig`/`.npmrc`/`.pypirc`, any CI workflow publishing on a tag push (its exact tag pattern). Read; run none.";
   - "Check <v> is free with an exact-version query (`npm view <pkg>@<v> version`, `https://pypi.org/pypi/<pkg>/<v>/json`, `https://crates.io/api/v1/crates/<crate>/<v>`); not found is free. Set it per the project's scheme as one commit only if it is not already <v>.";
   - "Delete old build output, build fresh, run the project's checks, make the real release files: `npm pack` (a `.tgz`), wheel and sdist in `dist/`, `cargo package`. List each (`tar -tzf`, `unzip -l`, `twine check`); flag env files, keys, `.pem`, credentials, unshipped tests or fixtures, source maps, anything over 1 MB, missing entry points or types.";
   - "Do not log in, publish, tag or push. Reply: HEAD, where the version is set, each file's path and `shasum -a 256`, the project's own command that uploads exactly those files (`npm publish <file>.tgz --tag <channel>`, `twine upload <files>`; `cargo publish` repacks the same tree) or the CI tag pattern, check results, size, anything flagged."
4. On the notice, verify yourself: `git -C <path> status --porcelain` empty, HEAD <sha> or one bump commit on it; the files' version is <v> and their sha256 match; list them yourself and read the listing; re-run the exact-version query. Show the user version, registry, channel, files and the command.
5. Only if the user said to publish ("get it ready" did not; never re-ask), once, visibly: `terminal.new` with `cwd` = the package directory (`terminal.moveToWorktree` only files it under the worktree), then `terminal.sendCommandOwned` with step 4's command and a sentinel; a failure is reported, never re-sent or swapped for another command. Any login or OTP is the user's to type there, never yours. CI-published: the command is `git tag -a <tag> -m <tag> && git push <remote> <tag>` with the workflow's pattern, then the run's `headSha` must be the bump commit (`gh run list --json headSha,status`).

```json tool=terminal.sendCommandOwned
{"terminalId":"<terminal id>","command":"npm publish ./<pkg>-<v>.tgz --tag <channel>; printf '__DT_%s rc=%d\\n' r7q2 $?"}
```

6. Read `terminal.getStatus` (`includeOutput:{"lines":50}`) paced until the sentinel; a shell sends no notice, and a pending OTP is the user's continuation. `rc=0` is not yet proof.
7. Prove it: <v> under the intended tag (`npm view <pkg> dist-tags`) with your file's hash (npm `dist.shasum` = `shasum <tgz>`; PyPI JSON `digests.sha256`; crates.io `checksum`); then in a fresh `mktemp -d` outside every checkout, a clean consumer: `npm init -y && npm i <pkg>@<v>`, import it; `python -m venv v && v/bin/pip install --no-cache-dir <pkg>==<v>`, import it; `cargo new smoke && cargo add <crate>@=<v> && cargo build`. Registry lag is normal: re-read, never re-publish.

On notice or resume: the agent's report is a claim until step 4. A question within scope: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`. A flagged file or taken version goes to the user.

Stop and recovery: one corrective round with the agent. Publish runs once; after an ambiguous end read the registry: <v> with your hash means published, go to step 7; absent or another hash: report, never retry blind. Never unpublish, yank, deprecate or move a dist-tag unasked; a broken release gets a new patch.

Report back: package, version, registry, channel; the commit published and whether pushed; listing summary, hashes, anything flagged; the publish terminal id and its output; registry read and clean-consumer result, each with the command; what is unverified.
