"""The pipeline: DDL + queries → analysis → decisions → artifacts.

One entry point shared by the CLI and the API, so both always agree.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .analyze import Analysis, analyze
from .emitters.document import DocumentResult, emit_document
from .parsing.ddl import parse_ddl
from .parsing.queries import parse_queries
from .report.tradeoffs import build_report, render_markdown
from .rules.engine import DecisionSet, decide


@dataclass
class RunResult:
    analysis: Analysis
    decisions: DecisionSet
    document: DocumentResult
    report: dict
    markdown: str


def apply_overrides(analysis: Analysis, overrides: dict) -> Analysis:
    """Patch user-supplied estimates over the structural inferences.

    Format:
        {"fanout": {"parent->child": [min, max|null]},
         "row_width": {"table": bytes},
         "co_access": {"parent->child": 0.0..1.0}}
    """
    if not overrides:
        return analysis

    fanout = overrides.get("fanout", {})
    co_access = overrides.get("co_access", {})

    rels = []
    for r in analysis.relationships:
        key = f"{r.parent}->{r.child}"
        updates = {}
        if key in fanout:
            lo, hi = fanout[key]
            updates["fanout_min"] = int(lo)
            updates["fanout_max"] = None if hi is None else int(hi)
            updates["unbounded"] = hi is None
        if key in co_access:
            updates["co_access"] = float(co_access[key])
        rels.append(replace(r, **updates) if updates else r)

    widths = overrides.get("row_width", {})
    tables = {
        name: replace(stats, row_width=int(widths[name])) if name in widths else stats
        for name, stats in analysis.tables.items()
    }
    return replace(analysis, relationships=rels, tables=tables)


def run(schema_sql: str, queries_sql: str = "", dialect: str | None = None,
        overrides: dict | None = None) -> RunResult:
    model = parse_ddl(schema_sql, dialect=dialect)
    workload = parse_queries(queries_sql, model) if queries_sql.strip() else parse_queries("", model)
    analysis = analyze(model, workload)
    if overrides:
        analysis = apply_overrides(analysis, overrides)
    decisions = decide(analysis)
    document = emit_document(analysis, decisions)
    report = build_report(analysis, decisions, document)
    return RunResult(
        analysis=analysis,
        decisions=decisions,
        document=document,
        report=report,
        markdown=render_markdown(report),
    )
