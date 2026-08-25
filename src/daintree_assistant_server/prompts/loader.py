"""Prompt template loading.

All prompt text is server-owned and lives in this package as ``.md`` and
``.md.j2`` files. The loader renders templates and exposes raw prompt bodies.
Startup validation ensures every template parses and the base prompt exists, so
a malformed prompt fails readiness rather than a request.

**Prompt bodies are read once, at construction.** They are immutable files
shipped inside the package — nothing can change them while the process runs — so
re-reading them per request bought nothing and cost a directory glob plus seven
file reads on the hot path of every turn, before the model was even contacted.
The loader is built once per process (``main.build_container``), so eager loading
also moves any missing-file or unreadable-file failure to startup, where it fails
readiness instead of a request.

If prompt hot-reload is ever wanted for local iteration, it belongs in an
explicitly development-only subclass rather than as a tax on every production
request. Today the dev loop restarts the process.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    TemplateError,
    select_autoescape,
)

# Stable prompt-family labels (surfaced in metadata and logs). Pre-release: these
# are fixed family names, NOT versions — do not date-stamp them or bump them on
# prompt edits. Everything on disk is simply "the current prompt." If real
# release versioning is ever needed, that is a deliberate, separate change.
MAIN_PROMPT_VERSION = "main"

# Main-prompt template names.
TEMPLATE_STARTUP_CONTEXT = "main/startup_context.md.j2"
TEMPLATE_RUNTIME_CONTEXT = "main/runtime_context.md.j2"
TEMPLATE_INTEGRATIONS = "main/integrations.md.j2"
TEMPLATE_LOADED_RUNBOOKS = "main/loaded_runbooks.md.j2"
TEMPLATE_TURN_FOOTER = "main/turn_footer.md.j2"
TEMPLATE_RESPONSE_CONTRACT = "main/response_contract.md.j2"
TEMPLATE_DISPLAY_CONTEXT = "main/display_context.md.j2"
TEMPLATE_DOCS_CONTEXT = "main/docs_context.md.j2"

# Fast-response templates. A SEPARATE loaded-runbooks template from the main
# one on purpose: the executor's version instructs `runbook.step.advance` and
# `runbook.run.get`, tools the fast model has no way to call and must never be
# told about. Reusing it would put two uncallable tool names in front of a model
# whose only job is to describe intent.
TEMPLATE_FAST_RESPONSE_RUNBOOKS = "fast_response/loaded_runbooks.md.j2"

_ALL_TEMPLATES = (
    TEMPLATE_STARTUP_CONTEXT,
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_INTEGRATIONS,
    TEMPLATE_LOADED_RUNBOOKS,
    TEMPLATE_TURN_FOOTER,
    TEMPLATE_RESPONSE_CONTRACT,
    TEMPLATE_DISPLAY_CONTEXT,
    TEMPLATE_DOCS_CONTEXT,
    TEMPLATE_FAST_RESPONSE_RUNBOOKS,
)


# Character budget per cache. Chosen against the WIDEST plausible storage: a
# Python string containing any non-BMP character costs 4 bytes per character, so
# this bounds each cache at roughly 8 MiB even in that worst case, and ~2 MiB for
# ordinary mostly-ASCII prompt text.
_CACHE_CHAR_BUDGET = 2_000_000


class RenderCache:
    """A tiny bounded LRU for rendered prompt fragments.

    Deliberately not ``functools.lru_cache``: the cached value depends on
    instance state (the prompt loader), and a decorator on a method would key on
    ``self`` and keep every assembler alive for the life of the process.

    Not thread-safe, and that is checked rather than assumed. The service does use
    ``asyncio.to_thread`` in two places — the catalog reload and runbook-learning
    file writes — but neither touches a render cache: they do disk I/O and hand
    back a new ``Catalog``. Every cache access happens on the event loop, so a
    lock on the request path would protect against nothing and cost every turn.

    If a cached render is ever called from a worker thread, this needs a lock —
    the worst case is a torn ``OrderedDict`` rather than merely a duplicated
    render.
    """

    def __init__(self, maxsize: int, max_chars: int = _CACHE_CHAR_BUDGET) -> None:
        self._maxsize = maxsize
        self._max_chars = max_chars
        self._entries: OrderedDict[tuple[str, ...], str] = OrderedDict()
        self._chars = 0
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, int]:
        """Counters, for a caller that wants to log them.

        ``hits`` and ``misses`` have been incremented since this class was
        written and read by nothing but a unit test, which makes them a
        measurement nobody has ever taken. ``entries`` and ``chars`` come along
        because the two bounds were chosen rather than measured: whether 64
        entries is the right size is a question only production can answer, and
        it cannot answer a question nobody asked it.
        """
        return {
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self._entries),
            "chars": self._chars,
        }

    def get(self, key: tuple[str, ...]) -> str | None:
        value = self._entries.get(key)
        # `is None`, never truthiness: an empty render is a legitimate cached
        # value (no active runbooks renders to almost nothing), and treating it as
        # a miss would re-render it forever while reporting a misleading hit rate.
        if value is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: tuple[str, ...], value: str) -> None:
        # Decline a single oversized value rather than let it evict the entire
        # cache to make room for itself.
        if len(value) > self._max_chars:
            return
        if key in self._entries:
            self._chars -= len(self._entries[key])
        self._entries[key] = value
        self._entries.move_to_end(key)
        self._chars += len(value)
        # Evict until BOTH bounds hold. The entry count alone does not bound
        # memory: a runbook body may be up to 256 KiB, so 64 three-runbook renders is
        # ~96 MiB of text — and CPython stores a string with any non-ASCII
        # character at 2 or 4 bytes per character, so the real figure can be
        # several times the byte length of the same text on disk.
        while self._entries and (
            len(self._entries) > self._maxsize or self._chars > self._max_chars
        ):
            _, evicted = self._entries.popitem(last=False)
            self._chars -= len(evicted)


class PromptError(Exception):
    """Raised when a prompt is missing or fails to load."""


def default_prompts_dir() -> Path:
    return Path(__file__).resolve().parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


class PromptLoader:
    """Renders prompt templates and serves raw prompt bodies from memory."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        """Load from ``prompts_dir``, falling back to the packaged prompts.

        A directory that holds only SOME assets is an overlay, not a broken
        prompt set: the prompt-variant benchmarks ship a single alternative
        template and expect everything else to come from the package. Templates
        resolve overlay-first through a ``ChoiceLoader``, and
        raw bodies fall back per directory — so a variant can override one file
        without vendoring forty.
        """
        self.dir = prompts_dir or default_prompts_dir()
        packaged = default_prompts_dir()
        self._fallback: Path | None = (
            packaged if self.dir.resolve() != packaged.resolve() else None
        )
        search = [FileSystemLoader(str(self.dir))]
        if self._fallback is not None:
            search.append(FileSystemLoader(str(self._fallback)))
        self.env = Environment(
            loader=ChoiceLoader(search),
            autoescape=select_autoescape(enabled_extensions=()),
            keep_trailing_newline=True,
            trim_blocks=False,
            lstrip_blocks=False,
            # The whole point of this class is that prompt files do not change
            # under a running process. Leaving Jinja's auto-reload on would let a
            # mid-run edit produce MIXED generations: keys already in the render
            # caches keep the old bytes while a new key picks up the new template.
            # One consistent generation per process is the contract.
            auto_reload=False,
        )
        # Read once. See the module docstring: these are immutable packaged files
        # and the loader is a process-wide singleton, so the only thing per-request
        # reads bought was latency.
        self._base_prompt = self._load_base()
        self._base_prompt_no_declaration = self._load_base_without_declaration()
        self._subagent_prompt = self._load_parts("main/subagent")
        self._task_prompts = self._load_dir("tasks")
        self._fast_response_prompts = self._load_dir("fast_response")

    def _resolve(self, relative: str) -> Path:
        """The overlay's copy of ``relative`` if it has one, else the package's."""
        candidate = self.dir / relative
        if candidate.exists() or self._fallback is None:
            return candidate
        return self._fallback / relative

    # ---- eager loading --------------------------------------------------

    #: The partial that instructs the `[[DAINTREE:FINAL]]`/`[[DAINTREE:TOOLS]]`
    #: control line. Dropped from the base prompt unless a compaction producer is
    #: actually configured — see :meth:`base_prompt`.
    DECLARATION_PART = "15-response-declaration"

    def _load_base(self) -> str:
        return self._load_parts("main/base")

    def _load_base_without_declaration(self) -> str:
        return self._load_parts("main/base", skip={self.DECLARATION_PART})

    def _load_parts(self, relative: str, *, skip: frozenset[str] | set[str] = frozenset()) -> str:
        # A persona prompt is authored as ordered section partials
        # (``<relative>/NN-*.md``) and assembled here into one stable block;
        # filename order is load-bearing, so partials are read sorted by name.
        # Both personas — the orchestrator (``main/base``) and the read-only
        # sub-agent (``main/subagent``) — are built this way, so an overlay can
        # replace either one partial-by-partial.
        parts_dir = self._resolve(relative)
        if not parts_dir.is_dir():
            raise PromptError(f"missing prompt dir: {parts_dir}")
        parts = sorted(parts_dir.glob("*.md"))
        if not parts:
            raise PromptError(f"no prompt parts in {parts_dir}")
        # Omission is by filename stem and stays quiet when the stem is absent: a
        # prompt directory legitimately varies (the variant benchmarks ship one
        # template, tests ship one partial), so a missing partial is not evidence
        # of a rename here. That alarm belongs where the PACKAGED set is the
        # subject — see test_the_declaration_partial_still_exists_to_be_omitted.
        kept = [p for p in parts if p.stem not in skip]
        if not kept:
            # Omitting the only partial leaves an EMPTY system prompt, which is a
            # far worse outcome than the missing partial the caller asked about:
            # the model would run with no persona, no hard rules and no contract,
            # and nothing downstream inspects the prompt for emptiness.
            raise PromptError(f"omitting {sorted(skip)} would empty {parts_dir}")
        return "\n\n".join(_read(p).strip("\n") for p in kept)

    def _load_dir(self, name: str) -> dict[str, str]:
        """Every ``.md`` in a prompt subdirectory, keyed by filename stem.

        Loads the whole directory rather than a known list, so a prompt file that
        exists but is not registered is still available, and — more usefully — a
        REGISTERED prompt that is missing fails in :meth:`validate` with a name,
        rather than at the first request that needs it.
        """
        directory = self._resolve(name)
        if not directory.is_dir():
            raise PromptError(f"missing prompt dir: {directory}")
        return {p.stem: _read(p).rstrip("\n") for p in sorted(directory.glob("*.md"))}

    # ---- raw bodies -----------------------------------------------------

    def base_prompt(self, *, response_declaration: bool = True) -> str:
        """The permanent assistant prompt, with the declaration rule optional.

        The `[[DAINTREE:FINAL]]`/`[[DAINTREE:TOOLS]]` control line only pays for
        itself when something CONSUMES it. Its consumer is compaction, and until a
        producer ships every deployment holds ``NullCompactor`` — which is exactly
        when this argument is False, so the rule is omitted rather than paid for.
        Omitting it saves 646 bytes: the partial plus the blank line that joined
        it to its neighbours.

        Both variants are built at load, not per call: the choice is
        deployment-wide (a container holds one compactor for its lifetime), so the
        rendered prompt stays byte-identical across callers and the prefix cache
        does not split. Selecting per request would be the bug this avoids.
        """
        if response_declaration:
            return self._base_prompt
        return self._base_prompt_no_declaration

    def subagent_prompt(self) -> str:
        """The read-only sub-agent persona (``profile="subagent"``).

        A separate prompt rather than a modifier on the base one: the sub-agent
        has a different job (answer one question), a different audience (the
        orchestrator, not a human), and a different output contract (the final
        message IS the deliverable). Layering that onto the orchestrator prompt
        would mean shipping ~8 KB of terminal, worktree, and spawn discipline to
        a worker that cannot do any of it, and then contradicting it line by line.
        """
        return self._subagent_prompt

    def fast_response_prompt(self) -> str:
        """The static system prompt for the preamble stage."""
        return self._read_fast("base")

    def _read_fast(self, stem: str) -> str:
        try:
            return self._fast_response_prompts[stem]
        except KeyError:
            raise PromptError(f"missing fast-response prompt: {stem}") from None

    def task_system_prompt(self, task_id: str) -> str:
        try:
            return self._task_prompts[task_id]
        except KeyError:
            raise PromptError(f"missing task prompt: {task_id}") from None

    # ---- content hashes -------------------------------------------------

    def content_hashes(self) -> dict[str, str]:
        """Short content hashes of the loaded prompt bodies.

        For diagnostics and benchmark provenance: a latency or quality number is
        only comparable against another run of the *same* prompts, and
        ``prompt_version`` is a fixed family label ("main"), not a revision. These
        hashes are what actually distinguish two builds.

        Computed on demand rather than at construction — nothing on the request
        path needs them, and hashing ~40 KB of text at startup for a field that
        may never be read is the wrong trade.
        """

        def digest(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

        hashes = {
            "base": digest(self._base_prompt),
            "subagent": digest(self._subagent_prompt),
        }
        for task_id, body in self._task_prompts.items():
            hashes[f"task:{task_id}"] = digest(body)
        # Templates too. Without them, two builds whose selector or loaded-runbooks
        # INSTRUCTIONS differ report identical hashes — and those templates carry
        # as much behaviour as the raw bodies do.
        for name in _ALL_TEMPLATES:
            source, _, _ = self.env.loader.get_source(self.env, name)  # type: ignore[union-attr]
            hashes[f"template:{name}"] = digest(source)
        return hashes

    # ---- rendering ------------------------------------------------------

    def render(self, template_name: str, /, **context: object) -> str:
        try:
            template = self.env.get_template(template_name)
            return template.render(**context)
        except TemplateError as exc:
            raise PromptError(f"failed to render {template_name}: {exc}") from exc

    # ---- startup validation --------------------------------------------

    # The markers the response contract SENDS THE MODEL TO. The contract is in the
    # cache-stable system region and carries no numbers of its own, so these strings
    # are the other half of a cross-reference, not decoration (issue #38).
    _DISPLAY_CONTEXT_MARKERS = ("# Display context", "Usable content width:", "Pipe-table cap:")

    def _validate_display_context(self) -> None:
        """Render the display block and require it to actually carry the geometry.

        Compiling is not enough for this one template. An OVERLAY that blanks it
        still compiles, renders to "", and assembles into a frame-only tail — while
        the stable contract goes on telling the model to read a ``# Display context``
        block that is not there, and the output lint goes on grading against the
        width it was supposed to state. That is silent, and it is exactly the
        prompt/lint drift the split was meant to make impossible, so it fails
        readiness here instead.
        """
        rendered = self.render(
            TEMPLATE_DISPLAY_CONTEXT, content_width=56, is_narrow=True, max_table_columns=3
        )
        for marker in self._DISPLAY_CONTEXT_MARKERS:
            if marker not in rendered:
                raise PromptError(
                    f"{TEMPLATE_DISPLAY_CONTEXT} rendered without {marker!r}; the response "
                    "contract references this block for the terminal geometry it does not "
                    "state itself"
                )

    def validate(self) -> None:
        """Ensure the base prompt is present and every template parses.

        Construction already surfaced unreadable files; this checks the things a
        successful read cannot: that the base prompt is non-empty, that every
        template compiles, and that every REGISTERED prompt actually exists.
        """
        if not self._base_prompt.strip():
            raise PromptError("base prompt is empty")
        if not self._subagent_prompt.strip():
            raise PromptError("subagent prompt is empty")
        for name in _ALL_TEMPLATES:
            try:
                self.env.get_template(name)
            except TemplateError as exc:
                raise PromptError(f"template {name} failed to load: {exc}") from exc
        self._validate_display_context()
        # The fast-response stage's own prompts. Registered here so a missing or
        # misnamed file fails READINESS with a name, rather than at the first turn
        # that reaches the stage — which, with the feature defaulting to off,
        # could be weeks after the file was lost.
        if not self.fast_response_prompt().strip():
            raise PromptError("fast-response base prompt is empty")
        # Every REGISTERED task must have its prompt on disk. Without this a task
        # whose prompt file is missing or misnamed passes readiness and the whole
        # test suite, then 500s the first time a client calls it — PromptError has
        # no handler. Imported lazily: task_runner imports the loader.
        from ..services.task_runner import build_profiles

        for task_id in build_profiles():
            self.task_system_prompt(task_id)
