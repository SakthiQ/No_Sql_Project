"""Golden decision files + the properties every decision set must hold.

A golden diff after a rule change is an explicit, reviewable semantic shift —
that is the point of golden files. The property tests are the safety net:
every table accounted for, every decision justified, every rule id real."""
from __future__ import annotations

from nosqlmigrate.analyze import analyze
from nosqlmigrate.parsing.queries import parse_queries
from nosqlmigrate.rules.catalog import CATALOG
from nosqlmigrate.rules.engine import decide
from tests.conftest import assert_matches_golden, fixture_dir


def _decide(model, name):
    workload = parse_queries((fixture_dir(name) / "queries.sql").read_text(), model)
    return decide(analyze(model, workload))


def test_golden_decisions(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    ds = _decide(model, fixture_name)
    assert_matches_golden(ds.to_dict(), fixture_name, "decisions")


# ---- the expected outcome per fixture (the design intent, pinned) ----------


def test_ecommerce_decisions(model_ecommerce):
    ds = _decide(model_ecommerce, "ecommerce")

    def action(parent, child):
        return next(
            (d.rule_id, d.action) for d in ds.decisions
            if d.parent == parent and d.child == child
        )

    assert action("orders", "order_items") == ("D03", "EMBED")
    assert action("customers", "addresses") == ("D03", "EMBED")
    assert action("customers", "orders") == ("D01", "REFERENCE")
    assert action("orders", "order_events") == ("D01", "REFERENCE")
    assert action("orders", "payments") == ("D09", "REFERENCE")
    assert action("products", "order_items") == ("R00", "REFERENCE")  # already embedded under orders
    junction = next(d for d in ds.decisions if d.subject == "junction:product_categories")
    assert (junction.rule_id, junction.action) == ("D07", "FOLD")
    assert junction.signals["bounded_side"] == "categories"
    assert junction.signals["host"] == "products"


def test_blog_decisions(model_blog):
    ds = _decide(model_blog, "blog")

    def action(parent, child):
        return next(
            (d.rule_id, d.action) for d in ds.decisions
            if d.parent == parent and d.child == child
        )

    assert action("authors", "posts") == ("D01", "REFERENCE")
    assert action("posts", "comments") == ("D01", "REFERENCE")
    assert action("posts", "page_views") == ("D01", "REFERENCE")
    assert action("categories", "posts") == ("D05", "DUPLICATE")
    assert action("comments", "comments") == ("S01", "REFERENCE")
    junction = next(d for d in ds.decisions if d.subject == "junction:post_tags")
    assert (junction.rule_id, junction.action) == ("D07", "FOLD")
    # tags: no standalone access → its fields live inside posts documents only
    assert ds.dispositions["tags"].disposition == "dropped"


def test_library_decisions(model_library):
    ds = _decide(model_library, "library")

    def action(parent, child):
        return next(
            (d.rule_id, d.action) for d in ds.decisions
            if d.parent == parent and d.child == child
        )

    assert action("books", "book_details") == ("D04", "EMBED")
    assert action("Genres", "books") == ("D05", "DUPLICATE")
    assert action("branches", "book_copies") == ("D05", "DUPLICATE")
    assert action("books", "book_copies") == ("D09", "REFERENCE")
    assert action("members", "loans") == ("D01", "REFERENCE")
    assert action("book_copies", "loans") == ("D01", "REFERENCE")
    assert ds.dispositions["book_details"].disposition == "embedded"
    assert ds.dispositions["Genres"].disposition == "dropped"
    assert ds.dispositions["branches"].disposition == "dropped"
    junction = next(d for d in ds.decisions if d.subject == "junction:book_authors")
    assert junction.signals["bounded_side"] == "authors"
    assert junction.signals["host"] == "books"
    # authors have standalone reads → collection kept alongside the duplicated names
    assert ds.dispositions["authors"].disposition == "collection"


# ---- properties: true for every fixture, always -----------------------------


def test_every_table_has_a_disposition(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    ds = _decide(model, fixture_name)
    assert set(ds.dispositions) == set(model.tables)


def test_every_decision_cites_a_real_rule_and_carries_reasoning(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    ds = _decide(model, fixture_name)
    assert ds.decisions
    for d in ds.decisions:
        assert d.rule_id in CATALOG, f"unknown rule {d.rule_id}"
        assert d.gains and d.costs and d.mitigations, f"{d.subject}: missing rationale"
        assert d.signals, f"{d.subject}: no signals recorded"
        assert d.confidence in ("high", "medium", "low")


def test_dispositions_reference_real_rules(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    ds = _decide(model, fixture_name)
    for disp in ds.dispositions.values():
        assert disp.rule_id in CATALOG or disp.rule_id == "T00"
        assert disp.reason
