"""Rule coverage the fixtures don't reach: D02 (size risk), D06 (shared
mutable parent), D08 (large junction), D10 (deep nesting), and near-misses.
Each test builds a minimal schema + workload that isolates one rule."""
from __future__ import annotations

from nosqlmigrate.analyze import analyze
from nosqlmigrate.parsing.ddl import parse_ddl
from nosqlmigrate.parsing.queries import parse_queries
from nosqlmigrate.rules.engine import decide


def run(schema: str, queries: str):
    model = parse_ddl(schema, dialect="postgres")
    workload = parse_queries(queries, model)
    return decide(analyze(model, workload))


def _edge(ds, parent, child):
    return next(d for d in ds.decisions if d.parent == parent and d.child == child
                and d.subject.startswith("edge:"))


# ---------------------------------------------------------------- D02 size risk


def test_d02_hard_size_risk_references():
    schema = """
    CREATE TABLE big_docs (doc_id INT PRIMARY KEY, title VARCHAR(200));
    CREATE TABLE big_sections (
      doc_id INT NOT NULL REFERENCES big_docs (doc_id),
      section_no INT NOT NULL,
      body1 VARCHAR(8000), body2 VARCHAR(8000), body3 VARCHAR(8000), body4 VARCHAR(8000),
      PRIMARY KEY (doc_id, section_no)
    );
    """
    queries = """
    -- @weight 10 read
    SELECT d.doc_id, d.title, s.section_no, s.body1 FROM big_docs d
    JOIN big_sections s ON s.doc_id = d.doc_id WHERE d.doc_id = ?;
    -- @weight 1 read
    SELECT doc_id, title FROM big_docs WHERE doc_id = ?;
    -- @weight 1 write
    INSERT INTO big_docs (title) VALUES (?);
    """
    decision = _edge(run(schema, queries), "big_docs", "big_sections")
    assert decision.rule_id == "D02"
    assert decision.action == "REFERENCE"
    assert decision.signals["est_embedded_size"] > 512 * 1024


def test_d02_soft_zone_embeds_subset():
    schema = """
    CREATE TABLE mid_docs (doc_id INT PRIMARY KEY, title VARCHAR(200));
    CREATE TABLE mid_sections (
      doc_id INT NOT NULL REFERENCES mid_docs (doc_id),
      section_no INT NOT NULL,
      body1 VARCHAR(500), body2 VARCHAR(500),
      created_at TIMESTAMPTZ,
      PRIMARY KEY (doc_id, section_no)
    );
    """
    queries = """
    -- @weight 10 read
    SELECT d.doc_id, d.title, s.section_no, s.body1 FROM mid_docs d
    JOIN mid_sections s ON s.doc_id = d.doc_id WHERE d.doc_id = ?;
    -- @weight 1 read
    SELECT doc_id, title FROM mid_docs WHERE doc_id = ?;
    -- @weight 1 write
    INSERT INTO mid_docs (title) VALUES (?);
    """
    decision = _edge(run(schema, queries), "mid_docs", "mid_sections")
    assert decision.rule_id == "D02"
    assert decision.action == "EMBED_SUBSET"
    assert decision.signals["est_embedded_size"] > 64 * 1024


# ---------------------------------------------------------------- D06 shared mutable


def test_d06_shared_mutable_parent_references():
    schema = """
    CREATE TABLE prices (price_id INT PRIMARY KEY, sku VARCHAR(40), amount DECIMAL(10,2));
    CREATE TABLE orders (order_id INT PRIMARY KEY, price_id INT NOT NULL REFERENCES prices (price_id),
                         status VARCHAR(20));
    CREATE TABLE quotes (quote_id INT PRIMARY KEY, price_id INT NOT NULL REFERENCES prices (price_id),
                         note TEXT);
    """
    queries = """
    -- @weight 10 read
    SELECT o.order_id, o.status, p.sku, p.amount FROM orders o
    JOIN prices p ON p.price_id = o.price_id WHERE o.order_id = ?;
    -- @weight 7 read
    SELECT order_id, status FROM orders WHERE status = ?;
    -- @weight 2 read
    SELECT order_id, status FROM orders WHERE order_id = ?;
    -- @weight 5 read
    SELECT q.quote_id, p.amount FROM quotes q
    JOIN prices p ON p.price_id = q.price_id WHERE q.quote_id = ?;
    -- @weight 6 write
    UPDATE prices SET amount = ? WHERE price_id = ?;
    """
    decision = _edge(run(schema, queries), "prices", "orders")
    assert decision.rule_id == "D06"
    assert decision.action == "REFERENCE"
    assert "fan-out" in decision.gains


# ---------------------------------------------------------------- D08 large junction


def test_d08_junction_with_both_sides_unbounded():
    schema = """
    CREATE TABLE users (user_id INT PRIMARY KEY, email VARCHAR(255), display_name VARCHAR(120));
    CREATE TABLE teams (team_id INT PRIMARY KEY, name VARCHAR(200), description VARCHAR(2000));
    CREATE TABLE team_members (
      team_id INT NOT NULL REFERENCES teams (team_id),
      user_id INT NOT NULL REFERENCES users (user_id),
      PRIMARY KEY (team_id, user_id)
    );
    """
    queries = """
    -- @weight 10 read
    SELECT u.user_id, u.display_name FROM users u
    JOIN team_members tm ON tm.user_id = u.user_id WHERE tm.team_id = ?;
    -- @weight 10 read
    SELECT t.team_id, t.name FROM teams t
    JOIN team_members tm ON tm.team_id = t.team_id WHERE tm.user_id = ?;
    -- @weight 3 write
    UPDATE users SET display_name = ? WHERE user_id = ?;
    -- @weight 2 write
    UPDATE teams SET description = ? WHERE team_id = ?;
    -- @weight 4 write
    INSERT INTO team_members (team_id, user_id) VALUES (?, ?);
    """
    ds = run(schema, queries)
    junction = next(d for d in ds.decisions if d.subject == "junction:team_members")
    assert junction.rule_id == "D08"
    assert junction.action == "KEEP"
    assert ds.dispositions["team_members"].disposition == "collection"


def test_d08_junction_with_payload_keeps_collection():
    schema = """
    CREATE TABLE users (user_id INT PRIMARY KEY, email VARCHAR(255), full_name VARCHAR(120),
                        avatar_url VARCHAR(400));
    CREATE TABLE teams (team_id INT PRIMARY KEY, name VARCHAR(200));
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
    ds = run(schema, queries)
    junction = next(d for d in ds.decisions if d.subject == "junction:team_members")
    assert junction.rule_id == "D08"
    assert junction.signals["has_payload"] is True


# ---------------------------------------------------------------- D10 deep nesting


def test_d10_breaks_embed_chains_deeper_than_three():
    schema = """
    CREATE TABLE level_a (a_id INT PRIMARY KEY, name VARCHAR(100));
    CREATE TABLE level_b (b_id INT PRIMARY KEY, a_id INT NOT NULL REFERENCES level_a (a_id), seq_no INT NOT NULL);
    CREATE TABLE level_c (c_id INT PRIMARY KEY, b_id INT NOT NULL REFERENCES level_b (b_id), seq_no INT NOT NULL);
    CREATE TABLE level_d (d_id INT PRIMARY KEY, c_id INT NOT NULL REFERENCES level_c (c_id), seq_no INT NOT NULL);
    CREATE TABLE level_e (e_id INT PRIMARY KEY, d_id INT NOT NULL REFERENCES level_d (d_id), seq_no INT NOT NULL);
    """
    queries = """
    -- @weight 100 read  the hot path reads a whole subtree in one query
    SELECT a.*, b.*, c.*, d.*, e.*
    FROM level_a a
    JOIN level_b b ON b.a_id = a.a_id
    JOIN level_c c ON c.b_id = b.b_id
    JOIN level_d d ON d.c_id = c.c_id
    JOIN level_e e ON e.d_id = d.d_id
    WHERE a.a_id = ?;
    -- @weight 1 write
    INSERT INTO level_a (name) VALUES (?);
    """
    ds = run(schema, queries)
    # levels b, c, d embed (depths 1, 2, 3); e would sit at depth 4 → flipped
    assert _edge(ds, "level_a", "level_b").action == "EMBED"
    assert _edge(ds, "level_b", "level_c").action == "EMBED"
    assert _edge(ds, "level_c", "level_d").action == "EMBED"
    flipped = _edge(ds, "level_d", "level_e")
    assert flipped.rule_id == "D10"
    assert flipped.action == "REFERENCE"


# ---------------------------------------------------------------- near-misses


def test_near_miss_reported_for_almost_fired_rule():
    schema = """
    CREATE TABLE parents (parent_id INT PRIMARY KEY, name VARCHAR(100));
    CREATE TABLE children (child_id INT PRIMARY KEY, parent_id INT NOT NULL REFERENCES parents (parent_id),
                           note VARCHAR(200));
    """
    queries = """
    -- @weight 11 read
    SELECT p.parent_id, p.name, c.child_id, c.note FROM parents p
    JOIN children c ON c.parent_id = p.parent_id WHERE p.parent_id = ?;
    -- @weight 9 read
    SELECT parent_id, name FROM parents WHERE parent_id = ?;
    -- @weight 1 write
    INSERT INTO parents (name) VALUES (?);
    """
    decision = _edge(run(schema, queries), "parents", "children")
    # co-access 11/20 = 0.55 — under the 0.60 threshold but within 25%
    assert decision.rule_id == "R00"
    near = [nm for nm in decision.near_misses if nm.rule_id == "D03"]
    assert near and "co-access" in near[0].detail
    assert decision.signals["co_access"] == 0.55
