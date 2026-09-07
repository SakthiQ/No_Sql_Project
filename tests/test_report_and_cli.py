"""Reporter, pipeline, and CLI: the end-to-end layer."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nosqlmigrate.pipeline import apply_overrides, run
from tests.conftest import fixture_dir

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def library_result():
    return run(
        (fixture_dir("library") / "schema.sql").read_text(),
        (fixture_dir("library") / "queries.sql").read_text(),
    )


# ---- the report --------------------------------------------------------------


def test_report_structure(library_result):
    report = library_result.report
    assert report["summary"]["tables"] == 9
    assert report["summary"]["collections"] == 5
    assert 0 < report["summary"]["single_lookup_read_fraction"] <= 1
    assert report["decisions"] and report["dispositions"]
    assert report["document"]["collections"]
    assert report["consistency_risks"]  # library duplicates genre/branch names
    assert report["query_mapping"]["cross_collection"]  # loans queries span collections


def test_markdown_renders_the_full_story(library_result):
    md = library_result.markdown
    for fragment in (
        "# NoSQL schema proposal",
        "## Table dispositions",
        "`D04 ONE_TO_ONE_COACCESSED`",
        "**Gain:**",
        "**Cost:**",
        "**Mitigation:**",
        "## Query mapping",
        "## Consistency risks",
    ):
        assert fragment in md, f"missing {fragment!r}"


def test_unmigratable_section_lists_diagnostics():
    result = run(
        (fixture_dir("ecommerce") / "schema.sql").read_text(),
        (fixture_dir("ecommerce") / "queries.sql").read_text(),
    )
    assert any("VIEW" in u["message"] for u in result.report["unmigratable"])
    assert "VIEW" in result.markdown or "unmigratable" in result.markdown.lower()


def test_single_lookup_fraction(library_result):
    """Most library reads (book page, copies, member loans...) collapse into
    one collection; the fraction must be honest, not aspirational."""
    fraction = library_result.report["summary"]["single_lookup_read_fraction"]
    assert 0.5 < fraction < 1.0


# ---- overrides -----------------------------------------------------------------


def test_overrides_change_the_outcome():
    schema = (fixture_dir("ecommerce") / "schema.sql").read_text()
    queries = (fixture_dir("ecommerce") / "queries.sql").read_text()

    baseline = run(schema, queries)
    orders_items = next(
        d for d in baseline.decisions.decisions
        if d.parent == "orders" and d.child == "order_items"
    )
    assert orders_items.action == "EMBED"

    # pretend each order has thousands of items: D02 flips it to REFERENCE
    overridden = run(schema, queries, overrides={
        "fanout": {"orders->order_items": [1000, None]},
    })
    flipped = next(
        d for d in overridden.decisions.decisions
        if d.parent == "orders" and d.child == "order_items"
    )
    assert flipped.action == "REFERENCE"
    assert flipped.rule_id == "D01"


def test_apply_overrides_patches_co_access():
    result = run(
        (fixture_dir("library") / "schema.sql").read_text(),
        (fixture_dir("library") / "queries.sql").read_text(),
    )
    patched = apply_overrides(result.analysis, {"co_access": {"books->book_copies": 0.9}})
    rel = patched.rel("books", "book_copies")
    assert rel.co_access == 0.9


# ---- CLI -------------------------------------------------------------------------


def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "nosqlmigrate.cli", *args],
        capture_output=True, text=True, cwd=REPO,
    )


def test_cli_analyze_end_to_end(tmp_path):
    out = tmp_path / "out"
    proc = _cli(
        "analyze", str(fixture_dir("blog") / "schema.sql"),
        "-q", str(fixture_dir("blog") / "queries.sql"),
        "--target", "all", "-o", str(out),
    )
    assert proc.returncode == 0, proc.stderr
    for name in ("report.md", "report.json", "document.json", "cassandra.cql",
                 "neo4j.cypher", "er.mmd"):
        assert (out / name).exists(), f"missing {name}"
    report = json.loads((out / "report.json").read_text())
    assert report["summary"]["tables"] == 7


def test_cli_rules_lists_catalog():
    proc = _cli("rules")
    assert proc.returncode == 0
    assert "D03" in proc.stdout and "CONTAINED_1N" in proc.stdout


def test_cli_without_queries_still_works(tmp_path):
    out = tmp_path / "out"
    proc = _cli("analyze", str(fixture_dir("library") / "schema.sql"), "-o", str(out))
    assert proc.returncode == 0, proc.stderr
    # no workload: co-access is zero, so everything degrades to references
    report = json.loads((out / "report.json").read_text())
    assert report["summary"]["tables"] == 9
    assert report["summary"]["workload"]["queries"] == 0
