# TODO

Deliberately-deferred work: things we have decided to do, but not yet. Each entry
names its **trigger** — the condition that makes it worth doing — because the
reason something is deferred is usually more useful than the task itself.

This file is *not* covered by `tests/unit/test_docs_drift.py`. Everything here is
future tense by construction, so nothing in it is pinned to the code. Check any
claim about today's behaviour against the source before acting on it.

---

## Account authentication and sign-in

**Trigger: now. This is the next piece of work, not a someday.**

The door is currently open. The server holds the upstream credential
(`CEREBRAS_API_KEY`) and funds every turn from it, and `auth.py` serves a request
with no `Authorization` header as an anonymous principal. Combined with a public
Cloud Run service that is an open, Daintree-funded endpoint with no rate limiting
— which is the accepted state for this stage and the reason this entry is first.

**The seam already exists**, deliberately, and nothing about it is a shim:

- `auth.authenticate` is the one place a request becomes a `Principal`. The
  anonymous branch becomes a rejection; the rest of the server does not move.
- `Principal.api_key` is `None` on every real request today but is still carried,
  and `CerebrasProvider._resolve_key` still prefers a request-supplied credential
  over the server's own. So a per-account credential is a **value** flowing
  through existing plumbing, not new plumbing.
  `tests/integration/test_endpoints.py::test_a_caller_supplied_key_reaches_selector_and_main_calls`
  keeps that path live and tested precisely so it does not rot before it is used.
- A malformed bearer is *already* a 401 on every route. Absence is what is
  permitted, not garbage.

What has to be decided rather than merely built: whether an authenticated caller
still spends Daintree's key (an account is an entitlement) or brings their own
(an account is a credential mapping). The code supports either; the tests that
enumerate the open door — `test_every_route_is_served_without_an_authorization_header`,
`test_an_absent_header_is_served_anonymously`, and the smoke script's
"Authentication posture" block — are the ones that must start failing when this
lands, and they are written to be found.

---

## Watch `gemma-4-31b`'s replacement

**Trigger: when Cerebras retires the preview model.** Announced, not scheduled.

`config.CEREBRAS_MODEL` is the whole code change, but not the whole job — see
`docs/PROVIDER_POLICY.md` §9. Two things beyond output quality have to be
re-decided:

1. **Thinking posture.** Nothing in this server thinks, and that is a property of
   *this model* rather than a flag: `gemma-4-31b` does not reason, and Cerebras
   offers no way to switch reasoning off (a bare `reasoning_effort` string with no
   "off" value). A reasoning-capable replacement is therefore a deliberate posture
   decision with a real latency cost, not a drop-in.
   `tests/live/test_live_cerebras.py::test_live_serving_model_reports_no_reasoning_tokens`
   asserts zero reasoning tokens so this cannot be missed silently.
2. **Prompt budgets and the baseline.** The per-file budget tests and
   `benchmarks/baseline.json` are sized against this model's context window and
   its behaviour under the current prompts. Refresh the baseline deliberately
   (`python -m tools.baseline capture`) and explain the move.
