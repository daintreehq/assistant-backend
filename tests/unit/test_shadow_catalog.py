"""The synthetic catalogs the scale harness measures against.

These distractors exist to make a recall number mean something, so the ways they
can quietly stop doing that are what is pinned here: drifting from the real
skill format, becoming trivially separable, colliding with real ids, or ceasing
to be reproducible. A weak distractor pool produces a confidently green scale
result that means the opposite of what it appears to.

No model is loaded — generation is pure text.
"""

from __future__ import annotations

import re

import pytest

from benchmarks.skill_selection.shadow_catalog import (
    SHAPES,
    Distractor,
    generate_distractors,
    shadow_catalog,
)
from daintree_assistant_server.skills.catalog import (
    Catalog,
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.local.documents import skill_document


def _words(text: str) -> set[str]:
    """Content words of a routing document. Four letters up, to skip the
    connective tissue every document shares regardless of subject."""
    return set(re.findall(r"[a-z]{4,}", text.lower()))


@pytest.fixture(scope="module")
def real() -> Catalog:
    return load_catalog(default_skills_dir())


@pytest.fixture(scope="module")
def pool(real: Catalog) -> list[Distractor]:
    return generate_distractors(400, real)


def test_generation_is_reproducible(real: Catalog) -> None:
    """A recall number is only comparable across runs if the catalog is."""
    first = [d.skill.id for d in generate_distractors(120, real)]
    second = [d.skill.id for d in generate_distractors(120, real)]
    assert first == second


def test_sizes_are_nested_so_a_sweep_can_reuse_its_vectors(real: Catalog) -> None:
    """``shadow_catalog(200)`` must be a prefix of ``shadow_catalog(500)``.

    Not cosmetic: the sweep embeds the largest size first and lets the
    classifier's digest-keyed memo serve every smaller one. Non-nested sizes
    would silently turn a 220-second sweep into a four-hour one.
    """
    pool = generate_distractors(500, real)
    small = [s.id for s in shadow_catalog(200, real, pool).skills]
    large = [s.id for s in shadow_catalog(500, real, pool).skills]
    assert set(small) < set(large)


def test_every_distractor_is_a_valid_skill(pool: list[Distractor]) -> None:
    """They go through ``SkillMeta`` validation, so a generated document cannot
    be a shape the loader would reject on disk."""
    for d in pool:
        assert d.skill.meta.id and d.skill.meta.title
        assert d.skill.meta.summary and d.skill.meta.when_to_use
        assert d.skill.content_hash.startswith("sha256:")


def test_no_distractor_shadows_a_real_skill_id(real: Catalog, pool: list[Distractor]) -> None:
    """A collision would silently REPLACE a real skill rather than crowd it,
    which turns a recall measurement into a different experiment."""
    ids = {d.skill.id for d in pool}
    assert not (ids & set(real.known_ids))
    assert len(ids) == len(pool)


def test_a_shadow_catalog_keeps_every_real_skill(real: Catalog, pool: list[Distractor]) -> None:
    catalog = shadow_catalog(300, real, pool)
    assert set(real.known_ids) <= set(catalog.known_ids)
    assert len(catalog) == 300


def test_every_declared_shape_is_actually_produced(pool: list[Distractor]) -> None:
    """``SHAPES`` is the weighting, and a template that stopped being reachable
    would narrow the pool without changing the declared mix."""
    produced = {d.shape for d in pool}
    assert produced == {name for name, _ in SHAPES}


def test_the_pool_is_not_uniformly_adversarial(pool: list[Distractor]) -> None:
    """A pool of nothing but paraphrases measures a catalog nobody would ship.

    Hard-weighted but mixed: the easy `unrelated` shape has to be a real share,
    and the hard shapes together have to be a bigger one.
    """
    counts = {shape: sum(d.shape == shape for d in pool) for shape in {d.shape for d in pool}}
    total = len(pool)
    assert 0.2 < counts["unrelated"] / total < 0.5
    hard = counts["paraphrase"] + counts["scope_split"] + counts["collision"]
    assert hard / total > 0.35


def test_derived_distractors_overlap_their_source_vocabulary(
    real: Catalog, pool: list[Distractor]
) -> None:
    """The whole point: a distractor has to look like the skill it crowds.

    Measured as token overlap against the source's own routing document, not as
    a substring of the id — an earlier version asserted the latter and failed on
    ``runoff-tiebreaker`` versus "tie-breaker", which is a hyphen, not a weak
    distractor. A generator that emitted generic text would leave retrieval an
    easier job at 500 skills than it has at 38, and the sweep would report that
    as safety.
    """
    by_id = {s.id: s for s in real.skills}
    overlaps = []
    for d in (x for x in pool if x.derived_from):
        source = _words(skill_document(by_id[d.derived_from].meta))
        derived = _words(skill_document(d.skill.meta))
        overlaps.append(len(source & derived) / max(len(source), 1))

    assert overlaps
    # Half the source's vocabulary, at the median. Well above what unrelated
    # English prose shares, and low enough that a paraphrase is not required to
    # be a copy.
    median = sorted(overlaps)[len(overlaps) // 2]
    assert median > 0.5, f"derived distractors share only {median:.2f} of source vocabulary"


def test_unrelated_distractors_do_NOT_overlap_the_real_catalog(
    real: Catalog, pool: list[Distractor]
) -> None:
    """The control for the test above.

    Without it, a generator that made every distractor a near-copy would pass
    the overlap check and produce a pool with no easy half — which is not the
    catalog shape being modelled, and would make the recall numbers pessimistic
    rather than merely wrong.
    """
    catalog_words = set().union(*(_words(skill_document(s.meta)) for s in real.skills))
    unrelated = [d for d in pool if d.shape == "unrelated"]
    assert unrelated
    shares = [
        len(_words(skill_document(d.skill.meta)) - catalog_words)
        / max(len(_words(skill_document(d.skill.meta))), 1)
        for d in unrelated
    ]
    assert sorted(shares)[len(shares) // 2] > 0.2, "the easy shape is not actually foreign"


def test_the_hard_shapes_carry_an_exclusion_clause(pool: list[Distractor]) -> None:
    """`scope_split`, `sibling` and `collision` defer to their source.

    The negative-clause structure the real catalog uses, and the one a lexical
    shortlist was measured to get backwards. Asserted as "defers to it", not "by
    dotted id" — see the id-reference test below for why most of them use the
    title instead.
    """
    by_id = {s.id: s for s in load_catalog(default_skills_dir()).skills}
    for shape in ("scope_split", "sibling", "collision"):
        for sample in (d for d in pool if d.shape == shape):
            assert sample.derived_from is not None
            when = sample.skill.meta.when_to_use
            assert "Do not use" in when
            source = by_id[sample.derived_from]
            assert source.id in when or source.title.lower() in when.lower()


def test_id_references_do_not_swamp_the_real_catalogs_fan_in(
    real: Catalog, pool: list[Distractor]
) -> None:
    """A distractor naming its source by DOTTED ID is a measurement hazard.

    The query carries the active ids literally, through ``ACTIVE_PREFIX``, while
    a skill's own routing document does not contain its id at all
    (``skill_document`` renders title/summary/whenToUse). So a pool where every
    exclusion names an id hands each real skill a crowd of synthetic competitors
    holding an exact token sequence the target itself lacks — and the
    dense-retrieval numbers would then be measuring the generator.

    The real catalog averages 4.1 outgoing id references per skill. This keeps
    the synthetic fan-in within a small multiple of that rather than an order of
    magnitude above it.
    """
    ids = set(real.selectable_ids)
    naming = sum(1 for d in pool if any(i in d.skill.meta.when_to_use for i in ids))
    assert naming / len(pool) < 0.40, "too many distractors name a real skill id"
    assert naming > 0, "no distractor names an id — the real catalog does"


def test_every_rendered_document_is_distinct(real: Catalog, pool: list[Distractor]) -> None:
    """Two rows with the same TEXT are one vector occupying two slots.

    The id is not part of a rendered document, so a template that varied only the
    id produced text-identical rows — measured, 961 distractors collapsed to 492
    distinct documents with one repeated seven times. A "1,000-skill" catalog
    that is really 530 surfaces plus multiplicity does not measure catalog
    growth; it measures duplication, and every conclusion past 200 skills rested
    on it.
    """
    documents = [skill_document(d.skill.meta) for d in pool]
    assert len(set(documents)) == len(documents)
    # ...and distinct from the real catalog's, or a distractor would BE a real
    # skill's document and score as a retrieval hit for it.
    assert not (set(documents) & {skill_document(s.meta) for s in real.skills})


def test_a_distractor_does_not_repeat_its_lead_in(pool: list[Distractor]) -> None:
    """"Use when Use for a one-off..." is text no real skill carries.

    A doubled lead appeared on a third of the first pool, from stripping only
    "Use when" while the catalog also uses "Use for", "Use ONLY" and
    "Use whenever". A distractor separable on a formatting artifact is not
    measuring what the harness claims.
    """
    leads = ("use when", "use for", "use only", "use whenever", "reach for this when",
             "applies when", "pick this when", "choose this when", "follow this when",
             "the right runbook when")
    for d in pool:
        when = " ".join(d.skill.meta.when_to_use.lower().split())
        for outer in leads:
            for inner in leads:
                assert f"{outer} {inner} " not in when, f"{d.skill.id}: {when[:80]!r}"


def test_asking_for_more_than_the_pool_holds_is_an_error(real: Catalog) -> None:
    """Silently returning a smaller catalog would report a 500-skill recall
    number taken at 200."""
    pool = generate_distractors(10, real)
    with pytest.raises(ValueError, match="pool holds"):
        shadow_catalog(500, real, pool)


def test_a_shadow_smaller_than_the_real_catalog_is_an_error(
    real: Catalog, pool: list[Distractor]
) -> None:
    with pytest.raises(ValueError, match="smaller than the real catalog"):
        shadow_catalog(5, real, pool)
