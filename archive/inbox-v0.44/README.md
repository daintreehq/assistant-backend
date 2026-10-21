# Retired v0.44 assistant inbox model

Archived on 2026-10-21 when serving moved to one current direct-control catalog. This directory is outside the Python package and Docker build context. Nothing here is loaded by startup, search, exact retrieval, current contract tests or training compilation.

- `runbooks/`: the 69 frozen inbox overrides, preserved byte for byte.
- `surfaces/`: the frozen app tool captures used by those procedures.
- `cases/`: inbox-only evaluation cases, with their messages and expectations preserved. Their text remains excluded from training, including history and turn context.
- `corpus-retired.jsonl`: 229 curated examples removed from the active corpus, with original file, section, defaults and reason. Raw generation journals remain unchanged in `training/journals/`.
- `trials/`: the retired role planner, owed register and failure scenarios. Python sources are retained as text and are not executable maintenance tools.

Original relative links and source paths inside frozen files describe the historical checkout. Use Git history to reconstruct that system. Do not copy these procedures into the current catalog to restore compatibility; that would reintroduce tools current Daintree no longer offers.
