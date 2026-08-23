"""Typed secret redaction for TRAINING DERIVATIVES, not for raw traces.

The distinction is the whole design. A raw trace is the restricted original and is
preserved exactly — "repairing" it would destroy the record it exists to be, and a
redactor that ran at capture time would silently rewrite evidence. Redaction runs
on the way OUT, when a trace becomes a training example, and the raw trace stays
as it was.

Replacements are TYPED and KEYED: ``<REDACTED:API_KEY:h_8f24>``. The type keeps the
example learnable — a model should still see that a credential was there — and the
keyed suffix lets two occurrences of the same secret be recognised as the same
within an approved dataset without exposing it. The key is what stops the suffix
being a dictionary attack: an unkeyed hash of a short secret is trivially
reversible, and a redaction that can be undone is not one.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any

#: Deliberately conservative and anchored on STRUCTURE rather than on entropy.
#: An entropy heuristic flags base64 blobs, git SHAs and UUIDs — all of which are
#: ordinary content here — and missing a real key while shredding a commit hash
#: is the worst of both outcomes.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
        ),
    ),
    ("API_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("API_KEY", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("API_KEY", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("API_KEY", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("BEARER", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}")),
    ("COOKIE", re.compile(r"(?i)\b(?:set-)?cookie:\s*[^\r\n]{8,}")),
    ("ENV_SECRET", re.compile(
        r"(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY)[A-Z0-9_]*\s*[=:]\s*"
        r"['\"]?[^\s'\"]{8,}"
    )),
)


@dataclass(frozen=True)
class Redaction:
    """Where a secret was, and what replaced it. Provenance, not the secret."""

    type: str
    start_byte: int
    end_byte: int
    replacement: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "replacement": self.replacement,
        }


@dataclass(frozen=True)
class RedactionResult:
    text: str
    redactions: tuple[Redaction, ...]

    @property
    def count(self) -> int:
        return len(self.redactions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transformation": "typed_redaction",
            "redactions": [r.as_dict() for r in self.redactions],
        }


def _tag(key: bytes, kind: str, secret: str) -> str:
    digest = hmac.new(key, f"{kind}\0{secret}".encode(), hashlib.sha256).hexdigest()
    # 8 hex characters, not 4. The suffix exists so an analyst can tell "this is
    # the same credential again" within an approved dataset; at 16 bits a corpus
    # of a few thousand distinct secrets collides by birthday and starts asserting
    # sameness that is not there. 32 bits is still far too short to attack and
    # long enough to be trusted.
    return f"<REDACTED:{kind}:h_{digest[:8]}>"


def redact(text: str, *, key: bytes) -> RedactionResult:
    """Replace recognised secrets, preserving byte offsets in the provenance.

    Offsets are recorded against the ORIGINAL text, so a reviewer holding the raw
    trace can find exactly what was removed without the derivative carrying it.
    Overlapping matches from different patterns are UNIONED rather than resolved
    first-wins, so an overlap can only ever redact more than one pattern found,
    never less. See the comment on the grouping loop for the leak that rule
    exists to prevent.
    """
    raw_spans: list[tuple[int, int, str]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            raw_spans.append((match.start(), match.end(), kind))

    # OVERLAP GROUPS, unioned — not first-wins.
    #
    # The obvious version sorts by (start, -length) and skips anything starting
    # before the cursor. That silently loses a secret: a SHORT match that begins
    # earlier and overlaps a LONGER one suppresses it entirely. Concretely,
    # `MY_SECRET=-----BEGIN RSA PRIVATE KEY-----…` matches ENV_SECRET at offset 0
    # and PRIVATE_KEY at offset 10; the short match wins, and the whole key body
    # then survives into the training derivative — the exact failure this module
    # exists to prevent, wearing the shape of a passing test.
    #
    # Taking the union always redacts AT LEAST the bytes any pattern matched, so
    # an overlap can only ever redact more, never less.
    raw_spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    spans: list[tuple[int, int, str, str]] = []
    for start, end, kind in raw_spans:
        if spans and start < spans[-1][1]:
            prev_start, prev_end, prev_kind, _ = spans[-1]
            merged_end = max(prev_end, end)
            # Name the group after whichever single match covered the most — the
            # more specific finding, and the one a reviewer wants to see.
            winner = prev_kind if (prev_end - prev_start) >= (end - start) else kind
            spans[-1] = (prev_start, merged_end, winner, text[prev_start:merged_end])
            continue
        spans.append((start, end, kind, text[start:end]))

    out: list[str] = []
    redactions: list[Redaction] = []
    cursor = 0
    # Byte offsets accumulated INCREMENTALLY. The obvious spelling —
    # `len(text[:start].encode())` per match — re-slices and re-encodes the whole
    # prefix every time, which is quadratic in the number of redactions. Measured
    # on a secret-dense blob it was already 0.18s at 256KB, and this module runs
    # over blobs capped at 8 MiB.
    byte_cursor = 0
    # No overlap guard here: the grouping loop above merged every overlapping
    # match, so `spans` is non-overlapping and ascending by construction. A
    # `start < cursor` skip would be dead code, and dead code in a security
    # boundary is worse than absent — it reads as a protection that is running.
    for start, end, kind, secret in spans:
        replacement = _tag(key, kind, secret)
        gap = text[cursor:start]
        out.append(gap)
        out.append(replacement)
        start_byte = byte_cursor + len(gap.encode("utf-8"))
        end_byte = start_byte + len(secret.encode("utf-8"))
        redactions.append(
            Redaction(
                type=kind,
                start_byte=start_byte,
                end_byte=end_byte,
                replacement=replacement,
            )
        )
        byte_cursor = end_byte
        cursor = end
    out.append(text[cursor:])
    return RedactionResult(text="".join(out), redactions=tuple(redactions))
