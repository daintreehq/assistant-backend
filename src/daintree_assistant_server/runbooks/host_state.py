"""Coarse Remote Host state sent by Daintree on every runbook request.

``Daintree-Hosts`` carries no names, ids or counts. Missing, repeated or unknown
values are unreported and do not narrow search. Exact retrieval is unaffected.
The retired inbox model and its lane header no longer select a catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

HostsValue = Literal["none", "configured", "connected"]
HostEligibility = Literal["any", "configured", "connected"]

_HOSTS: dict[str, HostsValue] = {value: value for value in get_args(HostsValue)}
HOST_ELIGIBILITY: frozenset[str] = frozenset(get_args(HostEligibility))
HOSTS_NAMESPACE = "daintree.hosts."
HOSTS_HEADER = "daintree-hosts"


@dataclass(frozen=True)
class HostState:
    """A reported snapshot. ``None`` in its place means unreported."""

    hosts: HostsValue


def parse_host_state(hosts: str | None) -> HostState | None:
    value = _HOSTS.get(hosts.strip().lower()) if hosts is not None else None
    return HostState(hosts=value) if value is not None else None


def fits(eligibility: HostEligibility, state: HostState) -> bool:
    if eligibility == "any":
        return True
    if eligibility == "configured":
        return state.hosts != "none"
    return state.hosts == "connected"
