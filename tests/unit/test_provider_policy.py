"""Endpoint-selection policy: the privacy floor, and what a caller may change.

The single property worth defending here is that **no caller-controlled input can
weaken the privacy floor**. Everything a client may send narrows or re-ranks
routing; `data_collection: "deny"` is not derived from any of it.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.contracts.daintree_api import Routing
from daintree_assistant_server.providers.policy import (
    DEFAULT_POLICY,
    DENIED_ENDPOINTS,
    LATENCY_BOUND,
    EndpointSort,
    PrivacyMode,
    RoutingPolicy,
    RoutingPreference,
    routing_preference,
)

# ---------------------------------------------------------------------------
# The shipped default.
# ---------------------------------------------------------------------------


def test_the_default_policy_is_no_training_throughput_first() -> None:
    assert DEFAULT_POLICY.privacy is PrivacyMode.NO_TRAINING
    assert DEFAULT_POLICY.sort is EndpointSort.THROUGHPUT
    assert DEFAULT_POLICY.require_parameters is True
    assert DEFAULT_POLICY.allow_fallbacks is True


def test_the_default_wire_block_is_exactly_what_we_document() -> None:
    assert DEFAULT_POLICY.to_wire() == {
        "sort": "throughput",
        "data_collection": "deny",
        "require_parameters": True,
        "allow_fallbacks": True,
        "ignore": ["digitalocean"],
        "preferred_max_latency": {"p50": 1.0, "p90": 2.0, "p99": 5.0},
    }


def test_the_default_does_not_send_zdr() -> None:
    """The posture decision. ZDR narrows the pool substantially and has not been
    live-tested against every request shape, so it ships off — and every piece of
    user-facing copy is written to match."""
    assert "zdr" not in DEFAULT_POLICY.to_wire()


def test_zdr_is_additive_not_a_replacement() -> None:
    """ZDR is the retention filter; data_collection is the training filter.
    Sending only `zdr` would quietly drop the no-training floor."""
    wire = RoutingPolicy(privacy=PrivacyMode.ZDR).to_wire()
    assert wire["zdr"] is True
    assert wire["data_collection"] == "deny"


# ---------------------------------------------------------------------------
# The floor holds against every caller input.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "preference",
    [
        RoutingPreference(privacy=PrivacyMode.NO_TRAINING),
        RoutingPreference(privacy=PrivacyMode.ZDR),
        RoutingPreference(sort=EndpointSort.PRICE),
        RoutingPreference(sort=EndpointSort.LATENCY),
        RoutingPreference(only=("deepinfra",)),
        RoutingPreference(ignore=("wafer",)),
        RoutingPreference(
            privacy=PrivacyMode.ZDR,
            sort=EndpointSort.PRICE,
            only=("a", "b"),
            ignore=("c",),
        ),
    ],
)
def test_no_caller_preference_can_weaken_the_privacy_floor(
    preference: RoutingPreference,
) -> None:
    wire = DEFAULT_POLICY.merged_with(preference).to_wire()
    assert wire["data_collection"] == "deny"


def test_a_preference_re_ranks_and_narrows_only() -> None:
    wire = DEFAULT_POLICY.merged_with(
        RoutingPreference(
            privacy=PrivacyMode.ZDR, sort=EndpointSort.PRICE, only=("deepinfra",)
        )
    ).to_wire()
    assert wire["sort"] == "price"
    assert wire["zdr"] is True
    assert wire["only"] == ["deepinfra"]
    # Untouched server decisions.
    assert wire["require_parameters"] is True
    assert wire["allow_fallbacks"] is True


def test_merging_none_is_the_identity() -> None:
    assert DEFAULT_POLICY.merged_with(None) is DEFAULT_POLICY


def test_merging_does_not_mutate_the_shared_default() -> None:
    """The default is a module-level singleton shared by every request. If a merge
    edited it in place, one caller's ZDR choice would leak into everyone else's
    routing for the life of the process."""
    before = DEFAULT_POLICY.to_wire()
    DEFAULT_POLICY.merged_with(RoutingPreference(privacy=PrivacyMode.ZDR))
    assert DEFAULT_POLICY.to_wire() == before


# ---------------------------------------------------------------------------
# The first-byte bound, and the one axis that varies.
# ---------------------------------------------------------------------------


def test_every_call_is_bounded_on_first_byte_by_default() -> None:
    """The bound is on the DEFAULT policy, not bolted on by a derivation.

    This is the fix for the failure that motivated it: the bound used to be added
    by `for_structured_call()`, so the main chat call — the only upstream call a
    short question makes — was the one call in the system with no first-byte
    protection at all, elected purely on tokens/second. A default cannot be
    forgotten by a code path the way a derivation can.
    """
    assert DEFAULT_POLICY.preferred_max_latency == LATENCY_BOUND
    assert DEFAULT_POLICY.to_wire()["preferred_max_latency"] == LATENCY_BOUND
    # And on a bare policy too, so a future construction site inherits it.
    assert RoutingPolicy().to_wire()["preferred_max_latency"] == LATENCY_BOUND


def test_the_bound_constrains_the_typical_case_and_not_only_the_tail() -> None:
    """p50 is the load-bearing percentile, and the reason is worth pinning.

    p90/p99 alone describe an endpoint's bad days. An endpoint whose TYPICAL first
    byte is slow has an unremarkable tail and would still win the election — which
    is exactly the "it takes a while to start" symptom this exists to remove.
    """
    assert "p50" in LATENCY_BOUND
    assert LATENCY_BOUND["p50"] <= LATENCY_BOUND["p90"] <= LATENCY_BOUND["p99"]


def test_first_byte_ranking_changes_the_axis_and_nothing_else() -> None:
    """The whole of the per-call variation, asserted as "nothing else moved".

    One policy serves every call; `ranked_on_first_byte` flips `sort` for the one
    call site whose shape is known to be prefill-dominated. If a future edit makes
    it change the pool — a filter, an `only`, a different bound — the design has
    quietly reverted to per-call-shape policies and this fails.
    """
    base = DEFAULT_POLICY.to_wire()
    ranked = DEFAULT_POLICY.ranked_on_first_byte().to_wire()
    assert ranked["sort"] == "latency"
    assert base["sort"] == "throughput"
    assert {k: v for k, v in ranked.items() if k != "sort"} == {
        k: v for k, v in base.items() if k != "sort"
    }


def test_a_caller_sort_preference_still_wins_over_the_first_byte_axis() -> None:
    """`ranked_on_first_byte` REPLACES the sort key, so it is applied BEFORE a
    caller preference and never after. A caller who paid for `price` and silently
    got `latency` would be a preference the server only pretends to honour."""
    policy = DEFAULT_POLICY.ranked_on_first_byte().merged_with(
        RoutingPreference(sort=EndpointSort.PRICE, privacy=PrivacyMode.ZDR)
    )
    wire = policy.to_wire()
    assert wire["sort"] == "price"
    # ...while the parts that are not the caller's to trade survive the merge.
    assert wire["preferred_max_latency"] == LATENCY_BOUND
    assert wire["zdr"] is True
    assert wire["data_collection"] == "deny"


def test_deriving_a_policy_does_not_mutate_the_shared_default() -> None:
    before = DEFAULT_POLICY.to_wire()
    DEFAULT_POLICY.ranked_on_first_byte()
    assert DEFAULT_POLICY.to_wire() == before
    # And each policy owns its bound dict rather than aliasing the constant, which
    # a later in-place edit would otherwise corrupt for the whole process.
    assert RoutingPolicy().preferred_max_latency is not LATENCY_BOUND
    assert RoutingPolicy().preferred_max_latency is not RoutingPolicy().preferred_max_latency


def test_the_bound_survives_a_caller_preference() -> None:
    policy = DEFAULT_POLICY.merged_with(RoutingPreference(sort=EndpointSort.PRICE))
    assert policy.to_wire()["preferred_max_latency"] == LATENCY_BOUND
    assert policy.to_wire()["sort"] == "price"


def test_the_wire_block_key_order_is_stable() -> None:
    """This dict is serialized into every request body, and the prompt cache keys
    on a byte-identical prefix. Wobbling key order would be a silent cache miss on
    otherwise identical turns."""
    a = list(DEFAULT_POLICY.to_wire())
    b = list(RoutingPolicy().to_wire())
    assert (
        a
        == b
        == [
            "sort",
            "data_collection",
            "require_parameters",
            "allow_fallbacks",
            "ignore",
            "preferred_max_latency",
        ]
    )


# ---------------------------------------------------------------------------
# Wire -> typed conversion.
# ---------------------------------------------------------------------------


def test_an_absent_block_is_none() -> None:
    assert routing_preference(None) is None


def test_an_empty_block_is_none_so_the_fast_path_is_taken() -> None:
    """A client sending `routing: {}` must not force a re-render of a
    byte-identical policy — that would cost a prompt-cache miss for nothing."""
    assert routing_preference(Routing()) is None


def test_a_populated_block_converts_to_typed_values() -> None:
    pref = routing_preference(
        Routing(privacy="zdr", sort="price", only=["deepinfra"], ignore=["wafer"])
    )
    assert pref is not None
    assert pref.privacy is PrivacyMode.ZDR
    assert pref.sort is EndpointSort.PRICE
    assert pref.only == ("deepinfra",)
    assert pref.ignore == ("wafer",)


@pytest.mark.parametrize("bad", ["BAD SLUG", "Upper", "has space", "x" * 65, ""])
def test_invalid_endpoint_slugs_are_rejected_at_the_boundary(bad: str) -> None:
    """A slug is forwarded upstream verbatim, so it is validated as input rather
    than discovered as an inscrutable provider error."""
    with pytest.raises(ValueError):
        Routing(only=[bad])


def test_unknown_privacy_or_sort_values_are_rejected() -> None:
    """The closed set is the point: a pass-through would let a client disable the
    privacy floor or pin an endpoint that cannot do `response_format`."""
    with pytest.raises(ValueError):
        Routing(privacy="none")
    with pytest.raises(ValueError):
        Routing(sort="cheapest")


def test_the_routing_block_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        Routing(data_collection="allow")


def test_endpoint_lists_are_bounded() -> None:
    with pytest.raises(ValueError):
        Routing(only=[f"p{i}" for i in range(25)])


# ---------------------------------------------------------------------------
# Propagation.
#
# A privacy choice is only worth as much as its weakest call. Every upstream call
# a turn makes ships the caller's content, so a path that drops the preference
# honours the request exactly where the user can see it and silently downgrades
# everywhere else.
# ---------------------------------------------------------------------------


def test_the_selector_call_carries_the_callers_routing() -> None:
    """The selector sends the conversation upstream just like the main call."""
    import inspect

    from daintree_assistant_server.skills.selector import Selector

    build = inspect.getsource(Selector._build_request)
    assert "routing=routing" in build, (
        "the selector no longer forwards the caller's routing preference. A ZDR "
        "turn would route its conversation to a non-ZDR endpoint here."
    )


def test_the_task_runner_carries_the_callers_routing() -> None:
    """Utility tasks ship terminal tails, transcripts and memories upstream."""
    import inspect

    from daintree_assistant_server.services.task_runner import TaskRunner

    source = inspect.getsource(TaskRunner)
    assert source.count("routing=routing") >= 2, (
        "a task provider request (first attempt or repair) no longer forwards the "
        "caller's routing preference"
    )


def test_the_assembler_carries_the_callers_routing() -> None:
    import inspect

    from daintree_assistant_server.services.prompt_assembler import PromptAssembler

    build = inspect.getsource(PromptAssembler.build_main_request)
    assert "routing=routing_preference(request.routing)" in build


def test_the_forced_tool_call_subrequest_carries_routing() -> None:
    """`structured_tool_call` rebuilds its request field by field, which is
    exactly how the api_key forwarding bug was almost made — same shape, same
    trap."""
    import inspect

    from daintree_assistant_server.providers.openrouter import OpenRouterProvider

    source = inspect.getsource(OpenRouterProvider.structured_tool_call)
    assert "routing=request.routing" in source


# ---------------------------------------------------------------------------
# The output-quality denylist.
# ---------------------------------------------------------------------------


def test_the_default_excludes_the_denied_endpoints() -> None:
    """The quality floor ships on, like the privacy floor.

    Entries are named hosts backed by measured evidence (see DENIED_ENDPOINTS);
    this asserts only that whatever is on the list actually reaches the wire.
    """
    assert DEFAULT_POLICY.ignore == DENIED_ENDPOINTS
    assert DEFAULT_POLICY.to_wire()["ignore"] == list(DENIED_ENDPOINTS)


def test_a_caller_ignore_list_is_unioned_onto_the_floor_not_substituted() -> None:
    """The asymmetry that makes `ignore` a floor rather than a preference.

    Every other field a caller sends REPLACES the server's. If `ignore` did too,
    any caller sending an ignore list at all would silently re-enable a host the
    server excluded on measured output quality — routing getting LOOSER via the
    block whose whole contract is that it cannot.
    """
    merged = DEFAULT_POLICY.merged_with(RoutingPreference(ignore=("someotherhost",)))
    for slug in DENIED_ENDPOINTS:
        assert slug in merged.ignore
    assert "someotherhost" in merged.ignore


def test_a_caller_cannot_re_enable_a_denied_endpoint() -> None:
    """The direct attack: name ONE denied host in your own ignore list and hope
    the substitution drops the rest of the floor with it.

    Asserted against a synthetic TWO-entry floor on purpose. Against the shipped
    singleton this test is tautological — a replacing implementation handed
    ``("digitalocean",)`` also returns ``("digitalocean",)`` and passes — so it
    would gate nothing until the day someone adds a second entry, which is
    exactly when they would want it already gating.
    """
    floor = RoutingPolicy(ignore=("badhost", "worsehost"))
    merged = floor.merged_with(RoutingPreference(ignore=("badhost",)))
    assert merged.ignore == ("badhost", "worsehost")
    # Deduplicated, not appended twice: this list goes on the wire.
    assert merged.ignore.count("badhost") == 1


def test_only_does_not_override_the_denylist() -> None:
    """A caller pinning a denied endpoint gets an empty pool and a clean
    `upstream_no_compliant_provider` from upstream — never a quiet downgrade onto
    the host the server excluded."""
    wire = DEFAULT_POLICY.merged_with(RoutingPreference(only=("digitalocean",))).to_wire()
    assert wire["only"] == ["digitalocean"]
    assert wire["ignore"] == list(DENIED_ENDPOINTS)


def test_the_denylist_survives_every_other_caller_preference() -> None:
    merged = DEFAULT_POLICY.merged_with(
        RoutingPreference(privacy=PrivacyMode.ZDR, sort=EndpointSort.PRICE, only=("novita",))
    )
    assert merged.to_wire()["ignore"] == list(DENIED_ENDPOINTS)


def test_the_merged_ignore_order_is_deterministic() -> None:
    """The block is serialized into every request body and the prompt cache keys
    on a byte-identical prefix, so a set-derived order would be a silent miss."""
    a = DEFAULT_POLICY.merged_with(RoutingPreference(ignore=("b", "a")))
    b = DEFAULT_POLICY.merged_with(RoutingPreference(ignore=("b", "a")))
    assert a.ignore == b.ignore == DENIED_ENDPOINTS + ("b", "a")
