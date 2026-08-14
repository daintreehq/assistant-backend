# Harness fixtures

## `cli_tool_inventory.json`

The captured CLI tool projection — the OpenAI function-spec list the Go CLI sends
in `input.tools` every turn. It is the single largest region of a `/respond`
request (~77 KB of compact wire JSON — ~63% of the assembled prompt bytes; the
~96 KB on disk here is the same payload pretty-printed) and the region the
assembler's whole cache layout exists to keep byte-stable, so the prompt-cost,
injection, and session-trace suites load this rather than a toy stub.

**Shape:** a bare JSON array of `{"type": "function", "function": {...}}` dicts —
exactly what `RespondRequest.input.tools` accepts, no envelope. Serialized with
`json.dumps(tools, indent=1, ensure_ascii=True)` and **no trailing newline**.
Keeping that formatting makes a refresh an additive diff rather than a whole-file
reflow. Wire names use `__` (`worktree__list`), not `.` — that separator is the
CLI's OpenAI-name projection of its dotted internal tool ids, and several suites
match on it.

The tool **count** is deliberately not restated in prose anywhere (here,
`../fixtures.py`, or `benchmarks/README.md`): the count is data in this file, so
drift shows up as a measurement change rather than a documentation lie.
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
4. Re-run the deterministic cost suite and refresh any snapshot numbers the
   READMEs quote:

   ```bash
   python -m benchmarks.prompt_cost.bench run
   ```

Last refreshed 2026-07-28 (72 → 85 tools; the previous capture predated the
`forge.*` issue/PR readers, `recipe.list`/`run`, `git.getProjectPulse`, the
`workflow.*` entry points, and the `worktree.*` readers — i.e. most of the surface
the current skills lean on).
