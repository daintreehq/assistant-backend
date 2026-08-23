"""Who may be recorded, decided explicitly rather than inferred.

The service has no account model yet (``auth.py`` does not read the
``Authorization`` header at all), so there is nobody to ask for consent and no
per-account flag to read. That is exactly why this module exists as a real
decision object instead of an ``if settings.trace_mode == "full_internal"``
scattered through the orchestrator: when account auth lands, ONE function changes
and every caller inherits it.

The standing rule is the conservative one: **using the service is not consent.**
Content capture is available only where an operator has deliberately turned it on
in a non-hardened environment, and a hardened deployment that asks for it fails
readiness rather than quietly capturing (see ``Settings.trace_problems``).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Capture modes, widening left to right.
#:
#: ``off``            record nothing at all.
#: ``metadata``       record the trace skeleton — ids, timings, hashes, usage —
#:                    with no content blobs. Safe anywhere; it is what the
#:                    operational logs already carry, in a shape an exporter can
#:                    read.
#: ``full_internal``  record exact content. Controlled internal testing ONLY.
TraceCaptureMode = str


@dataclass(frozen=True)
class TraceEligibility:
    """The decision, with the reason attached so a trace can explain itself.

    ``reason`` is written into the header. A trace that cannot say WHY it was
    considered training-eligible is one nobody can audit later, and "we think
    these were internal" is not a basis for training on somebody's terminal
    output.
    """

    capture_metadata: bool
    capture_content: bool
    training_eligible: bool
    reason: str

    @property
    def enabled(self) -> bool:
        return self.capture_metadata or self.capture_content


#: The answer when nothing is configured. Allocation-light and shared, because
#: the orchestrator asks on every request and the overwhelmingly common answer
#: is "no".
DISABLED = TraceEligibility(
    capture_metadata=False,
    capture_content=False,
    training_eligible=False,
    reason="trace_capture_off",
)


def decide(
    *,
    mode: TraceCaptureMode,
    is_hardened: bool,
    profile: str = "assistant",
) -> TraceEligibility:
    """Resolve capture and training eligibility for one request.

    ``profile`` is accepted and currently does not narrow the decision: a
    sub-agent turn is part of the same task and its trace is part of the same
    training example. It is a parameter rather than a future edit because the
    call sites are the hard part to add later, not the branch.
    """
    if mode == "off":
        return DISABLED

    if mode == "metadata":
        return TraceEligibility(
            capture_metadata=True,
            capture_content=False,
            training_eligible=False,
            reason="metadata_only",
        )

    if mode == "full_internal":
        # Belt and braces. `Settings.trace_problems()` already fails readiness for
        # this combination, so a hardened deployment should never reach here — but
        # a readiness gate protects the DEPLOYMENT, and this protects the REQUEST.
        # If the two ever disagree, the safe answer must be the one nearest the
        # data.
        if is_hardened:
            return TraceEligibility(
                capture_metadata=True,
                capture_content=False,
                training_eligible=False,
                reason="content_capture_refused_in_hardened_environment",
            )
        return TraceEligibility(
            capture_metadata=True,
            capture_content=True,
            training_eligible=True,
            reason="internal_controlled_build",
        )

    # An unrecognized mode is a configuration error, and the failure direction
    # for a configuration error about RECORDING PEOPLE'S CONTENT is off.
    return DISABLED
