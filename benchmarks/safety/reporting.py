"""Human-readable reporting for the safety gauntlet's results doc."""

from __future__ import annotations

from typing import Any

from .runner import GUARD_ORDER


def _accuracy_of(g: dict[str, Any]) -> float:
    """The guard's headline accuracy: whichever accuracy metric it reports."""
    for key in ("rejection_accuracy", "validate_accuracy", "repair_accuracy",
                "verification_accuracy", "structural_reject_accuracy"):
        if key in g:
            return float(g[key])
    return 1.0 if g.get("pass") else 0.0


def print_summary(doc: dict[str, Any]) -> None:
    meta, summary = doc["meta"], doc["summary"]
    verdict = "PASS" if summary["all_pass"] else "FAIL"
    print(f"\n== safety (deterministic) | {meta['cases_total']} checks | {verdict} ==")
    print(
        f"false_accept_total : {summary['false_accept_total']}   "
        "(zero-tolerance — must be 0)"
    )
    header = f"{'guard':<22} {'cases':>5} {'accuracy':>9} {'false-accepts':>14}  verdict"
    print(header)
    for name in GUARD_ORDER:
        g = summary["guards"][name]
        zt = sum(g["zero_tolerance"].values())
        acc = f"{100 * _accuracy_of(g):.1f}%"
        print(f"{name:<22} {g['cases']:>5} {acc:>9} {zt:>14}  "
              f"{'PASS' if g['pass'] else 'FAIL'}")
    extras = doc["summary"]["guards"]
    rs = extras["result_schema"]
    print(f"\nresult_schema conforms accuracy : {100 * rs['conforms_accuracy']:.1f}%")
    st = extras["state_token"]
    print(f"state_token genuine round-trip  : {'ok' if st['roundtrip_ok'] else 'BROKEN'}")
    wf = extras["workflow_validation"]
    print(f"workflow degrade-not-crash      : {'ok' if wf['degrade_correct'] else 'BROKEN'}")


def print_failures(doc: dict[str, Any]) -> None:
    any_failures = False
    for name in GUARD_ORDER:
        failures = doc["guards"][name]["failures"]
        if not failures:
            continue
        any_failures = True
        print(f"\nFailing checks in {name} ({len(failures)}):")
        for f in failures:
            print(f"  ✗ {f['id']}: {f.get('detail') or f}")
    if not any_failures:
        print("\nNo failing checks.")
