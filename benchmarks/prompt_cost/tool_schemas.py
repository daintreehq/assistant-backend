"""Per-tool schema cost, so the audit has a target list instead of a total.

Tool schemas are the largest region of a request — 79.3 KiB against the pinned
inventory, ~65% of the deterministic fixture — and every measurement of them
until now has been an AGGREGATE over the whole array (``tools_bytes``, the
``tools_*_bytes`` log fields, the ``system_tools_bytes`` the cost benchmark
gates). An aggregate answers "how big is the tool region" and refuses the only
question an author can act on, which is *which tools*.

Nothing here is a gate. It is a report, run by hand before editing tool prose,
and it is deliberately offline: it reads the pinned CLI inventory fixture and
the packaged skill catalog, makes no network call, and costs nothing.

**Bytes are not the ranking.** A tool's cost is its size times how often it is
OFFERED, and the second factor spans more than an order of magnitude. So each
row carries a computed **reach** — how many of the catalog's skills would admit
this tool if selected — and the sort is reach first, bytes second. That order IS
the audit order: trimming 200 bytes from a tool on every request beats trimming
800 from one that rides a tenth of turns.

Reach is derived from the admission rules rather than assumed from a label,
because the labels do not line up with exposure the way they look like they do:

* ``ALWAYS_AVAILABLE`` is on every request there is, skill-less ones included —
  the permanent base prompt instructs it, and withholding an instructed tool is
  a contradiction the model cannot resolve;
* a tool no skill declares is on every request *too*, for the opposite reason:
  projection argues from metadata and there is none about a tool the catalog has
  never heard of. Two different arguments, identical exposure, so they share the
  widest rank;
* the SCAFFOLD rides every skill-bearing turn, and so — today — does a domain
  foundation's declared tools, because admission pairs each selected specific
  with its foundation and 38 of the 39 skills point at one. Same reach, again
  reached differently;
* a tool declared by three runbooks is offered three times as widely as one
  declared by a single runbook, which a tier alone cannot express at all.

``tier`` is kept beside the number as the REASON, not the ranking.

**What reach is not.** It is the static, baseline projection over the packaged
catalog — how widely a tool *could* be admitted — and not observed frequency:
it does not know which skills actually get selected, and it deliberately ignores
the two per-request admission paths (a forced ``tool_choice``, an unresolved
tool call), which depend on a turn rather than on the catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.tool_projection import (
    ALWAYS_AVAILABLE,
    SKILL_SCAFFOLD_TOOLS,
    tools_bytes,
    wire_name,
)

#: The same capture the hygiene tests and the deterministic cost benchmark read,
#: so a number here and a number there describe one inventory.
_INVENTORY = (
    Path(__file__).resolve().parents[1] / "_harness" / "fixtures" / "cli_tool_inventory.json"
)


def _bytes(value: Any) -> int:
    """Compact, non-ASCII-preserving, unsorted — as the provider will serialize it.

    The same encoding ``tools_bytes`` and the prompt-segment manifest use. A
    default ``json.dumps`` would inflate every non-ASCII character and pad with
    separators nothing sends, which is a difference of several percent on an
    inventory this size — enough to move a tool's rank.
    """
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _prose_bytes(value: Any) -> int:
    """Every ``description`` string anywhere in the schema, recursively.

    ``function.description`` is only the top of it. The inventory carries
    substantial prose under the parameter schema's own nested ``description``
    keys, and counting that as undifferentiated "parameter bytes" hid most of
    what a description audit is actually able to edit — a structurally large
    schema with terse prose would outrank a smaller one that is nearly all
    prose, which is backwards for this report's one job.
    """
    if isinstance(value, dict):
        total = 0
        for key, child in value.items():
            if key == "description" and isinstance(child, str):
                total += len(child.encode("utf-8"))
            else:
                total += _prose_bytes(child)
        return total
    if isinstance(value, list):
        return sum(_prose_bytes(item) for item in value)
    return 0


#: Admission reasons, widest-looking first. NOT the sort key — see `reach`.
_TIERS = ("always", "scaffold", "undeclared", "foundation", "skill")

#: Both sets are written in DOTTED spelling — the form a skill's `requiredTools`
#: uses — while the CLI inventory carries the wire spelling (`fs__read`). Comparing
#: them raw silently classified every core tool as `skill`, which reported the
#: never-withholdable floor as 4.2% of the inventory when it is nearer a quarter,
#: and put the one tier that is paid on EVERY request at the bottom of the audit
#: order. `wire_name` is idempotent, so converting here is safe either way.
_ALWAYS = frozenset(wire_name(name) for name in ALWAYS_AVAILABLE)
_SCAFFOLD = frozenset(wire_name(name) for name in SKILL_SCAFFOLD_TOOLS)


def _tier(name: str, declared_by: list[str], foundations: frozenset[str]) -> str:
    if name in _ALWAYS:
        return "always"
    if name in _SCAFFOLD:
        # Admitted whenever ANY skill is active, because `loaded_skills.md.j2`
        # instructs them and no skill declares them — so their exposure tracks
        # "a skill loaded", which is most turns but not the skill-less ones.
        return "scaffold"
    if not declared_by:
        # Never withheld — see `tool_projection`. Offered as widely as a core
        # tool, but for the opposite reason: not because something instructed
        # it, because nothing knows anything about it.
        return "undeclared"
    if any(skill_id in foundations for skill_id in declared_by):
        # `_admit_with_domain_foundations` pairs every selected specific with its
        # domain foundation, and `project_tools` admits the foundation's declared
        # tools alongside the specific's. So a foundation tool's exposure is
        # "any skill in that namespace loaded", not "this one runbook loaded".
        return "foundation"
    return "skill"


def run_report() -> dict[str, Any]:
    inventory = json.loads(_INVENTORY.read_text(encoding="utf-8"))
    catalog = load_catalog()

    foundations = frozenset(s.id for s in catalog.skills if s.meta.foundation)
    declarers: dict[str, list[str]] = {}
    #: Which skills' SELECTION would admit each tool — the declarer itself, plus
    #: every skill whose domain foundation declares it, because admission pairs
    #: a specific with its foundation. This is the exposure number; a tier is
    #: only the reason for it.
    admitters: dict[str, set[str]] = {}
    for skill in catalog.skills:
        for tool in skill.meta.required_tools or ():
            name = wire_name(tool)
            declarers.setdefault(name, []).append(skill.id)
            admitters.setdefault(name, set()).add(skill.id)
    for skill in catalog.skills:
        foundation_id = catalog.domain_foundation(skill.id)
        if not foundation_id or foundation_id == skill.id:
            continue
        foundation = catalog.get(foundation_id)
        if foundation is None:
            continue
        for tool in foundation.meta.required_tools or ():
            admitters.setdefault(wire_name(tool), set()).add(skill.id)

    total_skills = len(catalog.skills)
    rows: list[dict[str, Any]] = []
    for entry in inventory:
        function = entry.get("function") or {}
        name = function.get("name", "")
        declared_by = sorted(declarers.get(name, []))
        tier = _tier(name, declared_by, foundations)
        # `always` and `undeclared` are offered on skill-less turns too, which no
        # count over the catalog can express — so they are ranked ABOVE a tool
        # every skill would admit, rather than tied with one.
        universal = tier in ("always", "undeclared")
        reach = (
            total_skills
            if tier == "scaffold"
            else total_skills
            if universal
            else len(admitters.get(name, ()))
        )
        rows.append(
            {
                "name": name,
                "schema_bytes": _bytes(entry),
                # Split out because they are edited by different people for
                # different reasons: prose is what this report exists to prune,
                # while the parameter STRUCTURE is mostly a function of the
                # arguments the tool genuinely takes.
                "description_bytes": _bytes(function.get("description", "")),
                "parameter_bytes": _bytes(function.get("parameters") or {}),
                # Every `description` in the schema, nested ones included — the
                # editable surface, and the one to rank a prose audit on.
                "prose_bytes": _prose_bytes(entry),
                "tier": tier,
                "offered_on_skill_less_turns": universal,
                "reach": reach,
                "declaring_skills": len(declared_by),
                "declared_by": declared_by,
            }
        )

    total = sum(row["schema_bytes"] for row in rows)
    rows.sort(
        key=lambda row: (
            not row["offered_on_skill_less_turns"],
            -row["reach"],
            -row["schema_bytes"],
        )
    )
    for row in rows:
        row["share"] = round(row["schema_bytes"] / total, 4) if total else 0.0

    by_tier = {
        tier: {
            "tools": sum(1 for row in rows if row["tier"] == tier),
            "bytes": sum(row["schema_bytes"] for row in rows if row["tier"] == tier),
        }
        for tier in _TIERS
    }

    # The tiers projection can never withhold, so this is what an ENFORCED,
    # skill-less assistant request carrying this pinned inventory proposes.
    # Deliberately not `scaffold`, which needs an active skill.
    #
    # Reported as the SERIALIZED subset, not a sum of member schemas: the array
    # carries brackets and separators, and `tools_bytes` — which every other
    # measurement in this repository uses — counts them. A member sum here would
    # be a second number for the same thing, smaller by exactly the punctuation,
    # and someone would eventually compare the two.
    by_name = {(e.get("function") or {}).get("name", ""): e for e in inventory}
    floor_entries = [
        by_name[row["name"]] for row in rows if row["offered_on_skill_less_turns"]
    ]
    floor_bytes = tools_bytes(floor_entries)
    array_bytes = tools_bytes(inventory)

    return {
        "meta": {"benchmark": "tool_schemas", "mode": "report", "inventory": len(rows)},
        "summary": {
            "total_bytes": total,
            "array_bytes": array_bytes,
            "skill_less_floor_bytes": floor_bytes,
            "skill_less_floor_share": (
                round(floor_bytes / array_bytes, 4) if array_bytes else 0.0
            ),
            "description_bytes": sum(row["description_bytes"] for row in rows),
            "prose_bytes": sum(row["prose_bytes"] for row in rows),
            "parameter_bytes": sum(row["parameter_bytes"] for row in rows),
            "by_tier": by_tier,
        },
        "tools": rows,
    }


def print_report(doc: dict[str, Any], *, limit: int = 20) -> None:
    summary = doc["summary"]
    print("\n== tool schema cost (pinned CLI inventory) ==")
    print(
        f"array: {summary['array_bytes'] / 1024:.1f} KiB across "
        f"{doc['meta']['inventory']} tools  "
        f"(prose {summary['prose_bytes'] / 1024:.1f} KiB across all description "
        f"fields, of which {summary['description_bytes'] / 1024:.1f} KiB is the "
        f"top-level one)"
    )
    print(
        f"skill-less enforced floor: {summary['skill_less_floor_bytes'] / 1024:.1f} KiB "
        f"({summary['skill_less_floor_share']:.1%}) — what an enforced assistant turn "
        "with no active skill proposes from this inventory"
    )
    for tier, stats in summary["by_tier"].items():
        print(f"  {tier:11} {stats['tools']:>3} tools  {stats['bytes'] / 1024:>6.1f} KiB")

    print(f"\naudit order (reach, then size) — top {limit}:")
    print(
        f"  {'tool':<38} {'tier':<11} {'reach':>7} {'schema':>7} {'prose':>7}"
    )
    for row in doc["tools"][:limit]:
        reach = "every" if row["offered_on_skill_less_turns"] else str(row["reach"])
        print(
            f"  {row['name']:<38} {row['tier']:<11} {reach:>7} "
            f"{row['schema_bytes']:>7} {row['prose_bytes']:>7}"
        )
