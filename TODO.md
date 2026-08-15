# TODO

Deliberately-deferred work: things we have decided to do, but not yet. Each entry
names its **trigger** — the condition that makes it worth doing — because the
reason something is deferred is usually more useful than the task itself.

This file is *not* covered by `tests/unit/test_docs_drift.py`. Everything here is
future tense by construction, so nothing in it is pinned to the code. Check any
claim about today's behaviour against the source before acting on it.

---

## Serve the main model directly from Novita, not through OpenRouter

**Trigger: the day Daintree has its own paid tier.** Not before.

Today every request's bearer token IS the caller's own OpenRouter key — it
authenticates the request and funds that turn's upstream calls, and the server
holds no upstream credential at all (`docs/KEY_HANDLING.md`). OpenRouter earns its
place in exactly that arrangement: the caller already has an account there, we
never touch their money, and the router's endpoint selection is a hedge across
~24 hosts serving the same weights, filtered to endpoints that do not collect or
train on request data (`_PROVIDER_ROUTING`, `docs/PROVIDER_POLICY.md` §3).

On a paid tier the arrangement inverts. **We** hold the credential and **we** pay
per token, so a router's margin stops being someone else's cost and starts being
ours, and "which of two dozen endpoints answered" stops being a hedge and starts
being an unpriced variable. At that point a single known host with a published
price is the better trade.

**The target** — <https://novita.ai/models/model-detail/deepseek-deepseek-v4-flash-0731>
(checked 2026-08-15; all of this is provider-side and moves):

- Same model, same slug: `deepseek/deepseek-v4-flash-0731`. No prompt
  recalibration, no benchmark re-baselining — the weights we are already
  calibrated against.
- OpenAI-compatible endpoint at `https://api.novita.ai/openai`, which is the wire
  shape `providers/openrouter.py` already speaks.
- $0.14 / M input, $0.028 / M cache read, $0.28 / M output, 1M context.

### What the work actually is

The provider swap itself is the small half. `providers/base.py` already defines a
narrow `ChatProvider` protocol that business logic depends on instead of
OpenRouter wire shapes, so a `providers/novita.py` implementing it touches nothing
in validation, selection, state, prompt assembly, or the endpoints. **Do not add
any abstraction for this ahead of time** — that protocol is the whole abstraction
needed, and it exists.

The large half is everything OpenRouter is currently doing for us that a single
host will not:

1. **Cost reporting.** OpenRouter returns `usage.cost` — what it actually billed,
   already accounting for the endpoint that served the call and any cache
   discount. Novita is one host with published per-token prices, so cost becomes
   something we *compute* from a price table we now have to keep current. The
   `absent means unknown, never free` rule and the `cost_complete` flag
   (`providers/base.py`, `docs/DAINTREE_API.md` §5c) have to survive that
   change intact — a stale price table producing a confidently wrong number is
   worse than the `None` it replaces.
2. **The privacy posture.** `data_collection: "deny"` is a *filter over a pool*;
   against one vendor it is a contract term instead, and the whole of
   `providers/policy.py` (`PrivacyMode`, `RoutingPreference`, the per-request
   `routing` block a caller may narrow) needs a real answer for what it means with
   nothing left to route between. Read Novita's actual terms before writing a
   single user-facing string, then update `docs/PROVIDER_POLICY.md` §3–§4 and
   everything downstream of it.
3. **Failover.** OpenRouter fails over between endpoints; one host means an
   outage is an outage. Decide deliberately whether OpenRouter stays as a
   fallback path — this is the one place a two-provider design might be earned
   rather than legacy.
4. **The credential inversion, which is the real project.** `main._build_provider`
   passes `api_key=None` on purpose so a propagation bug fails loudly instead of
   quietly billing a shared account. A paid tier bills a shared account *by
   design*, so that guard, `_resolve_key`, `/v1/daintree/auth/verify` (which
   probes the caller's key against the provider) and
   `test_serving_provider_is_built_without_a_default_credential` all invert
   meaning at once. And with a server-held key comes everything
   `docs/INTERNAL_BETA.md` §4 says we deliberately do not have: admission control,
   rate limiting, per-account quotas. That reasoning — "the backend funds no spend
   of its own" — expires on exactly this day.
5. **`openai/gpt-5.6-sol` does not move.** The skill builder/improver is
   local-development-only and has no Novita equivalent, so it stays on OpenRouter.
   "One provider" means one provider *on the serving path*.

### Surfaces that name OpenRouter and will need updating

`config.py` (`OPENROUTER_BASE_URL`, and the hardened-environment check that it is
the `openrouter.ai` origin), `docs/PROVIDER_POLICY.md` (authoritative — §1 says
"the **only** upstream is OpenRouter"), `docs/KEY_HANDLING.md`,
`docs/INTERNAL_BETA.md`, `docs/DAINTREE_API.md`, `README.md`, `CLAUDE.md`,
`.env.example`, `tests/unit/test_docs_drift.py`, `tests/live/`,
`tools/baseline.py` + `benchmarks/baseline.json`, and the CLI's sign-in copy.

Per the pre-release rule in `CLAUDE.md`: this is a **replacement**, not an
addition. No parallel versioned surface, no compatibility shim, no migration path
— except where a fallback provider (3 above) is chosen as a deliberate design.
