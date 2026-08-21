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

- `auth.authenticate` is the one place a request becomes a `Principal`. It
  currently ignores the `Authorization` header entirely — see `docs/KEY_HANDLING.md`
  for why *ignore* rather than *optional*: a CLI mid-migration is still sending
  its old OpenRouter key, and reading it would forward that key to Cerebras and
  blame the caller for the rejection. When accounts land, this function starts
  reading the header again and the rest of the server does not move.
- `ProviderChatRequest.api_key` still overrides the server's default in
  `CerebrasProvider._resolve_key`. So a per-account credential is a **value**
  flowing through existing plumbing, not new plumbing. Benchmarks and live tests
  are its only callers today, which is what keeps it from rotting before it is
  used.

What has to be decided rather than merely built: whether an authenticated caller
still spends Daintree's key (an account is an entitlement) or brings their own
(an account is a credential mapping). The code supports either; the tests that
enumerate the open door are the ones that must start failing when this
lands, and they are written to be found:
`test_every_authorization_header_is_ignored`,
`test_no_route_hands_a_caller_supplied_key_to_the_provider`,
`test_every_route_ignores_an_authorization_header_entirely`, and the smoke
script's "Authentication posture" block.

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
