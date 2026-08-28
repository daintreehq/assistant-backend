"""Which runbook, if any, gets to say one line before the answer starts.

This is the RUNBOOK OPENER, and it replaces the Cerebras fast-preamble stage on
the same slot on screen and under the same display-only contract. The difference
is where the sentence comes from: the preamble was written per turn by a second
model on a second host, racing the executor's first byte; an opener is authored
once in a runbook's frontmatter and chosen by a dict lookup. No call, no
credential, no race, no failure mode — so it lands with `meta`, before the
executor has opened, which is earlier than the stage it replaces could ever
reach.

That trade is only sound because of what the sentence is allowed to say. A
model-written preamble could describe THIS request; an opener is picked off a
classifier score before anything has been read, so it can only describe the SHAPE
of the work. `RunbookMeta.opener` carries the rule authors have to write to.

**The gate is per-runbook, and it is announce-once.** Three rules, and each one
exists because the obvious simpler version is wrong:

* **A score floor** (`settings.runbook_opener_threshold`). The active set is
  deliberately recall-biased — a runbook joins on p>=0.65, because a missing
  runbook costs more than a spare one — and announcing every load would put a
  confident sentence in front of the user on the strength of a coin-flip. The
  floor is much higher than the admission floor for exactly that reason: loading
  a runbook speculatively is cheap and invisible, and saying "I'll do X" when the
  turn is not about X is neither.
* **Once per runbook, per conversation.** The announced set rides the signed
  state token, so it survives across stateless instances. A workflow that stays
  loaded for eleven rounds announces on the first one; nothing repeats.
* **A near-miss is NOT written off.** A runbook that loads BELOW the floor is
  left unannounced and unrecorded, so if a later turn loads it with real
  confidence it still gets its line. This is what makes the floor a statement
  about certainty rather than a one-shot lottery — the first vague mention of a
  topic does not burn the announcement for the turn that actually commits to it.

**When several qualify, one speaks and the rest go quiet.** All of them are
written off; only the top-scoring one is shown. Showing several would stack
server-authored sentences above an answer that has not started, and the runner-up
is by construction a description of the same turn — so the alternative to
suppressing it is repeating it. Writing the losers off (rather than leaving them
to announce next round) is the deliberate half: they were live for this turn, the
user has already been told what the turn is about, and a delayed announcement
mid-workflow describes work that is by then underway.

Pure and total: no I/O, no clock, and it never raises. Nothing here can fail a
turn, which is the property that lets the orchestrator call it inline on the
first-byte path.
"""

from __future__ import annotations

import json
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from .catalog import Catalog

#: How many announced runbook ids the state token carries.
#:
#: A bound rather than a policy: the token travels through the client on every
#: round, so this cannot be unbounded even though the natural set — "runbooks
#: announced in this conversation" — is. 64 is comfortably past the whole shipped
#: catalog and past any plausible single conversation.
CAP_ANNOUNCED_RUNBOOKS = 64

#: Byte budget for the same list, and the bound that actually protects the token.
#:
#: The count cap alone does NOT bound the token, because a runbook id is only
#: bounded by ``RunbookMeta``'s 256-character ceiling. Measured: 64 ids at that
#: length produce a 26,441-byte token against ``codec.MAX_TOKEN_BYTES`` of
#: 16,384 — so the server would have ISSUED a token it then rejects as oversized
#: on the very next round, turning a long conversation into a 413 that names the
#: client's own state field. Today's ids run to 46 characters and the real list
#: is ~3 KiB, so this is a cliff rather than a live fault; it is bounded here
#: because "no id is ever that long" is a property of the current catalog, not of
#: this code.
#:
#: 2 KiB leaves the rest of the payload (the active set, the catalog revision,
#: the docs query, the turn id) the whole of the remaining budget.
CAP_ANNOUNCED_BYTES = 2048


@dataclass(frozen=True)
class OpenerDecision:
    """What to show, and what the next state token has to remember.

    ``announced`` is the ids to WRITE OFF — the ones shown plus the ones
    suppressed. It is deliberately not derivable from ``runbook_id``: the whole
    point of suppression is that ids the user never saw are still spent.
    """

    #: The line to emit, or ``None`` for the overwhelmingly common case of a turn
    #: that opens straight into its answer.
    text: str | None = None
    #: Which runbook wrote it. Diagnostics and logging only.
    runbook_id: str | None = None
    #: Ids to add to the conversation's announced set, INCLUDING the one shown.
    announced: tuple[str, ...] = ()
    #: Qualifying runbooks that were suppressed in favour of ``runbook_id``.
    #: Logged rather than acted on — a rate that stops being ~0 means the floor
    #: is admitting more than one confident runbook per turn, which is a signal
    #: about the catalog's separation, not about this module.
    suppressed: tuple[str, ...] = ()


def choose_opener(
    *,
    scored_active_ids: Sequence[str],
    scores: Mapping[str, float],
    already_announced: Collection[str],
    catalog: Catalog,
    threshold: float,
) -> OpenerDecision:
    """Pick at most one opener for this turn.

    ``scored_active_ids`` is the turn's active set narrowed to what the selector
    actually RANKED. Deliberately not "newly loaded": the two are the same list
    on an ordinary first load and come apart in precisely the case the near-miss
    rule exists for — a runbook admitted at 0.80 stays active, so a later
    instruction re-scoring it at 0.97 would find it missing from the
    newly-loaded list and its caption would be suppressed forever, which is the
    opposite of what a floor is for.

    ``scores`` is empty on every reused, degraded and cleared decision, so a
    tool-continuation round, a fail-open and a background wake all fall out here
    with no special case: they ranked nothing, so nothing carried over can
    announce itself.

    A runbook with no score is silently ineligible, and that is load-bearing
    rather than defensive: a domain foundation paired in by admission and a
    server-pinned documentation runbook both reach the active set WITHOUT a
    score, because neither was ranked. Neither should announce — the user asked
    for neither — and requiring a score is what says so without this module
    needing to know how either got there.
    """
    announced_before = set(already_announced)
    qualified: list[tuple[float, str, str]] = []
    for sid in scored_active_ids:
        if sid in announced_before:
            continue
        score = scores.get(sid)
        # `isfinite` rather than a bare comparison. A NaN fails EVERY
        # comparison, so `score < threshold` is False for one and a NaN would
        # sail through the floor it is least entitled to clear — then sort
        # ahead of a legitimate 0.97 and write it off unheard. Nothing observed
        # produces one; it costs a call to make the floor mean what it says.
        if score is None or not math.isfinite(score) or score < threshold:
            continue
        runbook = catalog.get(sid)
        if runbook is None:
            continue
        opener = runbook.opener
        if not opener:
            # Above the floor and deliberately silent. NOT written off: there is
            # nothing to spend, and recording it would grow the token for a
            # runbook that can never produce a line.
            continue
        qualified.append((score, sid, opener))

    if not qualified:
        return OpenerDecision()

    # Highest score wins; the id breaks a tie so the choice is deterministic
    # rather than dependent on admission order — two runbooks at an identical
    # float is vanishingly unlikely and a coin flip that moves between deploys is
    # not something to leave in a user-visible path.
    qualified.sort(key=lambda row: (-row[0], row[1]))
    _score, winner, text = qualified[0]
    return OpenerDecision(
        text=text,
        runbook_id=winner,
        announced=tuple(sid for _s, sid, _t in qualified),
        suppressed=tuple(sid for _s, sid, _t in qualified[1:]),
    )


def fold_announced(
    prior: Sequence[str], newly_announced: Sequence[str]
) -> list[str]:
    """The announced set the next state token should carry.

    Order is oldest-first and preserved, so both caps drop the OLDEST. Deduped
    against ``prior`` because an id already recorded must not be re-appended —
    that would move it to the young end of the list and make it the LAST thing
    evicted, which inverts the rule for exactly the runbooks that were announced
    longest ago.

    Two caps, because they bound different things: ``CAP_ANNOUNCED_RUNBOOKS``
    keeps the list a sane length, and ``CAP_ANNOUNCED_BYTES`` keeps the TOKEN
    inside ``codec.MAX_TOKEN_BYTES``. A count cap cannot do the second job — a
    runbook id is bounded only by 256 characters — and overflowing it would make
    the server issue a token it rejects on the next round.

    Overflowing either cap costs at most a repeated caption for a runbook
    announced very early in an extremely long conversation, which is the right
    failure: dropping the NEWEST would repeat the most recent one instead.
    """
    seen = set(prior)
    out = list(prior)
    for sid in newly_announced:
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    if len(out) > CAP_ANNOUNCED_RUNBOOKS:
        out = out[-CAP_ANNOUNCED_RUNBOOKS:]
    # Measured over the JSON encoding rather than the ids alone: what reaches the
    # token is a list, so each entry costs its quotes and separator too.
    while out and len(json.dumps(out, separators=(",", ":"))) > CAP_ANNOUNCED_BYTES:
        out.pop(0)
    return out
