"""Paired hub/lane scenarios: structural checks and the report.

A hosts scenario is one piece of work seen from both ends. The person asks the hub
(a sidebar assistant with Remote Hosts) in their own words; the hub briefs the
assistant on the host (a lane); the lane searches for the work the brief names, and
follows the runbook that comes back. Retrieval has to land on the right runbook at
BOTH ends, and a wrong-role runbook (a hub baseline delivered to a lane that cannot
execute it) is a failure however well the right one ranked.

The cases are ordinary ``cases/*.yaml`` cases with ``scenario`` and ``role`` (see
``cases.py``), so ``bench local`` scores them with everything else. This module is
what ordinary cases do not get:

``validate_pairs``
    Run on every case load. A scenario must have both ends, a successful selection at
    each, and a deliberately wrong-role trap; a lane success carries the hub's emitted
    ``brief`` (objective, scope, report and stop boundaries, small, no runbook pasted
    in); queries are shaped as the app prescribes (one sentence of 8-15 words, never
    a runbook id) and the lane's query is drawn from its brief.

``print_pairs``
    The report ``bench pairs`` prints: per scenario and end, pass or fail, what was
    actually loaded, which stage lost any missing runbook, and any forbidden load.

Why the app's query shape matters: the help session's runbook rule (``Runbooks
First``, ``electron/services/helpSessionRunbooks.ts``) tells the assistant to search
with one sentence of 8-15 words in the user's voice, specifics left out. A pasted
brief or a query that names a runbook id is not how either end searches, so passing
on one would prove nothing about discovery.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .cases import BenchCase

#: The app's own wake line prefix. A first turn that is only this line is the
#: lane's entry, and is exempt from the one-sentence query shape.
WAKE_LINE_PREFIX = "Daintree: assistant inbox"
#: The whole line, as the app formats it (``formatLaneWakeLine``): a first turn that only
#: starts like it is not exempt from the query shape.
WAKE_LINE = re.compile(
    r"^Daintree: assistant inbox has (a new message|\d+ new messages) "
    r"\(exchanges? [A-Za-z0-9, -]+\)\. Read (it|them) with assistant\.getInbox\.$"
)
_SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")

#: The cases that assert a wrong-role or wrong-owner answer is NOT given. A ``no-task``
#: case is one where the right answer is that nothing needs loading.
TRAP_CATEGORIES = ("routing-trap", "no-task")

#: What a brief carries: the objective with its deliverable, the scope it may touch,
#: what to report (the output), and where to stop and ask back.
BRIEF_LABELS = ("Objective", "Scope", "Report", "Stop")
#: A brief is a short message by reference. A runbook pasted in to compensate for
#: routing is the failure this ceiling exists to catch.
BRIEF_MAX_CHARS = 900
QUERY_WORDS = (8, 15)

_RUNBOOK_ID = re.compile(r"\bdaintree\.[a-z0-9-]+(?:\.[a-z0-9-]+)*", re.I)
_PROCEDURE_MARKERS = ("on notice or resume", "stop and recovery", "report back:")
_WORD = re.compile(r"[a-z][a-z-]{3,}")
_STOPWORDS = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "have",
        "into",
        "your",
        "their",
        "them",
        "then",
        "than",
        "they",
        "what",
        "when",
        "where",
        "which",
        "would",
        "should",
        "could",
        "while",
        "will",
        "been",
        "being",
        "does",
        "done",
        "only",
        "also",
        "each",
        "over",
        "such",
    ]
)
#: Content words a lane's query must share with its brief's objective: enough that the
#: query was drawn from the brief, few enough that rewording it is not penalised.
MIN_SHARED_WORDS = 3


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


def _query(case: BenchCase) -> str:
    return " ".join(m.content or "" for m in case.messages if m.role == "user")


def brief_problems(brief: str) -> list[str]:
    """What is wrong with an emitted hub brief, as sentences; empty when it is sound."""
    problems = [
        f"no '{label}:' line"
        for label in BRIEF_LABELS
        if not re.search(rf"^[ \t]*{label}:[ \t]*\S", brief, re.M)
    ]
    if len(brief) > BRIEF_MAX_CHARS:
        problems.append(f"{len(brief)} chars, over {BRIEF_MAX_CHARS}: a brief is by reference")
    if _RUNBOOK_ID.search(brief):
        problems.append("names a runbook id: the brief says what the work is, not where it lives")
    if any(marker in brief.lower() for marker in _PROCEDURE_MARKERS):
        problems.append("carries runbook procedure text")
    return problems


def objective_line(brief: str) -> str:
    match = re.search(r"^[ \t]*Objective:[ \t]*(.+)$", brief, re.M)
    return match.group(1) if match else ""


def validate_pairs(cases: list[BenchCase]) -> list[str]:
    """Every structural problem across the scenarios, one sentence each."""
    problems: list[str] = []
    scenarios: dict[str, list[BenchCase]] = defaultdict(list)
    for case in cases:
        if case.scenario is not None:
            scenarios[case.scenario].append(case)

    for name, members in sorted(scenarios.items()):
        successes = {
            role: [c for c in members if c.role == role and c.category not in TRAP_CATEGORIES]
            for role in ("hub", "lane")
        }
        for role, found in successes.items():
            if not any(c.required for c in found):
                problems.append(f"{name}: no successful selection at the {role} end")
        if not any(c.category in TRAP_CATEGORIES for c in members):
            problems.append(f"{name}: no wrong-role routing trap")
        if not any(c.brief for c in successes["lane"]):
            problems.append(f"{name}: no lane case carries the hub's emitted brief")

        for case in members:
            label = f"{name}/{case.id}"
            query = _query(case)
            wake = bool(WAKE_LINE.match(query))
            if query.startswith(WAKE_LINE_PREFIX) and not wake:
                problems.append(f"{label}: starts like the wake line but is not the app's line")
            if len(case.messages) != 1 or case.messages[0].role != "user":
                problems.append(f"{label}: one user message, as one search is one sentence")
            if _RUNBOOK_ID.search(query):
                problems.append(f"{label}: the query names a runbook id, which proves no discovery")
            count = len(query.split())
            low, high = QUERY_WORDS
            if not wake and not low <= count <= high:
                problems.append(f"{label}: query is {count} words, the app asks for {low}-{high}")
            if not wake and _SENTENCE_BREAK.search(query):
                problems.append(f"{label}: query is more than one sentence")
            if wake and case.role != "lane":
                problems.append(f"{label}: a wake line is the lane's first turn, not the hub's")
            if case.brief is not None:
                for problem in brief_problems(case.brief):
                    problems.append(f"{label}: brief {problem}")
                shared = _words(query) & _words(objective_line(case.brief))
                if len(shared) < MIN_SHARED_WORDS:
                    problems.append(
                        f"{label}: query shares {len(shared)} content words with the brief's "
                        f"objective, needs {MIN_SHARED_WORDS}: it must be drawn from the brief"
                    )
    return problems


def pair_cases(cases: list[BenchCase]) -> list[BenchCase]:
    return [c for c in cases if c.scenario is not None]


def print_pairs(doc: dict[str, Any], cases: list[BenchCase]) -> int:
    """Print the per-scenario report from a ``bench local`` doc; the number of failures."""
    index = {cid: i for i, cid in enumerate(doc["case_ids"])}
    lost: dict[str, list[str]] = defaultdict(list)
    for loss in doc.get("losses") or []:
        p = "-" if loss["p"] is None else f"{loss['p']:.3f}"
        lost[loss["case"]].append(f"{loss['runbook']} ({loss['stage']}, p={p})")
    wrong = doc.get("forbidden") or []

    by_scenario: dict[str, list[BenchCase]] = defaultdict(list)
    for case in cases:
        by_scenario[str(case.scenario)].append(case)
    failures = 0
    print(f"== paired role cases: {len(by_scenario)} scenarios, {len(cases)} cases ==")
    for name, members in sorted(by_scenario.items()):
        print(f"\n{name}")
        for case in sorted(
            members, key=lambda c: (c.role != "hub", c.category in TRAP_CATEGORIES, c.id)
        ):
            i = index[case.id]
            ok = doc["recall_pass"][i]
            failures += not ok
            kind = "trap" if case.category in TRAP_CATEGORIES else "pick"
            print(f"  {'ok  ' if ok else 'FAIL'} {str(case.role):4} {kind}  {case.id}")
            print(f"         loaded {doc['picked'][i]}")
            if lost.get(case.id):
                print(f"         lost   {', '.join(lost[case.id])}")
            if i < len(wrong) and wrong[i]:
                print(f"         FORBIDDEN LOAD {wrong[i]}")
    print(f"\n{len(cases) - failures}/{len(cases)} paired cases pass")
    return failures
