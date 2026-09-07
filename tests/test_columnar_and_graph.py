"""Cassandra and Neo4j emitters."""
from __future__ import annotations

import pytest

from nosqlmigrate.emitters.columnar import emit_columnar
from nosqlmigrate.emitters.graph import emit_graph
from nosqlmigrate.pipeline import run
from tests.conftest import fixture_dir


@pytest.fixture(scope="module")
def results():
    out = {}
    for name in ("ecommerce", "blog", "library"):
        out[name] = run(
            (fixture_dir(name) / "schema.sql").read_text(),
            (fixture_dir(name) / "queries.sql").read_text(),
        )
    return out


# ---- Cassandra ---------------------------------------------------------------


def test_one_table_per_query_shape(results):
    cass = emit_columnar(results["blog"].analysis, results["blog"].decisions)
    names = [t.name for t in cass.tables]
    assert "comments_by_post_id" in names
    assert "posts_by_author_id" in names or "posts_by_slug" in names or "posts_all" in names


def test_partition_and_clustering_keys(results):
    cass = emit_columnar(results["blog"].analysis, results["blog"].decisions)
    comments = next(t for t in cass.tables if t.name == "comments_by_post_id")
    assert comments.partition_keys == ["post_id"]
    # thread query sorts by created_at
    assert any(c == "created_at" for c, _ in comments.clustering_keys)


def test_full_scan_warning_fires(results):
    cass = emit_columnar(results["blog"].analysis, results["blog"].decisions)
    posts_all = next(t for t in cass.tables if t.name == "posts_all")
    assert any("full scan" in w.lower() for w in posts_all.warnings)


def test_unbounded_partition_warning_fires(results):
    cass = emit_columnar(results["ecommerce"].analysis, results["ecommerce"].decisions)
    events = next(t for t in cass.tables if t.base_table == "order_events")
    assert any("partition grows without bound" in w for w in events.warnings)


def test_write_amplification_reported(results):
    cass = emit_columnar(results["ecommerce"].analysis, results["ecommerce"].decisions)
    bases = {t.base_table for t in cass.tables}
    assert "orders" in bases
    amp = [w for w in cass.write_amplification if w["table"] == "orders"]
    if len([t for t in cass.tables if t.base_table == "orders"]) > 1:
        assert amp and amp[0]["detail"].startswith("each orders write")


def test_cql_renders_valid_shape(results):
    cass = emit_columnar(results["library"].analysis, results["library"].decisions)
    cql = cass.render()
    assert "CREATE TABLE" in cql
    assert "PRIMARY KEY ((" in cql
    for t in cass.tables:
        assert f"CREATE TABLE {t.name} (" in cql


def test_denormalized_lookup_fields_ride_along(results):
    """The copies query selects branch_name via a join; the D05 decision means
    it can be denormalized into the Cassandra table."""
    cass = emit_columnar(results["library"].analysis, results["library"].decisions)
    copies = next(t for t in cass.tables if t.base_table == "book_copies")
    assert "branch_name" in {n for n, _ in copies.columns}


# ---- Neo4j -----------------------------------------------------------------------


def test_nodes_and_relationships(results):
    graph = emit_graph(results["library"].analysis, results["library"].decisions)
    labels = {n.label for n in graph.nodes}
    assert {"Book", "BookCopy", "Loan", "Member", "Author", "Genre", "Branch"} <= labels
    types = {r.rel_type for r in graph.relationships}
    assert "HAS_LOAN" in types
    assert "HAS_AUTHOR" in types  # junction fold


def test_junction_becomes_relationship_with_payload():
    schema = """
    CREATE TABLE users (user_id INT PRIMARY KEY, email VARCHAR(255), full_name VARCHAR(120));
    CREATE TABLE teams (team_id INT PRIMARY KEY, name VARCHAR(200), description VARCHAR(2000));
    CREATE TABLE team_members (
      team_id INT NOT NULL REFERENCES teams (team_id),
      user_id INT NOT NULL REFERENCES users (user_id),
      role VARCHAR(40),
      PRIMARY KEY (team_id, user_id)
    );
    """
    queries = """
    -- @weight 5 read
    SELECT u.user_id, tm.role FROM users u JOIN team_members tm ON tm.user_id = u.user_id WHERE tm.team_id = ?;
    -- @weight 1 write
    UPDATE teams SET name = ? WHERE team_id = ?;
    """
    result = run(schema, queries)
    graph = emit_graph(result.analysis, result.decisions)
    assert "TEAM_MEMBER" in {r.rel_type for r in graph.relationships}
    rel = next(r for r in graph.relationships if r.rel_type == "TEAM_MEMBER")
    assert "role" in rel.properties


def test_never_traversed_lookup_becomes_property(results):
    """blog categories are duplicated (D05) and no query joins them → property."""
    graph = emit_graph(results["blog"].analysis, results["blog"].decisions)
    assert any(p["table"] == "categories" for p in graph.property_placements)
    assert "Category" not in {n.label for n in graph.nodes}
    # tags ARE traversed (posts by tag) → they stay nodes
    assert "Tag" in {n.label for n in graph.nodes}


def test_self_reference_relationship(results):
    graph = emit_graph(results["blog"].analysis, results["blog"].decisions)
    self_rels = [r for r in graph.relationships if r.source == r.target == "Comment"]
    assert self_rels and self_rels[0].rel_type == "PARENT_OF"


def test_constraints_render(results):
    graph = emit_graph(results["library"].analysis, results["library"].decisions)
    cypher = graph.render()
    assert "REQUIRE n.book_id IS UNIQUE" in cypher
    assert "CREATE INDEX" in cypher
    # 1:1 collapse is a property placement, not a node
    assert "BookDetail" not in cypher
