"""Case loading/validation for the safety gauntlet's YAML corpora.

Two corpora are data (``cases/*.yaml``), validated at load so a typo fails fast
instead of silently scoring:

- **wire validation** (``guard: wire_validation``) — each case carries a
  ``request`` payload for ``RespondRequest.model_validate`` plus ``expect:
  reject|pass``. The envelope boilerplate (``protocol_version``/``session``) is
  default-filled so cases stay readable; a case may override either.
- **result_schema** (``guard: result_schema``) — ``mode: validate`` cases carry
  a ``schema`` (or a settings-sized ``schema_synth``) plus ``expect:
  reject|pass`` and an optional ``injection: true`` marker (the zero-tolerance
  subset: an instruction-bearing key that MUST NOT be accepted); ``mode:
  conforms`` cases carry ``schema`` + ``value`` + ``expect: conform|violate``,
  and the schema itself must pass the real validator at load (production only
  ever calls ``conforms`` on validated schemas).

The four ``schema_synth`` kinds exist because their payloads are functions of
the live ``Settings`` caps (and far too large/deep to write out as YAML).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.result_schema import validate_result_schema

from .._harness.cases import CaseError, load_yaml_cases

CASES_DIR = Path(__file__).resolve().parent / "cases"

WIRE_EXPECTS = frozenset({"reject", "pass"})
VALIDATE_EXPECTS = frozenset({"reject", "pass"})
CONFORMS_EXPECTS = frozenset({"conform", "violate"})
SYNTH_KINDS = frozenset({"over_depth", "over_size", "over_enum", "over_properties"})

_DEFAULT_ENVELOPE: dict[str, Any] = {
    "protocol_version": 1,
    "session": {"id": "bench-session", "turn_id": "turn-1"},
}


def synth_schema(kind: str, settings: Settings) -> dict[str, Any]:
    """Build the settings-sized oversize/over-depth schemas that cannot live in YAML."""
    if kind == "over_depth":
        node: dict[str, Any] = {"type": "string"}
        for _ in range(settings.max_result_schema_depth + 2):
            node = {"type": "array", "items": node}
        return node
    if kind == "over_size":
        return {"type": "string", "const": "x" * (settings.max_result_schema_bytes + 128)}
    if kind == "over_enum":
        return {
            "type": "string",
            "enum": [f"v{i}" for i in range(settings.max_result_schema_enum + 1)],
        }
    if kind == "over_properties":
        return {
            "type": "object",
            "properties": {
                f"p{i}": {"type": "string"}
                for i in range(settings.max_result_schema_properties + 1)
            },
        }
    raise CaseError(f"unknown schema_synth kind {kind!r}")


def load_wire_cases(cases_dir: Path = CASES_DIR) -> list[dict[str, Any]]:
    raw = [c for c in load_yaml_cases(cases_dir) if c.get("guard") == "wire_validation"]
    if not raw:
        raise CaseError("no wire_validation cases found")
    for case in raw:
        cid, src = case["id"], case["_source"]
        if case.get("expect") not in WIRE_EXPECTS:
            raise CaseError(f"{src}/{cid}: expect must be one of {sorted(WIRE_EXPECTS)}")
        request = case.get("request")
        if not isinstance(request, dict) or "input" not in request:
            raise CaseError(f"{src}/{cid}: 'request' must be a mapping with an 'input' block")
        merged = copy.deepcopy(_DEFAULT_ENVELOPE)
        merged.update(copy.deepcopy(request))
        case["request"] = merged
    return raw


def load_result_schema_cases(
    settings: Settings, cases_dir: Path = CASES_DIR
) -> list[dict[str, Any]]:
    raw = [c for c in load_yaml_cases(cases_dir) if c.get("guard") == "result_schema"]
    if not raw:
        raise CaseError("no result_schema cases found")
    for case in raw:
        cid, src = case["id"], case["_source"]
        mode = case.get("mode")
        if mode == "validate":
            _load_validate_case(case, cid, src, settings)
        elif mode == "conforms":
            _load_conforms_case(case, cid, src, settings)
        else:
            raise CaseError(f"{src}/{cid}: mode must be 'validate' or 'conforms'")
    return raw


def _load_validate_case(case: dict[str, Any], cid: str, src: str, settings: Settings) -> None:
    if case.get("expect") not in VALIDATE_EXPECTS:
        raise CaseError(f"{src}/{cid}: expect must be one of {sorted(VALIDATE_EXPECTS)}")
    synth = case.get("schema_synth")
    if synth is not None:
        if synth not in SYNTH_KINDS:
            raise CaseError(f"{src}/{cid}: schema_synth must be one of {sorted(SYNTH_KINDS)}")
        if "schema" in case:
            raise CaseError(f"{src}/{cid}: give 'schema' or 'schema_synth', not both")
        case["schema"] = synth_schema(synth, settings)
    elif not isinstance(case.get("schema"), dict):
        raise CaseError(f"{src}/{cid}: 'schema' must be a mapping (or use schema_synth)")
    if case.get("injection") and case["expect"] != "reject":
        raise CaseError(f"{src}/{cid}: injection: true only makes sense on expect: reject")


def _load_conforms_case(case: dict[str, Any], cid: str, src: str, settings: Settings) -> None:
    if case.get("expect") not in CONFORMS_EXPECTS:
        raise CaseError(f"{src}/{cid}: expect must be one of {sorted(CONFORMS_EXPECTS)}")
    if not isinstance(case.get("schema"), dict):
        raise CaseError(f"{src}/{cid}: 'schema' must be a mapping")
    if "value" not in case:
        raise CaseError(f"{src}/{cid}: conforms case needs a 'value' key (null is allowed)")
    # Production only calls conforms() on an already-validated schema; enforce
    # the same precondition here (on a copy — the validator strips noise keys
    # in place) so a conforms case cannot quietly test an impossible schema.
    try:
        validate_result_schema(copy.deepcopy(case["schema"]), settings)
    except DaintreeError as exc:
        raise CaseError(
            f"{src}/{cid}: conforms schema would be rejected by validate_result_schema: "
            f"{exc.message}"
        ) from exc
