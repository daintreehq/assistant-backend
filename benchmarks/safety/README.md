# Safety / correctness gauntlet

A **100% deterministic** benchmark (no API key, no network, CI-safe) that drives
the REAL production guard functions over corpora of hostile and edge inputs and
reports, per guard, an accuracy number plus **zero-tolerance false-accept
counters that must all be 0**. Nothing is stubbed or reimplemented — every
section imports the same function `/respond` (or the task runner) calls.

| section | production guard | zero-tolerance counter(s) |
|---|---|---|
| `wire_validation` | `services.validation.validate_respond_request` (+ the strict Pydantic contract) | `false_accept_count` |
| `result_schema` | `services.result_schema.validate_result_schema` / `conforms` | `injection_false_accept_count`, `false_accept_count` |
| `tool_call_repair` | `services.tool_call_repair.repair_tool_call_arguments` / `StreamToolCallBuffer` | `false_repair_count` (a repair may never invent a wrong value) |
| `sanitization` | `text_utils.strip_control_chars` / `sanitize_line` / `cap_bytes` / `frame_data_block` | `residual_control_count`, `breakout_count`, `cap_overflow_count` |
| `state_token` | `state.codec.StateCodec` (fixed key/salt/clock) | `forgeries_accepted` |
| `workflow_validation` | `services.workflow_validation.validate_plan` / `validate_reconcile` | `missed_reject_count`, `crash_count` |

## Quick start

```bash
# from the repo root, venv active — free, no API calls
python -m benchmarks.safety.bench run
python -m benchmarks.safety.bench run --no-save
python -m benchmarks.safety.bench report results/<run>.json
```

Exit code is **1 when any check fails or any zero-tolerance counter is
non-zero** — a red run is a genuine guard hole (or a genuine behavior change),
never something to fix by weakening a case. `summary.false_accept_total` in the
saved doc is the single number to watch.

## Cases

The wire-validation and result_schema corpora are data (`cases/*.yaml`),
validated at load — a malformed case fails immediately instead of silently
scoring:

- `10-wire-validation.yaml` — `RespondRequest` payloads with `expect:
  reject|pass`. A reject may land at either layer: the strict Pydantic contract
  (`extra="forbid"`) or the semantic validator; both are the production 400
  path. Covers client `system`/`developer` roles, every reserved tool name
  (including via prior assistant tool_calls and `tool_choice`), duplicate/
  invalid tool names, `json_object`+tools, dangling/orphaned tool messages,
  remote image URLs — plus valid requests that must NOT be over-rejected
  (instruction-like *text* is data, not a wire violation).
- `20-result-schema.yaml` — `mode: validate` schemas with `expect: reject|pass`
  (`injection: true` marks the instruction-bearing subset: `description`,
  `title`, `$ref`, `$comment`, `examples`, string-valued noise keys, …) and
  `mode: conforms` value/schema pairs with `expect: conform|violate`. The
  settings-sized over-depth/size/enum/properties rejects are built by the
  loader (`schema_synth:`), since they are functions of the live caps.

The forgery, sanitization-fuzz, repair, and workflow sections are inline Python
(`runner.py`): their cases are constructed values — signed tokens, streaming
fragments, Pydantic model instances — not data. The sanitization fuzz runs a
**fixed-seed** generated corpus (plus hand-written nasties) through property
checks, so runs stay byte-for-byte reproducible.

Expected log noise: the repair section deliberately feeds malformed JSON, so
the production module logs its own `tool_call_repair_failed` fail-open warnings
during a run. That is the guard working, not a suite error.

## Semantics pinned on purpose

A few cases freeze deliberate production postures, so a regression in either
direction fails:

- selector-adjacent fail-open vs fail-closed splits: an unknown workflow
  `tool_name` **degrades** (`next_action` nulled + warning) while a structural
  defect (cycle, duplicate id, terminal-status move) **raises**; an empty tool
  inventory disables grounding entirely.
- a reconcile against an empty workflow snapshot skips the graph checks (the
  client is the source of truth) but still rejects duplicate `add_nodes`.
- `cap_bytes` truncates on a character boundary (never emits broken UTF-8);
  `frame_data_block` strips control characters *before* neutralizing
  delimiters, so `</ctx\x00>` cannot reassemble into a live closing tag.
- scalar noise keys (`maxTokens: 4096`, `optional: true`) are stripped and the
  schema passes; the same key with a **string** value must still be rejected.

## Results

Runs save to `results/<timestamp>-deterministic.json` (gitignored): a `meta`
block, a `summary` with `false_accept_total` + per-guard pass lines, and a
per-guard detail section (per-case outcomes for the enumerable sections,
failure-only detail for the fuzz corpus). `report` re-prints a saved doc.

The scoring harness has been negative-controlled: neutering
`validate_respond_request` in-memory produces 22 wire false-accepts, and
neutering `validate_result_schema` produces 23 (13 injection) — so a green run
reflects the guards, not the harness.
