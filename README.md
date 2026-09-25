# Daintree runbook search

A stateless MCP server with one tool, `search_runbooks`. Describe a task in plain
language and it returns the Daintree runbooks that fit — step-by-step procedures for
work in Daintree and its agent workspace — ranked, with their full instructions.
Anyone can add it: no credential, no account, no session.

```json
{
  "mcpServers": {
    "daintree-runbooks": { "type": "http", "url": "https://assistant.daintree.org/v1/daintree/mcp" }
  }
}
```

The tool, its result shape and the protocol details are in [docs/MCP.md](docs/MCP.md).

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
git submodule update --init cli           # the CLI source the runbook hygiene tests read
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
