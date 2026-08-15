"""Case-suite coverage matrix — which catalog skills the suite actually tests.

Offline and free: loads the real catalog and the real case suite, nothing else.
No API key, no network.

The exit gate has TWO halves, and they answer different questions.

**Recall (``req``) — is the skill REACHABLE?** A skill with zero ``required``
cases is never tested for recall: the selector can fail to pick it on every
conversation in the world and the benchmark still reports a green run, because
nothing ever demanded it. Not hypothetical — five skills sat in the catalog with
zero recall coverage for three weeks and no run ever noticed.

**Traps (``trap``) — is it reached for the RIGHT REASONS?** Recall alone is a
weak gate. Routing quality in this catalog does not live in the positive clause
of a ``whenToUse``; it lives in the carve-outs that follow it — "do not use for
X, use ``daintree.<other>`` instead". Each carve-out is a CLAIM that a specific
confusion exists and that the catalog text resolves it. An untested carve-out is
an unverified claim, and a suite can be 100% green on recall while every one of
those claims is false: a case whose bait matches only the positive clause is
passed by a selector that has never once distinguished the neighbours.

So a skill that names neighbours must be demanded by at least one
``routing-trap`` case that ALSO excludes a declared neighbour — i.e. a case that
actively asserts the neighbour must not be picked. A skill with no carve-outs
claims no confusion and owes no trap; the gate does not invent one for it.

**Declared neighbours are parsed from the ``whenToUse`` text**, by scanning it
for other catalog skill ids (fully qualified, on a token boundary). That is the
carve-out convention this catalog is written in. A carve-out that names a skill
in SHORTHAND — "use the competition skill" — is invisible here and its skill
reads as owing nothing; the fix is to write the id, not to soften the gate.

**Foundations are gated differently on recall, on purpose.** A domain foundation
is paired with a selected specific deterministically DOWNSTREAM of the
classifier, so a case whose answer is a specific must not demand it
(``cases.py`` rejects that shape outright). Counting a foundation's ``req`` the
same way as a specific's would therefore pressure an author into writing exactly
the case the loader rejects. The only conversation that tests a foundation for
recall is one where it is the WHOLE answer — no runbook squarely matches, load
it ALONE — so a foundation's recall gate reads ``alone`` instead of ``req``.
Its TRAP gate needs no special case: a trap requires the skill in
``expect.required``, which for a foundation is the ``alone`` shape anyway.

Seven per-skill numbers, each a count of CASES (not trials) except ``nbr``:

``nbr``
    Declared confusable neighbours — other catalog skill ids named in this
    skill's ``whenToUse``. Not a case count; it is what the skill OWES.
``trap``
    Cases in category ``routing-trap`` demanding this skill where at least one
    declared neighbour is in neither ``required`` nor ``optional`` — the case
    asserts that neighbour must NOT be picked. **Routing** coverage, read by the
    exit gate for every skill with ``nbr > 0``.
``req``
    Cases demanding the skill (``expect.required``). **Recall** coverage — the
    column the exit gate reads for every NON-foundation skill.
``alone``
    Cases where the skill is the ENTIRE required set. For a foundation this is
    the recall column the gate reads; for a specific it is informational (a
    lean, unambiguous match).
``opt``
    Cases where the skill is allowed but not demanded (``expect.optional``,
    typically the paired domain foundation). Permissive: it proves nothing about
    recall and nothing about precision.
``act``
    Cases carrying the skill in the prior active set (``active``). Continuation /
    eviction pressure — whether the selector keeps or drops an in-progress skill.
``neg``
    Cases where the skill is in neither ``required`` nor ``optional``, so picking
    it scores as an ``extra``. **Precision** coverage, and implicit: every case is
    a negative for every skill it does not allow, which is why ``neg`` is large
    and uninteresting on its own — a skill whose ONLY coverage is ``neg`` is
    exactly the zero-recall hole this gate catches.

Deliberately a bench subcommand and NOT a pytest test: ``benchmarks/`` is outside
``testpaths`` on purpose, and importing the case tree into CI would make CI depend
on the live-benchmark suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from daintree_assistant_server.skills.catalog import Catalog

from .cases import BenchCase

# The category whose cases count as traps. A trap has to be labelled as one: the
# report groups on `category`, and letting any case with an excluded neighbour
# count would make almost every case a trap for almost every skill (see `neg`).
TRAP_CATEGORY = "routing-trap"


def declared_neighbours(catalog: Catalog) -> dict[str, tuple[str, ...]]:
    """Map each skill id to the other catalog ids its ``whenToUse`` fences against.

    Scans for OTHER CATALOG IDS rather than a ``daintree.*`` pattern, so the
    parse stays namespace-agnostic (fixture catalogs use other prefixes) and can
    never yield an id that is not in the catalog. The lookaround stops a partial
    hit: an id that is a prefix of another (``a.b`` inside ``a.b.c``) must not
    count as a mention of the shorter one. A trailing ``.`` is only disqualifying
    when MORE id characters follow it — a carve-out that ends its sentence on the
    id ("...use daintree.agents.recover-agent.") still counts.
    """
    ids = [s.id for s in catalog.skills]
    patterns = {
        sid: re.compile(rf"(?<![\w.\-]){re.escape(sid)}(?![\w\-])(?!\.[\w\-])") for sid in ids
    }
    out: dict[str, tuple[str, ...]] = {}
    for skill in catalog.skills:
        text = skill.meta.when_to_use
        out[skill.id] = tuple(
            other for other in ids if other != skill.id and patterns[other].search(text)
        )
    return out


@dataclass(frozen=True)
class SkillCoverage:
    """Per-skill case counts across the whole suite."""

    skill_id: str
    foundation: bool
    neighbours: tuple[str, ...]
    trap: int
    required: int
    alone: int
    optional: int
    active: int
    negative: int

    @property
    def has_recall_coverage(self) -> bool:
        """Recall coverage: is this skill REACHABLE at all?

        A foundation is only ever demanded ALONE (see the module docstring), so a
        ``required`` count that came from riding along with specifics would not
        prove the selector can pick it when it IS the answer.
        """
        return self.alone > 0 if self.foundation else self.required > 0

    @property
    def has_trap_coverage(self) -> bool:
        """Routing coverage: is this skill reached for the RIGHT REASONS?

        Only owed by a skill that DECLARES a confusable neighbour. No carve-outs
        means no claim to verify, so no trap is demanded — the gate must not
        pressure an author into padding the suite with a manufactured confusion.
        """
        return self.trap > 0 if self.neighbours else True

    @property
    def gate_ok(self) -> bool:
        """Both halves of the exit gate, for the caller's exit code."""
        return self.has_recall_coverage and self.has_trap_coverage


def build_coverage(catalog: Catalog, cases: list[BenchCase]) -> list[SkillCoverage]:
    """Count, for every catalog skill, the cases that exercise it.

    Iterates the CATALOG (not the cases), so a skill no case mentions at all still
    gets a row of zeros rather than silently vanishing from the report — that
    absence is the whole point.

    SELECTABLE skills only. One is server-pinned and hidden from the classifier;
    a routing corpus cannot cover a skill the classifier never chooses, and
    demanding it would only produce a case that misstates the expectation.
    """
    neighbours = declared_neighbours(catalog)
    rows: list[SkillCoverage] = []
    selectable = {m["id"] for m in catalog.selector_metadata()}
    for skill in catalog.skills:
        sid = skill.id
        if sid not in selectable:
            # SERVER-PINNED, and therefore not the classifier's to reach. The
            # documentation runbook is loaded off the turn's `docs_query` (see
            # config.DOCS_SKILL_ID) and is hidden from the selector's catalog
            # entirely, so demanding a recall case for it would demand a case
            # asserting the selector picks something it cannot see — a case that
            # can only be written by lying about the expectation. Its loading is
            # covered by unit tests over `resolve_active_skills`, which is where a
            # deterministic rule belongs.
            continue
        nbrs = neighbours[sid]
        rows.append(
            SkillCoverage(
                skill_id=sid,
                foundation=skill.meta.foundation,
                neighbours=nbrs,
                trap=sum(
                    1
                    for c in cases
                    if c.category == TRAP_CATEGORY
                    and sid in c.required
                    and any(n not in c.allowed for n in nbrs)
                ),
                required=sum(1 for c in cases if sid in c.required),
                alone=sum(1 for c in cases if c.required == [sid]),
                optional=sum(1 for c in cases if sid in c.optional),
                active=sum(1 for c in cases if sid in c.active),
                negative=sum(1 for c in cases if sid not in c.allowed),
            )
        )
    return rows


def print_coverage(catalog: Catalog, cases: list[BenchCase]) -> list[SkillCoverage]:
    """Print the matrix; return the rows so the caller can set the exit code."""
    rows = build_coverage(catalog, cases)
    # +13 leaves room for the " [foundation]" suffix on the widest id.
    width = max([len(r.skill_id) + 13 for r in rows] + [20])

    print(
        f"\n== skill_selection coverage | {len(catalog.skills)} catalog skills | "
        f"{len(cases)} cases =="
    )
    print(
        "counts are CASES: trap=neighbour-excluding routing-trap  req=required  "
        "alone=sole-required\n                  opt=allowed  act=prior-active  neg=implicit"
        "   (nbr is not a case count)"
    )
    print("recall gate reads `req`, except a [foundation], which is gated on `alone`")
    print("routing gate reads `trap`, and only for a skill with nbr > 0")
    print(
        f"\n{'skill':<{width}}  {'nbr':>4} {'trap':>5} {'req':>4} {'alone':>6} "
        f"{'opt':>4} {'act':>4} {'neg':>4}"
    )
    print("-" * (width + 42))
    for row in rows:
        flags = []
        if not row.has_recall_coverage:
            flags.append("NO REQUIRED CASE")
        if not row.has_trap_coverage:
            flags.append("NO TRAP CASE")
        flag = f"   <- {', '.join(flags)}" if flags else ""
        label = f"{row.skill_id}{' [foundation]' if row.foundation else ''}"
        print(
            f"{label:<{width}}  {len(row.neighbours):>4} {row.trap:>5} {row.required:>4} "
            f"{row.alone:>6} {row.optional:>4} {row.active:>4} {row.negative:>4}{flag}"
        )

    categories = sorted({c.category for c in cases})
    print(f"\ncategories ({len(categories)}): {', '.join(categories)}")

    recall_gaps = [r for r in rows if not r.has_recall_coverage]
    trap_gaps = [r for r in rows if not r.has_trap_coverage]
    if not rows:
        print(
            "\nEMPTY CATALOG — nothing to cover. A coverage report over zero skills "
            "proves\nnothing, so this counts as a gap rather than a pass "
            "(load_catalog returns an\nempty catalog for an empty/missing skills dir "
            "instead of raising)."
        )
        return rows

    if recall_gaps:
        print(f"\nRECALL GAP — {len(recall_gaps)} skill(s) with zero required cases:")
        for row in recall_gaps:
            need = "a case where it is the SOLE required id" if row.foundation else "one case"
            print(f"  {row.skill_id} (needs {need})")
        print(
            "\nThese skills are untested for recall: the selector could miss them on "
            "every\nconversation and the suite would still report a clean run. Add at "
            "least one case\nwith the skill in expect.required — and for a foundation, "
            "one where nothing else is\nrequired alongside it (the foundation-only "
            "category), since a foundation is never\ndemanded next to a specific."
        )
    if trap_gaps:
        print(f"\nROUTING GAP — {len(trap_gaps)} skill(s) declaring neighbours with zero traps:")
        for row in trap_gaps:
            print(f"  {row.skill_id} fences against: {', '.join(row.neighbours)}")
        print(
            "\nEach of those carve-outs CLAIMS a confusion the catalog text resolves, "
            "and no case\never tests the claim. Add one case in category "
            f"{TRAP_CATEGORY!r} with the skill in\nexpect.required and at least one "
            "declared neighbour left out of required AND optional,\nso the case asserts "
            "the neighbour must not be picked. The user text must be the kind of\nthing "
            "a real person says — echoing the skill's own trigger phrases measures "
            "keyword\noverlap, not routing — and the ground truth must be decidable from "
            "the whenToUse text\non disk. If no clause decides it, the carve-out is "
            "unverifiable: fix the catalog text\nrather than inventing a case to turn "
            "this number green."
        )
    if not recall_gaps and not trap_gaps:
        print(
            f"\nAll {len(rows)} catalog skills have a required case, and every skill "
            "declaring\nconfusable neighbours has a routing trap that excludes one."
        )
    return rows
