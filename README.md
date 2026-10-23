# Daintree Assistant

The runbooks and selection backend for the [Daintree Assistant](https://daintree.org).
Runbooks are procedures that help an assistant coordinate agents, manage worktrees,
review work, recover from failures, and finish tasks in Daintree.

This repository is public so you can inspect those procedures and help improve
them by reporting what happened when a workflow failed or was missing.

## Help improve the Assistant

**Issues are contributions. You do not need to write code.**

[Report a workflow that failed](https://github.com/daintreehq/assistant-backend/issues/new?template=workflow-failure.yml)
or [suggest a missing workflow](https://github.com/daintreehq/assistant-backend/issues/new?template=missing-workflow.yml).
The most useful reports tell us:

- what you asked the Assistant to do;
- what you expected to happen;
- what actually happened, including where it stopped or went wrong;
- your Daintree version, agent and agent version, and operating system;
- a short, redacted example, if one helps explain the problem.

Wrong runbook selections, procedures that get stuck, incorrect assumptions about
your setup, and missing workflows all belong here. You do not need to know which
runbook was involved. Search existing issues first, and omit credentials, private
code, personal paths and full transcripts.

**We do not accept pull request contributions.** Maintainers investigate reports
and implement the changes. Public pull request creation is restricted to
repository collaborators. See [CONTRIBUTING.md](CONTRIBUTING.md) for reporting
guidance and [SECURITY.md](SECURITY.md) for private vulnerability reports.

## Use the runbooks

The hosted MCP endpoint is public and requires no account or credential:

```json
{
  "mcpServers": {
    "daintree-runbooks": {
      "type": "http",
      "url": "https://assistant.daintree.org/v1/daintree/mcp"
    }
  }
}
```

`search_runbooks` selects procedures for a plain-language task. `get_runbook`
retrieves a procedure by ID. Selected procedures include their instructions and
supporting foundations; neighbouring search results carry metadata. Foundation
instructions are delivered once per MCP session unless explicitly requested again.

This server supplies instructions. Daintree's own MCP connection supplies the
actions that operate agents, terminals, worktrees and hosts. The app and this
backend are separate projects; making this backend public does not install or
launch Daintree.

See the [MCP contract](docs/MCP.md) and
[client setup](docs/agents/DAINTREE_RUNBOOK_CLIENT.md).

## How selection works

Runbooks are Markdown files with YAML frontmatter in
[`src/daintree_assistant_server/runbooks/catalog/`](src/daintree_assistant_server/runbooks/catalog/).
Selection runs locally in the server, without a hosted model call:

1. A fine-tuned bi-encoder retrieves the 32 nearest runbooks for a task.
2. A set-wise head ranks those candidates and selects how many apply, including zero.

The head reads embeddings rather than per-runbook IDs. New runbooks can be
embedded when the catalog loads; training improves their selection against
similar procedures. The [training guide](training/README.md) and
[experiment record](training/EXPERIMENTS.md) describe the design and measurements.

## Repository guide

| Path | Contents |
| --- | --- |
| `src/daintree_assistant_server/` | MCP server, selector runtime and current runbooks |
| `docs/MCP.md` | Tools, response shapes and protocol behaviour |
| `docs/RUNBOOKS.md` | Maintainer guide to writing and validating procedures |
| `benchmarks/` | Selection cases and routing quality checks |
| `training/` | Synthetic training examples, curation and training tools |
| `tests/` | Server, catalog, selector and deployment checks |
| `infra/gcp/` | Maintainer deployment and smoke-test scripts |
| `archive/` | Retired procedures and experiments, excluded from the package |

The retired v0.44 inbox catalog is historical material. All clients receive the
current direct-control catalog; the archive is never loaded or packaged.

## Maintainer setup

Python 3.12 is required. Reporting an issue requires none of these tools.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" -c requirements.lock

ruff check .
mypy --strict src
python -m training.compile_corpus --check
python -m benchmarks.runbook_selection.bench coverage
```

The selector's model binaries are stored separately from Git. Full server startup,
model-dependent tests and routing evaluation require the exact files named by
`selector.lock`. The current artifact store requires authorised Google Cloud
access; a public clone alone does not grant that access. The hosted endpoint above
is available without downloading a model.

With access to the artifact store and `gcloud` installed:

```bash
training/fetch_selector.sh
python -m pytest --cov --cov-report=term:skip-covered
python -m benchmarks.runbook_selection.bench local

cp .env.example .env
./dev
```

The local MCP URL with the example settings is
`http://127.0.0.1:8473/v1/daintree/mcp`. Runbook changes follow the checks in the
[maintainer authoring guide](docs/RUNBOOKS.md). Training and cloud deployment
require separate maintainer access and explicit authorisation.

Changes land on `develop`. Pushing `main` deploys through Cloud Build: build a
candidate, smoke-test it, then promote it. See
[deployment instructions](infra/gcp/README.md).

## Licence

The original code, runbooks and synthetic data in this repository are available
under the [MIT licence](LICENSE). Third-party dependencies and base models retain
their own licences; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
