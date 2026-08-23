# Harness fixtures

## `cli_tool_inventory.json`

The captured CLI tool projection — the OpenAI function-spec list the Go CLI sends
in `input.tools` every turn. It is the single largest region of a `/respond`
request (~77 KB of compact wire JSON — ~63% of the assembled prompt bytes; the
~96 KB on disk here is the same payload pretty-printed) and the region the
assembler's whole cache layout exists to keep byte-stable, so the prompt-cost,
injection, and session-trace suites load this rather than a toy stub.

> **It is not a pure capture right now.** The skill -> runbook rename edited two
> entries in place — `skill__run__get` -> `runbook__run__get` and
> `skill__step__advance` -> `runbook__step__advance`, along with their `skillId`
> argument key and their descriptions. The pinned CLI still registers the `skill.*`
> ids, so those entries describe the CLI AFTER its rename rather than at the pinned
> commit. Everything else is the capture verbatim. The divergence is bounded and
> enforced by `test_the_fixtures_run_ahead_of_the_cli_only_where_we_said_they_do`,
> and `test_the_gate_above_retires_itself_when_the_cli_renames` FAILS the moment the
> Go side renames — that failure is the cue to re-capture per the recipe below and
> delete both tests. Do not add further hand edits under this exception.

**Shape:** a bare JSON array of `{"type": "function", "function": {...}}` dicts —
exactly what `RespondRequest.input.tools` accepts, no envelope. Serialized with
`json.dumps(tools, indent=1, ensure_ascii=True)` and **no trailing newline**.
Keeping that formatting makes a refresh an additive diff rather than a whole-file
reflow. Wire names use `__` (`worktree__list`), not `.` — that separator is the
CLI's OpenAI-name projection of its dotted internal tool ids, and several suites
match on it.

The tool **count** is deliberately not restated in prose as a CURRENT fact (here,
`../fixtures.py`, or `benchmarks/README.md`): the count is data in this file, so
drift shows up as a measurement change rather than a documentation lie. The
refresh log at the bottom is the one exception, and is dated for that reason — it
records what a particular capture contained, which is history and cannot go stale.
`load_tool_inventory()` / `tool_names()` in `../fixtures.py` are the only readers.

### Regenerating

The CLI lives in the `cli/` git submodule, which is **pinned to a commit and never
fetched by CI**, so this fixture is refreshed by hand rather than automatically.

The CLI now ships a committed generator for exactly this, so refreshing it needs
no source edits on that side and no guessing about how its registry is wired:

```
go run ./cmd/tooldump              # the projection a normal launch sends, to stdout
go run ./cmd/tooldump -o out.json
```

It boots a real offline `App` in a throwaway state directory and projects
`OpenAITools(nil)` through the same conversion a live turn uses, so what it prints
is the payload, not a re-derivation of it. Everything the old recipe here explained
about building the registry by hand — `DefaultToolBuilder(&App{})`, the module
path, deleting the throwaway afterwards — is obsolete; that knowledge now lives in
`cli/internal/app/toolinventory.go`, next to the wiring it depends on. This is what
lets CI check the fixture instead of only checking that our two copies agree with
each other.

1. Populate the submodule: `git submodule update --init cli` (from the repo root).
2. Generate (the first build of the CLI tree takes a couple of minutes):

   ```bash
   (cd cli && go run ./cmd/tooldump -o /tmp/cli_tools.json)
   git -C cli status --porcelain   # must be empty — the submodule stays pinned and clean
   ```
3. Re-serialize into this fixture's formatting so the diff stays additive. The
   generator emits two-space indentation with a trailing newline; this fixture uses
   `indent=1`, `ensure_ascii=True`, no trailing newline. Normalizing here rather
   than matching formats keeps a refresh from reflowing the whole file:

   ```bash
   python - <<'PY'
   import json, pathlib
   tools = json.loads(pathlib.Path('/tmp/cli_tools.json').read_text())
   dst = pathlib.Path('benchmarks/_harness/fixtures/cli_tool_inventory.json')
   dst.write_text(json.dumps(tools, indent=1, ensure_ascii=True), encoding='utf-8')
   print(len(tools), 'tools')
   PY
   ```
4. Refresh `tests/fixtures/cli_tool_names.txt` **in the same commit**. It must be
   exactly the sorted name projection of this file, and
   `tests/unit/test_runbook_catalog_hygiene.py` fails if the two disagree. Doing one
   and forgetting the other is the obvious mistake, and nothing else would notice:
   both files still parse, and the damage only surfaces as a runbook declaring a tool
   the CLI no longer has.

   ```bash
   python - <<'PY'
   import json, pathlib
   tools = json.loads(pathlib.Path('benchmarks/_harness/fixtures/cli_tool_inventory.json').read_text())
   dst = pathlib.Path('tests/fixtures/cli_tool_names.txt')
   lines = dst.read_text(encoding='utf-8').splitlines()
   head = max(i for i, ln in enumerate(lines) if ln.startswith('#') or not ln.strip())
   dst.write_text('\n'.join(lines[:head + 1] + sorted(t['function']['name'] for t in tools)) + '\n')
   PY
   ```
5. Re-run the deterministic cost suite, then refresh the baseline and the snapshot
   numbers the READMEs quote. `test_docs_drift.py` fails on a stale
   `benchmarks/prompt_cost/README.md` header, so this is a gate, not a courtesy:

   ```bash
   python -m benchmarks.prompt_cost.bench run
   python -m tools.baseline capture > benchmarks/baseline.json
   ```

Last refreshed 2026-08-15 (85 → 79 → 77 tools). The 79 → 77 step is the CLI
dropping its `docs__search` / `docs__getPage` / `docs__getRelatedPages` family:
the backend classifies documentation questions on the selector call and searches
the public docs MCP itself, so the client no longer carries a docs tool or a second
MCP connection. It added `forge__getChecks`, a typed CI-state read. Net effect on
the wire is larger than the count suggests — the compact tool region fell ~10%,
84.2 KB → 75.8 KB, and it is 63% of an assembled request.
