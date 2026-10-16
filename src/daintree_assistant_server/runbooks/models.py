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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .host_state import HOST_ELIGIBILITY, HOSTS_NAMESPACE, HostEligibility

# Allowed values for the ``risk`` enum. Descriptive only: nothing grants or
# refuses an action because of it.
RISK_VALUES: frozenset[str] = frozenset(
    {"read", "local", "ui", "terminal", "project", "external", "git", "system"}
)

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _clean_names(values: list[str], *, kind: str, pattern: re.Pattern[str] | None) -> list[str]:
    """Strip, shape-check and de-duplicate-check a list of names."""
    out: list[str] = []
    for raw in values:
        name = raw.strip()
        if not name or (pattern is not None and not pattern.match(name)):
            raise ValueError(f"invalid {kind} {raw!r}")
        if name in out:
            raise ValueError(f"duplicate {kind} {name!r}")
        out.append(name)
    return out


class RunbookMeta(BaseModel):
    """Validated runbook frontmatter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2048)
    when_to_use: str = Field(min_length=1, max_length=4096, alias="whenToUse")
    # Layer marker: a foundation runbook carries the base judgment for a whole
    # domain (how to read state, choose safe actions) rather than one concrete
    # workflow. Search returns the flag on every result; it is not a classifier
    # feature.
    foundation: bool = Field(default=False)
    # DESCRIPTIVE metadata, NOT policy. Nothing enforces `risk`, the selector
    # never sees it and search does not return it; it tells a reader of the file
    # what tier of mutation the runbook's steps perform.
    risk: str = "read"
    # Daintree app MCP action ids (``agent.launch``, ``terminal.getOutput``), never a
    # host's namespaced wire name. `required_tools` is what the documented baseline
    # path calls; `optional_tools` is what a conditional branch, a caller variant
    # (the owned twin of a send) or an optional enhancement calls. Neither is a
    # capability gate: the body still checks the caller's actual surface.
    required_tools: list[str] = Field(default_factory=list, alias="requiredTools")
    optional_tools: list[str] = Field(default_factory=list, alias="optionalTools")
    # Other runbooks whose instructions must be in hand before this one is
    # followed. Retrieval and search deliver them as `supporting_runbooks`, together
    # with the nearest domain foundation, which needs no declaration. Instruction
    # dependencies, not workflow steps: nothing executes them.
    requires: list[str] = Field(default_factory=list)
    # POLICY, unlike the fields above: the caller host state a `daintree.hosts.*`
    # runbook fits (`host_state.fits`). Search leaves out a runbook the reported
    # state does not fit, and offers every one when no state is reported.
    # Required in that namespace and refused outside it.
    host_eligibility: HostEligibility | None = Field(default=None, alias="hostEligibility")

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
        return _clean_names(v, kind="required tool name", pattern=TOOL_NAME_RE)

    @field_validator("optional_tools")
    @classmethod
    def _check_optional_tools(cls, v: list[str]) -> list[str]:
        return _clean_names(v, kind="optional tool name", pattern=TOOL_NAME_RE)

    @field_validator("requires")
    @classmethod
    def _check_requires(cls, v: list[str]) -> list[str]:
        cleaned = _clean_names(v, kind="required runbook id", pattern=None)
        for ref in cleaned:
            if any(seg == "" for seg in ref.split(".")):
                raise ValueError(f"required runbook id {ref!r} has an empty dotted segment")
        return cleaned

    @model_validator(mode="after")
    def _check_cross_field(self) -> RunbookMeta:
        # A tool is either on the baseline path or conditional, not both: listing it
        # twice leaves a reader unable to tell whether the procedure needs it.
        overlap = sorted(set(self.required_tools) & set(self.optional_tools))
        if overlap:
            raise ValueError(f"tools listed as both required and optional: {overlap}")
        if self.id in self.requires:
            raise ValueError(f"runbook {self.id!r} requires itself")
        in_hosts = self.id.startswith(HOSTS_NAMESPACE)
        if in_hosts and self.host_eligibility is None:
            raise ValueError(
                f"runbook {self.id!r} must declare hostEligibility "
                f"(one of {sorted(HOST_ELIGIBILITY)})"
            )
        if not in_hosts and self.host_eligibility is not None:
            raise ValueError(
                f"runbook {self.id!r} declares hostEligibility outside {HOSTS_NAMESPACE}*"
            )
        # Whether the other ids exist, are public and form no cycle is a catalog
        # question — see `Catalog._validate_dependencies`.
        return self


def content_hash_of(meta: RunbookMeta, normalized_body: str) -> str:
    """The identity of a runbook's *content* — every metadata field plus body.

    Returned as ``content_hash`` on every served runbook and folded into
    ``Catalog.revision``, so a caller holding a checkpoint can tell whether the
    instructions it followed are still the ones being served. Both depend on it
    being derived from the current content rather than carried alongside it.
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
    described the old body, so a caller comparing hashes would have been told
    nothing changed. Deriving it makes that state unconstructable rather than
    merely unreached.
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
        pure prompt weight, so they are not shown to the classifier.
        """
        return {
            "id": self.meta.id,
            "summary": self.meta.summary,
            "when_to_use": self.meta.when_to_use,
            "foundation": self.meta.foundation,
        }
