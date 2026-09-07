"""Workload extraction: access patterns from the fixture queries."""
from nosqlmigrate.parsing.queries import parse_queries, split_statements
from tests.conftest import fixture_dir


def _workload(model, name):
    sql = (fixture_dir(name) / "queries.sql").read_text()
    return parse_queries(sql, model)


# -- statement splitting / directives ---------------------------------------


def test_split_statements_directives():
    sql = (
        "-- intro comment\n"
        "-- @weight 5 @tag read\nSELECT 1;\n\n"
        "-- @weight 2\nINSERT INTO t VALUES (1);\n"
    )
    chunks = split_statements(sql)
    assert len(chunks) == 2
    assert chunks[0][0] == {"weight": "5", "tag": "read"}
    assert chunks[1][0] == {"weight": "2"}
    assert chunks[0][1].upper().startswith("SELECT 1")


def test_undirected_statement_gets_default_weight():
    chunks = split_statements("SELECT 1;")
    assert chunks == [({}, "SELECT 1")]


# -- ecommerce ----------------------------------------------------------------


def test_directives_parsed(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    by_weight = sorted(w.queries, key=lambda q: -q.weight)
    assert by_weight[0].weight == 60.0  # order with items
    assert any(q.weight == 25.0 and q.kind.value == "INSERT" for q in w.queries)


def test_statement_count(model_ecommerce):
    assert len(_workload(model_ecommerce, "ecommerce").queries) == 18


def test_join_edges(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    q0 = w.queries[0]
    assert q0.tables == ("orders", "order_items")
    assert q0.joins == (("order_items", "orders"),)


def test_predicates_and_sorts(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    history = w.queries[3]  # customer order history
    pred = history.predicates[0]
    assert (pred.table, pred.column, pred.op) == ("orders", "customer_id", "=")
    assert [(s.table, s.column, s.desc) for s in history.sorts] == [("orders", "placed_at", True)]


def test_insert_shape(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    inserts = [q for q in w.queries if q.kind.value == "INSERT"]
    assert len(inserts) == 4
    event_insert = next(q for q in inserts if q.tables == ("order_events",))
    assert set(event_insert.set_columns) == {"order_id", "event_type", "payload", "event_time"}
    assert event_insert.is_write


def test_update_shape(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    updates = [q for q in w.queries if q.kind.value == "UPDATE"]
    assert len(updates) == 2
    status_update = next(q for q in updates if q.tables == ("orders",))
    assert status_update.set_columns == ("status",)
    assert (status_update.predicates[0].column, status_update.predicates[0].op) == ("order_id", "=")


def test_weight_aggregations(model_ecommerce):
    w = _workload(model_ecommerce, "ecommerce")
    assert w.weight_touching("order_events") == 2 + 1 + 25
    assert w.write_weight_touching("order_events") == 25
    assert w.weight_touching_both("orders", "order_items") == 60


# -- blog ----------------------------------------------------------------------


def test_is_not_null_predicates(model_blog):
    w = _workload(model_blog, "blog")
    front = next(q for q in w.queries if q.tables == ("posts",) and q.sorts)
    assert "IS NOT NULL" in {p.op for p in front.predicates}


def test_aggregates_extracted(model_blog):
    w = _workload(model_blog, "blog")
    assert any("COUNT" in q.aggregates for q in w.queries)
    tag_cloud = next(q for q in w.queries if "COUNT" in q.aggregates)
    assert set(tag_cloud.tables) == {"post_tags", "tags"}


def test_update_with_quoted_reserved_word(model_blog):
    w = _workload(model_blog, "blog")
    edit = next(q for q in w.queries if q.kind.value == "UPDATE" and q.tables == ("posts",))
    assert set(edit.set_columns) == {"body", "order"}


# -- library --------------------------------------------------------------------


def test_quoted_table_resolves(model_library):
    w = _workload(model_library, "library")
    genre_query = next(q for q in w.queries if "Genres" in q.tables)
    assert genre_query.joins == (("Genres", "books"),)


def test_three_way_join_edges(model_library):
    w = _workload(model_library, "library")
    by_author = next(q for q in w.queries if set(q.tables) == {"books", "book_authors", "authors"})
    assert set(by_author.joins) == {("authors", "book_authors"), ("book_authors", "books")}


def test_ilike_predicate(model_library):
    w = _workload(model_library, "library")
    search = next(q for q in w.queries if q.tables == ("books",) and q.sorts)
    assert "ILIKE" in {p.op for p in search.predicates}
