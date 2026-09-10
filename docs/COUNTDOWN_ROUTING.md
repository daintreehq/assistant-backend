# Plain countdown routing

The ranking query normalizes a complete bare countdown request to the equivalent
`Remind me in <duration>` form. The frozen selector recognizes this reminder
shape more reliably than countdown wording. The model still selects from the
caller's catalog using its existing cardinality, admission floors and active cap.
This is a bounded input normalization, not a new runbook or a model retrain.

`runbooks/local/documents.py` applies it only to the newest user message, before
fitting the query to the token budget. History, active-runbook context and turn
context retain their existing handling. The executor receives the original
request, and `last_user_text` still gives the documentation classifier the
original question. Catalog metadata and embeddings are unchanged.

The recognized forms use set/start/create/give me, timer/countdown, and a numeric
or spelled duration in seconds, minutes or hours. A full-message match permits
simple politeness and final punctuation. Quoted text, documentation questions,
negations, named reminders, list/cancel requests and compound actions retain
their original ranking text. This deliberately does not try to parse every way
to describe delayed work; the ordinary model handles other forms.

The shared renderer also supplies future training runs. The current artifact
predates this normalization; its measured reminder behavior is the basis for
the bridge. Run the full local selection benchmark when changing this grammar,
and compare existing selections and documentation decisions separately from
new countdown cases. The additional boundary cases live in
`benchmarks/runbook_selection/cases/24-countdown-boundaries.yaml`.
