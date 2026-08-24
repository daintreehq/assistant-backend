"""Runbook data models and validation rules.

The on-disk frontmatter uses camelCase keys (``whenToUse``, ``requiredTools``);
those are mapped to snake_case fields. ``extra="forbid"`` keeps the catalog
free of stray hidden metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Allowed values for the ``risk`` enum (mirrors the Daintree CLI).
RISK_VALUES: frozenset[str] = frozenset(
    {"read", "local", "ui", "terminal", "project", "external", "git", "system"}
)

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class RunbookMeta(BaseModel):
    """Validated runbook frontmatter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2048)
    when_to_use: str = Field(min_length=1, max_length=4096, alias="whenToUse")
    # Layer marker: a foundation runbook carries the base judgment for a whole
    # domain (how to read state, choose safe actions) rather than one concrete
    # workflow. `selector_view()` exposes this flag, but the selector itself does
    # not consume it as a classifier feature and does not pair a foundation with
    # the specifics it picks — admission does that afterward, deterministically —
    # see runbooks/active_set.py::_admit_with_domain_foundations.
    foundation: bool = Field(default=False)
    # DESCRIPTIVE metadata, NOT policy. Nothing in this server enforces `risk`: it
    # gates nothing, is not sent to the selector, and never crosses the wire. Real
    # risk gating is PER-TOOL, in the CLI's confirmation matrix — a runbook cannot
    # authorize anything its tools don't already allow. It is kept because the
    # runbook-learning builder writes it: a human reviewing a generated runbook in
    # `git diff` can see at a glance what tier of mutation the runbook authorizes.
    # That reviewer signal is the whole value — do not delete it as dead.
    risk: str = "read"
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")

    @field_validator("id")
    @classmethod
    def _check_id_segments(cls, v: str) -> str:
        """Reject empty dotted segments — they silently break foundation pairing.

        ``Catalog._domain_of`` splits an id on its final dot and ``domain_foundation``
        walks the remaining prefixes, so ``.x`` collapses into the EMPTY domain and
        ``x.`` makes an id its own domain key. Both load fine and then pair with the
        wrong foundation (or none) with no error anywhere. This is deliberately only a
        well-formedness check, not the learner's stricter ``daintree.<domain>.<workflow>``
        shape: hand-authored ids and test fixtures legitimately use other namespaces.
        """
        if any(seg == "" for seg in v.split(".")):
            raise ValueError(f"runbook id {v!r} has an empty dotted segment")
        return v

    @field_validator("risk")
    @classmethod
    def _check_risk(cls, v: str) -> str:
        if v not in RISK_VALUES:
            raise ValueError(f"risk {v!r} not in {sorted(RISK_VALUES)}")
        return v

    @field_validator("required_tools")
    @classmethod
    def _check_required_tools(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            name = raw.strip()
            if not name or not TOOL_NAME_RE.match(name):
                raise ValueError(f"invalid required tool name {raw!r}")
            out.append(name)
        return out


def content_hash_of(meta: RunbookMeta, normalized_body: str) -> str:
    """The identity of a runbook's *content* — metadata plus body.

    Feeds ``Catalog.revision`` (so a rewritten runbook busts client-side runbook
    state) and keys the assembler's rendered-runbook cache. Both depend on it being
    derived from the current body rather than carried alongside it.
    """
    canonical = json.dumps(
        meta.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(
        (canonical + "\n---\n" + normalized_body).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class Runbook:
    """A fully-loaded runbook: validated metadata, normalized body, content hash.

    ``content_hash`` is DERIVED, not supplied. It was a plain field, which meant
    ``dataclasses.replace(runbook, body=...)`` produced a runbook whose stored hash
    described the old body — and the rendered-runbook cache keys on that hash, so
    such an object would have served the previous runbook while the catalog
    reported the new one. Deriving it makes that state unconstructable rather
    than merely unreached.
    """

    meta: RunbookMeta
    body: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        # object.__setattr__ because the dataclass is frozen; this is the
        # documented way to populate a derived field on a frozen dataclass.
        object.__setattr__(self, "content_hash", content_hash_of(self.meta, self.body))

    @property
    def id(self) -> str:
        return self.meta.id

    @property
    def title(self) -> str:
        return self.meta.title

    @property
    def summary(self) -> str:
        return self.meta.summary

    @property
    def when_to_use(self) -> str:
        return self.meta.when_to_use

    @property
    def risk(self) -> str:
        return self.meta.risk

    def selector_view(self) -> dict[str, object]:
        """The bounded metadata the selector is allowed to see.

        Deliberately narrower than the frontmatter: only the fields the selector
        prompt actually renders AND instructs on. ``title`` restates the summary,
        and ``risk``/``required_tools`` are near-non-discriminative within a
        single-product catalog with no instruction referencing them — they were
        pure prompt weight, so they are not shown to the classifier. (They remain
        in :meth:`public_view` and are still read server-side, e.g.
        ``required_tools`` for workflow-plan runbook outlines.)

        Request-varying state is NOT here: which runbooks are currently active is
        rendered once after the catalog block, so the per-runbook lines stay
        byte-identical across turns for DeepSeek's content-prefix cache.
        """
        return {
            "id": self.meta.id,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "foundation": self.meta.foundation,
        }

    def public_view(self) -> dict[str, object]:
        """The metadata exposed by the public catalog endpoint (never the body)."""
        return {
            "id": self.meta.id,
            "title": self.meta.title,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "risk": self.meta.risk,
            "required_tools": list(self.meta.required_tools),
        }

    def ref(self) -> dict[str, str]:
        """A minimal {id, title} reference."""
        return {"id": self.meta.id, "title": self.meta.title}
