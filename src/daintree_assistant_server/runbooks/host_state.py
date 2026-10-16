"""The caller's coarse host state, and which host runbooks it lets search offer.

Daintree reports the state on the runbooks connection it sets up at launch, as
two request headers sent on every POST (``docs/MCP.md``):

- ``Daintree-Hosts``: ``none`` (no Remote Host configured), ``configured`` (at
  least one, but none connected with messaging on) or ``connected`` (at least one
  connected with messaging on).
- ``Daintree-Lane``: ``reachable`` when another machine's assistant can message
  this caller, which is how a Remote Host's own assistant gets its work. Absent
  means not reachable.

Only that coarse state leaves the machine: no host names, counts or ids. A
snapshot that is missing or malformed is UNREPORTED, and unreported filters
nothing, so an older Daintree or any other caller searches the whole catalog as
before. Nothing is kept between requests; every request carries its own.

Each ``daintree.hosts.*`` runbook declares the state it fits as
``hostEligibility`` (``models.RunbookMeta``), one of ``HOST_ELIGIBILITY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

HostsValue = Literal["none", "configured", "connected"]
HostEligibility = Literal["any", "configured", "connected", "lane"]

_HOSTS: dict[str, HostsValue] = {value: value for value in get_args(HostsValue)}
HOST_ELIGIBILITY: frozenset[str] = frozenset(get_args(HostEligibility))

#: The namespace whose runbooks must declare ``hostEligibility``, and the only
#: one that may.
HOSTS_NAMESPACE = "daintree.hosts."

HOSTS_HEADER = "daintree-hosts"
LANE_HEADER = "daintree-lane"
LANE_REACHABLE = "reachable"


@dataclass(frozen=True)
class HostState:
    """A reported snapshot. ``None`` in its place means unreported."""

    hosts: HostsValue
    lane_reachable: bool = False


def parse_host_state(hosts: str | None, lane: str | None) -> HostState | None:
    """The snapshot two header values describe, or ``None`` when it is unreported.

    Fails open, never with an error: a missing or unknown ``Daintree-Hosts``, or a
    ``Daintree-Lane`` carrying anything but ``reachable``, makes the whole snapshot
    unreported rather than reading an unknown value as a definite "no".
    """
    value = _HOSTS.get(hosts.strip().lower()) if hosts is not None else None
    if value is None:
        return None
    if lane is None:
        return HostState(hosts=value)
    if lane.strip().lower() != LANE_REACHABLE:
        return None
    return HostState(hosts=value, lane_reachable=True)


def fits(eligibility: HostEligibility, state: HostState) -> bool:
    """Whether a runbook declaring ``eligibility`` may be offered in ``state``.

    ``configured`` runbooks stay offered while every host is down, because they
    are the ones wanted right after a host drops: recovering, parking, surveying,
    diagnosing. A lane's runbooks follow whether it can be messaged, not whether it
    has hosts of its own.
    """
    if eligibility == "any":
        return True
    if eligibility == "lane":
        return state.lane_reachable
    if eligibility == "configured":
        return state.hosts != "none"
    return state.hosts == "connected"
