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

#: Hard ceiling on a confirmation, in characters.
#:
#: 240 is two sentences, and the number is triangulated rather than chosen. Three
#: revisions of this catalog bracketed it:
#:
#: * A 140-char one-line ANNOUNCEMENT ("I'll take a look at the failing check").
#:   Too thin to say anything, and it duplicated the executor's own opening.
#: * A 400-char comprehension PARAGRAPH. Measured at 62.6 words and 350
#:   characters on average — about fifteen seconds of reading in a window that is
#:   often one second, so the answer arrived mid-paragraph.
#: * An 80-char CAPTION. Scannable, but too compressed to state a goal; it read
#:   as cryptic.
#:
#: The research settles it at a two-part form (`docs/RUNBOOK_CONFIRMATIONS.md`):
#: a lead clause that lands inside a 500ms glance, then one sentence of scope
#: that the rest of the window absorbs. UX-writing practice puts those at 2-4 and
#: 4-8 words for a TRANSIENT status line; this one PERSISTS in the transcript and
#: the window runs 0.5-5s depending on prompt-cache warmth, so the second half
#: can carry a real sentence. `tests/unit/test_prompt_budgets.py` also holds a
#: 38-word ceiling and a two-sentence shape, which are what actually bite.
CONFIRMATION_MAX_CHARS = 240


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
    # The CONFIRMATION: a one-line caption shown the instant a user hits Enter, the
    # first time this runbook is loaded with real confidence. It replaces the
    # Cerebras fast-preamble stage — same slot on screen, same display-only
    # contract — with authored text instead of a second model call.
    #
    # **IT IS A RECEIPT, NOT A SENTENCE.** The shape is
    # ``<task mode> — <one stable boundary, distinction or invariant>``, six to
    # twelve words, noun-led, no trailing period:
    #
    #     Failure diagnosis — the exact error matters more than the visible symptom
    #
    # Two independent constraints force that shape, and both were arrived at the
    # expensive way.
    #
    # **It may not announce, because the executor already does.** The executor
    # opens its own reply with "I'll start by reading the current state…" —
    # measured, and unchanged by three separate attempts to prompt it out of
    # doing so. A confirmation that also announces is not a slightly worse confirmation; it
    # is a visible duplicate of the line rendering directly beneath it (the CLI
    # joins the two into one bubble), and a reply meant to feel fast reads as
    # padded. Naming the activity as a NOUN PHRASE is what says what is happening
    # without competing for that register.
    #
    # **It may not be prose, because there is no time to read prose.** A previous
    # revision authored these as two-or-three-sentence comprehension paragraphs,
    # in the register the retired stage's own prompt taught. Measured over the
    # catalog: 62.6 words and 350 characters on average, roughly fifteen seconds
    # of reading placed in a one-second window. The answer arrives mid-paragraph
    # and the reader is interrupted by the thing they were waiting for.
    #
    # **Consistency across the catalog is a FEATURE, not repetition to be
    # avoided.** Thirty-seven differently-shaped pseudo-insights read as canned
    # text pretending to be bespoke; thirty-seven identically-shaped captions read
    # as product UI, and a heavy user starting ten conversations a day recognises
    # the caption instead of re-reading it.
    #
    # What it buys, and it is the only thing available at that instant: the user
    # learns their request was ROUTED, and into which mode. That is actionable —
    # a wrong caption is legibly wrong and they can interrupt at once, where a
    # vague paragraph would have hidden the misroute. This matters because the
    # 0.90 threshold is not certainty; see `config.RUNBOOK_CONFIRMATION_THRESHOLD`.
    #
    # Optional. A domain foundation is paired in without a score and the
    # server-pinned documentation runbook is hidden from the selector, so neither
    # can ever be shown — a confirmation on either is dead text, and the tests say so
    # in both directions.
    #
    # NOT a selector signal. `selector_view()` does not expose it and
    # `runbooks/local/documents.py` builds its embedding document from
    # title/summary/whenToUse only, so adding or editing a confirmation changes no
    # vector and needs no retrain. It DOES enter `content_hash_of` (it is
    # frontmatter), so an edit busts `catalog_revision` — which is correct: a
    # client holding state from before the edit should re-select.
    confirmation: str | None = Field(default=None, max_length=CONFIRMATION_MAX_CHARS)

    @field_validator("confirmation")
    @classmethod
    def _check_opener(cls, v: str | None) -> str | None:
        """One line of plain text, or nothing at all.

        A newline is rejected rather than collapsed: the confirmation is emitted as a
        single ``preamble`` event and a multi-line one would render as a block of
        server-authored text sitting above an answer that has not started. One
        caption on one line is the shape.

        An empty-after-strip value is rejected rather than silently becoming
        ``None`` — ``confirmation: ""`` in frontmatter is a mistake, and the failure a
        mistake deserves is a load error, not a runbook that quietly never
        announces.
        """
        if v is None:
            return None
        text = v.strip()
        if not text:
            raise ValueError("confirmation must not be blank; omit the key instead")
        if "\n" in text or "\r" in text:
            raise ValueError("confirmation must be a single line")
        return text

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

    @property
    def confirmation(self) -> str | None:
        return self.meta.confirmation

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
