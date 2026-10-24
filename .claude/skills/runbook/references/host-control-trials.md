# Current Remote Host trials

The current catalog uses `host.list` and `host.call`. It has no inbox, exchange ledger or hub/lane role pair. Historical trial plans in `archive/inbox-v0.44/trials/` describe that retired system and must not be run against current Daintree.

For a host procedure, validate its outer calls against `help-core` or `help-full` and its inner calls against the matching `relayed-*` surface. A delegated assistant is an agent terminal: resolve it through `assistant.list`, start at most once through `assistant.start`, and preserve its terminal id and generation when sending. Its brief must be executable with the ordinary catalog on that machine.

Offline evidence comes from the current captured schemas, tagged examples, host eligibility and dependency tests, routing cases, and the app's host-control contract tests. These prove the protocol and selection, not live notice delivery or successful task execution.

A live trial needs an isolated Shell and host, a scratch repository, a pinned base commit and an identified caller tier. Exercise the requested path and one relevant refusal or uncertain-write recovery. Record outer and inner outcomes, call ids, terminal ids, assistant generations, submissions, notices actually received, and artifact checks performed by the operator. A Core trial cannot use the Full check runner (`project.runCheck`); `project.runCommand` runs at either tier. Never replay an ambiguous launch or send.

Only run live app trials when authorized. When the environment is unavailable, record the precise scenario and expected evidence in the work's `trials/owed.md`; a passing pane trial or an archived inbox scenario does not close that obligation.
