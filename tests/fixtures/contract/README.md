# Cross-service contract fixtures

**These files are the contract.** Not an illustration of it, not a test convenience —
the literal bytes the backend, the CLI and the website are to agree on. Copy them; do not
paraphrase them. (See *Consumers* below for which of those three actually reads them
today — the answer is currently one.)

## Why they exist

The account contract was implemented twice, correctly, by two repositories that never
compared notes. The backend returned an identity-only answer with the entitlement fields
absent; the CLI required `checked_at` on every success. **Both test suites were green**,
because each had written its own fixture from its own reading of the same prose. Nothing
was wrong with either implementation in isolation and the pair could not talk to each
other.

A shared literal is the only thing that catches that. Prose gets read twice; a byte string
does not.

## Layout

| | |
|---|---|
| `account/` | what this **backend** returns from `GET /v1/daintree/account` — the CLI's input |
| `entitlement/` | what the **website** returns to this backend — this backend's input |
| `manifest.json` | the table: every file, whether it is valid or what it resolves to, and for the invalid ones the rule that refuses it |

`manifest.json` also fixes the **reference instant** — `2026-08-26T12:00:00Z`. Every
`checked_at` in `entitlement/` is a literal offset from it, which is what lets the age
bounds be asserted at the second without a test ever sleeping.

There are two legitimate ways to consume those offsets, and they prove different things:

- **Pin `now` to the reference instant.** Exact, so this is the only way to assert a
  boundary — `granted_cache_at_the_bound.json` is precisely `86400 + 300` seconds old, and
  only a frozen clock can show that it is *inside* an inclusive bound. Unit tests do this.
- **Re-apply the offset to wall-clock time.** Lets the real freshness code run unpatched,
  at the cost of a small cushion so a valid fixture cannot be tipped over its bound by the
  milliseconds between stamping and parsing. Approximate, so it proves outcomes and never
  boundaries. The integration suite does this and says so.

What no consumer should do is read `checked_at` as an absolute time — by the time anyone
runs these, the reference instant is in the past and every fixture would read as expired.

## Consumers

- `tests/unit/test_account_contract.py` — the response model against `account/`. Every
  valid file must parse and re-emit the same fields; every invalid one must be refused for
  the rule the manifest names.
- `tests/integration/test_account_contract_boundary.py` — the real route, the real
  entitlement client and the real paid-access gate, driven end to end. It consumes every
  `entitlement/` body and compares the result against the `account/` bodies for the shapes
  a route can actually produce; the invalid `account/` files stay at model level, because
  the point of them is that no route can build one.
- **the CLI** (`github.com/daintreehq/assistant`) — *intended* consumer, and not yet a
  real one. The CLI is meant to copy `account/` byte-for-byte as its own decode fixtures,
  so that a drift in either repository shows up as a diff rather than as a runtime
  mismatch. **That copy does not exist yet** — it lands with the CLI's own account work,
  and until it does this directory is one repository's statement of the contract rather
  than a shared one.

## Editing

A change here is a **cross-repository protocol change**, not a test edit. Both the backend
and the CLI must move together, in the same way a wire-shape change to `/respond` would.
Adding a file is cheap and safe; changing an existing one means some client somewhere is
already decoding the old bytes.

Add new files to `manifest.json` in the same commit — the tests iterate the manifest, not
the directory, so an unlisted fixture is a file nothing reads.

## The subject and its hash

Every fixture uses one subject and its canonical hash:

```
subject:  8f14e45f-ea8b-4c1d-9b2a-0000feedface
hash:     b4c864ea44cbb4a1
```

The algorithm is `sha256("daintree-assistant-subject:" + subject)`, first 8 bytes,
lowercase hex. The **hash above is asserted as a literal**, not recomputed — a test that
recomputes it with the same algorithm it is checking agrees with itself no matter what the
algorithm is, which is exactly the failure this vector exists to catch. Three
implementations (backend, CLI, website) must all produce that string; a drift in any one
produces a perfectly good hash of the same subject that never compares equal, and nothing
fails — support correlation just quietly stops working.
