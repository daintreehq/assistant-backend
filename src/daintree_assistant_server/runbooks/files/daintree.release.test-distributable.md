---
id: daintree.release.test-distributable
title: Test the built distributable
summary: Build the thing users actually install (installer, app bundle, binary, container image or packed package) from an exact revision, install and run it in a clean setting away from the source checkout, and report startup, bundled assets, defaults and the key flows with evidence.
whenToUse: 'Use when the user wants the BUILT ARTIFACT tested the way a user gets it, not the source tree — "it works from source, but does the installer work?", "test the built .dmg on a clean profile", "run the Docker image we would ship, not the dev server", "does the packed npm tarball actually work when installed?", "smoke-test the release binary on a fresh machine setup". The deliverable is the artifact built from a recorded revision with its build command, installed or run in a clean consumer setting (temp dir, new venv, container, empty user-data dir; no dev dependencies, no repo env files), and startup, assets, defaults and the named flows checked, each with its result. Do not use for the project''s unit, lint or build checks from source (use daintree.ci.run-local-checks), for an end-to-end suite against a worktree''s dev server (use daintree.ci.run-e2e-suite), to try a pull request''s head from source (use daintree.git.test-pull-request-locally), to upload the package to a registry (use daintree.release.publish-package), or for several agents exploring the running product for bugs (use daintree.orchestration.bug-bash).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
---
A pass from the source checkout is not evidence here: the question is whether the packaged artifact carries what it needs. A tester agent builds and runs it; you check the artifact, its revision and one smoke yourself. Nothing is published, pushed or signed.

On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Pin the artifact kind (npm tarball, wheel or sdist, container image, CLI binary, desktop installer or app bundle), the revision (tag or SHA; a branch is read now and recorded as a SHA) and the flows to check (the user's, else install, first start and the one core flow).
2. A worktree at that revision: reuse a non-main `worktree.list` row whose HEAD is the SHA with a clean tree and no agent `working` there (`terminal.list` with its `worktreeId`, then `terminal.getStatus` on those ids); else `worktree.createWithRecipe` with `source:{"kind":"newBranch","branchName":"dist-test/<short sha>","baseBranch":"<branch containing it>"}`, and `worktree.waitUntilReady` until `setupState:"ready"`. That starts, and runs setup, at the branch's tip, not <sha>.
3. Launch one agent there, `name:"Dist test: <artifact>"`, `notify:true, handback:true`. The brief:
   - "`git checkout --detach <sha>` if HEAD differs, then re-run the project's install for that revision; HEAD must be <sha> and the tree clean before the build, else stop. Build with the project's own packaging command (release or dist script, Dockerfile, packager config); record the command, the artifact path and its identity: `shasum -a 256` of a file (tarball, wheel, binary, installer; an `.app` bundle: of its zip or dmg); the image ID for a container (`docker build --iidfile`, not a tag).";
   - "Test the artifact, never the checkout, from a fresh `mktemp -d` OUTSIDE every repository (inside one, Node and Python resolve the source tree), with `HOME` pointed at a fresh temp dir and no repo env vars; list any variable, credential or host service it needed. npm: `npm init -y` there and install the `.tgz`; Python: a new venv, `pip install <wheel>`, run from that directory; container: `docker run --rm` of that image ID with no bind mount of the repo and no repo `.env`, only documented variables; binary: copy it there and run it; desktop app: install or copy it to a temp location and start it with an empty user-data dir or profile.";
   - "Check: it starts without error; bundled assets, migrations and templates are present (no missing-file errors, `--version` reports <version>); first run with no config uses sane defaults; then each flow: <flows>. For each: the command, what happened, pass or fail with the output line.";
   - "Never `pip install -e`, `npm link`, run from the repo or add dev dependencies to make it pass. A failure is a finding: report it with its evidence; change no source, commit and push nothing, publish nothing. A code-signing or Gatekeeper block is a finding too; never disable it system-wide. Remove only the temp dirs and containers you created; keep the artifact."
   - "Reply: HEAD, build command, artifact path and identity, the clean setting used, each check with its result, and what you could not check."

```json tool=agent.launch
{"agentId":"claude","name":"Dist test: CLI tarball","worktreeId":"<worktree id>","prompt":"<artifact, revision, flows, the brief above>","notify":true,"handback":true}
```

On notice or resume:
- A failed check is the result, not a task: do not edit, commit, launch a fix or re-test one; report it, offering the fix separately (daintree.ci.diagnose-and-fix).
- Before reporting, check its claims yourself: `git -C <path> rev-parse HEAD` is <sha>; the artifact's identity matches its report (`shasum -a 256`; `docker image inspect <image id>`); list the artifact (`tar -tzf`, `unzip -l`, the image's entrypoint and created time) and look for what the failures name as missing, plus env files or keys that should not ship.
- Re-run one smoke yourself from a clean location (the install and `--version`, or the container's start and a health request) and compare. A result the agent reached from the checkout, an editable install, a bind mount or the user's dev config does not count: send it back once.
- A question within scope: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`.

Stop and recovery: one corrective round; then report what stands. Never rebuild over a result you are still checking, or relaunch on an ambiguous launch (reconcile with `terminal.list`). A flow that needs a person at the GUI stays "not exercised", never "pass".

Report back: artifact kind, revision and build command, artifact path and identity; the clean setting; each check and flow with pass, fail or not exercised and its evidence; what you re-ran yourself vs what the agent reports; findings that block shipping; the agent's terminal id and what it left behind.
