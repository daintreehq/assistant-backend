"""The caller's host model and coarse host state, and which host runbooks they fit.

Daintree describes its caller on the runbooks connection it sets up at launch, in
request headers sent on every POST (``docs/MCP.md``):

- ``Daintree-Host-Control``: ``1`` from a build that drives its Remote Hosts
  directly (``host.call``) and delegates to a host's assistant as it would to any
  agent. Its catalog is the CONTROL catalog. Absent, as from v0.44, or anything
  but ``1``: the INBOX catalog, the cross-host messaging model v0.44 shipped
  (``catalog.load_catalogs``). This one fails closed: an unknown value must never
  show a v0.44 client tools it does not have.
- ``Daintree-Hosts``: ``none`` (no Remote Host configured), ``configured`` (at
  least one, but none connected with control on, or with messaging on for an
  inbox caller) or ``connected`` (at least one connected with it on).
- ``Daintree-Lane``, inbox callers only: ``reachable`` when another machine's
  assistant can message this caller, which is how a v0.44 Remote Host's own
  assistant gets its work. Absent means not reachable. A control caller has no
  lanes, so the header is not read for one.

Only that coarse state leaves the machine: no host names, counts or ids. A hosts
snapshot that is missing or malformed is UNREPORTED, and unreported filters
nothing, so such a caller searches its whole catalog. Nothing is kept between
requests; every request carries its own.

Each ``daintree.hosts.*`` runbook declares the state it fits as
``hostEligibility`` (``models.RunbookMeta``), one of ``HOST_ELIGIBILITY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

HostModel = Literal["inbox", "control"]
HostsValue = Literal["none", "configured", "connected"]
HostEligibility = Literal["any", "configured", "connected", "lane"]

_HOSTS: dict[str, HostsValue] = {value: value for value in get_args(HostsValue)}
HOST_ELIGIBILITY: frozenset[str] = frozenset(get_args(HostEligibility))

#: The namespace whose runbooks must declare ``hostEligibility``, and the only
#: one that may.
HOSTS_NAMESPACE = "daintree.hosts."

HOST_CONTROL_HEADER = "daintree-host-control"
#: The one value naming the host-control model. Daintree sends it as a version so
#: a later model can be told apart; until there is one, nothing else is control.
HOST_CONTROL_VERSION = "1"
HOSTS_HEADER = "daintree-hosts"
LANE_HEADER = "daintree-lane"
LANE_REACHABLE = "reachable"


@dataclass(frozen=True)
class HostState:
    """A reported snapshot. ``None`` in its place means unreported."""

    hosts: HostsValue
    lane_reachable: bool = False


@dataclass(frozen=True)
class CallerState:
    """Which catalog a request is served from, and the host state that narrows its
    search (``None``: unreported)."""

    model: HostModel
    hosts: HostState | None = None


def parse_host_model(values: list[str]) -> HostModel:
    """The model the ``Daintree-Host-Control`` header values name.

    ``control`` only for exactly one header carrying exactly ``1``. Absent,
    repeated, empty or anything else is ``inbox``: a v0.44 client sends none, and
    a value this server does not know is not evidence of a client that has
    ``host.call``.
    """
    # HTTP's optional whitespace around a value is not part of it; nothing else is
    # forgiven (a no-break space is an unknown value, not a padded "1").
    if len(values) == 1 and values[0].strip(" \t") == HOST_CONTROL_VERSION:
        return "control"
    return "inbox"


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


def parse_caller_state(host_control: list[str], hosts: str | None, lane: str | None) -> CallerState:
    """The caller a request's headers describe. A control caller's lane header is
    ignored rather than parsed: lanes are the inbox model's, and a stray one must
    not make its hosts snapshot unreported."""
    model = parse_host_model(host_control)
    if model == "control":
        return CallerState(model=model, hosts=parse_host_state(hosts, None))
    return CallerState(model=model, hosts=parse_host_state(hosts, lane))


def fits(eligibility: HostEligibility, state: HostState) -> bool:
    """Whether a runbook declaring ``eligibility`` may be offered in ``state``.

    ``configured`` runbooks stay offered while every host is down, because they
    are the ones wanted right after a host drops: recovering, parking, surveying,
    diagnosing. A lane's runbooks (inbox catalog only) follow whether it can be
    messaged, not whether it has hosts of its own.
    """
    if eligibility == "any":
        return True
    if eligibility == "lane":
        return state.lane_reachable
    if eligibility == "configured":
        return state.hosts != "none"
    return state.hosts == "connected"
