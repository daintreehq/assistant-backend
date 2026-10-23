# Contributing to the Daintree Assistant

**Report what did not work and what you expected. Issues are the contribution
we want; you do not need to submit code.** Maintainers investigate reports,
decide how they fit the Assistant, and implement the changes. We do not accept
pull request contributions. GitHub restricts new pull requests to repository
collaborators for maintainer work.

## Report a failed workflow

Search [existing issues](https://github.com/daintreehq/assistant-backend/issues)
first. If one matches, add the relevant differences or a minimal example there.
Otherwise use the
[workflow failure form](https://github.com/daintreehq/assistant-backend/issues/new?template=workflow-failure.yml).

Include:

1. **The task:** the request you gave the Assistant, redacted where needed.
2. **Expected outcome:** what successful behaviour would have looked like.
3. **Actual outcome:** what happened and where it diverged. Mention whether the
   wrong procedure was selected, the procedure got stuck, or it assumed something
   that was not true on your machine. It is fine if you cannot tell which.
4. **Environment:** Daintree version, agent and agent version, operating system,
   and whether the work was local or on a remote host. Use “unknown” for versions
   you cannot find.
5. **Minimal evidence:** steps to reproduce, an error message, a runbook ID, or a
   short redacted excerpt, if available. These are helpful, not prerequisites.

One distinct problem per issue is easiest to investigate. An intermittent failure
is useful even if you cannot reproduce it reliably; say how often it occurs.

For example:

> Task: “Move the two agents into separate worktrees and let them continue.”
> Expected: each agent continues in its own worktree.
> Actual: both worktrees were created, but one agent continued in the original
> checkout. I am not sure which runbook was selected.

Use the
[missing workflow form](https://github.com/daintreehq/assistant-backend/issues/new?template=missing-workflow.yml)
for a task the Assistant cannot handle yet. Describe the task and the outcome you
want; a proposed implementation is unnecessary. For substantial behaviour
changes, start with the desired workflow and outcome in an issue.

## Keep reports safe to publish

Issues and their attachments are public. Share only material you have permission
to publish. Remove API keys, tokens, credentials, private source code, customer
data, personal details, machine names and identifying paths. Replace names and
paths with consistent placeholders so the sequence remains understandable.

Do not upload full transcripts, raw session exports, `.env` files, diagnostic
bundles or complete terminal logs. A few relevant, redacted lines are usually
enough. If the problem cannot be explained publicly without exposing sensitive
material, describe the symptoms without attaching it.

For a vulnerability or exposed credential, use the private route in
[SECURITY.md](SECURITY.md).

## What happens to a report

A maintainer may ask for a small clarification or reproduction. A report can lead
to a runbook correction, a new procedure, a benchmark case or a generalised
synthetic training example. Private details and raw transcripts do not belong in
the repository. Maintainers choose priorities and scope; an issue does not imply
a delivery date or a commitment to implement a proposal.

The [runbook guide](docs/RUNBOOKS.md) describes the maintainer implementation and
validation process. Reading it, running Python, downloading a model and writing a
patch are not required to contribute an issue.
