"""Prompt template loading and versioning.

All prompt text is server-owned and lives in this package as ``.md`` and
``.md.j2`` files. The loader renders templates and exposes raw prompt bodies.
Startup validation ensures every template parses and the base prompt exists, so
a malformed prompt fails readiness rather than a request.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

# Immutable prompt-family versions (surfaced in metadata and logs).
MAIN_PROMPT_VERSION = "main-2026-06-27.2"
SELECTOR_PROMPT_VERSION = "selector-2026-06-26.1"

# Main-prompt template names.
TEMPLATE_RUNTIME_CONTEXT = "main/runtime_context.md.j2"
TEMPLATE_INTEGRATIONS = "main/integrations.md.j2"
TEMPLATE_LOADED_SKILLS = "main/loaded_skills.md.j2"
TEMPLATE_TURN_FOOTER = "main/turn_footer.md.j2"
TEMPLATE_SELECTOR_SYSTEM = "selector/system.md.j2"

_ALL_TEMPLATES = (
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_INTEGRATIONS,
    TEMPLATE_LOADED_SKILLS,
    TEMPLATE_TURN_FOOTER,
    TEMPLATE_SELECTOR_SYSTEM,
)


class PromptError(Exception):
    """Raised when a prompt is missing or fails to load."""


def default_prompts_dir() -> Path:
    return Path(__file__).resolve().parent


class PromptLoader:
    """Renders prompt templates and reads raw prompt bodies."""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self.dir = prompts_dir or default_prompts_dir()
        self.env = Environment(
            loader=FileSystemLoader(str(self.dir)),
            autoescape=select_autoescape(enabled_extensions=()),
            keep_trailing_newline=True,
            trim_blocks=False,
            lstrip_blocks=False,
        )

    # ---- raw bodies -----------------------------------------------------

    def base_prompt(self) -> str:
        # The permanent base prompt is authored as ordered section partials
        # (``main/base/NN-*.md``) and assembled here into one stable block;
        # filename order is load-bearing, so partials are read sorted by name.
        base_dir = self.dir / "main" / "base"
        if not base_dir.is_dir():
            raise PromptError(f"missing base prompt dir: {base_dir}")
        parts = sorted(base_dir.glob("*.md"))
        if not parts:
            raise PromptError(f"no base prompt parts in {base_dir}")
        sections = [
            p.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n") for p in parts
        ]
        return "\n\n".join(sections)

    def task_system_prompt(self, task_id: str) -> str:
        path = self.dir / "tasks" / f"{task_id}.md"
        if not path.is_file():
            raise PromptError(f"missing task prompt: {task_id}")
        return path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")

    # ---- rendering ------------------------------------------------------

    def render(self, template_name: str, /, **context: object) -> str:
        try:
            template = self.env.get_template(template_name)
            return template.render(**context)
        except TemplateError as exc:
            raise PromptError(f"failed to render {template_name}: {exc}") from exc

    # ---- startup validation --------------------------------------------

    def validate(self) -> None:
        """Ensure the base prompt is present and every template parses."""
        base = self.base_prompt()
        if not base.strip():
            raise PromptError("base prompt is empty")
        for name in _ALL_TEMPLATES:
            try:
                self.env.get_template(name)
            except TemplateError as exc:
                raise PromptError(f"template {name} failed to load: {exc}") from exc
