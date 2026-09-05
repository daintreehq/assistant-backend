# Maintaining the runbook catalog

Runbooks are executable guidance for the Assistant model. The backend owns their files, selection and injection; the CLI owns tool execution, permissions, local state and supervision. Daintree owns the underlying MCP actions and project UI. Read all three layers before teaching an operation.

The catalog lives in [`src/daintree_assistant_server/runbooks/files`](../src/daintree_assistant_server/runbooks/files). The [September 2026 audit](RUNBOOK_AUDIT.md) records the review of all 39 entries, concrete corrections and remaining runtime limitations.

## Authoritative contracts

| Question | Source |
|---|---|
| Which metadata fields are valid? | [`runbooks/models.py`](../src/daintree_assistant_server/runbooks/models.py), [`loader.py`](../src/daintree_assistant_server/runbooks/loader.py) |
| What does selection read? | [`local/documents.py`](../src/daintree_assistant_server/runbooks/local/documents.py), [`local/selector.py`](../src/daintree_assistant_server/runbooks/local/selector.py) |
| Which instructions actually reach the model? | [`active_set.py`](../src/daintree_assistant_server/runbooks/active_set.py), [`prompt_assembler.py`](../src/daintree_assistant_server/services/prompt_assembler.py), [`30-runbooks.md`](../src/daintree_assistant_server/prompts/main/base/30-runbooks.md) |
| What can the CLI call, with which arguments? | [`cli_tool_inventory.json`](../benchmarks/_harness/fixtures/cli_tool_inventory.json), then the handlers in `cli/internal/tools/` |
| What does a forwarded MCP action do? | The live `tool.search` → `tool.schema` contract and Daintree's action implementation, not a similarly named CLI wrapper |
| How is user-facing confirmation written? | [`RUNBOOK_CONFIRMATIONS.md`](RUNBOOK_CONFIRMATIONS.md) |
| How are fixtures refreshed? | [`benchmarks/_harness/fixtures/README.md`](../benchmarks/_harness/fixtures/README.md) |

The fixture records the pinned CLI contract and works in CI without the submodule. Do not refresh it from a different checkout without updating the pin and provenance together. A current Daintree checkout may be newer than the pinned CLI; identify which layer changed and teach feature detection where necessary.

## Editing a procedure

1. Read its whole body, foundation, related runbooks and relevant handler/tests. Resolve contradictions in the original instruction rather than appending an opposite caveat. Keep shared rules in the foundation and specific failure handling next to the step it affects.
2. State target scope, concrete arguments, result envelope, success evidence, partial failure/retry behavior and stopping condition. Preserve returned ids. A successful tool envelope can contain a failed check; a missing result section is not an empty workspace; a wait is not verification.
3. Preserve the user's requested outcome and existing authority. A runbook does not authorize extra writes, terminal closure or public messages. Conversely, it should not demand another approval for already authorized work; runtime tool gates apply independently.
4. Distinguish tool rounds from user turns. A dependent call must wait for a producer's result, but can run in the next tool round of the same user turn. End the user turn after establishing async supervision, not merely because a spawn minted an id.
5. Keep resumption evidence separate from execution. Scratch is session-local; flat workflow rows, execution graphs and runbook step checkpoints are different records. Recover their actual state before replaying a write. An idempotency key is a bounded dedupe mechanism, not indefinite exactly-once execution.
6. Put complete CLI argument examples in a tagged JSON fence. The tool name belongs in the fence metadata; the JSON object contains only its arguments. For example:

````markdown
```json tool=forge.getPR
{"worktreeId":"<resolved worktree id>","prNumber":42}
```
````

   [`test_runbook_examples.py`](../tests/unit/test_runbook_examples.py) checks these against the captured CLI schemas. For a deferred CLI call it also validates the nested target arguments. It does not validate arbitrary prose, negative examples, nested raw MCP arguments, result semantics or permissions. The JSON Schema validator is a development-only dependency. Those still require handler review.
7. Keep bodies under the existing size budgets. Remove duplication before adding another rule. Do not increase a ceiling to accommodate explanatory history; put that history in this maintainer documentation.

Changing a body or frontmatter updates the catalog content revision and invalidates stale selection state. The ranker's document is derived from title, summary and `whenToUse`, not the body. Preserve that routing text unless routing needs to change; changed routing text is re-embedded and must be measured. `requiredTools` provides focus hints and workflow-plan grounding, not a capability gate.

## Verification

From the repository root, with development dependencies installed:

```bash
rtk proxy .venv/bin/python -m pytest tests/unit/test_runbook_examples.py tests/unit/test_relocated_base_rules.py tests/unit/test_tool_projection.py tests/unit/test_runbook_catalog_hygiene.py tests/unit/test_prompt_budgets.py tests/unit/test_runbook_confirmations.py tests/unit/test_runbooks_loader.py tests/unit/test_pinned_docs_runbook.py tests/integration/test_runbooks_reload.py tests/integration/test_runbook_confirmation_stream.py -q
rtk proxy .venv/bin/python -m benchmarks.runbook_selection.bench coverage
rtk proxy .venv/bin/python -m benchmarks.runbook_selection.bench local --no-save
rtk proxy .venv/bin/python -m ruff check .
rtk proxy git diff --check
```

For a catalog-wide change, run the full Python suite and compare routing against the original catalog. Passing the routing quality gate does not mean every case passes, and unchanged selection does not establish execution quality. Report existing misses separately from regressions. Live tool-use trials require a connected Daintree instance and a backend; do not claim such validation from offline schemas or selector tests.

Do not regenerate prompt goldens indiscriminately. Inspect a failure first and update only intentional prompt changes. Do not alter generated CLI references or selector artifacts to make prose checks pass.

The [main-assistant behavior battery](../benchmarks/main_behavior/README.md) adds synthetic tool trajectories through the real backend response loop. Use isolation to separate procedure quality from routing, and end-to-end mode to exercise actual selection. Offline replay verifies the harness; paid model trials and human review are separate qualification steps, not implied by the catalog checks above.
