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
fetched by CI**, so this fixture cannot be regenerated automatically and drifts
silently whenever the CLI adds or changes a tool. It is refreshed by hand.

The projection call is `(*tools.Registry).OpenAITools(nil)` — `nil` means "no
filter", i.e. the full surface, in registration order
(`cli/internal/tools/registry.go`). The registry is not self-populating: the CLI
fills it in `App.Create` via `DefaultToolBuilder(a *App)`
(`cli/internal/app/app.go`, `cli/internal/app/tools.go`).

You do NOT need a booted app. `DefaultToolBuilder` only reads fields off the
`*App` into adapter structs — it never calls through them — so a zero-value
`&App{}` produces the identical registration set, which is what makes this a
30-second job instead of a wiring exercise.

1. Populate the submodule: `git submodule update --init cli` (from the repo root).
2. Inside `cli/`, write a **throwaway** test at `internal/app/zz_dumptools_test.go`
   (that package can reach both `DefaultToolBuilder` and the registry):

   ```go
   package app

   import (
       "encoding/json"
       "os"
       "testing"

       "github.com/daintreehq/assistant/internal/tools"
   )

   func TestZZDumpTools(t *testing.T) {
       out := os.Getenv("DUMP_TOOLS_TO")
       if out == "" {
           t.Skip("set DUMP_TOOLS_TO")
       }
       built, err := DefaultToolBuilder(&App{})
       if err != nil {
           t.Fatalf("build: %v", err)
       }
       reg := tools.NewRegistry()
       if err := reg.RegisterAll(built...); err != nil {
           t.Fatalf("register: %v", err)
       }
       specs, err := reg.OpenAITools(nil)
       if err != nil {
           t.Fatalf("project: %v", err)
       }
       b, _ := json.Marshal(specs)
       if err := os.WriteFile(out, b, 0o644); err != nil {
           t.Fatalf("write: %v", err)
       }
       t.Logf("wrote %d tools", len(specs))
   }
   ```

   The module path is `github.com/daintreehq/assistant` (it was renamed from
   `daintreehq/daintree-assistant`); check it against the pinned `cli/go.mod`
   rather than trusting this line.
3. Run it — the first build of the CLI tree takes a couple of minutes — then
   **delete the throwaway**; the submodule is pinned and must stay clean:

   ```bash
   DUMP_TOOLS_TO=/tmp/cli_tools.json go test ./internal/app/ -run TestZZDumpTools -count=1
   rm cli/internal/app/zz_dumptools_test.go && git -C cli status --porcelain   # must be empty
   ```
4. Re-serialize into this fixture's formatting so the diff stays additive:

   ```bash
   python - <<'PY'
   import json, pathlib
   tools = json.loads(pathlib.Path('/tmp/cli_tools.json').read_text())
   dst = pathlib.Path('benchmarks/_harness/fixtures/cli_tool_inventory.json')
   dst.write_text(json.dumps(tools, indent=1, ensure_ascii=True), encoding='utf-8')
   print(len(tools), 'tools')
   PY
   ```
5. Re-run the deterministic cost suite and refresh any snapshot numbers the
   READMEs quote:

   ```bash
   python -m benchmarks.prompt_cost.bench run
   ```

Last refreshed 2026-07-28 (72 → 85 tools; the previous capture predated the
`forge.*` issue/PR readers, `recipe.list`/`run`, `git.getProjectPulse`, the
`workflow.*` entry points, and the `worktree.*` readers — i.e. most of the surface
the current skills lean on).
