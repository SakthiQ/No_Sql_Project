"""The trade-off report: the product.

Anyone can flatten a schema; the value is a defensible, traceable rationale
for every embed-vs-reference call. Markdown for humans, JSON for machines —
same content, one pass.
"""
from __future__ import annotations

from ..analyze import Analysis
from ..emitters.document import DocumentResult
from ..rules.engine import DecisionSet


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _single_lookup_reads(analysis: Analysis, document: DocumentResult) -> tuple[float, list, list]:
    """(fraction of weighted reads served by one lookup, served queries,
    cross-collection queries)."""
    total = 0.0
    served = 0.0
    single: list[dict] = []
    cross: list[dict] = []
    for q in analysis.workload.queries:
        if q.is_write or not q.tables:
            continue
        total += q.weight
        homes = {document.home_of.get(t, t) for t in q.tables}
        entry = {
            "weight": q.weight,
            "tables": list(q.tables),
            "collections": sorted(homes),
            "query": " ".join(q.raw.split())[:110],
        }
        if len(homes) == 1:
            served += q.weight
            single.append(entry)
        else:
            cross.append(entry)
    fraction = (served / total) if total > 0 else 0.0
    return fraction, single, cross


def _consistency_risks(analysis: Analysis, decisions: DecisionSet) -> list[dict]:
    """Duplicated data that must fan out on change."""
    risks: list[dict] = []
    for d in decisions.decisions:
        if d.action == "DUPLICATE":
            risks.append({
                "kind": "denormalized lookup",
                "rule": d.rule_id,
                "detail": f"{d.parent} fields inside {d.child} documents "
                          f"({', '.join(d.signals.get('duplicated_fields', []))}) — "
                          f"renaming a {d.parent} value fans out across {d.child}",
            })
        elif d.action == "FOLD" and d.signals.get("duplicated_fields"):
            bounded = d.signals.get("bounded_side")
            risks.append({
                "kind": "junction array",
                "rule": d.rule_id,
                "detail": f"{bounded} entries embedded in {d.parent} documents — "
                          f"vocabulary changes fan out across {d.parent}",
            })
        elif d.action == "EMBED" and d.signals.get("write_ratio_child", 0) > 0.2:
            risks.append({
                "kind": "mutable embedded child",
                "rule": d.rule_id,
                "detail": f"{d.child} is embedded in {d.parent} but has write ratio "
                          f"{_pct(d.signals['write_ratio_child'])} — child edits rewrite "
                          f"the parent document",
            })
    return risks


def build_report(analysis: Analysis, decisions: DecisionSet, document: DocumentResult) -> dict:
    """The machine-readable report: everything, structured."""
    fraction, single, cross = _single_lookup_reads(analysis, document)
    workload = analysis.workload
    read_weight = sum(q.weight for q in workload.queries if not q.is_write)
    write_weight = sum(q.weight for q in workload.queries if q.is_write)

    return {
        "summary": {
            "dialect": analysis.model.dialect,
            "tables": len(analysis.model.tables),
            "relationships": len(analysis.relationships),
            "junctions": {k: list(v.sides) for k, v in analysis.junctions.items()},
            "workload": {
                "queries": len(workload.queries),
                "read_weight": read_weight,
                "write_weight": write_weight,
            },
            "collections": len(document.collections),
            "single_lookup_read_fraction": round(fraction, 3),
            "diagnostics": [d.to_dict() for d in analysis.model.diagnostics],
        },
        "decisions": [d.to_dict() for d in decisions.decisions],
        "dispositions": [decisions.dispositions[k].to_dict()
                         for k in sorted(decisions.dispositions)],
        "document": document.to_dict(),
        "query_mapping": {
            "single_lookup": single,
            "cross_collection": cross,
        },
        "consistency_risks": _consistency_risks(analysis, decisions),
        "unmigratable": [
            d.to_dict() for d in analysis.model.diagnostics
            if d.code in ("UNMIGRATABLE", "UNMIGRATABLE_CHECK")
        ],
    }


# --------------------------------------------------------------------- markdown


def _decision_card(d: dict) -> list[str]:
    lines = [
        f"#### `{d['subject']}` — **{d['action']}** via `{d['rule_id']} {d['rule_name']}` "
        f"(confidence: {d['confidence']})",
        "",
    ]
    s = d["signals"]
    interesting = [
        k for k in ("cardinality", "co_access", "child_standalone", "write_ratio_child",
                    "fanout_min", "fanout_max", "unbounded", "est_embedded_size",
                    "bounded_side", "host", "duplicated_fields", "embedded_under")
        if k in s
    ]
    if interesting:
        parts = []
        for k in interesting:
            v = s[k]
            if isinstance(v, float):
                v = round(v, 2)
            parts.append(f"`{k}={v}`")
        lines.append("Signals: " + " · ".join(parts))
        lines.append("")
    lines.append(f"**Gain:** {d['gains']}")
    lines.append("")
    lines.append(f"**Cost:** {d['costs']}")
    lines.append("")
    lines.append(f"**Mitigation:** {d['mitigations']}")
    if d["near_misses"]:
        lines.append("")
        lines.append("**Why not the others?**")
        for nm in d["near_misses"]:
            lines.append(f"- `{nm['rule_id']} {nm['rule_name']}`: {nm['detail']}")
    lines.append("")
    return lines


def render_markdown(report: dict) -> str:
    """Machine-readable report → the human-readable design review."""
    summary = report["summary"]
    lines: list[str] = [
        "# NoSQL schema proposal — trade-off report",
        "",
        f"Source: {summary['tables']} tables, {summary['relationships']} relationships "
        f"({summary['dialect']} dialect), {summary['workload']['queries']} workload queries.",
        "",
        f"**Proposal:** {summary['collections']} collections. "
        f"{_pct(report['summary']['single_lookup_read_fraction'])} of weighted reads are "
        f"served by a single document lookup (no join, no `$lookup`).",
        "",
        "## Table dispositions",
        "",
        "| Table | Disposition | Into | Rule | Reason |",
        "|---|---|---|---|---|",
    ]
    for disp in report["dispositions"]:
        lines.append(
            f"| `{disp['table']}` | {disp['disposition']} | "
            f"{disp['into'] or '—'} | `{disp['rule_id']}` | {disp['reason']} |"
        )

    lines += ["", "## Decisions", ""]
    for d in report["decisions"]:
        lines += _decision_card(d)

    lines += ["## Query mapping", ""]
    cross = report["query_mapping"]["cross_collection"]
    if cross:
        lines.append(
            f"**{_pct(report['summary']['single_lookup_read_fraction'])} of weighted reads "
            f"are single-collection lookups.** The rest span collections:"
        )
        lines.append("")
        for entry in cross:
            lines.append(
                f"- {' → '.join(f'`{c}`' for c in entry['collections'])} — "
                f"`{entry['query']}`"
            )
    else:
        lines.append("**Every read in the workload is served by a single collection.**")

    risks = report["consistency_risks"]
    lines += ["", "## Consistency risks (denormalization debt)", ""]
    if risks:
        for r in risks:
            lines.append(f"- **{r['kind']}** (`{r['rule']}`): {r['detail']}")
    else:
        lines.append("None — no duplicated mutable data in this proposal.")

    unmigratable = report["unmigratable"]
    lines += ["", "## Handle in the application layer", ""]
    if unmigratable:
        for u in unmigratable:
            lines.append(f"- `{u['code']}`: {u['message']}"
                         + (f" — `{u['excerpt']}`" if u["excerpt"] else ""))
    else:
        lines.append("Nothing unmigratable detected in the DDL.")

    lines += [
        "",
        "## Indexes",
        "",
    ]
    for coll in report["document"]["collections"]:
        if coll["indexes"]:
            lines.append(f"**`{coll['name']}`**")
            lines.append("")
            for idx in coll["indexes"]:
                keys = ", ".join(f"{k['field']}:{'↓' if k['direction'] < 0 else '↑'}"
                                 for k in idx["keys"])
                unique = " · unique" if idx["unique"] else ""
                lines.append(f"- `{keys}`{unique} — {idx['reason']}")
            lines.append("")

    lines += [
        "---",
        "",
        "_Estimates (fan-out, row width, co-access) are structural inferences from the DDL "
        "and the supplied workload weights — not measurements. Override any of them via "
        "`--overrides` and re-run._",
    ]
    return "\n".join(lines)
