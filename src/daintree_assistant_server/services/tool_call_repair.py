"""Schema-guided repair of model-emitted tool-call arguments.

The upstream model occasionally emits arguments that are a *near miss* against
the client-declared tool schema. The canonical case: most ``terminal.*`` tools
take a single ``terminalId`` string (and echo that field back in results), but
the batch tools (``terminal.extract``, ``terminal.extract.json``,
``terminal.awaitAll``) declare ``terminalIds`` — an array. The model
pattern-matches the sibling convention, the CLI decodes strictly (unknown
fields are rejected), and the near miss costs a full failed round-trip before
the model self-corrects.

This module fixes the unambiguous cases server-side, before the tool call is
emitted to the client:

- **near-miss key rename** — an argument key absent from the schema is renamed
  onto a declared property when exactly one property matches after
  normalization (case/punctuation-insensitive, singular/plural tolerant) and
  that property is not already set;
- **scalar <-> array coercion** — a value is wrapped into a one-element array
  when the declared type is ``array`` and the value matches the declared item
  type, and a one-element array is unwrapped when the declared type is scalar.

Anything ambiguous is left untouched: the CLI's strict decode plus the model's
own self-correction loop remain the backstop. Every entry point fails open —
on any unexpected shape the original arguments pass through verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.openai import ToolDef
from ..observability import get_logger

logger = get_logger("daintree.tool_call_repair")

ParameterIndex = Mapping[str, Mapping[str, Any]]


def build_parameter_index(tools: Sequence[ToolDef] | None) -> dict[str, dict[str, Any]]:
    """Map tool name -> object-shaped ``parameters`` schema.

    Tools without a usable object schema (no ``properties``) are omitted —
    their calls pass through unrepaired.
    """
    index: dict[str, dict[str, Any]] = {}
    for tool in tools or ():
        params = tool.function.parameters
        if not isinstance(params, dict) or params.get("type") != "object":
            continue
        properties = params.get("properties")
        if isinstance(properties, dict) and properties:
            index[tool.function.name] = params
    return index


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def _matches_type(value: Any, schema_type: Any) -> bool:
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "object":
        return isinstance(value, dict)
    return False


_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


def _rename_target(key: str, properties: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    """The single declared property ``key`` unambiguously maps onto, or None."""
    norm = _normalize_key(key)
    if not norm:
        return None
    candidates = []
    for prop in properties:
        if prop in args:
            continue
        prop_norm = _normalize_key(prop)
        if prop_norm == norm or prop_norm == norm + "s" or prop_norm + "s" == norm:
            candidates.append(prop)
    return candidates[0] if len(candidates) == 1 else None


def _value_satisfiable(value: Any, prop: Any) -> bool:
    """True when ``value`` fits the property's declared type, either directly
    or via the scalar<->array coercion rules below — a rename that would still
    fail the schema is worse than leaving the original field for the model's
    own retry (the error then names the field the model actually sent)."""
    if not isinstance(prop, dict):
        return True
    if "type" not in prop:
        return True  # genuinely unconstrained — any value fits
    declared = prop.get("type")
    if not isinstance(declared, str):
        # A union or otherwise non-scalar declaration (`"type": ["array", "null"]`).
        # We cannot cheaply prove the value fits, and a rename that still fails the
        # schema is worse than leaving the model's own field name in the error. Refuse.
        return False
    if declared == "array":
        if isinstance(value, list):
            return True
        items = prop.get("items")
        item_type = items.get("type") if isinstance(items, dict) else None
        return value is not None and (item_type is None or _matches_type(value, item_type))
    if _matches_type(value, declared):
        return True
    return (
        declared in _SCALAR_TYPES
        and isinstance(value, list)
        and len(value) == 1
        and _matches_type(value[0], declared)
    )


def repair_arguments(
    schema: Mapping[str, Any], args: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Apply the conservative repair rules to parsed arguments.

    Returns the (possibly new) arguments dict plus human-readable notes for
    each repair applied; an empty notes list means the input was returned
    untouched.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return args, []
    notes: list[str] = []
    repaired = dict(args)

    for key in list(repaired):
        if key in properties:
            continue
        target = _rename_target(key, properties, repaired)
        if target is not None and _value_satisfiable(repaired[key], properties.get(target)):
            repaired[target] = repaired.pop(key)
            notes.append(f'renamed "{key}" -> "{target}"')

    for key, value in list(repaired.items()):
        prop = properties.get(key)
        if not isinstance(prop, dict):
            continue
        declared = prop.get("type")
        # A union type (`"type": ["string", "null"]`) is a list, and `list in frozenset`
        # raises TypeError: unhashable type. Without this guard one union-typed property
        # aborts the whole call's repair — including a correct rename already applied in
        # the loop above. Mirrors the same guard in `_value_satisfiable`.
        if not isinstance(declared, str):
            continue
        if declared == "array" and value is not None and not isinstance(value, list):
            items = prop.get("items")
            item_type = items.get("type") if isinstance(items, dict) else None
            if item_type is None or _matches_type(value, item_type):
                repaired[key] = [value]
                notes.append(f'wrapped "{key}" into a one-element array')
        elif (
            declared in _SCALAR_TYPES
            and isinstance(value, list)
            and len(value) == 1
            and _matches_type(value[0], declared)
        ):
            repaired[key] = value[0]
            notes.append(f'unwrapped one-element array "{key}"')

    if not notes:
        return args, []
    return repaired, notes


def repair_tool_call_arguments(
    name: str, arguments: str, index: ParameterIndex
) -> tuple[str, list[str]]:
    """Repair one call's JSON-encoded arguments string. Fails open: any parse
    failure, unknown tool, or unexpected shape returns the input verbatim."""
    try:
        schema = index.get(name)
        if not schema or not arguments or not arguments.strip():
            return arguments, []
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            return arguments, []
        repaired, notes = repair_arguments(schema, parsed)
        if not notes:
            return arguments, []
        return json.dumps(repaired, separators=(",", ":"), ensure_ascii=False), notes
    except Exception as exc:  # noqa: BLE001 — repair must never break the turn
        logger.warning("tool_call_repair_failed", tool=name, error=type(exc).__name__)
        return arguments, []


class StreamToolCallBuffer:
    """Buffers streaming tool-call delta fragments so each call's arguments can
    be repaired as a whole before being forwarded to the client.

    Upstream streams a tool call as fragments keyed by ``index`` (id/type/name
    first, then argument pieces), and MAY interleave fragments of several
    calls, so nothing is forwarded until the stream ends: the client only
    dispatches tools once the round is complete, so holding the calls costs no
    real latency. ``flush`` returns one consolidated fragment per call — a
    single fragment carrying the whole call is a valid chunking of the same
    wire shape, so the client accumulator is unaffected.

    Fail-open: a fragment without a usable integer ``index`` switches the
    buffer to passthrough — everything pending flushes in order, and that
    fragment plus everything after it is returned verbatim.
    """

    def __init__(self, index: ParameterIndex) -> None:
        self._index = index
        self._pending: dict[int, dict[str, Any]] = {}
        self._passthrough = False
        # (tool name, notes) per repaired call, for the caller to log.
        self.repairs: list[tuple[str, list[str]]] = []
        # Every call name this buffer consolidated, in flush order. The name is
        # ACCUMULATED across fragments (``add`` appends), so a caller reading
        # names off raw fragments can see "terminal" and "__rename" as two
        # separate calls; only the consolidated value is a real tool name.
        # Discarded with the buffer when an attempt is re-rolled, which is what
        # keeps a rejected attempt's calls out of the caller's accounting.
        self.called_names: list[str] = []

    def add(self, fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Absorb one delta's fragments; return anything to forward now."""
        ready: list[dict[str, Any]] = []
        for fragment in fragments:
            if self._passthrough:
                ready.append(fragment)
                continue
            if not isinstance(fragment, dict):
                ready.extend(self._enter_passthrough(fragment))
                continue
            idx = fragment.get("index")
            if not isinstance(idx, int) or isinstance(idx, bool):
                ready.extend(self._enter_passthrough(fragment))
                continue
            entry = self._pending.setdefault(
                idx, {"id": "", "type": "", "name": "", "arguments": []}
            )
            if isinstance(fragment.get("id"), str) and fragment["id"]:
                entry["id"] = entry["id"] or fragment["id"]
            if isinstance(fragment.get("type"), str) and fragment["type"]:
                entry["type"] = entry["type"] or fragment["type"]
            function = fragment.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    entry["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    entry["arguments"].append(function["arguments"])
        return ready

    def flush(self) -> list[dict[str, Any]]:
        """Complete and return every pending call (stream end)."""
        out: list[dict[str, Any]] = []
        for idx in sorted(self._pending):
            out.append(self._consolidate(idx, self._pending[idx]))
        self._pending.clear()
        return out

    @property
    def names_complete(self) -> bool:
        """Whether ``called_names`` is the WHOLE set of calls this round made.

        False once the buffer has entered passthrough: from that point fragments
        are forwarded verbatim and never consolidated, so their names are not
        recorded. A caller measuring "did the model call anything it should not
        have" must treat that as UNKNOWN — an unmeasured call reported as zero is
        a false all-clear, and the whole point of the measurement is to decide
        when it is safe to start withholding tools.
        """
        return not self._passthrough

    def _enter_passthrough(self, fragment: Any) -> list[dict[str, Any]]:
        self._passthrough = True
        flushed = self.flush()
        flushed.append(fragment)
        return flushed

    def _consolidate(self, idx: int, entry: dict[str, Any]) -> dict[str, Any]:
        arguments = "".join(entry["arguments"])
        repaired, notes = repair_tool_call_arguments(entry["name"], arguments, self._index)
        if notes:
            self.repairs.append((entry["name"], notes))
        if entry["name"]:
            self.called_names.append(entry["name"])
        return {
            "index": idx,
            "id": entry["id"],
            "type": entry["type"] or "function",
            "function": {"name": entry["name"], "arguments": repaired},
        }
