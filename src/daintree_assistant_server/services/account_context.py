"""The account a paid request is attributed to.

**Deliberately small, and small is the design.** It carries what future accounting
genuinely needs to answer "who spent this, and under which plan" — and nothing
else. Every field that could identify a person to a human reader is either absent
or pseudonymous, because this object's whole job is to travel INTO the paid path
and end up beside a cost figure in a log line.

**The raw subject is deliberately ABSENT**, and that is a decision rather than an
omission. It was carried briefly, on the reasoning that durable accounting will
eventually want a real key to join on — but "eventually" is doing the work in that
sentence, and a `repr=False` field is not the safety property it looks like:
``dataclasses.asdict``, ``vars``, and FastAPI's own dataclass encoder all walk
straight past it. The type would have been advertising a guarantee it could not
keep, in exchange for an identifier nothing reads yet.

So the rule here is simply that there is NOWHERE to put one. When durable
accounting needs the real subject, it gets an identity object of its own, designed
so the serializers refuse it rather than merely declining to print it — and
whoever builds it will have to say so out loud. Until then a paid request does not
carry a raw user id through eight call frames on the strength of a comment.

There is no credential here and no field one could occupy either. The bearer is
request material, dropped as soon as the entitlement lookup is done with it.

**This wave records attribution only.** Nothing decrements a quota and nothing
raises ``usage_limit_reached`` — numeric plan allowances are a product decision
nobody has made, and inventing them here would set prices.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountContext:
    """Who a paid turn belongs to, in the form safe to write down."""

    #: The support correlation id: a truncated SHA-256 of the subject, computed
    #: the way the CLI computes it. This is the identifier that travels.
    subject_hash: str

    #: The correlation id for the request that produced this turn, so a cost line
    #: joins to the request log without a second key.
    request_id: str

    #: Which OAuth client the credential was issued to. Server-derived from the
    #: verified token rather than anything the caller asserts, and part of the
    #: attribution because the same person through two clients is two grants.
    oauth_client_id: str | None = None

    #: The plan in force when the turn began, when entitlement was resolved. None
    #: means "not looked up" — an identity-only rollout, or entitlement off — and
    #: is deliberately not the same as a plan named "none".
    plan_id: str | None = None

    #: Where the entitlement answer came from, and whether it was fresh. Recorded
    #: beside the plan because a cost attributed under a stale verdict is a cost
    #: attributed under a plan the account may no longer have.
    entitlement_source: str | None = None
    entitlement_stale: bool | None = None

    def log_fields(self) -> dict[str, object]:
        """The attribution as metadata, and the ONLY projection anything logs.

        A hand-written projection rather than a filtered ``asdict``, so a field
        added to this class is not logged until somebody adds it here too —
        widening what reaches a log line stays a decision rather than a side
        effect of adding a field.
        """
        return {
            "account_subject_hash": self.subject_hash,
            "account_client_id": self.oauth_client_id,
            "account_plan_id": self.plan_id,
            "account_entitlement_source": self.entitlement_source,
            "account_entitlement_stale": self.entitlement_stale,
        }
