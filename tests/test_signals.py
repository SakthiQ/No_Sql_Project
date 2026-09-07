"""Signals: the numbers rules reason over. Golden snapshots + the invariants
that matter most (cardinality, junctions, lookups, unbounded growth)."""
import pytest

from nosqlmigrate.analyze import analyze
from nosqlmigrate.parsing.queries import parse_queries
from tests.conftest import assert_matches_golden, fixture_dir


def _analysis(model, name):
    workload = parse_queries((fixture_dir(name) / "queries.sql").read_text(), model)
    return analyze(model, workload)


def test_golden_signals(fixture_name, request):
    model = request.getfixturevalue(f"model_{fixture_name}")
    analysis = _analysis(model, fixture_name)
    assert_matches_golden(analysis.to_dict(), fixture_name, "signals")


# ---- cardinality ----------------------------------------------------------


def test_payments_is_one_to_one(model_ecommerce):
    a = _analysis(model_ecommerce, "ecommerce")
    rel = a.rel("orders", "payments")
    assert rel is not None and rel.cardinality == "1:1"


def test_book_details_is_one_to_one(model_library):
    a = _analysis(model_library, "library")
    rel = a.rel("books", "book_details")
    assert rel is not None and rel.cardinality == "1:1"


def test_orders_is_one_to_many(model_ecommerce):
    a = _analysis(model_ecommerce, "ecommerce")
    assert a.rel("customers", "orders").cardinality == "1:N"


# ---- junctions ------------------------------------------------------------


def test_junction_detection(model_ecommerce, model_blog, model_library):
    a_ec = _analysis(model_ecommerce, "ecommerce")
    assert set(a_ec.junctions) == {"product_categories"}
    a_bl = _analysis(model_blog, "blog")
    assert set(a_bl.junctions) == {"post_tags"}
    a_li = _analysis(model_library, "library")
    assert set(a_li.junctions) == {"book_authors"}
    assert a_li.junctions["book_authors"].sides == ("authors", "books")


def test_order_items_is_not_a_junction(model_ecommerce):
    """PK (order_id, line_no): line_no is not an FK column, so this is a weak
    entity, not a junction — the distinction D07/D08 depend on."""
    a = _analysis(model_ecommerce, "ecommerce")
    assert "order_items" not in a.junctions
    assert a.tables["order_items"].role == "child"


# ---- lookups ---------------------------------------------------------------


def test_lookup_classification(model_library):
    a = _analysis(model_library, "library")
    assert a.tables["Genres"].is_lookup
    assert a.tables["branches"].is_lookup
    assert not a.tables["members"].is_lookup  # wide entity, not a lookup
    assert not a.tables["books"].is_lookup


def test_written_tables_are_never_lookups(model_ecommerce):
    a = _analysis(model_ecommerce, "ecommerce")
    assert not a.tables["products"].is_lookup  # stock updates in workload
    assert a.tables["categories"].is_lookup


# ---- unbounded growth -------------------------------------------------------


def test_unbounded_children(model_ecommerce, model_blog, model_library):
    a_ec = _analysis(model_ecommerce, "ecommerce")
    assert a_ec.rel("orders", "order_events").unbounded
    assert a_ec.rel("customers", "orders").unbounded
    assert not a_ec.rel("customers", "addresses").unbounded
    assert not a_ec.rel("orders", "order_items").unbounded  # capped by line_no

    a_bl = _analysis(model_blog, "blog")
    assert a_bl.rel("posts", "comments").unbounded
    assert a_bl.rel("posts", "page_views").unbounded
    assert a_bl.rel("authors", "posts").unbounded

    a_li = _analysis(model_library, "library")
    assert a_li.rel("members", "loans").unbounded
    assert a_li.rel("book_copies", "loans").unbounded  # copy_no is an FK, not a cap
    assert not a_li.rel("books", "book_copies").unbounded  # copies are enumerated


# ---- co-access and write ratios ---------------------------------------------


def test_co_access_is_read_weighted(model_ecommerce):
    """INSERT INTO orders must not count as co-access with order_items."""
    a = _analysis(model_ecommerce, "ecommerce")
    rel = a.rel("orders", "order_items")
    reads_orders = 60 + 12 + 8 + 6 + 3
    assert rel.co_access == pytest.approx(60 / reads_orders, abs=1e-3)


def test_hot_write_detection(model_library):
    a = _analysis(model_library, "library")
    assert a.tables["book_copies"].write_ratio > 0.7  # checkout/return status flips
    assert a.tables["books"].write_ratio < 0.1


def test_standalone_access(model_ecommerce):
    a = _analysis(model_ecommerce, "ecommerce")
    # order_events: queried alone for analytics + inserted without reads
    assert a.rel("orders", "order_events").child_standalone > 0.9
    # addresses: never touched without customers
    assert a.rel("customers", "addresses").child_standalone == 0.0


# ---- roles -------------------------------------------------------------------


def test_roles(model_library, model_blog):
    a_li = _analysis(model_library, "library")
    assert a_li.tables["book_details"].role == "owned"  # PK = FK → weak entity
    assert a_li.tables["books"].role == "hub"
    a_bl = _analysis(model_blog, "blog")
    assert a_bl.tables["comments"].has_hierarchy  # self-referencing FK
