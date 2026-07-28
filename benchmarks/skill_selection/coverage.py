"""Case-suite coverage matrix — which catalog skills the suite actually tests.

Offline and free: loads the real catalog and the real case suite, nothing else.
No API key, no network.

The gate this exists for: a skill with **zero `required` cases is never tested for
recall**. The selector can fail to pick it on every conversation in the world and
the benchmark still reports a green run, because nothing ever demanded it. That is
not hypothetical — five skills sat in the catalog with zero recall coverage for
three weeks and no run ever noticed. So ``bench coverage`` exits non-zero on a
recall gap, and is meant to be run whenever the catalog changes.

**Foundations are gated differently, on purpose.** A domain foundation is paired
with a selected specific deterministically DOWNSTREAM of the classifier, so a
case whose answer is a specific must not demand it (``cases.py`` rejects that
shape outright). Counting a foundation's ``req`` the same way as a specific's
would therefore pressure an author into writing exactly the case the loader
rejects. The only conversation that tests a foundation for recall is one where
it is the WHOLE answer — no runbook squarely matches, load it ALONE — so a
foundation's gate reads ``alone`` instead of ``req``. That is what the
``foundation-only`` cases exist to supply, and now it is stated rather than
accidental.

Five per-skill counts, each a count of CASES (not trials):

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

from dataclasses import dataclass

from daintree_assistant_server.skills.catalog import Catalog

from .cases import BenchCase


@dataclass(frozen=True)
class SkillCoverage:
    """Per-skill case counts across the whole suite."""

    skill_id: str
    foundation: bool
    required: int
    alone: int
    optional: int
    active: int
    negative: int

    @property
    def has_recall_coverage(self) -> bool:
        """Recall coverage, read by the exit gate.

        A foundation is only ever demanded ALONE (see the module docstring), so a
        ``required`` count that came from riding along with specifics would not
        prove the selector can pick it when it IS the answer.
        """
        return self.alone > 0 if self.foundation else self.required > 0


def build_coverage(catalog: Catalog, cases: list[BenchCase]) -> list[SkillCoverage]:
    """Count, for every catalog skill, the cases that exercise it.

    Iterates the CATALOG (not the cases), so a skill no case mentions at all still
    gets a row of zeros rather than silently vanishing from the report — that
    absence is the whole point.
    """
    rows: list[SkillCoverage] = []
    for skill in catalog.skills:
        sid = skill.id
        rows.append(
            SkillCoverage(
                skill_id=sid,
                foundation=skill.meta.foundation,
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
        "counts are CASES: req=required  alone=sole-required  opt=allowed  "
        "act=prior-active  neg=implicit"
    )
    print("recall gate reads `req`, except a [foundation], which is gated on `alone`")
    print(f"\n{'skill':<{width}}  {'req':>4} {'alone':>6} {'opt':>4} {'act':>4} {'neg':>4}")
    print("-" * (width + 30))
    for row in rows:
        flag = "" if row.has_recall_coverage else "   <- NO REQUIRED CASE"
        label = f"{row.skill_id}{' [foundation]' if row.foundation else ''}"
        print(
            f"{label:<{width}}  {row.required:>4} {row.alone:>6} {row.optional:>4} "
            f"{row.active:>4} {row.negative:>4}{flag}"
        )

    categories = sorted({c.category for c in cases})
    print(f"\ncategories ({len(categories)}): {', '.join(categories)}")

    gaps = [r for r in rows if not r.has_recall_coverage]
    if not rows:
        print(
            "\nEMPTY CATALOG — nothing to cover. A coverage report over zero skills "
            "proves\nnothing, so this counts as a gap rather than a pass "
            "(load_catalog returns an\nempty catalog for an empty/missing skills dir "
            "instead of raising)."
        )
    elif gaps:
        print(f"\nRECALL GAP — {len(gaps)} skill(s) with zero required cases:")
        for row in gaps:
            need = "a case where it is the SOLE required id" if row.foundation else "one case"
            print(f"  {row.skill_id} (needs {need})")
        print(
            "\nThese skills are untested for recall: the selector could miss them on "
            "every\nconversation and the suite would still report a clean run. Add at "
            "least one case\nwith the skill in expect.required — and for a foundation, "
            "one where nothing else is\nrequired alongside it (the foundation-only "
            "category), since a foundation is never\ndemanded next to a specific."
        )
    else:
        print(f"\nEvery catalog skill has at least one required case ({len(rows)} skills).")
    return rows
