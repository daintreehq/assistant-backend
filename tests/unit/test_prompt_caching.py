"""Prompt asset caching.

Two separate mechanisms, with different correctness stories:

* **Prompt bodies are read once at construction.** Safe because they are
  immutable files inside the package and the loader is a process-wide singleton.
* **Rendered fragments are memoised in a bounded LRU.** Safe only if the key
  captures everything the output depends on — so the interesting tests here are
  the ones that prove a *changed input* produces changed bytes, not the ones that
  prove a repeated input is fast.

A cache that returns stale prompt text is worse than no cache: the model silently
operates on instructions that no longer exist on disk, and nothing fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daintree_assistant_server.prompts.loader import PromptError, PromptLoader, RenderCache

SRC_PROMPTS = (
    Path(__file__).resolve().parents[2] / "src" / "daintree_assistant_server" / "prompts"
)


# ---------------------------------------------------------------------------
# Eager loading.
# ---------------------------------------------------------------------------


def test_no_prompt_file_is_read_on_the_request_path(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The point of the change. Every turn used to glob a directory and read
    eight partials before the model was even contacted."""
    loader = PromptLoader()

    reads: list[str] = []
    original = Path.read_text

    def _tracked(self: Path, *args: object, **kwargs: object) -> str:
        reads.append(str(self))
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _tracked)

    loader.base_prompt()
    loader.task_system_prompt("checkpoint")

    assert reads == [], f"prompt files were read on the request path: {reads}"


def test_bodies_are_served_from_memory_after_a_file_changes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Intended behaviour, stated explicitly so it is not mistaken for a bug.

    A packaged prompt cannot change under a running process. If one somehow does
    — an editor mid-deploy, a half-written file — continuing to serve the version
    that passed startup validation is the *safe* outcome; re-reading it per
    request would let a partial write reach the model.
    """
    import shutil

    staged = tmp_path / "prompts"
    shutil.copytree(SRC_PROMPTS, staged)
    loader = PromptLoader(staged)
    before = loader.base_prompt()

    (staged / "main" / "base" / "00-persona.md").write_text("REPLACED", encoding="utf-8")

    assert loader.base_prompt() == before
    # A NEW loader — the real deployment unit — does see the change.
    assert "REPLACED" in PromptLoader(staged).base_prompt()


def test_a_missing_prompt_dir_fails_at_construction(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Eager loading moves this failure to startup, where it fails readiness
    rather than the first request that happens to need it.

    Simulated by making the incomplete directory the PACKAGED one, because a
    directory that merely differs from the package is an overlay and legitimately
    falls back to it (see the overlay test below).
    """
    from daintree_assistant_server.prompts import loader as loader_module

    (tmp_path / "main" / "base").mkdir(parents=True)
    (tmp_path / "main" / "base" / "00.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(loader_module, "default_prompts_dir", lambda: tmp_path)

    with pytest.raises(PromptError, match="missing prompt dir"):
        PromptLoader(tmp_path)


def test_a_partial_directory_is_an_overlay_not_a_broken_prompt_set(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The prompt-variant benchmarks ship ONE alternative template and expect the
    rest from the package. Requiring a full vendored copy would mean forty files
    to change one."""
    variant = tmp_path / "variant"
    (variant / "main").mkdir(parents=True)
    (variant / "main" / "integrations.md.j2").write_text(
        "VARIANT INTEGRATIONS {{ mcp_servers | length }}", encoding="utf-8"
    )

    loader = PromptLoader(variant)
    loader.validate()

    # The overridden template wins...
    assert "VARIANT INTEGRATIONS" in loader.render("main/integrations.md.j2", mcp_servers=[])
    # ...and everything it does not provide comes from the package.
    assert loader.base_prompt() == PromptLoader().base_prompt()
    assert loader.task_system_prompt("checkpoint")


def test_templates_do_not_auto_reload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One consistent prompt generation per process.

    With Jinja's auto-reload left on, an edit mid-run produced MIXED generations:
    keys already in the render caches kept the old bytes while a new key picked
    up the new template — two different instruction sets in one process.
    """
    variant = tmp_path / "variant"
    (variant / "main").mkdir(parents=True)
    template = variant / "main" / "integrations.md.j2"
    template.write_text("FIRST", encoding="utf-8")

    loader = PromptLoader(variant)
    assert loader.render("main/integrations.md.j2") == "FIRST"

    template.write_text("SECOND", encoding="utf-8")
    assert loader.render("main/integrations.md.j2") == "FIRST"


def test_content_hashes_distinguish_two_prompt_sets(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`prompt_version` is a fixed family label, so it cannot tell two builds
    apart. These hashes are what makes a benchmark number attributable."""
    import shutil

    staged = tmp_path / "prompts"
    shutil.copytree(SRC_PROMPTS, staged)
    before = PromptLoader(staged).content_hashes()

    (staged / "main" / "base" / "00-persona.md").write_text("CHANGED", encoding="utf-8")
    after = PromptLoader(staged).content_hashes()

    assert before["base"] != after["base"]
    # Only the changed asset moves.
    assert before["task:checkpoint"] == after["task:checkpoint"]


def test_content_hashes_cover_templates_too(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A template carries as much behaviour as a raw body.

    Hashing only the raw bodies meant two builds whose TEMPLATE instructions
    differed reported identical hashes — so a benchmark number could be
    attributed to the wrong prompt set.
    """
    import shutil

    staged = tmp_path / "prompts"
    shutil.copytree(SRC_PROMPTS, staged)
    before = PromptLoader(staged).content_hashes()

    template = staged / "main" / "response_contract.md.j2"
    template.write_text(template.read_text(encoding="utf-8") + "\nEXTRA", encoding="utf-8")
    after = PromptLoader(staged).content_hashes()

    key = "template:main/response_contract.md.j2"
    assert before[key] != after[key]
    assert before["base"] == after["base"]


# ---------------------------------------------------------------------------
# The bounded LRU itself.
# ---------------------------------------------------------------------------


def test_the_cache_is_bounded() -> None:
    """A runaway key space would be a slow memory leak in a process that is meant
    to be stateless."""
    cache = RenderCache(maxsize=3)
    for i in range(10):
        cache.put((str(i),), f"v{i}")
    assert len(cache._entries) == 3
    # The most recent survive; the oldest are gone.
    assert cache.get(("9",)) == "v9"
    assert cache.get(("0",)) is None


def test_the_cache_evicts_least_recently_USED_not_least_recently_added() -> None:
    cache = RenderCache(maxsize=2)
    cache.put(("a",), "A")
    cache.put(("b",), "B")
    cache.get(("a",))  # refresh a
    cache.put(("c",), "C")  # evicts b, not a
    assert cache.get(("a",)) == "A"
    assert cache.get(("b",)) is None


# ---------------------------------------------------------------------------
# Rendered fragments: a changed input must change the bytes.
# ---------------------------------------------------------------------------


def test_the_same_skill_set_in_ANY_order_renders_identical_bytes(assembler, catalog) -> None:  # type: ignore[no-untyped-def]
    """The failure this guards, and why rendering the same list twice missed it.

    The block was rendered in the selector's raw output order, so the same set
    classified in a different order produced different bytes — the skill
    sections merely swapped places. That cost a full upstream prefix-cache miss
    on everything behind the block — the integrations, startup and conversation
    rows — and a second LRU entry for a render that says the same thing. (It cost
    the tool schemas too until issue #39 moved the block below them.) Reversing the
    input is what makes the assertion bite — repeating one list cannot.
    """
    specifics = [s for s in catalog.skills if not s.meta.foundation]
    assert len(specifics) >= 2
    canonical = specifics[:2]
    assert canonical[0].id < canonical[1].id, "catalog order IS canonical id order"

    cache = assembler._loaded_skills_cache
    first = assembler.render_loaded_skills(canonical)
    hits_before, misses_before = cache.hits, cache.misses
    entries_before = len(cache._entries)

    second = assembler.render_loaded_skills(list(reversed(canonical)))

    assert second == first
    assert cache.hits == hits_before + 1
    assert cache.misses == misses_before, "the reversed call re-rendered"
    assert len(cache._entries) == entries_before, "the reversed call earned its own entry"


def test_a_different_skill_set_renders_different_bytes(assembler, catalog) -> None:  # type: ignore[no-untyped-def]
    ids = sorted(catalog.known_ids)
    one = assembler.render_loaded_skills(catalog.get_many(ids[:1]))
    two = assembler.render_loaded_skills(catalog.get_many(ids[:2]))
    assert one != two


def test_the_skill_cache_keys_on_CONTENT_not_id(assembler, catalog) -> None:  # type: ignore[no-untyped-def]
    """The failure this key choice exists to prevent.

    Skill learning rewrites a skill's body in place, keeping its id. An id-keyed
    cache would keep serving the pre-rewrite body until the process restarted —
    the model operating on a runbook that no longer exists on disk, with nothing
    failing to signal it.
    """
    from dataclasses import replace

    original = catalog.get_many(sorted(catalog.known_ids)[:1])
    before = assembler.render_loaded_skills(original)

    rewritten = [replace(original[0], body=original[0].body + "\n\nNEW STEP.")]
    assert rewritten[0].id == original[0].id

    after = assembler.render_loaded_skills(rewritten)
    assert after != before
    assert "NEW STEP." in after


def test_a_stale_content_hash_cannot_be_constructed(catalog) -> None:  # type: ignore[no-untyped-def]
    """The cache trusts `content_hash` completely, so the hash must be DERIVED.

    It used to be a plain field: `dataclasses.replace(skill, body=...)` produced
    a skill whose stored hash described the old body, and the cache would then
    serve the previous runbook while the catalog reported the new one. Deriving
    it in `__post_init__` makes that state unconstructable rather than merely
    unreached — the difference between an invariant and a convention.
    """
    from dataclasses import FrozenInstanceError, fields, replace

    original = catalog.get_many(sorted(catalog.known_ids)[:1])[0]

    # replace() recomputes rather than carrying the old hash forward.
    rewritten = replace(original, body=original.body + "\n\nNEW STEP.")
    assert rewritten.content_hash != original.content_hash

    # And the hash cannot be supplied at all.
    hash_field = next(f for f in fields(original) if f.name == "content_hash")
    assert hash_field.init is False, "content_hash is settable again; it must be derived"
    with pytest.raises(TypeError):
        type(original)(  # type: ignore[call-arg]
            meta=original.meta, body="anything", content_hash="sha256:lie"
        )
    with pytest.raises(FrozenInstanceError):
        original.content_hash = "sha256:lie"  # type: ignore[misc]


def test_the_catalog_revision_moves_when_any_body_changes(catalog) -> None:  # type: ignore[no-untyped-def]
    """The selector cache keys on the revision, so this is what makes a stale
    selector menu impossible rather than merely unlikely."""
    from dataclasses import replace

    from daintree_assistant_server.skills.catalog import Catalog

    skills = list(catalog.skills)
    edited = [replace(skills[0], body=skills[0].body + "\n\nNEW STEP."), *skills[1:]]
    assert Catalog(edited).revision != catalog.revision
