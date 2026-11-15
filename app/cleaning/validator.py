"""Hard invariants for a cleaning run: failure keeps raw and prevents promotion of the clean version."""
from __future__ import annotations

import pandas as pd

from app.schemas import CleaningStep, ValidationReport


def validate_cleaning(*, raw: dict[str, pd.DataFrame], clean: dict[str, pd.DataFrame], steps: list[CleaningStep],
                      failures: list[dict], rerun: dict[str, pd.DataFrame], issues_unresolved: list[str],
                      dups_removed: dict[str, int], profile_hashes: tuple[dict[str, str], dict[str, str]]) -> ValidationReport:
    checks: list[dict] = []

    def check(name: str, expected, actual) -> None:
        checks.append({"name": name, "passed": expected == actual, "expected": expected, "actual": actual})

    for ds in sorted(raw):
        check(f"row_invariant:{ds}", len(raw[ds]) - dups_removed.get(ds, 0), len(clean[ds]))
        check(f"column_invariant:{ds}", list(raw[ds].columns), list(clean[ds].columns))
        check(f"idempotency:{ds}", True, bool(rerun[ds].equals(clean[ds])) and profile_hashes[0][ds] == profile_hashes[1][ds])
    applied = [s for s in steps if s.status == "applied"]
    complete = all(s.step_id and s.reason and s.risk and s.source and s.status for s in applied)
    check("step_metadata", True, complete)
    parse_steps = [s for s in applied if s.transformation in ("parse_numeric", "parse_date_iso")]
    check("parse_failures_listed", True, len(failures) >= 0 and all(f.get("step_id") for f in failures)
          and all(any(f["step_id"] == s.step_id for s in parse_steps) for f in failures))
    return ValidationReport(
        passed=all(c["passed"] for c in checks), checks=checks,
        rows_accepted=sum(len(v) for v in raw.values()), rows_clean=sum(len(v) for v in clean.values()),
        exact_dups_removed=sum(dups_removed.values()), unresolved_issues=sorted(issues_unresolved),
    )
