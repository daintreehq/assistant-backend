"""Reading the ``Authorization`` header — strictly for the door, leniently for redaction.

**Two functions with opposite postures, and the split is the whole module.**

:func:`parse_bearer` is the SECURITY parser. It decides whether a credential was
presented, and it is unforgiving: exactly one header, exactly one credential,
exactly the characters a bearer token may contain. Anything else is refused
without interpretation, because interpreting a malformed credential is how one
implementation ends up disagreeing with another about what was sent.

:func:`credential_candidates` is the REDACTION extractor, and it must never refuse
anything. Its job is to say "these bytes might be a secret" so nothing downstream
copies them into a log line or a request id. A value the strict parser rejects is
*more* likely to hold a mis-pasted credential, not less — somebody put their token
in the wrong place — so refusing to look at it is exactly backwards. It cannot
raise, and it deliberately over-collects.

**Both read every raw header, and that is not a detail.** Starlette's
``Headers.get`` returns the first value and ``dict(scope["headers"])`` keeps the
last, so with two ``Authorization`` headers each of them silently drops one. For
the strict parser that would mean validating a different credential than the one
a proxy forwards; for redaction it would mean a token in the second header
reaching the logs untouched. Neither is recoverable afterwards, so the raw list is
the only input either function accepts.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

#: Header field values are ISO-8859-1 by RFC 7230, and every byte decodes under
#: it — which is what makes redaction total. A token with a byte outside the
#: bearer charset is refused by the strict parser below on the character rule,
#: not by a decode that threw before anything could look at it.
_HEADER_ENCODING = "iso-8859-1"

#: RFC 6750's ``b64token`` body charset — which already includes ``.``, the two
#: separators in a JWT's compact serialization. Anything else (a space, a comma, a
#: control character, a non-ASCII byte) makes the value malformed rather than a
#: token to salvage.
#:
#: ``+``, ``/`` and ``=`` are here because the RFC's grammar has them, not because
#: a JWT needs them: compact JWS is base64URL (``-`` and ``_``) and unpadded. They
#: are harmless — this function decides only whether a credential was PRESENTED,
#: and the verifier that follows enforces the far narrower shape of a real token.
_BEARER_BODY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~+/"
)

#: A generous ceiling on the WHOLE ``Authorization`` value, scheme included — not
#: on the credential alone. Named for what it measures, because the two differ by
#: seven characters and a limit whose subject is ambiguous gets tested against the
#: wrong thing. A Supabase access token is on the order of a kilobyte; this leaves
#: room for a fat claim set and still refuses a header being used as a payload.
#:
#: Checked FIRST, before any scan or strip, so an enormous value costs one length
#: comparison rather than a walk over megabytes.
MAX_AUTHORIZATION_VALUE_LENGTH = 8192

#: Authentication scheme names, which appear in the header and are not secrets.
#: Filtered out of the redaction candidates: without this, a scheme-only header
#: (``Authorization: Bearer``) makes the literal word ``Bearer`` "credential
#: material", and a perfectly ordinary request id containing it is thrown away.
_SCHEME_WORDS = frozenset({"bearer", "basic", "digest", "token", "apikey", "negotiate"})


class BearerOutcome(Enum):
    """What the ``Authorization`` header was, as a closed set.

    ``WELL_FORMED`` is named for exactly what it means and deliberately not ``OK``.
    Nothing here has been verified: the signature, the issuer, the audience and the
    expiry are all still unknown, and a value invented by anyone at all reaches this
    outcome. At an authentication seam a field called ``ok`` is read as "this caller
    is fine", which is the one thing it does not say.

    ``ABSENT`` and ``MALFORMED`` are different because their remedies are. Note the
    boundary deliberately: ``MALFORMED`` means **a header arrived and could not be
    read as a credential**, which includes an empty one and a bare scheme. Those
    carry no credential material, but they are not ``ABSENT`` — the client did try,
    and telling it "no credential was sent" would send someone to a sign-in when
    their token store handed the request an empty string. "What you sent is
    unusable" is both true and more useful, so every present-but-unreadable header
    maps to ``auth_token_invalid`` rather than ``auth_required``.
    """

    WELL_FORMED = "well_formed"
    ABSENT = "absent"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class BearerResult:
    """The parse, plus a reason that is for us rather than for the caller.

    ``reason`` names the specific rule that failed and is intended for a log line
    beside the request id. It must NOT reach the response: which check a rejected
    credential failed is diagnostic detail about something we did not accept, and
    handing it back turns the door into an oracle for what this deployment takes.
    """

    outcome: BearerOutcome
    #: Excluded from ``repr`` so the credential cannot reach a log line, a
    #: traceback or an exception message by way of somebody printing the result.
    #: Everything about this module is careful not to echo the token; a default
    #: dataclass repr would undo all of it in one line.
    token: str | None = field(default=None, repr=False)
    reason: str = ""

    @property
    def well_formed(self) -> bool:
        """Whether a syntactically valid credential was presented.

        Says nothing about whether it is genuine. Named to make that hard to
        misread at the call site.
        """
        return self.outcome is BearerOutcome.WELL_FORMED


def authorization_values(raw_headers: Iterable[tuple[bytes, bytes]]) -> list[str]:
    """Every ``Authorization`` value in an ASGI scope's raw header list, in order.

    Order is preserved because "more than one" is itself the finding: which of two
    headers a proxy would forward is not something this server can know, so it
    refuses rather than picking.
    """
    return [
        value.decode(_HEADER_ENCODING, errors="replace")
        for name, value in raw_headers
        if name.lower() == b"authorization"
    ]


def credential_candidates(values: Sequence[str]) -> list[str]:
    """Substrings of the header that might be credential material. Never raises.

    Over-collects on purpose, and the shape of the over-collection is the point.
    The whole value alone is not enough: a proxy-folded header

        Authorization: Bearer first-credential, Bearer second-credential

    yields one long candidate that neither contains nor is contained in a request
    id like ``trace-second-credential`` — so substring matching finds nothing and
    the credential is adopted, logged and echoed. Substring matching only works
    once the credential has been ISOLATED, so this splits the value into its
    fields first: on commas (the folded form), then on any whitespace run (which
    covers a tab-separated or otherwise malformed header the strict parser
    refuses).

    Scheme words are dropped. They are in the header and are not secrets, and
    keeping them means a scheme-only header turns the literal word ``Bearer`` into
    "credential material" and discards any request id containing it.

    The output is only ever compared against — never logged, never echoed — so a
    candidate that turns out not to be a secret costs nothing, while a missed one
    costs a credential in the log pipeline.
    """
    candidates: list[str] = []

    def add(candidate: str) -> None:
        cleaned = candidate.strip()
        if not cleaned or cleaned.lower() in _SCHEME_WORDS:
            return
        if cleaned not in candidates:
            candidates.append(cleaned)

    for value in values:
        add(value)
        for segment in value.split(","):
            add(segment)
            # `split()` with no argument splits on any whitespace RUN, so a
            # tab-separated or double-spaced header yields the same fields as a
            # well-formed one. That is deliberate: the more malformed the header,
            # the more likely it holds a credential somebody put in the wrong
            # place, and the less this can afford to be picky.
            for word in segment.split():
                add(word)
    return candidates


def parse_bearer(values: Sequence[str]) -> BearerResult:
    """Strictly interpret the ``Authorization`` header(s) as one bearer credential.

    Every refusal below closes a way two implementations could read one header
    differently:

    * **More than one header.** Which one a proxy forwards is not knowable here,
      so validating either would be validating a credential the upstream might not
      have seen. Refused rather than resolved.
    * **A comma in the value.** RFC 7235 lets a proxy fold repeated headers into
      one comma-separated value, so a comma is the folded form of the case above
      wearing a different shape. Refusing it keeps the two indistinguishable
      cases indistinguishable.
    * **Anything but exactly two space-separated parts.** ``Bearer`` alone has no
      credential; ``Bearer a b`` has two, and picking the first would accept a
      request whose meaning depends on the reader.
    * **A character outside the bearer charset.** Control characters, non-ASCII
      bytes and internal whitespace are all refused here rather than normalised —
      trimming or stripping means the value validated and the value used can
      differ.

    The scheme comparison is case-insensitive (RFC 7235 says schemes are), the
    credential is not.
    """
    if not values:
        return BearerResult(BearerOutcome.ABSENT, reason="no_authorization_header")
    if isinstance(values, str):
        # A bare `str` is a `Sequence[str]`, so this type-checks and then iterates
        # CHARACTERS. RAISED rather than normalised, and that is the opposite
        # choice from `main._safe_request_id` on purpose: there, a throw would take
        # down correlation for every request carrying a header, so a bad call has
        # to degrade. Here a bad call means somebody reached for a single-value
        # accessor — the exact bug this module exists to prevent — and it must be
        # impossible to ship rather than quietly survivable.
        raise TypeError(
            "parse_bearer takes every Authorization value, not one string — "
            "use services.bearer.authorization_values(scope['headers'])"
        )
    if len(values) > 1:
        return BearerResult(BearerOutcome.MALFORMED, reason="multiple_authorization_headers")

    raw = values[0]
    # First, before any scan or strip: an oversized value must cost one comparison.
    if len(raw) > MAX_AUTHORIZATION_VALUE_LENGTH:
        # Never echoed, and the length is not reported either — an oversized value
        # is still credential material.
        return BearerResult(BearerOutcome.MALFORMED, reason="authorization_header_too_long")
    if not raw.strip():
        return BearerResult(BearerOutcome.MALFORMED, reason="empty_authorization_header")
    if "," in raw:
        return BearerResult(BearerOutcome.MALFORMED, reason="comma_in_authorization_header")
    if raw != raw.strip():
        # Refused rather than trimmed, so the value checked and the value used are
        # the same string. Trimming is how one implementation validates ` tok ` and
        # another sends `tok`.
        return BearerResult(BearerOutcome.MALFORMED, reason="padded_authorization_header")

    scheme, separator, token = raw.partition(" ")
    if not separator:
        return BearerResult(BearerOutcome.MALFORMED, reason="malformed_authorization_header")
    if scheme.lower() != "bearer":
        return BearerResult(BearerOutcome.MALFORMED, reason="unsupported_authorization_scheme")
    # RFC 7235 separates scheme from credential with `1*SP`, so a run of spaces is
    # legal syntax. Only SPACE, though: a tab is not `SP`, and it survives as an
    # invalid character below rather than being read as a separator.
    token = token.lstrip(" ")
    # No `if not token` here: reaching this line with an empty token would need the
    # raw value to end in spaces, and the padding check above already refused that.
    # The empty case is owned by the padding-grammar check below, which is the one
    # that can still see it (a credential of nothing but `=`).
    if " " in token:
        return BearerResult(BearerOutcome.MALFORMED, reason="malformed_authorization_header")

    # `b64token` is `1*( body ) *"="`: at least one real character, and `=` only as
    # trailing padding. A set-membership test alone accepts `=`, `==a` and `a=b` —
    # a credential of pure padding, and padding in the middle of one.
    body = token.rstrip("=")
    if not body:
        return BearerResult(BearerOutcome.MALFORMED, reason="empty_bearer_credential")
    if not set(body) <= _BEARER_BODY_CHARS:
        return BearerResult(BearerOutcome.MALFORMED, reason="invalid_bearer_character")
    return BearerResult(BearerOutcome.WELL_FORMED, token=token)
