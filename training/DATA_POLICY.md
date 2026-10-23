# Public training and evaluation data

Every tracked file is public, including generation journals, curated examples,
compiled rows, benchmark cases and archived material. A journal is not private
storage. Keep raw user sessions, local diagnostics and working transcripts under
an ignored local directory such as `.tmp/`.

## Examples that may be committed

Use synthetic examples or generalise a reported failure into an invented
scenario. Keep the routing problem, expected outcome and necessary constraints;
replace identifying names, paths, hosts and project details with consistent
placeholders. Use reserved example domains for email addresses and URLs. Do not
copy private source code, customer material, credentials or a whole conversation.
Public issue text still needs this review before becoming a dataset row.

Before ingesting a generation journal, inspect the complete file for personal or
private context carried over from the generating session. Journal ingestion
archives the file in Git. Redact it before ingestion, not after publication.
The transcript digest's automated redaction is a starting point and still needs
human review.

## Provenance and redaction

Journals preserve reviewed synthetic generator output and are append-only after
that review. Corrections to labels and examples belong in the curated corpus.
Privacy cleanup is an exception: remove identifying material from every copy,
including journals and archives, then compile the corpus again. Record the reason
for the cleanup without repeating the removed information.

The public-repository cleanup on 2026-10-23 replaced identifying personal paths
and an email address in existing synthetic material. Labels and scenario intent
were preserved. Those journals are redacted records rather than byte-for-byte
copies of the original generator output.

Commit curated YAML changes together with the recompiled `selection.jsonl` and
`corpus-lock.json`. Recompiling is a data consistency check; it does not retrain
or alter the shipped model.
