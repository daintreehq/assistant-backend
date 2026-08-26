"""The deploy scripts are executable, so they get an executable gate.

`infra/gcp/bootstrap.sh` shipped with two failures in one seven-line block: it
referenced `$DEPLOY_SA_EMAIL`, which nothing defines, and called `warn`, which is
not a function in that file. Under `set -euo pipefail` the first kills the script
in the `if` branch and the second kills it in the `else` branch — so the block
could not complete either way, and the one-time GCP setup died partway through
whichever path a project happened to take.

Nothing caught it because `bash -n` only parses: an undefined variable and an
undefined command are both syntactically perfect. CI runs no GCP project to
execute these against and never will, so a static check is the available gate,
and these two failure classes are exactly the ones static analysis can settle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((REPO_ROOT / "infra" / "gcp").glob("*.sh"))

#: Variables the shell or the environment supplies. Kept short and explicit —
#: a long allowlist is how a real undefined-variable bug gets excused.
_AMBIENT_VARS = frozenset(
    {
        "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
        "@", "*", "#", "?", "$", "!", "-", "_",
        "HOME", "PATH", "PWD", "USER", "SHELL", "IFS", "LINENO", "BASH_SOURCE",
        "FUNCNAME", "PIPESTATUS", "RANDOM", "SECONDS", "OSTYPE", "HOSTNAME",
        "CLOUDSDK_CORE_PROJECT", "GOOGLE_CLOUD_PROJECT",
    }
)

_ASSIGNMENT = re.compile(r"(?:^|[\s;(])([A-Za-z_][A-Za-z0-9_]*)=")
_FOR_LOOP = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
#: `read -r NAME`, `read -r -d '' NAME <<EOF`, `read A B C`.
_READ_INTO = re.compile(r"\bread\b((?:\s+-[A-Za-z]+(?:\s+'[^']*')?)*)((?:\s+[A-Za-z_][A-Za-z0-9_]*)+)")
_BARE_LOCAL = re.compile(r"\b(?:local|declare|readonly)\s+((?:[A-Za-z_][A-Za-z0-9_]*\s*)+)$")
_FUNC_DEF = re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*\(\)\s*\{")
#: `${NAME}`, `${NAME:-default}`, `$NAME`. The operator is captured so the check
#: below can tell a DEFAULT-supplying form from every other expansion — only the
#: former is safe under `set -u`. `${MISSING#prefix}`, `${MISSING/a/b}` and
#: `${MISSING:1}` all still abort, and an earlier version of this regex excused
#: them by treating "any operator" as "has a default".
_VAR_USE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)((?::[-=?+])|[-=?+])?[^}]*\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def _defined_vars(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        for match in _ASSIGNMENT.finditer(line):
            names.add(match.group(1))
        for match in _FOR_LOOP.finditer(line):
            names.add(match.group(1))
        for match in _READ_INTO.finditer(line):
            names.update(match.group(2).split())
        match = _BARE_LOCAL.search(line.strip())
        if match:
            names.update(match.group(1).split())
    return names


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_script_reads_a_variable_nothing_defines(script: Path) -> None:
    """`$DEPLOY_SA_EMAIL` under `set -u` aborts the script.

    Uses with an explicit default (`${X:-}`) are fine and excused — that form is
    how a script legitimately reads something optional.
    """
    text = script.read_text(encoding="utf-8")
    if "set -u" not in text and "set -euo" not in text:
        pytest.skip(f"{script.name} does not run under `set -u`")
    defined = _defined_vars(text) | _AMBIENT_VARS

    unknown: list[str] = []
    for match in _VAR_USE.finditer(text):
        name = match.group(1) or match.group(3)
        has_default = bool(match.group(2))
        if name in defined or has_default:
            continue
        unknown.append(name)

    assert not unknown, (
        f"{script.name} reads {sorted(set(unknown))} but nothing assigns them. Under "
        "`set -u` that aborts the script at that line. Either define the variable or "
        "use the `${NAME:-default}` form if it is genuinely optional."
    )


#: Names that are helpers SOMEWHERE in this directory, plus the ones an author
#: reaches for by reflex. Deliberately a closed vocabulary rather than "every
#: word in command position": deciding whether an arbitrary word is a command
#: needs a shell parser — these scripts nest quotes inside `$( )` inside inline
#: `python3 -c` strings, and a scanner that guesses reports the embedded
#: language's keywords. This list decides nothing it cannot decide correctly.
_REFLEX_HELPERS = frozenset({"warn", "die", "fail", "info", "note", "err", "error", "abort"})


def _helper_vocabulary() -> frozenset[str]:
    defined: set[str] = set()
    for script in SCRIPTS:
        for line in script.read_text(encoding="utf-8").splitlines():
            match = _FUNC_DEF.match(line)
            if match:
                defined.add(match.group(1))
    return frozenset(defined | _REFLEX_HELPERS)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_script_calls_a_log_helper_it_never_defines(script: Path) -> None:
    """`warn` was never defined; under `set -e` the call kills the script.

    Scoped to the helper vocabulary above, and honest about the scope: this
    catches an author reaching for a sibling script's helper, or for the obvious
    name a file happens not to have — which is exactly what happened. It does
    NOT catch a misspelled external command, and it is not trying to.
    """
    text = script.read_text(encoding="utf-8")
    defined = {m.group(1) for m in (_FUNC_DEF.match(line) for line in text.splitlines()) if m}
    vocabulary = _helper_vocabulary() - defined

    called: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or _FUNC_DEF.match(raw):
            continue
        for keyword in ("if ", "elif ", "then ", "else ", "do ", "while ", "until ", "! "):
            if line.startswith(keyword):
                line = line[len(keyword) :].strip()
        word = line.split()[0] if line.split() else ""
        if word in vocabulary:
            called.append((number, word))

    assert not called, (
        f"{script.name} calls "
        + ", ".join(f"{word!r} (line {number})" for number, word in called)
        + " — a helper it does not define. Under `set -e` an undefined command "
        "aborts the script; if the helper belongs here, define it."
    )


# ---------------------------------------------------------------------------
# Negative controls. Without these, both analyzers above could be broken in a
# way that makes them report nothing, and the suite would go green — the exact
# failure mode of a gate that scans real files and finds them clean.
# ---------------------------------------------------------------------------


def test_there_are_scripts_to_check() -> None:
    """A glob that matches nothing passes every parametrized case vacuously."""
    assert SCRIPTS, "infra/gcp/*.sh matched no files — the checks above ran on nothing"


def test_the_variable_analyzer_diagnoses_a_known_bad_script(tmp_path) -> None:
    """The `$DEPLOY_SA_EMAIL` shape, reproduced, plus the forms it must NOT flag."""
    script = tmp_path / "bad.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nGOOD=1\necho \"$GOOD $MISSING\"\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="MISSING"):
        test_no_script_reads_a_variable_nothing_defines(script)

    ok = tmp_path / "ok.sh"
    ok.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        # Every assignment form the analyzer has to understand, or it reports a
        # false positive on a correct script — which is how a gate gets muted.
        'A=1\nlocal_like="x"\nfor ITEM in a b; do echo \"$ITEM\"; done\n'
        'read -r FROM_READ <<< \"x\"\nB=$(echo 2)\n'
        'echo \"$A $local_like $FROM_READ $B ${OPTIONAL:-fallback}\"\n',
        encoding="utf-8",
    )
    test_no_script_reads_a_variable_nothing_defines(ok)


def test_the_variable_analyzer_does_not_excuse_a_non_default_expansion(tmp_path) -> None:
    """`${MISSING#prefix}` aborts under `set -u` just like a bare `$MISSING`.

    This is a real hole the first version of the regex had: it captured any
    operator character and treated its presence as "a default was supplied", so
    every string-manipulation expansion of an undefined variable read as safe.
    """
    script = tmp_path / "trim.sh"
    script.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\necho \"${MISSING#prefix}\"\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="MISSING"):
        test_no_script_reads_a_variable_nothing_defines(script)


def test_the_helper_analyzer_diagnoses_a_known_bad_script(tmp_path) -> None:
    """The `warn` shape, reproduced — and a script that defines it passing."""
    bad = tmp_path / "bad.sh"
    bad.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nok() { echo "$*"; }\nwarn "oh no"\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="warn"):
        test_no_script_calls_a_log_helper_it_never_defines(bad)

    good = tmp_path / "good.sh"
    good.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nwarn() { echo "$*" >&2; }\nwarn "fine"\n',
        encoding="utf-8",
    )
    test_no_script_calls_a_log_helper_it_never_defines(good)


# ---------------------------------------------------------------------------
# Publishing the service is a CUTOVER, not a side effect of finishing a setup.
#
# `bootstrap.sh` used to grant `allUsers roles/run.invoker` on every run —
# ungated, unprompted, on a script whose own header says re-running it is how you
# finish the setup. Since the server holds the upstream credential and funds
# every turn from it, that published an open Daintree-funded model endpoint. The
# binding is per-SERVICE, so it also published every no-traffic release
# candidate's tag URL.
#
# These are static checks because CI runs no GCP project and never will. They are
# worth having anyway: what they assert is an ORDER, and an order is exactly what
# a well-meaning edit reverses.
# ---------------------------------------------------------------------------

BOOTSTRAP = REPO_ROOT / "infra" / "gcp" / "bootstrap.sh"


def _command_lines(text: str) -> list[tuple[int, str]]:
    """Lines that run something, rather than comment or print it.

    `echo`/`printf`/`ok` are excluded because this file DOCUMENTS the commands it
    declines to run — an operator is shown the revoke command and the cutover
    invocation — and a scanner that could not tell an instruction from an
    example would force the documentation out.
    """
    lines = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("echo ", "printf ", "ok ", "say ")):
            continue
        lines.append((number, raw))
    return lines


def test_bootstrap_does_not_publish_the_service_as_a_side_effect() -> None:
    """The grant must sit behind the named cutover AND behind a posture check.

    Typing the service name says "I mean to do this". It does not say the
    revision in front of us is the one that was supposed to receive it, and those
    two come apart during exactly the rollout this exists for — a cutover run
    against yesterday's revision, or against staging while thinking of
    production. So the posture is read back from the deployment before the
    binding is created.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    grants = [
        (number, line)
        for number, line in _command_lines(text)
        if "--member=allUsers" in line
    ]
    assert len(grants) == 1, (
        f"expected exactly one allUsers grant in bootstrap.sh, found {len(grants)}"
    )
    grant_line = grants[0][0]
    preceding = "\n".join(text.splitlines()[: grant_line - 1])
    assert 'PUBLIC_CUTOVER" != "$SERVICE"' in preceding, (
        "the public grant is not behind the named-cutover confirmation"
    )
    assert "POSTURE_OK" in preceding, (
        "the public grant does not read the deployed revision's posture first"
    )
    assert 'POSTURE_OK" != "ok"' in preceding, (
        "the posture is read and never acted on before the grant"
    )


def test_bootstrap_never_writes_or_reads_the_membership_list() -> None:
    """The gate secret is a list of digests of real user ids.

    A generator here could only invent members, and a helper that took a subject
    and hashed it would put a real user id into a shell history and a terminal
    scrollback on the way to the only place it is supposed to exist. This script
    creates the container and grants the read; publishing is an operator act.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "gcloud secrets create staging-allowed-subject-digests" in text, (
        "bootstrap no longer creates the gate secret's shell"
    )
    for forbidden in (
        "versions add staging-allowed-subject-digests",
        "versions access",
        "sha256sum",
        "shasum",
    ):
        offending = [
            line for _, line in _command_lines(text) if forbidden in line
        ]
        assert not offending, (
            f"bootstrap.sh {forbidden!r}: it must neither synthesize nor read a "
            f"subject or a digest ({offending})"
        )


def test_the_command_scanner_can_tell_an_instruction_from_an_example() -> None:
    """Negative control for the two checks above.

    Both scan for command lines and both pass over the real file, which proves
    nothing until the scanner is shown distinguishing the two cases it exists to
    separate — a printed revoke recipe, which must be allowed, and an actual
    binding, which must be found.
    """
    printed = 'echo "  gcloud run services add-iam-policy-binding --member=allUsers"\n'
    run = "gcloud run services add-iam-policy-binding --member=allUsers\n"
    assert not [line for _, line in _command_lines(printed) if "allUsers" in line]
    assert [line for _, line in _command_lines(run) if "allUsers" in line]
