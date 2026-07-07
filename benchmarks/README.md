# Benchmarks

Live-model benchmarks for the quality-critical small-model calls this server owns.
Everything here is **development tooling**: it is not part of the Python package, the
test suite, or CI (pytest `testpaths` and mypy are pinned to `src`/`tests`), and every
run makes **real, paid API calls**.

| benchmark | measures | entry point |
|---|---|---|
| `skill_selection/` | accuracy + latency of the automatic skill selector | `python -m benchmarks.skill_selection.bench` |

Conventions for adding a new benchmark:

- One subdirectory per benchmark, runnable as `python -m benchmarks.<name>.bench`.
- Drive the **real production code path** (the same classes `/respond` uses), with the
  prompt directory as the only injectable seam — a measured win must be exactly what
  production will see.
- Cases are data (`cases/*.yaml`), validated against real server state at load time.
- Run outputs land in `<name>/results/` (gitignored); the JSON document is the record.
