"""The fast visible preamble: one short line about the work that is starting.

**It runs BESIDE the executor, not ahead of it.** Both calls go out at the same
moment. Cerebras is expected to answer first — a few hundred milliseconds against
the executor's prefill of a ~25k-token prompt — and the line it writes fills that
gap. Nothing waits for it: the executor request is assembled and opened without
reference to whether this stage has produced anything, or is even still running.

That is the whole difference from the design this replaces, and it settles three
things that used to be hard:

* **The preamble is no longer executor INPUT.** It used to be appended as an
  assistant row with a server-owned continuation instruction, which is what forced
  the two calls to be sequential — the executor's prompt literally contained the
  preamble's bytes. Now the executor never sees it, so there is no handoff, no
  continuation row, no "visible bytes are the injected bytes" invariant, and no
  hidden technical plan riding along to steer the answer.
* **It is display-only.** The client SHOWS the line and must not commit it to
  conversation history (`docs/DAINTREE_API.md` section 8.14). If it did, the next
  turn would send the model an assistant turn it never wrote.
* **Late means dropped.** A preamble is only worth showing before the executor's
  first visible byte. After that the answer itself is on screen, and prefixing it
  with a description of work already visibly underway is worse than silence. The
  orchestrator closes that window; this module never learns whether its output was
  used.

**It is an optimisation, and it behaves like one.** No failure here can fail,
alter or delay the turn. A timeout, a transport error, output that fails
validation, runbooks that do not fit the budget — every one of them resolves to NO
PREAMBLE. There is deliberately no generic fallback sentence any more: the model
is now asked to judge whether a line is worth writing at all and to answer `NONE`
when it is not, and a server-authored filler sentence emitted on failure would
contradict exactly that judgement.

There is no retry, for a reason that changed but did not go away: one that
succeeds at five seconds no longer delays anything, but the window it needed to
land in has closed, so it is spend buying an answer that cannot be used.
`generate` does not raise, with one deliberate exception — `asyncio.CancelledError`
propagates, because cancellation belongs to the turn that owns it and swallowing
it here would outlive the deadline meant to end it.

**The context is deliberately thin.** One system prompt, at most three runbooks,
and the user's latest message. Not the transcript, not the tool inventory, not the
startup block. The job is one sentence about the shape of the work; everything
beyond that was context bought for a considered opening this stage no longer
writes, and it cost first-token latency on the one call that is racing.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from ..config import FAST_RESPONSE_MODEL, FAST_RESPONSE_REASONING_EFFORT, Settings
from ..contracts.daintree_api import RespondRequest
from ..observability import get_logger
from ..prompts.loader import TEMPLATE_FAST_RESPONSE_RUNBOOKS, PromptLoader
from ..providers.base import ChatProvider, ProviderChatRequest, ProviderUsage
from ..runbooks.catalog import Catalog
from ..runbooks.decision import SelectionDecision
from ..runbooks.models import Runbook
from ..runbooks.tool_projection import session_bucket
from ..text_utils import message_text

logger = get_logger(__name__)

#: How many runbooks the fast model reads.
#:
#: Three, and fixed — not "however many the turn happens to have loaded". The
#: previous design fed it the active set PLUS the selector's near-misses, on the
#: theory that a rejected second-ranked runbook is still evidence about the task.
#: It is, but it is evidence bought at roughly 8 KB a runbook on the call whose
#: entire value is landing first, to sharpen a sentence that is now under forty
#: words. Rank order already puts the best fit first, so a cap takes the cheapest
#: possible loss.
MAX_FAST_RUNBOOKS = 3

#: Hard ceilings on the visible text. Two or three sentences: enough to read as
#: understanding rather than a status line, and short enough that the real answer
#: arriving underneath it is not climbing out from behind a wall.
#:
#: **Both caps sit ABOVE what the prompt asks for, and that is the whole design.**
#: The prompt asks for two or three sentences under 80 words. These are the
#: REFUSAL thresholds, not the target — a validator set AT the prompt's own number
#: turns a model that complies-but-rounds-up into a silent failure rate, with no
#: fallback left to soften it. That is not hypothetical: the word cap was briefly
#: set to the prompt's figure and two of three live requests came back
#: `too_long_words`, showing nothing at all. Measured live, two or three good
#: sentences run 40-70 words; 100 leaves real headroom for a third long one.
#:
#: The WORD cap is the binding one; the character cap is a backstop. They must not
#: swap places — technical prose runs ~8.4 characters per word (measured against
#: this stage's own output), so 100 words is ~840 characters. The
#: character cap is deliberately loose enough that the word cap always binds
#: first, and exists for the one thing a word count cannot see — a single runaway
#: "word" like a path, a URL or a base64 blob.
MAX_PREAMBLE_WORDS = 100
MAX_PREAMBLE_CHARS = 1000

#: Generated tokens, and REASONING TOKENS COUNT AGAINST IT. `gpt-oss-120b` is a
#: reasoning model; `FAST_RESPONSE_REASONING_EFFORT` holds it at Cerebras's floor
#: but there is no "off", so a budget sized to the visible line alone would
#: truncate on the thinking and never reach the answer. Sized instead as thinking
#: room plus a line that the two caps above already bound far more tightly — so a
#: reply that runs long is truncated by the provider and rejected here on
#: `finish_reason == "length"`, rather than shown to a user mid-sentence. Raised
#: with the caps when the text went from one sentence to two or three.
MAX_OUTPUT_TOKENS = 600

#: The whole fast prompt, in characters. Down from 260,000, which was sized for
#: the full transcript this stage no longer reads. Three of the largest runbooks
#: in the catalogue come to ~46 KB together; the rest is the system prompt and one
#: message. At ~2.5 characters per token — a transcript is not prose, and paths
#: and JSON run nearer one — this is comfortably inside the model's 131k window.
_MAX_INPUT_CHARS = 80_000

#: How many recent USER messages the fast model reads. Three: enough that a short
#: follow-up ("do that", "and the other one too") is not describing a request the
#: model cannot see, and few enough that the cost stays bounded. Assistant and
#: tool rows are never included — they are the record of work, which is the one
#: thing this stage must not claim has happened.
MAX_FAST_USER_MESSAGES = 3

#: The TOTAL character budget across every message included, and the reason the
#: allocation below is greedy-newest rather than an even split.
#:
#: This host is fast and expensive, so history here is bought rather than free.
#: The newest message is the request; everything older is context for reading it.
#: So the newest takes what it needs FIRST and the remainder cascades backwards —
#: a long paste crowds the history out entirely (correctly: it IS the request),
#: and a two-word follow-up leaves almost the whole budget for the messages that
#: explain it.
_MAX_HISTORY_CHARS = 4_000

#: Below this an older message is dropped rather than cropped. A 40-character
#: fragment of a message is not context, it is a torn-off clause that reads as a
#: different request than the one that was made — worse than not seeing it.
#: Deliberately NOT applied to the newest message, which is always included at
#: whatever length survives: a preview of a task the model was not told about is
#: worthless, and the newest message is that task.
_MIN_USEFUL_MESSAGE_CHARS = 200

#: The decline sentinel. The model's second valid answer is silence, and it says
#: so with this exact token — see `prompts/fast_response/base.md`.
#:
#: A sentinel rather than an empty completion because an empty completion is
#: indistinguishable from a failure: a truncation, a filtered reply, a provider
#: returning nothing. A deliberate decline and a broken call must not land in the
#: same bucket, or the one metric that says whether the model's judgement is any
#: good reads as an error rate.
_DECLINE = re.compile(r"\A(?:none|no(?:ne)?\s*\.)\Z", re.IGNORECASE)

#: The sentinel written AFTER real text, which is a different failure.
#:
#: Observed live: the model produced three good sentences and then appended
#: "… remaining risks once it finishes. NONE". The whole-string pattern above does
#: not see that, so the token went straight to a user's screen as if it were part
#: of the sentence. Stripped rather than rejected — the text before it is the
#: model's actual answer and refusing the lot would cost a good preamble over a
#: stray token.
#:
#: Anchored on UPPERCASE and on a whitespace boundary, so a sentence that happens
#: to end in the ordinary word ("returned none") is untouched. The case-insensitive
#: whole-string pattern above stays case-insensitive because there the token IS the
#: whole reply and there is nothing to lose.
#: The lookbehind is what makes this safe AND sufficient. Observed in two shapes
#: on live traffic — "… once it finishes. NONE" and "… repository unchanged.NONE",
#: the second with no space at all — so anchoring on whitespace alone missed half
#: of them. Sentence-ending punctuation counts as a boundary; an ordinary word is
#: not clipped, because a letter before the token fails the lookbehind entirely.
_TRAILING_DECLINE = re.compile(r"(?<=[.!?\s])NONE\.?\s*\Z")

#: Verbs that describe work on the project — the things a preamble must not claim
#: to have already done. `read` carries a guard because "I have read-only access"
#: is an honest statement of a CONSTRAINT, not a claim to have read anything.
_DID = (
    r"checked|found|looked at|inspected|reviewed|read(?!-only)|ran|run|executed|fixed|"
    r"changed|edited|updated|added|removed|deleted|tested|verified|confirmed|"
    r"identified|discovered|determined|resolved|traced|debugged"
)

#: Adverbs a model slips between subject and verb. Without them "I have NOW fixed
#: it" walks straight past a pattern that catches "I have fixed it".
_ADVERB = r"(?:just|already|now|since|successfully|finally)\s+"

#: Claims that work has ALREADY happened, in the shapes a model actually produces
#: them. Measured against real phrasings rather than imagined ones — first-person
#: past alone missed participials ("Having inspected the tests…"), passives ("The
#: tests have been run") and impersonals ("Turns out the loader was wrong").
#:
#: Still the strictest check here, though the reason narrowed with the design.
#: These bytes no longer become the executor's own prior turn, so an overclaim can
#: no longer tell the MODEL the job is partly done — but it still tells the USER
#: so, moments before an answer appears that starts the work from nothing. A line
#: that lies about progress is the one failure mode that would make a reader trust
#: the whole reply less.
#:
#: Deliberately still NOT catching a bare "the tests fail": that is a restatement
#: of context the model was handed, and rejecting it would push every preview
#: toward uselessly vague wording.
_OVERCLAIM = re.compile(
    "|".join(
        (
            # "I checked", "we've now run"
            rf"\b(?:i|i've|i have|we|we've|we have)\s+(?:{_ADVERB})?(?:{_DID})\b",
            # Passive: "the tests have been run", "it was fixed"
            rf"\b(?:has|have|had|was|were)\s+(?:been\s+)?(?:{_ADVERB})?(?:{_DID})\b",
            # Participial: "Having inspected…", "After reading…", "After I checked…"
            rf"\b(?:having|after)\s+(?:i\s+|we\s+)?\w*(?:{_DID}|reading|checking|running|looking)\b",
            r"^\s*(?:looking|having looked|after looking)\s+(?:at|through|into)\b",
            # Impersonal discovery. Anchored so "whether it turns out to be X" —
            # an honest hedge about the FUTURE — is not swept up with it.
            r"(?:^|\.\s+)\s*(?:it\s+)?turns out\b",
            # Completion. Sentence-initial, so "I'll check whether it's fixed"
            # (a statement of intent) survives while "That's fixed now" does not.
            r"^\s*(?:done|fixed|all set|sorted)\b",
            r"(?:^|\.\s+)\s*(?:that's|thats|it's|its)\s+(?:now\s+)?(?:done|fixed|sorted)\b",
            r"\bthe (?:fix|change|update) is (?:in|done|live)\b",
        )
    ),
    re.IGNORECASE | re.MULTILINE,
)

#: Text that addresses a MODEL rather than the user.
#:
#: This used to be the security check of the module: the preamble became the
#: executor's own prior assistant turn, so "ignore the next message" was a prompt
#: injection carrying an assistant's authority, delivered by our own pipeline. It
#: is not that any more — nothing this stage writes reaches the executor.
#:
#: Kept anyway, and honestly relabelled: the fast model's input still contains
#: user-controlled text, and a line that starts issuing instructions is a line the
#: user should not be reading, whoever it is addressed to. A quality guard now,
#: not a boundary.
_ADDRESSES_THE_MODEL = re.compile(
    r"\b(?:ignore|disregard|override|bypass|skip)\s+(?:the\s+|any\s+|all\s+)?"
    r"(?:next|following|previous|prior|above|below|remaining|further)?\s*"
    r"(?:message|instruction|rule|step|system|prompt)s?\b"
    r"|\btreat\s+the\s+task\s+as\s+(?:complete|done|finished)\b"
    r"|\byou\s+(?:must|should|will)\s+(?:not\s+)?(?:call|use|emit|output|ignore)\b"
    r"|\bdo\s+not\s+(?:call|use|emit)\s+(?:any\s+)?tools?\b",
    re.IGNORECASE,
)

#: Structure the contract forbids: this is one flowing line, and a heading or
#: bullet in the FIRST visible bytes of a reply reads as the answer starting.
_STRUCTURE = re.compile(r"(?m)^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|\||```)")

#: A tag-shaped fragment anywhere in the line.
#:
#: The visible half is PLAIN PROSE, and markup in it is machinery leaking onto a
#: user's screen. This used to be enforced incidentally: the stage asked for a
#: hidden `<daintree_plan>` block alongside the visible line, and a dedicated
#: check refused any trace of that tag surviving the split. The block is gone and
#: the check went with it — but the property it protected did not, and a model
#: prompted to write one sentence can still emit `<thinking>`, a stray `<plan>`,
#: or half an HTML fragment. Cheap, and the visible line has no legitimate use for
#: a tag.
#:
#: Deliberately narrow: `<` followed by an optional slash and a LETTER. Arithmetic
#: ("a < b"), a comparison in a quoted identifier, and an arrow ("->") all survive.
_MARKUP = re.compile(r"<\s*/?\s*[a-zA-Z][\w:-]*\s*/?>")

#: Internal detail the user is never shown. The product is Daintree, not a
#: pipeline, and a leaked provider or model name is also a disclosure we did not
#: intend.
_DISCLOSURE = re.compile(
    r"\b(?:cerebras|gemma|deepseek|gpt-?oss|together\s*ai|openrouter|qwen|runbook|"
    r"system prompt|preamble|executor)\b",
    re.IGNORECASE,
)

#: Control markers the executor's own contract uses. A preamble carrying one would
#: be read as a turn-finality declaration by the scanner downstream.
_CONTROL_MARKER = re.compile(r"\[\[DAINTREE:[A-Z]+\]\]")

#: The register the line is asked NOT to use: a first-person commitment. LOGGED,
#: never rejected — and the asymmetry is deliberate.
#:
#: The cost of this drifting is a mildly repetitive opening, because the executor
#: announces its own actions anyway. The cost of rejecting it is losing the
#: preamble entirely, since there is no fallback left to fall back to. A real line
#: in the wrong register beats no line at all, so this measures the rate instead of
#: paying that price — if it climbs, the prompt is what to fix.
_ANNOUNCEMENT_OPENING = re.compile(
    r"^\s*(?:i'?l+\s|i\s+will\b|let\s+me\b|first,?\s+i\b|i'?m\s+going\s+to\b)",
    re.IGNORECASE,
)

#: Finish reasons that mean "the model stopped because it was done". Anything
#: else — a filter, a truncation, an unrecognised value — is not a whole reply.
_CLEAN_FINISH_REASONS = frozenset({"stop", "end_turn", None})

#: Exotic Unicode spaces, normalised to a plain one before anything else looks at
#: the line.
#:
#: Observed rather than imagined: on a live smoke of four turns, `gpt-oss-120b`
#: returned "a\u202f--verbose\u202fflag" — U+202F NARROW NO-BREAK SPACE — on one
#: of them. It is legitimate typography and every check here passes it, which is
#: the problem: it goes straight to a terminal, where a font without the glyph
#: draws a replacement box in the middle of the first sentence a user sees.
#:
#: Normalised rather than rejected. The line is otherwise fine, there is no
#: fallback left to fall back to, and the fix is exact — these all MEAN a space.
#: `\u200b` (zero-width space) is deliberately absent: it means "a break
#: opportunity here", not a space, and turning it into one would insert a gap the
#: model did not write.
_EXOTIC_SPACES = str.maketrans(dict.fromkeys("\u00a0\u2007\u2009\u202f\u2060\ufeff", " "))

#: A trailing `[...` with no closing bracket — what an arbitrary character
#: slice leaves behind when it cuts through a `[image: <media>]` marker
#: (`text_utils.message_text`'s rendering of an image part). Deliberately not
#: anchored on the literal word "image": a cut can land anywhere inside the
#: marker, including before "image" is fully spelled ("[ima", "[i", bare "["),
#: and any of those is equally a fragment rather than content. A COMPLETE
#: marker never matches — `[^\]]*` cannot consume the closing `]`, so anchoring
#: on end-of-string only fires when the bracket never closes.
_UNCLOSED_TRAILING_BRACKET = re.compile(r"\[[^\]]*$")

FastResponseSource = Literal["model", "declined", "none"]


@dataclass(frozen=True)
class FastResponsePolicyDecision:
    """Whether to run, whether to show it, and — always — why not."""

    run: bool
    visible: bool
    reason: str


@dataclass(frozen=True)
class FastResponseValidation:
    valid: bool
    content: str
    #: The model answered the decline sentinel. Not an error and not a rejection:
    #: `valid` is False because there is nothing to show, and this says the reason
    #: was judgement rather than failure.
    declined: bool = False
    warnings: tuple[str, ...] = ()
    reject_reason: str | None = None


@dataclass(frozen=True)
class FastResponseResult:
    """What the stage produced, and everything the turn needs to account for it."""

    content: str | None = None
    source: FastResponseSource = "none"
    degraded: bool = False
    reason: str | None = None
    usage: ProviderUsage | None = None
    latency_ms: int | None = None
    invocation_id: str | None = None
    content_sha256: str | None = None

    @property
    def has_content(self) -> bool:
        """Whether anything was produced — NOT whether it may be shown.

        Deliberately not called `visible`: shadow mode produces real, valid
        content that must never reach a client, and a property named `visible`
        returning True for it is the shape of the bug that leaks it. Visibility is
        `FastResponsePolicyDecision.visible`, and it stays there.
        """
        # `.strip()`, not a bare truth test: whitespace-only content would
        # otherwise count as a preamble and the client would render a blank line.
        return bool(self.content and self.content.strip())


def fast_context_runbooks(
    decision: SelectionDecision,
    active: list[Runbook],
    catalog: Catalog,
) -> list[Runbook]:
    """The at-most-three runbooks the fast stage reads.

    The active set leads, in rank order, and the selector's near-misses fill any
    remaining slot: a runbook the head ranked highly and admission threw away is
    still the strongest available evidence about what KIND of request this is, and
    on a turn that loaded only one runbook the second slot is free.

    Capped at :data:`MAX_FAST_RUNBOOKS`. The cap binds from the bottom of the
    ranking, so what goes is always the least likely fit — and, where the active
    set itself is larger than the cap, a runbook the turn will genuinely run but
    which the line was least likely to be about.

    Degrades to the active set alone: a reused or degraded decision has no ranking
    of its own, and borrowing the previous turn's would describe a request that is
    no longer being made.
    """
    seen = {r.id for r in active}
    picked: list[Runbook] = list(active[:MAX_FAST_RUNBOOKS])
    for rid in decision.ranked_runbook_ids:
        if len(picked) >= MAX_FAST_RUNBOOKS:
            break
        if rid in seen:
            continue
        runbook = catalog.get(rid)
        if runbook is not None:
            # Added to `seen` as well: the ranking is unique in production, but a
            # helper that only reads the set and never writes it would render the
            # same body twice the moment that stopped being true — spending budget
            # on a duplicate and reading to the model as emphasis.
            seen.add(rid)
            picked.append(runbook)
    return picked


def should_generate_fast_response(
    request: RespondRequest,
    *,
    settings: Settings,
    selector_ran: bool,
    no_task_fast_path: bool,
    active_runbooks: list[Runbook],
    provider_available: bool,
) -> FastResponsePolicyDecision:
    """One deterministic function, and every skip carries its reason.

    Deliberately NOT a second classifier. The turn already ran one, and adding
    another model call to decide whether to make a model call is the shape of
    problem this stage exists to avoid. The judgement that DOES need a model — is
    a line worth writing for this particular request — belongs to the fast model
    itself, which answers the decline sentinel when the answer is no.
    """

    def no(reason: str) -> FastResponsePolicyDecision:
        return FastResponsePolicyDecision(run=False, visible=False, reason=reason)

    if settings.fast_response_mode == "off":
        return no("mode_off")
    if not provider_available:
        return no("provider_unavailable")
    # Non-streaming has no first-byte to improve: the client sees one response
    # either way, and a preamble that cannot be delivered ahead of the answer is
    # spend with nothing to buy.
    if not request.generation.stream:
        return no("not_streaming")
    # A sub-agent reports to the orchestrator, not to a person. A user-facing
    # preview is wasted context and wasted spend.
    if request.profile != "assistant":
        return no("subagent_profile")
    if request.generation.response_format == "json_object":
        return no("json_response_format")
    # A continuation round is mid-turn: the user has already seen a preamble for
    # this instruction, and a fresh one before every tool round is the exact
    # behaviour that would make the feature obnoxious.
    #
    # Gated on the ROUND, not on whether selection ran. Selection reruns for
    # several reasons that have nothing to do with a new instruction — a caller
    # `force`, an invalid state token, a catalog revision change — so
    # `selector_ran` would let a mid-turn round produce a second preamble.
    if request.session.round > 0:
        return no("continuation_round")
    if not selector_ran:
        return no("selection_did_not_run")
    if no_task_fast_path:
        return no("no_task_turn")
    # A background wake has nobody watching. A line addressed to an empty terminal
    # is spend with no perceived-latency benefit to buy, which is the entire
    # justification for this stage.
    if request.turn is not None and request.turn.is_wake:
        return no("background_wake")
    if not active_runbooks:
        return no("no_active_runbooks")
    # A COMPELLED tool call is a turn with no prose to preview. `"none"` is the
    # opposite — it guarantees a prose-only answer, which is precisely the turn a
    # preamble suits — so it must not be swept up with the forcing values.
    choice = request.input.tool_choice
    if choice is not None and choice not in ("auto", "none"):
        return no("forced_tool_choice")

    # Own namespace, deliberately distinct from tool projection's default: two
    # rollouts hashing the same session id under the same bytes would always
    # co-enrol, making it impossible to tell the two experiments' effects apart.
    bucket = session_bucket(request.session.id, namespace=b"fast-response\x00")
    if bucket >= settings.fast_response_rollout_percent:
        return no("not_in_rollout_cohort")

    # Shadow RUNS but does not show: the point is to measure and trace real
    # preambles against real turns before any user sees one.
    visible = settings.fast_response_mode == "visible"
    return FastResponsePolicyDecision(run=True, visible=visible, reason="eligible")


def validate_preamble(text: str, *, finish_reason: str | None, had_tool_calls: bool) -> (
    FastResponseValidation
):
    """Conservative, and never repaired by a second model call.

    Asking a model to fix a preamble costs another round trip on the one path
    whose entire value is speed, and the rewrite would need validating too. A
    rejected preamble becomes no preamble, which — now that there is no fallback
    sentence — is the same outcome the model itself chooses when it declines.
    """

    def reject(reason: str) -> FastResponseValidation:
        return FastResponseValidation(valid=False, content="", reject_reason=reason)

    if had_tool_calls:
        # The request sends no tools at all, so this means the provider ignored
        # that. Never forward or execute it.
        return reject("tool_calls_returned")
    if finish_reason not in _CLEAN_FINISH_REASONS:
        # An ALLOWLIST, not a "not length" test. `content_filter` yields partial
        # text, and an unrecognised reason from a host whose wire is unverified
        # means we do not know whether the text is whole — which is the same
        # answer as truncated for a line about to be shown to a user.
        return reject(f"unclean_finish:{finish_reason}")

    content = text.translate(_EXOTIC_SPACES).strip()
    if not content:
        # NOT read as a decline. Silence has a spelling (`_DECLINE`) precisely so
        # that a model choosing to say nothing is distinguishable from a call that
        # produced nothing — see that pattern's note.
        return reject("empty")
    if _DECLINE.match(content):
        # The model's own judgement that text would be noise. `valid` is False
        # because there is nothing to show; `declined` is what stops it being
        # counted as a failure.
        return FastResponseValidation(valid=False, content="", declined=True)
    warnings: tuple[str, ...] = ()
    stripped = _TRAILING_DECLINE.sub("", content)
    if stripped != content:
        # Text AND the sentinel. The text is what the model actually wrote, so it
        # survives; the token is control material that must never be rendered.
        warnings = ("trailing_decline_sentinel",)
        content = stripped.strip()
        if not content:
            return FastResponseValidation(valid=False, content="", declined=True)
    if len(content) > MAX_PREAMBLE_CHARS:
        return reject("too_long_chars")
    if len(content.split()) > MAX_PREAMBLE_WORDS:
        return reject("too_long_words")
    if any(ch in content for ch in "\x00\r") or any(
        ord(ch) < 0x20 and ch != "\n" for ch in content
    ):
        return reject("control_characters")
    if _CONTROL_MARKER.search(content):
        return reject("control_marker")
    if _STRUCTURE.search(content):
        return reject("structured_output")
    if _MARKUP.search(content):
        return reject("markup_in_prose")
    if content.count("```") % 2 or content.startswith("{") or content.startswith("["):
        return reject("not_plain_prose")
    if _DISCLOSURE.search(content):
        return reject("internal_disclosure")
    if _OVERCLAIM.search(content):
        return reject("overclaim")
    if _ADDRESSES_THE_MODEL.search(content):
        return reject("addresses_the_model")
    return FastResponseValidation(valid=True, content=content, warnings=warnings)


class FastResponseService:
    """Builds the fast prompt, calls Cerebras once, and validates what comes back."""

    def __init__(
        self,
        *,
        settings: Settings,
        prompt_loader: PromptLoader,
        provider: ChatProvider | None,
    ) -> None:
        self._settings = settings
        self._prompts = prompt_loader
        self._provider = provider

    @property
    def available(self) -> bool:
        return self._provider is not None and self._settings.fast_response_mode != "off"

    def policy_for(
        self,
        request: RespondRequest,
        *,
        selector_ran: bool,
        no_task_fast_path: bool,
        active_runbooks: list[Runbook],
    ) -> FastResponsePolicyDecision:
        return should_generate_fast_response(
            request,
            settings=self._settings,
            selector_ran=selector_ran,
            no_task_fast_path=no_task_fast_path,
            active_runbooks=active_runbooks,
            provider_available=self._provider is not None,
        )

    # -- prompt ----------------------------------------------------------

    def build_messages(
        self, request: RespondRequest, *, active_runbooks: list[Runbook]
    ) -> list[dict[str, Any]] | None:
        """The fast model's input, or None when nothing usable fits.

        Three rows and no more: the system prompt, the runbook bodies, and the
        user's latest message. Layered widest-shared-first — only the last of
        those changes every turn, so Cerebras's prefix cache covers everything
        above it.

        Runbook bodies are never truncated. A half-rendered procedure would have
        the fast model describe steps that stop mid-thought, which is worse than
        no preview at all — so when they do not fit, whole runbooks are dropped
        from the BOTTOM of the ranking (the least likely fit goes first).

        Returns None when there is no latest message to describe: a line written
        without knowing what was asked is exactly the invented filler the prompt
        spends most of its length refusing.
        """
        system = self._prompts.fast_response_prompt()
        history = self._recent_user_messages(request)
        if not history:
            return None

        budget = _MAX_INPUT_CHARS - len(system) - sum(len(m) for m in history)
        # Dropped whole and from the bottom: `active_runbooks` arrives in rank
        # order, so the first is the most likely fit and the last is the most
        # expendable. Rendering is per-runbook rather than once over the list
        # because the decision is which ones SURVIVE, and that cannot be made
        # against a single rendered blob.
        kept: list[Runbook] = []
        for runbook in active_runbooks[:MAX_FAST_RUNBOOKS]:
            block = self._prompts.render(TEMPLATE_FAST_RESPONSE_RUNBOOKS, runbooks=[*kept, runbook])
            if len(block) > budget:
                break
            kept.append(runbook)
        if not kept:
            return None

        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": self._prompts.render(TEMPLATE_FAST_RESPONSE_RUNBOOKS, runbooks=kept),
            },
            # Oldest first, so the array reads as the conversation does and the
            # LAST row is the request — the position a model treats as current.
            *({"role": "user", "content": text} for text in history),
        ]

    @staticmethod
    def _recent_user_messages(request: RespondRequest) -> list[str]:
        """Up to three recent USER messages, cropped to a shared budget.

        Returned OLDEST FIRST, so the caller can lay them out the way the
        conversation ran and the newest ends up last.

        **The allocation is greedy from the newest backwards**, which is what
        satisfies the two requirements that pull against each other: the newest
        message should arrive whole where possible, and the budget must hold. So
        the newest takes what it needs, then whatever is left cascades to the one
        before it, and so on. A long paste therefore crowds the history out
        entirely — correct, because it IS the request — while a two-word follow-up
        leaves nearly the whole budget for the messages that make it legible.

        An older message whose remaining allowance falls under
        :data:`_MIN_USEFUL_MESSAGE_CHARS` is DROPPED rather than cropped to a
        stub. The newest never is: a preview of a task the model was not told
        about is worthless, so it is included at whatever length survives.

        Only ``user`` rows. Assistant and tool rows are the record of work
        already done, and this stage's single hardest rule is that it must not
        describe anything as having happened.

        ``message_text`` is the shared flattening helper for ``str | list[
        ContentPart] | None``: a text-plus-image message must not become empty
        text just because ``content`` is a list rather than a string. A bare
        ``isinstance(..., str)`` check here used to do exactly that, launching a
        preview of a task the fast model was never actually told about.
        """
        picked: list[str] = []
        remaining = _MAX_HISTORY_CHARS
        for message in reversed(request.input.messages):
            if len(picked) >= MAX_FAST_USER_MESSAGES or remaining <= 0:
                break
            if message.role != "user":
                continue
            text = message_text(message.content)
            if not text.strip():
                # Not a bare `not text`: several empty text parts flatten to a
                # join of empty strings — e.g. "\n" for two of them — which is
                # truthy and would otherwise become a blank row.
                continue
            newest = not picked
            if not newest and remaining < _MIN_USEFUL_MESSAGE_CHARS:
                break
            if len(text) > remaining:
                text = FastResponseService._crop(text, remaining)
                if not text:
                    continue
            remaining -= len(text)
            picked.append(text)
        picked.reverse()
        return picked

    @staticmethod
    def _crop(text: str, limit: int) -> str:
        """Head-crop to ``limit``, leaving no torn marker behind.

        The HEAD, not the tail: a long message puts its instruction first and its
        evidence after, and the evidence belongs to the model that is actually
        answering.

        The slice can land mid-marker — ``message_text`` renders an image part as
        ``[image: <media>]``, and cutting through it leaves a dangling
        ``[image...`` with no closing bracket, which is never legitimate content.

        **The cleanup never empties the result, and that guard is load-bearing.**
        A message can legitimately BEGIN with a bracket — a pasted log line, a
        Markdown link, ``[tool] output …`` — and for those the "dangling marker"
        runs from the first character to the end, so stripping it deletes the
        whole message. That is not a cosmetic loss: the caller reads an empty crop
        as "this message did not fit", moves on, and an OLDER message silently
        becomes the newest, making the preview describe a request the user did not
        just make. When the strip would leave nothing, the raw head is kept — a
        stray opening bracket on screen is a far smaller problem than describing
        the wrong request.
        """
        head = text[:limit]
        cleaned = _UNCLOSED_TRAILING_BRACKET.sub("", head).rstrip()
        return cleaned or head.rstrip()

    def cache_key(self, active_runbooks: list[Runbook]) -> str:
        """Groups requests sharing the same fast prefix, across sessions.

        Keyed on the PROMPT FAMILY and the runbook set rather than the session:
        the point is that two different conversations loading the same runbooks
        share a cacheable prefix, and a per-session key would defeat exactly that.
        Carries no session identity.
        """
        material = "|".join(sorted(r.content_hash for r in active_runbooks))
        digest = hashlib.sha256(f"fast-v2\0{material}".encode()).hexdigest()
        return f"fast-v2-{digest[:32]}"

    # -- generation ------------------------------------------------------

    async def generate(
        self,
        request: RespondRequest,
        *,
        active_runbooks: list[Runbook],
        request_id: str,
    ) -> FastResponseResult:
        """Call Cerebras once, under an absolute deadline. Never raises."""
        if self._provider is None:
            return FastResponseResult(reason="provider_unavailable", degraded=True)
        try:
            return await self._generate_inner(
                request, active_runbooks=active_runbooks, request_id=request_id
            )
        except asyncio.CancelledError:
            # The TURN was cancelled, not just this call. Cancellation belongs to
            # the request that owns it and must propagate.
            raise
        except Exception as exc:
            # The outer net. Prompt rendering, request construction, validation
            # and the final UTF-8 hash all live inside `_generate_inner`, and any
            # of them can raise on input we do not control — a lone surrogate in
            # the provider's reply reaches `.encode("utf-8")` and throws. None of
            # that may reach the turn.
            logger.info(
                "fast_response_failed",
                request_id=request_id,
                error_type=type(exc).__name__,
                code=getattr(exc, "code", None),
            )
            return FastResponseResult(degraded=True, reason="internal_error")

    async def _generate_inner(
        self,
        request: RespondRequest,
        *,
        active_runbooks: list[Runbook],
        request_id: str,
    ) -> FastResponseResult:
        # Narrowed by the caller, which returns early when there is no provider.
        assert self._provider is not None
        messages = self.build_messages(request, active_runbooks=active_runbooks)
        if messages is None:
            return FastResponseResult(degraded=True, reason="no_usable_context")

        provider_request = ProviderChatRequest(
            model=FAST_RESPONSE_MODEL,
            messages=messages,
            # No tools, no tool_choice, no response_format. The stage produces
            # prose and must not be able to produce anything else.
            tools=None,
            tool_choice=None,
            response_format=None,
            temperature=0.2,
            max_tokens=MAX_OUTPUT_TOKENS,
            stream=False,
            include_usage=True,
            # `gpt-oss-120b` reasons, and Cerebras defaults it to `medium`. The
            # adapter only forwards an effort when thinking is enabled, so these
            # two travel together: leaving `thinking_disabled=True` here would
            # drop the field and silently buy the default.
            thinking_disabled=False,
            reasoning_effort=FAST_RESPONSE_REASONING_EFFORT,
            timeout=self._settings.fast_response_timeout_seconds,
            cache_session_id=self.cache_key(active_runbooks),
        )

        started = time.perf_counter()
        try:
            # An ABSOLUTE deadline, not just the client's read timeout: a stalled
            # connect or a slow TLS handshake is invisible to a read timeout and
            # would leave a call running long past the window it could be shown in.
            async with asyncio.timeout(self._settings.fast_response_timeout_seconds):
                result = await self._provider.complete(provider_request)
        except asyncio.CancelledError:
            # The TURN was cancelled, not just this call. Propagate.
            raise
        except TimeoutError:
            return FastResponseResult(
                degraded=True,
                reason="timeout",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            # Deliberately broad, and deliberately not re-raised. Every provider
            # failure — transport, 4xx, 5xx, a malformed body — costs a preamble
            # and nothing else.
            logger.info(
                "fast_response_failed",
                request_id=request_id,
                error_type=type(exc).__name__,
                code=getattr(exc, "code", None),
            )
            return FastResponseResult(
                degraded=True,
                reason="provider_error",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        validation = validate_preamble(
            result.text,
            finish_reason=result.finish_reason,
            had_tool_calls=bool(result.tool_calls),
        )
        if validation.declined:
            # A SUCCESS with no output. `degraded` stays False and the source says
            # `declined`, so the rate at which the model chooses silence is
            # readable on its own rather than mixed into the failure count — it is
            # the number that says whether its judgement is working.
            logger.info(
                "fast_response_declined", request_id=request_id, latency_ms=latency_ms
            )
            return FastResponseResult(
                source="declined",
                reason="model_declined",
                usage=result.usage,
                latency_ms=latency_ms,
                invocation_id=f"inv_{request_id}_fast",
            )
        if not validation.valid:
            logger.info(
                "fast_response_rejected",
                request_id=request_id,
                reason=validation.reject_reason,
                latency_ms=latency_ms,
            )
            return FastResponseResult(
                degraded=True,
                reason=f"invalid_output:{validation.reject_reason}",
                usage=result.usage,
                latency_ms=latency_ms,
                invocation_id=f"inv_{request_id}_fast",
            )
        if _ANNOUNCEMENT_OPENING.match(validation.content):
            # Not a rejection — see the pattern's own note. This is the drift
            # signal for "the line went back to announcing", which is invisible in
            # every other metric because the turn looks fine.
            logger.info(
                "fast_response_announcement_register",
                request_id=request_id,
                # NO content. `observability.py` is the metadata-only plane by
                # contract — exact content belongs to `tracing/`, behind its own
                # config and its own access story. Logging even the first 40
                # characters would put model prose derived from a user's
                # conversation in front of everyone who can read a log line.
                content_chars=len(validation.content),
            )

        return FastResponseResult(
            content=validation.content,
            source="model",
            degraded=False,
            usage=result.usage,
            latency_ms=latency_ms,
            invocation_id=f"inv_{request_id}_fast",
            content_sha256=hashlib.sha256(validation.content.encode("utf-8")).hexdigest(),
        )


class PreambleGate:
    """The in-flight preamble call, and the one window it can still be shown in.

    Two calls now leave at the same moment and the fast one is expected to win by
    a wide margin — a few hundred milliseconds against the executor's prefill.
    Expected, not guaranteed, and this is what handles the other case.

    **The window closes at the executor's first VISIBLE byte, and never reopens.**
    Before that the screen is empty and a line describing the work is worth having.
    After it, the answer itself is arriving, and prefixing it with a preview of
    work the reader can already watch happening is worse than silence: it delays
    real output to say something that has stopped being true. So a preamble that
    arrives late is DROPPED, unshown — the call is billed and traced, and nothing
    is displayed.

    **A closed window does not cancel the call.** The request is already sent and
    already billed; cancelling buys nothing and loses the usage record. The task
    stays owned by the orchestrator's background set, and :meth:`finish` folds in
    whatever it reported if it lands before the turn ends.

    Polling is SYNCHRONOUS on purpose. It is called from inside the delta loop,
    between a chunk arriving and the delta it becomes reaching the client, and an
    await there would hand control back to the event loop in the one interval the
    turn is trying to keep tight.
    """

    def __init__(
        self,
        task: asyncio.Task[FastResponseResult] | None,
        policy: FastResponsePolicyDecision,
        *,
        request_id: str,
    ) -> None:
        self.task = task
        self.policy = policy
        self._request_id = request_id
        self._collected = task is None
        self._window_open = True
        self._final = False
        self.result: FastResponseResult | None = None
        #: The line was CLAIMED for display — the window closed in its favour.
        #: Not the same as having reached the client; see `shown`.
        self.claimed = False
        #: The `preamble` event was actually yielded. Set by the emitter AFTER
        #: the yield, never by `poll()`: a client that disconnects between the
        #: `status` event and the `preamble` event would otherwise be traced as
        #: `accepted=True` for a line it never received.
        self.shown = False
        self.drop_reason: str | None = None

    @property
    def pending(self) -> bool:
        """Whether an unsettled call is still worth waiting on."""
        return self.task is not None and not self._collected and self._window_open

    @property
    def usage(self) -> ProviderUsage | None:
        """What the call billed, or ``None`` for "nothing countable".

        ``None`` is ambiguous on its own — it covers both "the stage never ran"
        and "it ran and reported nothing" — which is why :attr:`billed_unknown`
        exists beside it rather than callers inferring one from the other.
        """
        return self.result.usage if self.result is not None else None

    @property
    def billed_unknown(self) -> bool:
        """A call that reached the provider and whose cost we cannot state.

        `turn_cost` reads `fast_response=None` as "the stage did not run", and
        an absent cost is then a truthful zero. That reading is WRONG for a
        request that went out and came back uncountable — a timeout (the host may
        have served and billed it anyway), a 200 with no usage block, or a call
        still in flight when the turn ended. Those are unknowns, and an unknown
        has to clear `complete` rather than be summed as zero.

        `latency_ms` is the discriminator because it is set exactly when a
        provider call was attempted: `provider_unavailable`, `no_usable_context`
        and a prompt-rendering failure never reach the wire and never bill.
        """
        if not self.policy.run or self.task is None:
            return False
        if self.usage is not None:
            return False
        if not self._collected:
            # Dispatched and still running when the turn ended.
            return True
        return self.result is not None and self.result.latency_ms is not None

    def _collect(self) -> None:
        """Read a FINISHED task exactly once. Never raises."""
        task = self.task
        if self._final or self._collected or task is None or not task.done():
            return
        self._collected = True
        if task.cancelled():
            # Not re-raised. The turn's OWN cancellation is delivered at its own
            # await points regardless, and raising here would convert an
            # optimisation's demise into the turn's.
            self.result = FastResponseResult(degraded=True, reason="cancelled")
            return
        error = task.exception()
        if error is not None:  # pragma: no cover - the service catches its own
            logger.info(
                "fast_response_failed",
                request_id=self._request_id,
                error_type=type(error).__name__,
            )
            self.result = FastResponseResult(degraded=True, reason="collect_failed")
            return
        self.result = task.result()

    def poll(self) -> FastResponseResult | None:
        """Settle a finished call and return it only if it may be SHOWN.

        ``None`` covers every reason there is nothing to display and they are not
        the same reason: the call is still running, it failed, the model declined,
        or shadow mode generated a real preamble that must never reach a client.

        Claiming and SHOWING are separate. This closes the window and hands the
        result over; :meth:`mark_shown` is what records that the event reached the
        wire, and only the emitter can know that.
        """
        if not self._window_open:
            return None
        self._collect()
        result = self.result
        if result is None or not result.has_content or not self.policy.visible:
            return None
        self.claimed = True
        self._window_open = False
        return result

    def mark_shown(self) -> None:
        """The `preamble` event has been yielded. Called only after the yield."""
        self.shown = True

    def close_window(self, reason: str) -> None:
        """The executor is on screen. Nothing this stage produces can be shown now.

        A drop is recorded only when the call's outcome is still UNKNOWN — which
        is what "it lost the race" actually means. A call that has already
        settled has its own reason for producing no line (the model declined, the
        provider failed, shadow mode withheld it), and attributing that to the
        race would corrupt the one number a rollback triggers on: every turn that
        showed a preamble also reaches here, and so does every turn that declined.
        """
        if not self._window_open:
            return
        self._window_open = False
        if self.task is None or self._collected:
            return
        self.drop_reason = reason
        logger.info("fast_response_dropped", request_id=self._request_id, reason=reason)

    def finish(self) -> FastResponseResult:
        """Settle for accounting, without waiting — and LATCH the answer.

        Deliberately does NOT await an unfinished task. It runs at the terminal
        event of a turn that is otherwise complete, and blocking that on an
        optimisation — for as long as `FAST_RESPONSE_TIMEOUT_SECONDS` — would let
        this stage delay the very thing it exists to make feel fast.

        **The first call is final, and that is the point.** It is called twice per
        turn — once before the `done` event, which carries the cost to the client,
        and once in the terminal `finally`, which writes the trace and the usage
        log. Without the latch a task finishing in between (during the compaction
        await, or while `done` is suspended at its own yield) would be summed into
        the log but not into `done`, and the turn would report two different
        totals. Losing usage that arrived microseconds too late is the cheaper
        error, and it is not silent: `billed_unknown` stays True, which clears
        `complete` on the reported cost.
        """
        self._collect()
        self._final = True
        if self.result is not None:
            return self.result
        # No result and nothing left that can produce one. `unsettled` is the
        # honest reason for a dispatched call that never landed; the policy's own
        # reason belongs only to a stage that never started.
        reason = self.policy.reason if self.task is None else "unsettled"
        self.result = FastResponseResult(source="none", reason=reason or "not_eligible")
        return self.result
