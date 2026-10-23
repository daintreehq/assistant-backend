# Public repository audit — 2026-10-23

## Outcome and contribution model

The repository presents the Daintree Assistant's runbooks and selection backend
as a public project. Useful contributions are reports of failed or missing
workflows, including the task, expected result and observed behaviour. Maintainers
investigate and implement changes; external pull request contributions are not
accepted.

The cleanup changes the current `develop` tree and repository settings. Git
history and existing branches are preserved. No issues or pull requests were
created, and the cleanup does not deploy the production service or retrain the
selector.

## Audit coverage

| Area | Review |
| --- | --- |
| Source tree | All 760 originally tracked files, plus cleanup additions (770 files in the final source scan); server code, runbooks, scripts, configuration and documentation |
| Training data | Curated YAML, 21,650 compiled rows, generation journals, provenance records and archived material |
| Local files | Confirmed environment files, credential exports, local agent configuration and session paths are excluded from Git and build/context bundles |
| GitHub conversations | 113 issue threads, 59 pull requests and 90 conversation comments; no inline review comments or submitted reviews were returned |
| Actions | Enumerated 483 runs; downloaded and scanned all 401 available log archives. GitHub returned HTTP 410 for the other 82 |
| Published artifacts | No GitHub releases or retained Actions artifacts existed at the time of review |
| Package | Built an isolated wheel and inspected all 187 members, metadata, bundled models and licence notices |
| Hosted endpoint | Read-only smoke test against the public domain |

Gitleaks v8.30.1 was downloaded from its upstream release and checksum-verified.
Its directory scan found no credential matches in the reviewed source snapshot,
available Actions logs, or public issue and pull request text. Additional scans
looked for identifying paths, email addresses, maintainer identifiers, cloud
project references and obsolete claims of repository privacy. GitHub's native
secret scanner returned no alerts at the time of the audit.

The scanner results are evidence about the reviewed material, not a guarantee
that all possible sensitive data can be recognised automatically. Unavailable
logs could not be inspected. This was a current-content privacy and public
readiness review, not a rewrite or exhaustive forensic examination of Git
history. Other branches retain their existing contents.

## Findings and changes

### Public documentation and reports

- Replaced the README with a public introduction, hosted MCP configuration,
  repository map and clear reporting path.
- Added `CONTRIBUTING.md`, explicitly welcoming incorrect selections, stuck
  procedures, missing workflows and assumptions that do not match the user's
  machine. Code, a local development environment and knowledge of runbook IDs are
  unnecessary to contribute.
- Added separate GitHub issue forms for failed and missing workflows. Expected
  and actual outcomes are distinct fields. Version details can be “unknown”.
- Kept evidence requests small and optional: a short redacted excerpt, error
  message or reproduction, rather than transcripts or diagnostic bundles.
- Disabled blank issue creation so public reports start with the relevant form.
- Added a pull request template explaining the policy and identifying collaborator
  pull requests as maintainer implementation work.
- Marked runbook authoring and repository automation as maintainer workflows.
  General runbooks for working on users' repositories still cover PR workflows;
  those procedures are product capabilities, not invitations to contribute PRs
  to this repository.

### Privacy and obsolete internal material

- Removed optional agent-capability inventories captured from a particular
  workstation. The shared client contract remains the supported setup reference.
- Replaced identifying personal paths, account handles and an email address in
  synthetic corpus sources and generation records with example values.
- Recompiled the curated corpus and its lockfile. The row count and label space
  are unchanged; no model weights were trained or replaced.
- Removed obsolete internal operations and storage-history notes from the public
  backlog. This removal does not assert that cloud resources were deleted or
  credentials revoked.
- Removed concrete production project defaults from cloud operations. Bootstrap,
  rollback and build watching require an explicit `PROJECT`; smoke testing accepts
  an explicit public URL without cloud credentials. Tests verify that missing
  project settings stop cloud operations before `gcloud` is called.
- Removed the raw infrastructure hostname from maintainer documentation and kept
  the public service domain.
- Kept app-repository references: that repository is public as well.

### Preventing accidental publication

- Added Git exclusions for local MCP/agent settings, agent worktrees and sessions,
  common private-key formats, cloud credential exports and logs.
- Added the corresponding credential exclusions to the Docker build context.
- Replaced outdated CopyTree guidance that recommended switching secret detection
  off. Context exports now exclude raw data, journals, archives and local session
  directories, and tell maintainers to retain secret detection and review exports.
- Added `training/DATA_POLICY.md`. Every data plane is public, including journals
  and archives. Real sessions stay in ignored local storage; examples must be
  synthetic or generalised and reviewed before ingestion.
- Updated journal, corpus and transcript-learning guidance to reflect public
  visibility and allow documented privacy redactions across every data plane.

### Licensing and distribution

- Added the MIT licence for original repository content and replaced the
  proprietary package metadata.
- Added public project URLs to package metadata.
- Added third-party notices identifying the ranking and retriever base models and
  their respective MIT and Apache 2.0 licences, with upstream licence copies.
- Removed a stale local build cache that had retained retired modules, then
  built again from an isolated copy of current source and the locked model.
- Included licences and notices in both packaging configuration and the Docker
  build. Wheel inspection confirmed the notices are present and local
  configuration, tests, raw training records and archived material are absent.

### GitHub settings applied

| Setting | Result |
| --- | --- |
| Repository visibility | Public, verified |
| Default branch | `develop`, preserved |
| Issues | Enabled for public contributions |
| Pull request creation | Restricted to collaborators |
| Description and homepage | Updated to describe the Assistant and issue contributions |
| Secret scanning | Enabled |
| Secret-scanning push protection | Enabled |
| Private vulnerability reporting | Enabled; linked from `SECURITY.md` and the issue chooser |
| Actions default token permissions | Already read-only, with PR review approval disabled; preserved |

No issue/comment bot or new hosted workflow was added. Public reports are not
required to submit code, and automatic dependency-update PRs remain disabled.

## Deliberately retained operational boundaries

The model binaries remain in the authenticated artifact store identified by
`selector.lock`. Its bucket locator and bootstrap default remain functional
references; they are not credentials and do not grant access. Full local serving
and model-dependent validation need authorised artifact access. The README now
states this directly instead of presenting the download as a public setup step.
Public users can inspect the runbooks and use the hosted MCP endpoint without
that access. No public model release, cloud bucket permission change, GPU rental
or production deployment is part of this source cleanup.

The server's request log records method, status, result count, coarse host state
and duration. The inspected MCP transport does not log query text or request
bodies. Production readiness responses already withhold startup error details.
These behaviours were retained.

## Validation

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| `mypy --strict src` | Passed, 26 source modules |
| Full `pytest -q --cov --cov-report=term:skip-covered` run | 825 passed; two fixtures still assumed an implicit cloud project and were corrected |
| Rerun of rollback and shell-script modules | 36 passed, including explicit-project refusal checks |
| Rerun of the smoke-contract module | 45 passed, including URL discovery with an explicit project |
| Coverage | 96.14%, above the required 90% floor |
| Corpus compilation check | Passed, 21,650 rows; 14 rows differ only by identifying-value substitutions, with labels and scenario structure preserved |
| `selector_store.py verify` | Installed artifacts match the unchanged model lock |
| Benchmark coverage | All 143 runbooks have required cases and required neighbour traps |
| Local routing benchmark | Quality gate passed on 1,749 cases, zero candidate misses; thresholds unchanged |
| Public endpoint smoke test | 13 passed, zero failures |
| Isolated wheel build | Passed; 187 members, exactly 26 current Python modules and four licence/notice files |
| GitHub issue forms and local documentation links | YAML/field checks passed; no broken local Markdown targets |
| Publication exclusions | Local environment, agent configuration, credential and session examples are ignored |
| Source, Actions logs and public conversation credential scans | Zero matches |
| `git diff --check` | Passed |

The first full suite's two failures were in test-environment setup, not the
cloud scripts' required refusal behaviour. After updating those fixtures to
supply an explicit example project, all 81 tests in the affected modules passed.
The remaining 746 tests had passed in the full run and their code was unchanged.
An additional duplicate full run was stopped after 304 passing tests once the
complete coverage result was available. The test environment reports one existing
Starlette/httpx deprecation warning.

The local routing gate is a regression threshold, not a claim of perfect
selection: this artifact scored 1,691/1,749 recall-pass cases and 1,577/1,749
strict passes. No model retraining or threshold change was used to make the
cleanup pass.
