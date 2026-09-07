"""nosqlmigrate command line.

    nosqlmigrate analyze schema.sql --queries queries.sql -o out/
    nosqlmigrate rules
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .report.diagrams import mermaid_er
from .rules.catalog import CATALOG


def _main_analyze(args: argparse.Namespace) -> int:
    from .pipeline import run

    schema = Path(args.schema).read_text()
    queries = Path(args.queries).read_text() if args.queries else ""
    overrides = None
    if args.overrides:
        overrides = json.loads(Path(args.overrides).read_text())

    result = run(schema, queries, dialect=args.dialect, overrides=overrides)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    (out / "report.json").write_text(json.dumps(result.report, indent=2, default=str) + "\n")
    (out / "report.md").write_text(result.markdown + "\n")
    (out / "document.json").write_text(
        json.dumps(result.document.to_dict(), indent=2, default=str) + "\n")
    (out / "er.mmd").write_text(mermaid_er(result.analysis) + "\n")

    if args.target in ("columnar", "all"):
        from .emitters.columnar import emit_columnar

        cassandra = emit_columnar(result.analysis, result.decisions)
        (out / "cassandra.cql").write_text(cassandra.render() + "\n")
    if args.target in ("graph", "all"):
        from .emitters.graph import emit_graph

        neo4j = emit_graph(result.analysis, result.decisions)
        (out / "neo4j.cypher").write_text(neo4j.render() + "\n")

    if args.print:
        print(result.markdown)

    summary = result.report["summary"]
    print(
        f"analyzed {summary['tables']} tables · {summary['relationships']} relationships · "
        f"{summary['workload']['queries']} queries → {summary['collections']} collections; "
        f"{summary['single_lookup_read_fraction']:.0%} of weighted reads single-collection",
        file=sys.stderr,
    )
    print(f"artifacts written to {out}/", file=sys.stderr)
    return 0


def _main_rules(args: argparse.Namespace) -> int:  # noqa: ARG001
    for meta in CATALOG.values():
        print(f"{meta.id}  {meta.name:28s} [{meta.target:8s}] {meta.action}")
        print(f"     {meta.summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nosqlmigrate",
        description="Propose and justify NoSQL schemas from a relational schema + workload.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="analyze a schema (+ workload)")
    p_analyze.add_argument("schema", help="path to the DDL file")
    p_analyze.add_argument("--queries", "-q", help="path to the workload queries file")
    p_analyze.add_argument("--dialect", default="auto",
                           choices=("auto", "mysql", "postgres", "postgresql", "mariadb", "pg"))
    p_analyze.add_argument("--target", default="document",
                           choices=("document", "columnar", "graph", "all"),
                           help="which target artifacts to emit (default: document)")
    p_analyze.add_argument("--out", "-o", default="out", help="output directory")
    p_analyze.add_argument("--overrides", help="JSON file with estimate overrides "
                                               "(fanout / row_width / co_access)")
    p_analyze.add_argument("--print", action="store_true", help="print the report to stdout")
    p_analyze.set_defaults(func=_main_analyze)

    p_rules = sub.add_parser("rules", help="list the rule catalog")
    p_rules.set_defaults(func=_main_rules)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
