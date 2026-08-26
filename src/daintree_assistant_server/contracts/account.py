"""The wire model for ``GET /v1/daintree/account``.

**A status read, not an authorization decision.** A known-missing or inactive
subscription is a 200 here, carrying ``access`` as data — the same fact becomes a
typed 402 only when it stops a paid request. Modelling it as an error on this
route would mean a client could not ask "what does my account look like?" without
handling four exception paths for perfectly ordinary answers, and someone with no
plan could not read their own status to find out they need one.

Everything here is bounded by Pydantic even though most of it came from a
dependency we trust and validated once already. Trust is not a property of the
bytes: the entitlement client validates the WEBSITE's contract, and this validates
OURS, so a future change on either side cannot quietly widen what a client
receives.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

#: The response shape's version, and — like the auth manifest's — a REAL version
#: rather than one of the labels CLAUDE.md forbids. It is wire metadata a client
#: reads before anything else, so it belongs in the same category as
#: ``protocol_version`` and the signed state token's ``TOKEN_VERSION``.
#:
#: Spelled as a literal on the field below rather than defaulted from here, so the
#: TYPE carries the contract: a build that meant to emit a different version would
#: fail to type-check rather than serialize one.
ACCOUNT_CONTRACT_VERSION = 1


class AccountResponse(BaseModel):
    """What this deployment knows about the caller's account.

    ``extra="forbid"`` and ``frozen=True`` follow the house rule for wire models:
    a field that arrives here by accident should be a construction error, not a
    surprise on somebody's terminal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1

    #: Display only, and OMITTED rather than invented when the token carries no
    #: email claim. A fabricated or defaulted value here would be rendered beside
    #: "you are signed in as", which is exactly the claim it must not guess at.
    email: str | None = Field(default=None, max_length=320)

    #: The support correlation id — a truncated SHA-256 of the subject, computed
    #: the same way the CLI computes it. The raw subject is deliberately absent:
    #: this response is rendered in terminals and pasted into issues.
    subject_hash: str = Field(pattern=r"^[0-9a-f]{16}$")

    #: ``unverified`` is this endpoint's own fourth answer and does not exist in
    #: the website's contract. It means identity is established and entitlement
    #: was not looked up — an identity-only rollout, or a lookup this deployment
    #: is not configured for. A client must not read it as "no plan": nothing was
    #: asked, so nothing was refused.
    access: Literal["granted", "subscription_required", "subscription_inactive", "unverified"]

    plan_id: Literal["standard", "pro"] | None = None
    subscription_status: str | None = Field(default=None, max_length=64)
    entitlement_source: Literal["polar", "cache"] | None = None
    #: Absent rather than false when no lookup happened. A bare ``false`` would
    #: read as "we checked, and the answer is fresh".
    entitlement_stale: bool | None = None
    #: AWARE, always. A naive timestamp would serialize without an offset and be
    #: read by the client in whatever zone it assumes — silently shifting how
    #: fresh a billing answer looks.
    checked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _entitlement_fields_agree_with_access(self) -> AccountResponse:
        """The cross-field rules, enforced HERE as well as upstream.

        The entitlement client validates the WEBSITE's contract; this validates
        ours, and the locked brief asks for exactly that separation — strict
        limits on the response even though the data came from a dependency we
        already checked.

        **It mirrors the client's whole closed table rather than a subset**, and
        the reason is worth stating because the alternative looks tidier. Rules
        that only half-agree drift: a later edit to one side produces a shape that
        one layer calls legal and the other refuses, and the disagreement surfaces
        as an intermittent 500 rather than as a contract failure. Every shape
        rejected below is already unreachable through this route — the client
        refuses those bodies before a verdict exists — so mirroring costs nothing
        and buys a second, independent statement of the same contract.

        Order is load-bearing in one place: the missing-plan check runs before the
        completeness checks, so a bare ``granted`` still reports the specific
        thing wrong with it rather than the first field it happens to be missing.
        """
        entitlement_fields = (
            self.plan_id,
            self.subscription_status,
            self.entitlement_source,
            self.entitlement_stale,
            self.checked_at,
        )
        if self.access == "unverified":
            # Nothing was asked, so nothing may be reported. A `plan_id: null`
            # beside `entitlement_stale: false` is three claims, none of them made.
            if any(f is not None for f in entitlement_fields):
                raise ValueError("access 'unverified' cannot carry entitlement fields")
            return self

        if self.access == "granted" and self.plan_id is None:
            raise ValueError("access 'granted' requires a plan_id")

        # A lookup HAPPENED, so it has a source and a time it happened at. Absent
        # is not a weaker version of either — a client cannot tell "we checked and
        # will not say when" from "we did not check", and those are the two answers
        # this endpoint exists to keep apart.
        if self.checked_at is None:
            raise ValueError(f"access '{self.access}' requires checked_at")
        if self.entitlement_source is None:
            raise ValueError(f"access '{self.access}' requires an entitlement_source")
        if self.entitlement_stale is None:
            raise ValueError(f"access '{self.access}' requires entitlement_stale")

        # The same closed table the entitlement client reads, stated in this
        # layer's vocabulary. `cache` and `stale` are one fact: the website's
        # fallback is what it serves when it could not reach the provider.
        if self.entitlement_stale and self.entitlement_source != "cache":
            raise ValueError("entitlement_stale is only valid with entitlement_source 'cache'")
        if self.entitlement_source == "cache" and not self.entitlement_stale:
            raise ValueError("entitlement_source 'cache' is always stale")
        if self.entitlement_source == "cache" and self.access != "granted":
            # A cached GRANT is safe to report. A cached REFUSAL is the website
            # saying what it believed before it lost the provider, and putting it
            # on somebody's account page as a settled fact is the same mistake as
            # billing them for it.
            raise ValueError("a cache-sourced entitlement may only report 'granted'")
        return self
