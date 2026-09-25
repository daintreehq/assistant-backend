# Daintree runbooks

A stateless MCP server that supplies Daintree runbooks: procedures that Claude Code
and Codex follow to orchestrate work in Daintree. They cover launching visible agents
and waiting on them through Daintree notices, worktrees, reviews, and issue and PR
workflows. `search_runbooks` takes a plain-language task and returns the runbooks
that fit, ranked, with their full instructions. `get_runbook` fetches one by id.
Both return the foundation runbook each procedure builds on. Anyone can add it: no
credential, no account, no session.

The server supplies instructions only. The actions the runbooks call
(`agent.launch`, `terminal.getStatus`, `terminal.notifyWhenIdle`, …) come from the
Daintree app's own MCP connection. See
[docs/agents/DAINTREE_RUNBOOK_CLIENT.md](docs/agents/DAINTREE_RUNBOOK_CLIENT.md).

```json
{
  "mcpServers": {
    "daintree-runbooks": { "type": "http", "url": "https://assistant.daintree.org/v1/daintree/mcp" }
  }
}
```

The tools, their result shapes and the protocol details are in [docs/MCP.md](docs/MCP.md).

## How search works

Runbooks are Markdown files with YAML frontmatter in
`src/daintree_assistant_server/runbooks/files/`. Search is a local two-stage
classifier (`runbooks/local/`), in process, with no model call:

1. A fine-tuned bi-encoder embeds the query and takes the 32 nearest runbooks by
   cosine from a table of embedded runbook documents (title, summary, `whenToUse`).
2. A set-wise head scores those 32 together and predicts how many the query needs,
   zero included. Its ranking is the result; its admitted set is `selected`.

The head reads runbook *embeddings*, not runbook ids, so a runbook added to the
catalog is embedded on load and searchable with no retrain. A retrain
(`.claude/skills/retrain-selector`, pipeline in `training/`) is what makes a new
runbook competitive against trained neighbours. `training/EXPERIMENTS.md` has the
measurements behind the design.

## Adding runbooks

[docs/RUNBOOKS.md](docs/RUNBOOKS.md) is the authoring guide. In short: add
`runbooks/files/<id>.md` (filename stem = `id`), give it a `whenToUse` that says when
it applies and names confusable neighbours by fully-qualified id, add a benchmark
case under `benchmarks/runbook_selection/cases/` that requires it, then:

```bash
pytest tests/unit
python -m benchmarks.runbook_selection.bench coverage   # every runbook has a case
python -m benchmarks.runbook_selection.bench local      # routing quality gate
```

## Development

```bash
git lfs pull                              # the selector artifact is Git LFS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" -c requirements.lock

pytest
ruff check .
mypy --strict src

cp .env.example .env                      # optional; sets PORT=8473
./dev                                     # serve the MCP endpoint locally
```

## Deploying

Pushing to `main` deploys to Cloud Run through `cloudbuild.yaml`: build, deploy a
no-traffic candidate, run `infra/gcp/smoke.sh` against it (including a real
search), then promote. Use the `/release` skill rather than doing it by hand.
`infra/gcp/` holds the one-time setup, the smoke test and a rollback script; see
[infra/gcp/README.md](infra/gcp/README.md).
