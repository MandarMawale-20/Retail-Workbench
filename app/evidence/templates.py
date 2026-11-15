"""Template answer renderer (default path): numbers come exclusively from facts."""
from __future__ import annotations

from app.evidence.facts import row_label
from app.query.schemas import EvidenceBundle, Fact, QueryPlan


def format_fact(fact: Fact) -> str:
    v = fact.value
    if isinstance(v, str):
        return v
    if fact.unit == "percent":
        return f"{v:,.2f}%"
    if fact.unit == "currency":
        return f"{v:,.2f}"
    if fact.unit == "count":
        return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"
    return f"{v:,.4g}" if abs(v) < 1 else f"{v:,.2f}"


def _label(alias: str, evidence: EvidenceBundle) -> str:
    for d in evidence.metric_definitions:
        if d["metric_id"] == alias:
            return d["name"].lower()
    return alias.replace("_", " ")


def _fact_lookup(evidence: EvidenceBundle) -> dict[str, Fact]:
    return {f.name: f for f in evidence.facts}


def render_template(plan: QueryPlan, evidence: EvidenceBundle, columns: list[str], rows: list[list], keys: int) -> str:
    facts = _fact_lookup(evidence)
    lines: list[str] = []
    measures = columns[keys:]
    if plan.intent in ("list", "distinct", "describe") or not measures:
        shown = rows[:10]
        lines.append(f"{plan.intent.capitalize()} result: {evidence.result_row_count} row(s).")
        for r in shown:
            lines.append("- " + " | ".join("(null)" if v is None else str(v) for v in r))
    elif keys == 0:
        parts = []
        for m in measures:
            fact = facts.get(f"overall|{m}")
            parts.append(f"{_label(m, evidence)}: {format_fact(fact)}" if fact else m)
        lines.append("; ".join(parts) + ".")
    else:
        lines.append(f"{', '.join(_label(m, evidence) for m in measures).capitalize()} by {', '.join(columns[:keys])}:")
        for i, r in enumerate(rows[:10], start=1):
            label = row_label(r, keys)
            cells = []
            for m in measures:
                fact = facts.get(f"{label}|{m}")
                cells.append(format_fact(fact) if fact else "n/a")
            lines.append(f"{i}. {label}: " + ", ".join(cells))
        diff, pct = facts.get(f"diff_top2|{measures[0]}"), facts.get(f"pct_diff_top2|{measures[0]}")
        if diff is not None and len(rows) >= 2 and isinstance(diff.value, (int, float)):
            direction = "higher" if diff.value >= 0 else "lower"
            shown_fact = Fact(**{**diff.model_dump(), "value": abs(diff.value)})
            pct_text = ""
            if pct is not None and isinstance(pct.value, (int, float)):
                pct_text = f" ({format_fact(Fact(**{**pct.model_dump(), 'value': abs(pct.value)}))})"
            lines.append(f"The first entry is {direction} than the second by {format_fact(shown_fact)}"
                         + (pct_text + "." if pct_text else "."))
    lines.append("")
    stage = "raw (unparsed text)" if evidence.dataset_stage == "raw" else "clean"
    lines.append(f"Basis: {'; '.join(evidence.assumptions) if evidence.assumptions else 'direct column values'}.")
    if evidence.filters:
        desc = ", ".join(f"{f['column_id']} {f['op']} {f['value']}" for f in evidence.filters)
        lines.append(f"Filters: {desc}.")
    versions = ", ".join(f"{d}@{v}" for d, v in sorted(evidence.dataset_versions.items()))
    joins = ", ".join(r["label"] for r in evidence.relationships)
    lines.append(f"Evidence: {stage} stage, datasets {versions}; rows considered {evidence.rows_considered:,}"
                 + (f"; joins {joins}" if joins else "") + f"; evidence {evidence.evidence_id}.")
    return "\n".join(lines)
